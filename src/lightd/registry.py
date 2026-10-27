"""Configured controllers: a WledController per id, kept fresh and following boards across IP changes."""

from __future__ import annotations
import logging
import threading
import time
from typing import Any, Callable

from wled import WledBrowser, WledController, WledError, normalize_mac
from wled.models import channel_warnings

from lightd.config import ControllerStore

log = logging.getLogger(__name__)

REFRESH_INTERVAL = 30.0  # re-read a healthy board's info/channels this often
RETRY_INTERVAL = 5.0  # retry a board that couldn't be read


def _layout(controller: WledController) -> str:
    """Channel lengths, for logging changes, e.g. "270+270+360"."""
    return "+".join(str(ch.length) for ch in controller.channels) if controller.channels else "none"


class ControllerEntry:
    def __init__(self, cid: str, config: dict[str, Any], controller: WledController) -> None:
        self.id = cid
        self.config = config
        self.controller = controller
        self.online = False
        self.error: str | None = None
        self.last_attempt = 0.0
        self.refreshing = False
        self.segment_status: dict[str, Any] = {"saved": False, "match": None, "reset": False}

    @property
    def configured_ip(self) -> str:
        return self.config["ip"]

    def channel_config(self, index: int) -> dict[str, Any]:
        chans = self.config.get("channels") or []
        return chans[index] if index < len(chans) else {}

    def reversed(self, index: int) -> bool:
        return bool(self.channel_config(index).get("reversed"))

    def zone(self, index: int) -> str:
        z = self.channel_config(index).get("zone")
        return z if z in ("inside", "outside") else "inside"

    def to_dict(self) -> dict[str, Any]:
        c = self.controller
        board_channels = c.channels or []
        configured = self.config.get("channels") or []
        warnings = channel_warnings(board_channels, c.outputs)
        channels = []
        for i in range(max(len(board_channels), len(configured))):
            cfg = configured[i] if i < len(configured) else {}
            ch = board_channels[i] if i < len(board_channels) else None
            channels.append({
                "index": i,
                "number": i + 1,
                "target": f"{self.id}/{i + 1}",
                "name": cfg.get("name", ""),
                "zone": self.zone(i),
                "reversed": bool(cfg.get("reversed")),
                "on_board": ch is not None,
                **({k: v for k, v in ch.to_dict().items() if k not in ("index", "name")} if ch else {"length": None}),
                "segment_name": ch.name if ch else None,
                "warnings": warnings[i] if ch else [],
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
            "segments": dict(self.segment_status),
            "warnings": ["segments reset"] if self.segment_status.get("reset") else [],
        }


class Registry:
    """Holds the configured controllers and keeps their board info current in a background thread."""

    def __init__(
        self,
        store: ControllerStore,
        transport: Any = None,
        browser: WledBrowser | None = None,
        controller_factory: Callable[..., WledController] | None = None,
        segments: Any = None,
    ) -> None:
        self.store = store
        self.transport = transport
        self.browser = browser
        self.segments = segments  # lightd.segments.SegmentStore: known-good segment snapshots (optional)
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
                log.info("%s: board moved from %s to %s", e.id, e.controller.host, match["ip"])
                e.controller.host = match["ip"]
                e.last_attempt = 0.0  # re-read at the new address
            if not match.get("online", True):
                e.online = False

    def refresh(self, entry: ControllerEntry) -> None:
        entry.refreshing = True
        entry.last_attempt = time.monotonic()
        was_online, old_error = entry.online, entry.error
        before = _layout(entry.controller)
        try:
            entry.controller.refresh()
            entry.online, entry.error = True, None
        except WledError as e:
            entry.online, entry.error = False, str(e)
        finally:
            entry.refreshing = False
        c = entry.controller
        if entry.online and self.segments is not None:
            try:
                self.segments.check(entry)
            except Exception as e:  # never let the snapshot logic break refreshing
                log.warning("%s: segment check failed: %s", entry.id, e)
        if entry.online and not was_online:
            log.info("%s: online at %s, %d LEDs, %d channel(s)", entry.id, c.host,
                     c.info.led_count if c.info else 0, len(c.channels or []))
        elif not entry.online and (was_online or entry.error != old_error):
            log.warning("%s: unreachable at %s: %s", entry.id, c.host, entry.error)
        if entry.online and was_online and before != _layout(c):
            log.info("%s: channels changed: %s -> %s", entry.id, before, _layout(c))
        if entry.online and (was_online is False or before != _layout(c)):
            for ch, warnings in zip(c.channels or [], channel_warnings(c.channels or [], c.outputs)):
                for w in warnings:
                    log.warning("%s/%d: %s", entry.id, ch.index + 1, w)

    def refresh_all(self) -> None:
        """Re-read every board now, in parallel, and wait for them all (each bounded by its HTTP timeout)."""
        self._follow_discovery()
        threads = [threading.Thread(target=self.refresh, args=(e,), daemon=True, name=f"Refresh-{e.id}")
                   for e in self.entries()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

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
