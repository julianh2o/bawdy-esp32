"""Per-controller network configuration (IP + DDP port) for real WLED hardware."""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any

DEFAULT_DDP_PORT = 4048


def parse_controllers(data: dict[str, Any]) -> tuple[dict[str, str], dict[str, int]]:
    """Parse ``{"wled_01": {"ip": "192.168.1.50", "port": 4048}, ...}`` into (ips, ports).

    Accepts the mapping directly or wrapped in a top-level ``"controllers"`` key
    (as embedded in ``patch.json``). A bare string is shorthand for ``{"ip": ...}``.
    ``port`` defaults to 4048.
    """
    section = data.get("controllers", data)
    if not isinstance(section, dict):
        raise ValueError("'controllers' must be an object mapping controller id to {ip, port}")
    ips: dict[str, str] = {}
    ports: dict[str, int] = {}
    for cid, entry in section.items():
        if isinstance(entry, str):
            entry = {"ip": entry}
        if not isinstance(entry, dict) or not entry.get("ip"):
            raise ValueError(f"Controller '{cid}' needs an 'ip'")
        ips[cid] = str(entry["ip"])
        ports[cid] = int(entry.get("port", DEFAULT_DDP_PORT))
    return ips, ports


def load_controllers(
    controllers_file: Path | str | None = None,
    patch_file: Path | str | None = None,
) -> tuple[dict[str, str], dict[str, int]]:
    """Load controller endpoints from a controllers file and/or a patch.json ``controllers`` section.

    Entries in ``controllers_file`` override those embedded in the patch file.
    Returns empty dicts when neither provides any.
    """
    ips: dict[str, str] = {}
    ports: dict[str, int] = {}
    if patch_file and Path(patch_file).is_file():
        with open(patch_file, "r", encoding="utf-8") as f:
            patch_data = json.load(f)
        if "controllers" in patch_data:
            ips, ports = parse_controllers(patch_data)
    if controllers_file:
        with open(controllers_file, "r", encoding="utf-8") as f:
            file_ips, file_ports = parse_controllers(json.load(f))
        ips.update(file_ips)
        ports.update(file_ports)
    return ips, ports
