"""controllers.json: load, validate and save atomically.

Format::

    {"wled_01": {"ip": "10.10.1.111", "port": 4048, "name": "WLED", "mac": "...", "host": "...",
                 "channels": [{"name": "Front truss"}, {"name": "Back wall", "reversed": true}]}}

Extra keys are kept. Channel lengths are not stored; they come from the board.
"""

from __future__ import annotations
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_DDP_PORT = 4048
MAX_CHANNELS = 32

ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


class ConfigError(ValueError):
    pass


def validate_controllers(controllers: Any) -> dict[str, dict[str, Any]]:
    """Validate and normalize ``{id: {ip, port, channels, ...}}``. Accepts a ``{"controllers": {...}}`` wrapper."""
    if isinstance(controllers, dict) and isinstance(controllers.get("controllers"), dict):
        controllers = controllers["controllers"]
    if not isinstance(controllers, dict):
        raise ConfigError("controllers must be an object mapping controller id to {ip, port}")
    out: dict[str, dict[str, Any]] = {}
    seen: dict[tuple[str, int], str] = {}
    for cid, entry in controllers.items():
        if not isinstance(cid, str) or not ID_RE.match(cid):
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
        if (ip, port) in seen:
            raise ConfigError(f"Controllers '{seen[(ip, port)]}' and '{cid}' both use {ip}:{port}")
        seen[(ip, port)] = cid
        out[cid] = {**entry, "ip": ip, "port": port}
        if "channels" in entry:
            out[cid]["channels"] = _validate_channels(cid, entry["channels"])
    return out


def _validate_channels(cid: str, channels: Any) -> list[dict[str, Any]]:
    if not isinstance(channels, list) or len(channels) > MAX_CHANNELS:
        raise ConfigError(f"Controller '{cid}' channels must be a list of at most {MAX_CHANNELS}")
    out = []
    for i, ch in enumerate(channels):
        if not isinstance(ch, dict):
            raise ConfigError(f"Controller '{cid}' channel {i + 1} must be an object")
        name = ch.get("name", "")
        if not isinstance(name, str) or len(name.strip()) > 64:
            raise ConfigError(f"Controller '{cid}' channel {i + 1} name must be text of at most 64 characters")
        if "reversed" in ch and not isinstance(ch["reversed"], bool):
            raise ConfigError(f"Controller '{cid}' channel {i + 1} 'reversed' must be true or false")
        out.append({**ch, "name": name.strip()})
    return out


def write_json_atomic(path: Path, doc: Any) -> None:
    """Write JSON so readers never see a half-written file; keeps the file's mode (at least 0644)."""
    text = json.dumps(doc, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
        try:
            mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
            os.chmod(tmp, mode | 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except PermissionError:
        # Directory not writable (e.g. container uid): rewrite the file in place.
        if not path.is_file():
            raise
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def read_json_object(path: Path) -> dict[str, Any]:
    """The JSON object in ``path``; ``{}`` if the file doesn't exist."""
    if not path.is_file():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return data


def writable(path: Path) -> bool:
    if path.exists():
        return os.access(path, os.W_OK)
    return os.access(path.parent if path.parent.exists() else Path("."), os.W_OK)


class ControllerStore:
    """controllers.json on disk."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            return validate_controllers(read_json_object(self.path))
        except json.JSONDecodeError as e:
            raise ConfigError(f"{self.path} is not valid JSON: {e}")

    def save(self, controllers: Any) -> dict[str, dict[str, Any]]:
        data = validate_controllers(controllers)
        # Preserve a {"controllers": {...}} wrapper if the file uses one.
        try:
            existing = read_json_object(self.path)
        except (ConfigError, json.JSONDecodeError):
            existing = {}
        doc: Any = {**existing, "controllers": data} if isinstance(existing.get("controllers"), dict) else data
        write_json_atomic(self.path, doc)
        return data

    @property
    def writable(self) -> bool:
        return writable(self.path)
