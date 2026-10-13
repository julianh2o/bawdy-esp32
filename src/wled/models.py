"""Data describing a WLED board: its identity and its LED outputs (channels)."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

# WLED's color order codes (low nibble of an output's "order" in /json/cfg).
WLED_COLOR_ORDERS = {0: "GRB", 1: "RGB", 2: "BRG", 3: "RBG", 4: "BGR", 5: "GBR"}

# WLED bus types that drive RGBW pixels (SK6812 RGBW, TM1814).
RGBW_BUS_TYPES = {30, 31}


class WledError(Exception):
    """A board couldn't be reached or returned something unusable."""


@dataclass(frozen=True)
class Channel:
    """One LED output of a board (WLED calls these "LED outputs" / buses)."""

    index: int  # 0-based, in order of start LED
    start: int  # first LED of this output in the board's LED index space
    length: int
    pins: tuple[int, ...] = ()
    order: str = "GRB"  # the board applies this itself; DDP data is always RGB
    rgbw: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start,
            "length": self.length,
            "pins": list(self.pins),
            "order": self.order,
            "rgbw": self.rgbw,
        }


@dataclass
class BoardInfo:
    """What ``/json/info`` says about a board."""

    name: str | None = None
    mac: str | None = None
    version: str | None = None
    arch: str | None = None
    led_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mac": self.mac,
            "version": self.version,
            "arch": self.arch,
            "led_count": self.led_count,
        }


def info_from_json(info: dict[str, Any]) -> BoardInfo:
    leds = info.get("leds") or {}
    return BoardInfo(
        name=info.get("name"),
        mac=info.get("mac"),
        version=info.get("ver"),
        arch=info.get("arch"),
        led_count=int(leds.get("count") or 0),
        raw=info,
    )


def channels_from_cfg(cfg: dict[str, Any]) -> list[Channel]:
    """Channels from ``/json/cfg`` ``hw.led.ins``, sorted by start LED."""
    ins = ((cfg.get("hw") or {}).get("led") or {}).get("ins") or []
    outs = sorted(
        (o for o in ins if isinstance(o, dict) and int(o.get("len") or 0) > 0),
        key=lambda o: int(o.get("start") or 0),
    )
    return [
        Channel(
            index=i,
            start=int(o.get("start") or 0),
            length=int(o.get("len") or 0),
            pins=tuple(int(p) for p in (o.get("pin") or [])),
            order=WLED_COLOR_ORDERS.get(int(o.get("order") or 0) & 0x0F, str(o.get("order"))),
            rgbw=int(o.get("type") or 0) in RGBW_BUS_TYPES,
        )
        for i, o in enumerate(outs)
    ]


def normalize_mac(mac: str | None) -> str:
    """``68:FE:71:a4:ad:b4`` / ``68fe71a4adb4`` -> ``68fe71a4adb4`` (empty if unknown)."""
    return "".join(c for c in (mac or "").lower() if c in "0123456789abcdef")
