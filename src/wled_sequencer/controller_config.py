"""controllers.json editing and WLED board discovery for the /config page.

Discovery uses mDNS/Zeroconf: every WLED board advertises ``_wled._tcp`` on port 80,
with its MAC in a ``mac`` TXT record and its mDNS hostname (Config -> WiFi Setup ->
"mDNS address") as the instance name. Each newly found board is probed once at
``http://<ip>/json/info`` for its friendly name, LED count and firmware. Boards that
mDNS can't reach (other subnet, multicast filtered) can be added by IP with ``probe``.

This module is self-contained (the web image doesn't ship ``wled_engine``); the file
format matches ``wled_engine.controllers.parse_controllers``, which ignores extra keys
such as ``name`` and ``mac``.
"""

from __future__ import annotations
import ipaddress
import json
import os
import re
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

DEFAULT_DDP_PORT = 4048
WLED_SERVICE_TYPE = "_wled._tcp.local."

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


class ConfigError(ValueError):
    """Invalid controllers config submitted by the user."""


def validate_controllers(controllers: Any) -> dict[str, dict[str, Any]]:
    """Validate and normalize ``{id: {ip, port, ...}}``. Extra keys per entry are kept."""
    if not isinstance(controllers, dict):
        raise ConfigError("'controllers' must be an object mapping controller id to {ip, port}")
    out: dict[str, dict[str, Any]] = {}
    seen_endpoints: dict[tuple[str, int], str] = {}
    for cid, entry in controllers.items():
        if not isinstance(cid, str) or not _ID_RE.match(cid):
            raise ConfigError(f"Invalid controller id '{cid}' (letters, digits, '_', '-', '.')")
        if isinstance(entry, str):
            entry = {"ip": entry}
        if not isinstance(entry, dict):
            raise ConfigError(f"Controller '{cid}' must be an object")
        ip = str(entry.get("ip", "")).strip()
        if not ip or not _HOST_RE.match(ip):
            raise ConfigError(f"Controller '{cid}' needs a valid 'ip'")
        try:
            port = int(entry.get("port", DEFAULT_DDP_PORT))
        except (TypeError, ValueError):
            raise ConfigError(f"Controller '{cid}' has a non-numeric port")
        if not 1 <= port <= 65535:
            raise ConfigError(f"Controller '{cid}' port must be 1-65535")
        if (ip, port) in seen_endpoints:
            raise ConfigError(f"Controllers '{seen_endpoints[(ip, port)]}' and '{cid}' both use {ip}:{port}")
        seen_endpoints[(ip, port)] = cid
        out[cid] = {**entry, "ip": ip, "port": port}
        if "channels" in entry:
            out[cid]["channels"] = _validate_channels(cid, entry["channels"])
    return out


def _validate_channels(cid: str, channels: Any) -> list[dict[str, Any]]:
    """``channels`` is a list (one per LED output, in order) of ``{"name": ...}``; extra keys are kept."""
    if not isinstance(channels, list) or len(channels) > MAX_CHANNELS:
        raise ConfigError(f"Controller '{cid}' channels must be a list of at most {MAX_CHANNELS}")
    out = []
    for i, ch in enumerate(channels):
        if not isinstance(ch, dict):
            raise ConfigError(f"Controller '{cid}' channel {i + 1} must be an object")
        name = ch.get("name", "")
        if not isinstance(name, str) or len(name.strip()) > 64:
            raise ConfigError(f"Controller '{cid}' channel {i + 1} name must be text of at most 64 characters")
        out.append({**ch, "name": name.strip()})
    return out


def write_json_atomic(path: Path, doc: Any) -> None:
    """Write JSON so readers (the daemon) never see a half-written file."""
    text = json.dumps(doc, indent=2) + "\n"
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
        try:
            # mkstemp makes the file 0600; keep the original's mode (e.g. a+w) and
            # stay readable by the daemon and whoever edits it by hand.
            mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
            os.chmod(tmp, mode | 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except PermissionError:
        # Directory not writable (e.g. container uid): fall back to rewriting the file in place.
        if not path.is_file():
            raise
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return data


def _writable(path: Path) -> bool:
    return os.access(path, os.W_OK) if path.exists() else os.access(path.parent, os.W_OK)


def channel_layout(patch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per controller: channel lengths and total LEDs the engine sends, mirroring ``PatchTable``.

    A channel is its declared ``channel_lengths`` entry or its furthest segment, whichever is
    longer; channels are concatenated in index order into one DDP stream.
    """
    declared: dict[str, list[int]] = {}
    for cid, lengths in (patch.get("channel_lengths") or {}).items():
        try:
            declared[str(cid)] = [max(0, int(n)) for n in lengths]
        except (TypeError, ValueError):
            continue
    ends: dict[str, dict[int, int]] = {}
    fixtures: dict[str, set[str]] = {}
    for seg in patch.get("segments", []):
        try:
            cid = str(seg["controller_id"])
            ch = int(seg.get("channel_index", 0))
            end = int(seg.get("port_offset", 0)) + int(seg["pixel_count"])
        except (KeyError, TypeError, ValueError):
            continue
        chans = ends.setdefault(cid, {})
        chans[ch] = max(chans.get(ch, 0), end)
        fixtures.setdefault(cid, set()).add(str(seg.get("fixture_id", "")))
    out: dict[str, dict[str, Any]] = {}
    for cid, chans in sorted(ends.items()):
        # Only channels up to the last mapped one are sent.
        dec = declared.get(cid, [])
        lengths = [max(chans.get(i, 0), dec[i] if i < len(dec) else 0) for i in range(max(chans) + 1)]
        out[cid] = {"channels": lengths, "total_leds": sum(lengths), "fixtures": sorted(fixtures[cid])}
    return out


class ControllerConfigStore:
    """Reads and writes controllers.json, plus what patch.json expects of each controller."""

    def __init__(self, controllers_file: Path | str, patch_file: Path | str | None = None) -> None:
        self.path = Path(controllers_file)
        self.patch_path = Path(patch_file) if patch_file else None
        self._lock = threading.Lock()

    def _read_raw(self) -> dict[str, Any]:
        return _read_json_object(self.path)

    def load(self) -> dict[str, dict[str, Any]]:
        data = self._read_raw()
        section = data.get("controllers", data)
        return {cid: ({"ip": e} if isinstance(e, str) else dict(e)) for cid, e in section.items() if isinstance(e, (str, dict))}

    @property
    def writable(self) -> bool:
        return _writable(self.path)

    def save(self, controllers: Any) -> dict[str, dict[str, Any]]:
        """Validate and write. Keeps a top-level ``"controllers"`` wrapper if the file had one."""
        clean = validate_controllers(controllers)
        with self._lock:
            raw = self._read_raw() if self.path.is_file() else {}
            doc: dict[str, Any] = {**raw, "controllers": clean} if "controllers" in raw else clean
            write_json_atomic(self.path, doc)
        return clean

    def patch_expectations(self) -> dict[str, dict[str, Any]]:
        if not self.patch_path or not self.patch_path.is_file():
            return {}
        try:
            return channel_layout(_read_json_object(self.patch_path))
        except (OSError, ValueError):
            return {}


COLOR_ORDERS = ("RGB", "GRB", "BGR", "RGBW", "GRBW")
MAX_CHANNELS = 32


def load_universe_fixtures(path: Path | str | None) -> list[dict[str, Any]]:
    """The fixtures in universe.json, reduced to what the mapping UI needs."""
    if not path or not Path(path).is_file():
        return []
    data = _read_json_object(Path(path))
    out = []
    for f in data.get("fixtures", []):
        if not isinstance(f, dict) or "id" not in f:
            continue
        count = f.get("pixel_count")
        if count is None and f.get("rows") and f.get("cols"):
            count = int(f["rows"]) * int(f["cols"])
        out.append({
            "id": str(f["id"]),
            "name": f.get("name") or str(f["id"]),
            "type": f.get("type", "linear_strip"),
            "group": f.get("group", "default"),
            "pixel_count": int(count or 0),
            "tags": list(f.get("tags") or []),
        })
    return out


def validate_patch(patch: Any, fixtures: dict[str, int] | None = None) -> dict[str, Any]:
    """Validate and normalize a patch document (``PatchTable`` format plus ``channel_lengths``).

    ``fixtures`` maps fixture id -> pixel count; when given, segments must name a known fixture
    and stay within it. Raises ``ConfigError`` listing the problems.
    """
    if not isinstance(patch, dict):
        raise ConfigError("Patch must be a JSON object")
    errors: list[str] = []

    lengths: dict[str, list[int]] = {}
    raw_lengths = patch.get("channel_lengths") or {}
    if not isinstance(raw_lengths, dict):
        raise ConfigError("'channel_lengths' must map controller id to a list of LED counts")
    for cid, ls in raw_lengths.items():
        if not _ID_RE.match(str(cid)):
            errors.append(f"Invalid controller id '{cid}' in channel_lengths")
            continue
        try:
            vals = [int(n) for n in ls]
        except (TypeError, ValueError):
            errors.append(f"channel_lengths for '{cid}' must be whole numbers")
            continue
        if any(n < 0 or n > 65535 for n in vals) or len(vals) > MAX_CHANNELS:
            errors.append(f"channel_lengths for '{cid}' out of range")
            continue
        while vals and vals[-1] == 0:
            vals.pop()
        if vals:
            lengths[str(cid)] = vals

    segments: list[dict[str, Any]] = []
    for i, seg in enumerate(patch.get("segments") or []):
        where = f"Mapping {i + 1}"
        if not isinstance(seg, dict):
            errors.append(f"{where}: must be an object")
            continue
        try:
            clean = {
                "fixture_id": str(seg["fixture_id"]).strip(),
                "pixel_start": int(seg.get("pixel_start", 0)),
                "pixel_count": int(seg["pixel_count"]),
                "controller_id": str(seg["controller_id"]).strip(),
                "channel_index": int(seg.get("channel_index", 0)),
                "port_offset": int(seg.get("port_offset", 0)),
                "reversed": bool(seg.get("reversed", False)),
                "color_order": str(seg.get("color_order", "GRB")).upper(),
                "protocol": str(seg.get("protocol", "ddp")).lower(),
                "brightness_scale": round(float(seg.get("brightness_scale", 1.0)), 3),
            }
        except (KeyError, TypeError, ValueError) as e:
            errors.append(f"{where}: missing or invalid field ({e})")
            continue
        where = f"Mapping {clean['controller_id']} ch {clean['channel_index'] + 1} → {clean['fixture_id']}"
        if not clean["fixture_id"]:
            errors.append(f"{where}: no fixture")
        if not _ID_RE.match(clean["controller_id"]):
            errors.append(f"{where}: invalid controller id")
        if clean["pixel_count"] < 1 or clean["pixel_start"] < 0 or clean["port_offset"] < 0:
            errors.append(f"{where}: counts and offsets must be positive")
        if not 0 <= clean["channel_index"] < MAX_CHANNELS:
            errors.append(f"{where}: channel out of range")
        if clean["color_order"] not in COLOR_ORDERS:
            errors.append(f"{where}: color order must be one of {', '.join(COLOR_ORDERS)}")
        if clean["protocol"] != "ddp":
            errors.append(f"{where}: only the 'ddp' protocol is supported")
        if not 0.0 <= clean["brightness_scale"] <= 1.0:
            errors.append(f"{where}: brightness must be 0-1")
        if fixtures is not None:
            if clean["fixture_id"] not in fixtures:
                errors.append(f"{where}: fixture '{clean['fixture_id']}' isn't in universe.json")
            elif clean["pixel_start"] + clean["pixel_count"] > fixtures[clean["fixture_id"]]:
                errors.append(
                    f"{where}: pixels {clean['pixel_start']}-{clean['pixel_start'] + clean['pixel_count'] - 1} "
                    f"run past the fixture's {fixtures[clean['fixture_id']]} pixels"
                )
        declared = lengths.get(clean["controller_id"], [])
        ch = clean["channel_index"]
        if ch < len(declared) and declared[ch] and clean["port_offset"] + clean["pixel_count"] > declared[ch]:
            errors.append(f"{where}: ends at LED {clean['port_offset'] + clean['pixel_count']} but the channel has {declared[ch]}")
        segments.append(clean)

    # Two mappings writing the same LEDs of one channel.
    by_port: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for seg in segments:
        by_port.setdefault((seg["controller_id"], seg["channel_index"]), []).append(seg)
    for (cid, ch), segs in by_port.items():
        segs = sorted(segs, key=lambda s: s["port_offset"])
        for a, b in zip(segs, segs[1:]):
            if b["port_offset"] < a["port_offset"] + a["pixel_count"]:
                errors.append(
                    f"{cid} ch {ch + 1}: LEDs {b['port_offset']}-{a['port_offset'] + a['pixel_count'] - 1} "
                    f"are mapped to both '{a['fixture_id']}' and '{b['fixture_id']}'"
                )

    if errors:
        raise ConfigError("; ".join(errors))
    out = {k: v for k, v in patch.items() if k not in ("segments", "channel_lengths")}
    out["name"] = str(patch.get("name") or "Patch")
    out["segments"] = segments
    if lengths:
        out["channel_lengths"] = lengths
    return out


class PatchStore:
    """Reads and writes patch.json, validated against the fixtures in universe.json."""

    def __init__(self, patch_file: Path | str, universe_file: Path | str | None = None) -> None:
        self.path = Path(patch_file)
        self.universe_path = Path(universe_file) if universe_file else None
        self._lock = threading.Lock()

    @property
    def writable(self) -> bool:
        return _writable(self.path)

    def load(self) -> dict[str, Any]:
        data = _read_json_object(self.path)
        data.setdefault("name", "Patch")
        data.setdefault("segments", [])
        return data

    def fixtures(self) -> list[dict[str, Any]]:
        return load_universe_fixtures(self.universe_path)

    def save(self, patch: Any) -> dict[str, Any]:
        universe = self.fixtures() if self.universe_path and self.universe_path.is_file() else None
        fixtures = {f["id"]: f["pixel_count"] for f in universe} if universe is not None else None
        clean = validate_patch(patch, fixtures)
        with self._lock:
            raw = _read_json_object(self.path) if self.path.is_file() else {}
            # Keep keys this editor doesn't manage (e.g. an embedded "controllers" section).
            doc = {**{k: v for k, v in raw.items() if k != "channel_lengths"}, **clean}
            write_json_atomic(self.path, doc)
        return doc


# WLED LED output color order codes (const.h COL_ORDER_*).
_WLED_ORDERS = {0: "GRB", 1: "RGB", 2: "BRG", 3: "RBG", 4: "BGR", 5: "GBR"}


def probe_wled(ip: str, timeout: float = 2.0) -> dict[str, Any]:
    """Read ``/json/info`` (and, best effort, the LED outputs from ``/json/cfg``). Raises on network errors."""
    with urllib.request.urlopen(f"http://{ip}/json/info", timeout=timeout) as resp:
        info = json.loads(resp.read().decode("utf-8"))
    leds = info.get("leds") or {}
    result = {
        "name": info.get("name"),
        "mac": info.get("mac"),
        "version": info.get("ver"),
        "arch": info.get("arch"),
        "led_count": leds.get("count"),
        "rgbw": bool(leds.get("rgbw")),
        "outputs": None,
    }
    try:
        with urllib.request.urlopen(f"http://{ip}/json/cfg", timeout=timeout) as resp:
            cfg = json.loads(resp.read().decode("utf-8"))
        ins = ((cfg.get("hw") or {}).get("led") or {}).get("ins") or []
        result["outputs"] = sorted(
            (
                {
                    "start": int(o.get("start", 0)),
                    "len": int(o.get("len", 0)),
                    "pins": list(o.get("pin") or []),
                    "order": _WLED_ORDERS.get(int(o.get("order", 0)) & 0x0F, str(o.get("order"))),
                }
                for o in ins
            ),
            key=lambda o: o["start"],
        )
    except Exception:
        pass  # older firmware or a settings PIN: outputs stay unknown
    return result


def _ip_sort_key(ip: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in ip.split("."))
    except ValueError:
        return (999,)


class WledDiscovery:
    """Browses mDNS for WLED boards and keeps a table of what's been found.

    Nodes are keyed by mDNS instance name (stable across DHCP changes), or ``ip:<addr>``
    for boards added by hand. A board that withdraws its service stays listed as offline.
    """

    def __init__(
        self,
        prober: Callable[[str], dict[str, Any]] | None = probe_wled,
        service_type: str = WLED_SERVICE_TYPE,
    ) -> None:
        self.prober = prober
        self.service_type = service_type
        self.error: str | None = None
        self._nodes: dict[str, dict[str, Any]] = {}
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
            self.error = "mDNS discovery needs the 'zeroconf' package (pip install zeroconf)"
            return
        try:
            self._zc = Zeroconf(ip_version=IPVersion.V4Only)
        except OSError as e:
            self.error = f"Cannot start mDNS: {e}"
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
        props = info.properties or {}
        mac = props.get(b"mac")
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
        """Add or refresh an mDNS-discovered board; probes it when it's new or its IP changed."""
        now = time.time()
        key = f"mdns:{name}"
        with self._lock:
            # A board first added by IP is now known by name: merge it.
            manual = self._nodes.pop(f"ip:{ip}", None)
            entry = self._nodes.get(key)
            needs_probe = entry is None or entry["ip"] != ip or entry.get("info") is None
            if entry is None:
                entry = manual or {"info": None, "probe_error": None, "first_seen": now}
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
            self._nodes[key] = entry
        if needs_probe and self.prober is not None:
            threading.Thread(target=self._probe_key, args=(key,), daemon=True, name=f"WledProbe-{ip}").start()

    def mark_gone(self, name: str) -> None:
        with self._lock:
            entry = self._nodes.get(f"mdns:{name}")
            if entry:
                entry["online"] = False

    def _probe_key(self, key: str) -> dict[str, Any]:
        with self._lock:
            ip = self._nodes[key]["ip"]
        try:
            info, error = self.prober(ip), None
        except Exception as e:
            info, error = None, str(e)
        with self._lock:
            entry = self._nodes[key]
            entry["probe_error"] = error
            if info is not None:
                entry["info"] = info
                entry["mac"] = entry.get("mac") or info.get("mac")
            return dict(entry)

    def probe(self, ip: str) -> dict[str, Any]:
        """Probe a board by IP now (manual add) and store the result."""
        if self.prober is None:
            raise ConfigError("Probing is disabled")
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            raise ConfigError(f"'{ip}' is not an IPv4 address")
        now = time.time()
        with self._lock:
            key = next((k for k, n in self._nodes.items() if n["ip"] == ip), f"ip:{ip}")
            entry = self._nodes.setdefault(key, {
                "key": key, "source": "manual", "ip": ip, "instance": None, "hostname": None,
                "mac": None, "online": False, "first_seen": now, "last_seen": None,
                "info": None, "probe_error": None,
            })
        node = self._probe_key(key)
        if node["source"] == "manual":
            with self._lock:
                entry["online"] = node["info"] is not None
                if entry["online"]:
                    entry["last_seen"] = time.time()
                node = dict(entry)
        return node

    def nodes(self) -> list[dict[str, Any]]:
        with self._lock:
            out = [dict(n) for n in self._nodes.values()]
        return sorted(out, key=lambda n: _ip_sort_key(n["ip"]))
