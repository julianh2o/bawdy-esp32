"""Configured controllers: a WledController per id, kept fresh and following boards across IP changes."""

from __future__ import annotations
import threading
import time
from typing import Any, Callable

from wled import WledBrowser, WledController, WledError, normalize_mac

from lightd.config import ControllerStore

REFRESH_INTERVAL = 30.0  # re-read a healthy board's info/channels this often
RETRY_INTERVAL = 5.0  # retry a board that couldn't be read


class ControllerEntry:
    def __init__(self, cid: str, config: dict[str, Any], controller: WledController) -> None:
        self.id = cid
        self.config = config
        self.controller = controller
        self.online = False
        self.error: str | None = None
        self.last_attempt = 0.0
        self.refreshing = False

    @property
    def configured_ip(self) -> str:
        return self.config["ip"]

    def channel_config(self, index: int) -> dict[str, Any]:
        chans = self.config.get("channels") or []
        return chans[index] if index < len(chans) else {}

    def reversed(self, index: int) -> bool:
        return bool(self.channel_config(index).get("reversed"))

    def to_dict(self) -> dict[str, Any]:
        c = self.controller
        board_channels = c.channels or []
        configured = self.config.get("channels") or []
        channels = []
        for i in range(max(len(board_channels), len(configured))):
            cfg = configured[i] if i < len(configured) else {}
            ch = board_channels[i] if i < len(board_channels) else None
            channels.append({
                "index": i,
                "number": i + 1,
                "target": f"{self.id}/{i + 1}",
                "name": cfg.get("name", ""),
                "reversed": bool(cfg.get("reversed")),
                "on_board": ch is not None,
                **({k: v for k, v in ch.to_dict().items() if k not in ("index", "name")} if ch else {"length": None}),
                "segment_name": ch.name if ch else None,
            })
        return {
            "id": self.id,
            "name": self.config.get("name"),
            "ip": c.host,
            "configured_ip": self.configured_ip,
            "port": c.port,
            "mac": self.config.get("mac"),
            "host": self.config.get("host"),
            "online": self.online,
            "error": self.error,
            "identifying": c.identifying,
            "board": c.info.to_dict() if c.info else None,
            "channels": channels,
        }


class Registry:
    """Holds the configured controllers and keeps their board info current in a background thread."""

    def __init__(
        self,
        store: ControllerStore,
        transport: Any = None,
        browser: WledBrowser | None = None,
        controller_factory: Callable[..., WledController] | None = None,
    ) -> None:
        self.store = store
        self.transport = transport
        self.browser = browser
        self._factory = controller_factory or WledController
        self._entries: dict[str, ControllerEntry] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- config ----------

    def load(self) -> dict[str, dict[str, Any]]:
        """(Re)read controllers.json. Keeps existing controllers (and their board info) when ip/port match."""
        return self.apply(self.store.load())

    def apply(self, controllers: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        with self._lock:
            old = self._entries
            new: dict[str, ControllerEntry] = {}
            for cid, cfg in controllers.items():
                prev = old.get(cid)
                if prev is not None and prev.configured_ip == cfg["ip"] and prev.controller.port == cfg["port"]:
                    prev.config = cfg
                    new[cid] = prev
                else:
                    controller = self._factory(cfg["ip"], port=cfg["port"], transport=self.transport)
                    new[cid] = ControllerEntry(cid, cfg, controller)
            self._entries = new
        self._follow_discovery()
        return controllers

    def entries(self) -> list[ControllerEntry]:
        with self._lock:
            return list(self._entries.values())

    def get(self, cid: str) -> ControllerEntry | None:
        with self._lock:
            return self._entries.get(cid)

    def find_by_ip(self, ip: str, port: int | None = None) -> ControllerEntry | None:
        for e in self.entries():
            if e.controller.host == ip and (port is None or e.controller.port == port):
                return e
        return None

    # ---------- keeping boards fresh ----------

    def _follow_discovery(self) -> None:
        """Point controllers at the IP their board (matched by MAC, then mDNS host) currently answers on."""
        if self.browser is None:
            return
        boards = self.browser.boards()
        for e in self.entries():
            mac = normalize_mac(e.config.get("mac"))
            host = (e.config.get("host") or "").lower()
            match = next((b for b in boards if mac and normalize_mac(b.get("mac") or (b.get("info") or {}).get("mac")) == mac), None)
            match = match or next((b for b in boards if host and (b.get("hostname") or "").lower() == host), None)
            if match is None:
                continue
            if match["ip"] != e.controller.host:
                e.controller.host = match["ip"]
                e.last_attempt = 0.0  # re-read at the new address
            if not match.get("online", True):
                e.online = False

    def refresh(self, entry: ControllerEntry) -> None:
        entry.refreshing = True
        entry.last_attempt = time.monotonic()
        try:
            entry.controller.refresh()
            entry.online, entry.error = True, None
        except WledError as e:
            entry.online, entry.error = False, str(e)
        finally:
            entry.refreshing = False

    def maintain(self) -> None:
        """One maintenance pass: follow discovery, then (re)read boards that are due, each in its own thread."""
        self._follow_discovery()
        now = time.monotonic()
        for e in self.entries():
            if e.refreshing:
                continue
            due = RETRY_INTERVAL if (not e.online or e.controller.channels is None) else REFRESH_INTERVAL
            if now - e.last_attempt >= due:
                e.refreshing = True
                threading.Thread(target=self.refresh, args=(e,), daemon=True, name=f"Refresh-{e.id}").start()

    def start(self, interval: float = 1.0) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def run() -> None:
            while not self._stop.is_set():
                self.maintain()
                self._stop.wait(interval)

        self._thread = threading.Thread(target=run, daemon=True, name="Registry")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
