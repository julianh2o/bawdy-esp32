"""Find WLED boards on the LAN over mDNS (``_wled._tcp``), plus manual probing by IP.

mDNS is multicast and doesn't cross subnets/VLANs; ``probe(ip)`` covers boards elsewhere.
"""

from __future__ import annotations
import ipaddress
import logging
import threading
import time
from typing import Any, Callable

from wled.http import WledHttp
from wled.models import WledError, board_channels, info_from_json

log = logging.getLogger(__name__)

WLED_SERVICE_TYPE = "_wled._tcp.local."


def probe(host: str, timeout: float = 2.0) -> dict[str, Any]:
    """Board info plus its channels (``None`` if neither ``/json/cfg`` nor ``/json/state`` is readable).
    Raises ``WledError``."""
    http = WledHttp(host, timeout=timeout)
    info = info_from_json(http.info())
    docs = []
    for read in (http.cfg, http.state):
        try:
            docs.append(read())
        except WledError:
            docs.append(None)  # older firmware or a settings PIN
    cfg, state = docs
    channels = None if cfg is None and state is None else [c.to_dict() for c in board_channels(cfg, state, info.led_count)]
    return {**info.to_dict(), "channels": channels}


def _ip_sort_key(ip: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in ip.split("."))
    except ValueError:
        return (999,)


class WledBrowser:
    """Browses mDNS for WLED boards and keeps a table of what's been found.

    Boards are keyed by mDNS instance name (stable across DHCP changes), or ``ip:<addr>`` for
    boards probed by hand. A board that withdraws its service stays listed with ``online=False``.
    ``on_change(board)`` is called (from a background thread) whenever a board is added or updated.
    """

    def __init__(
        self,
        on_change: Callable[[dict[str, Any]], None] | None = None,
        prober: Callable[[str], dict[str, Any]] | None = probe,
        service_type: str = WLED_SERVICE_TYPE,
    ) -> None:
        self.on_change = on_change
        self.prober = prober
        self.service_type = service_type
        self.error: str | None = None
        self._boards: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._zc: Any = None
        self._browser: Any = None

    @property
    def listening(self) -> bool:
        return self._browser is not None

    def start(self) -> None:
        if self._zc is not None:
            return
        try:
            from zeroconf import IPVersion, Zeroconf
        except ImportError:
            self.error = "mDNS discovery needs the 'zeroconf' package"
            return
        try:
            self._zc = Zeroconf(ip_version=IPVersion.V4Only)
        except OSError as e:
            self.error = f"Cannot start mDNS: {e}"
            log.warning("%s", self.error)
            return
        self.error = None
        self._start_browser()

    def _start_browser(self) -> None:
        from zeroconf import ServiceBrowser

        self._browser = ServiceBrowser(self._zc, self.service_type, handlers=[self._on_state_change])

    def rescan(self) -> None:
        """Restart the browser so it queries the network again right away."""
        if self._zc is None:
            return
        if self._browser is not None:
            self._browser.cancel()
        self._start_browser()

    def stop(self) -> None:
        if self._browser is not None:
            self._browser.cancel()
            self._browser = None
        if self._zc is not None:
            self._zc.close()
            self._zc = None

    def _on_state_change(self, zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        from zeroconf import ServiceStateChange

        if state_change is ServiceStateChange.Removed:
            self.mark_gone(name)
        else:
            # Resolving blocks; keep it off zeroconf's event loop thread.
            threading.Thread(target=self._resolve, args=(name,), daemon=True, name="WledResolve").start()

    def _resolve(self, name: str) -> None:
        from zeroconf import IPVersion

        zc = self._zc
        if zc is None:
            return
        info = zc.get_service_info(self.service_type, name, timeout=3000)
        if info is None:
            return
        addrs = info.parsed_addresses(IPVersion.V4Only)
        if not addrs:
            return
        mac = (info.properties or {}).get(b"mac")
        self.record_service(
            name,
            ip=addrs[0],
            hostname=(info.server or "").rstrip(".") or None,
            mac=mac.decode("utf-8", errors="replace") if isinstance(mac, bytes) else None,
        )

    def _instance(self, name: str) -> str:
        suffix = "." + self.service_type
        return name[: -len(suffix)] if name.endswith(suffix) else name

    def record_service(self, name: str, ip: str, hostname: str | None = None, mac: str | None = None) -> None:
        """Add or refresh an mDNS-announced board; probes it when it's new or its IP changed."""
        now = time.time()
        key = f"mdns:{name}"
        with self._lock:
            manual = self._boards.pop(f"ip:{ip}", None)  # first probed by IP, now known by name: merge
            entry = self._boards.get(key)
            needs_probe = entry is None or entry["ip"] != ip or entry.get("info") is None
            if entry is None:
                entry = manual or {"info": None, "probe_error": None, "first_seen": now}
                log.info("Discovered %s at %s", self._instance(name), ip)
            entry.update({
                "key": key,
                "source": "mdns",
                "instance": self._instance(name),
                "ip": ip,
                "hostname": hostname,
                "mac": mac or entry.get("mac"),
                "online": True,
                "last_seen": now,
            })
            self._boards[key] = entry
            snapshot = dict(entry)
        self._notify(snapshot)
        if needs_probe and self.prober is not None:
            threading.Thread(target=self._probe_key, args=(key,), daemon=True, name=f"WledProbe-{ip}").start()

    def mark_gone(self, name: str) -> None:
        with self._lock:
            entry = self._boards.get(f"mdns:{name}")
            if not entry:
                return
            entry["online"] = False
            snapshot = dict(entry)
        self._notify(snapshot)

    def _notify(self, board: dict[str, Any]) -> None:
        if self.on_change is not None:
            try:
                self.on_change(board)
            except Exception:
                pass

    def _probe_key(self, key: str) -> dict[str, Any]:
        with self._lock:
            ip = self._boards[key]["ip"]
        try:
            info, error = self.prober(ip), None
        except Exception as e:
            info, error = None, str(e)
        with self._lock:
            entry = self._boards[key]
            entry["probe_error"] = error
            if info is not None:
                entry["info"] = info
                entry["mac"] = entry.get("mac") or info.get("mac")
            snapshot = dict(entry)
        self._notify(snapshot)
        return snapshot

    def probe(self, ip: str) -> dict[str, Any]:
        """Probe a board by IPv4 address now (manual add) and record it. Raises ``ValueError``."""
        if self.prober is None:
            raise ValueError("Probing is disabled")
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            raise ValueError(f"'{ip}' is not an IPv4 address")
        now = time.time()
        with self._lock:
            key = next((k for k, b in self._boards.items() if b["ip"] == ip), f"ip:{ip}")
            entry = self._boards.setdefault(key, {
                "key": key, "source": "manual", "ip": ip, "instance": None, "hostname": None,
                "mac": None, "online": False, "first_seen": now, "last_seen": None,
                "info": None, "probe_error": None,
            })
        board = self._probe_key(key)
        if board["source"] == "manual":
            with self._lock:
                entry["online"] = board["info"] is not None
                if entry["online"]:
                    entry["last_seen"] = time.time()
                board = dict(entry)
        return board

    def boards(self) -> list[dict[str, Any]]:
        with self._lock:
            out = [dict(b) for b in self._boards.values()]
        return sorted(out, key=lambda b: _ip_sort_key(b["ip"]))
