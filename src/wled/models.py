"""Data describing a WLED board: its identity, LED outputs and segments, and the channels built from them.

A channel is what lightd renders a pattern onto. Channels come from the board's segments (``/json/state``), or from
its LED outputs (``/json/cfg``) when it reports no segments. A segment named "Govee RGB" and one named "Govee White"
(same suffix, if any, e.g. "Govee RGB 2" / "Govee White 2") make one linked channel: the pattern's r, g, b go to the
RGB segment and its w to the white segment.
"""

from __future__ import annotations
import re
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
    """A run of LEDs that lightd drives as one strip: a segment, a linked Govee pair, or an LED output."""

    index: int  # 0-based, in order of start LED
    start: int  # first LED in the board's LED index space
    length: int
    pins: tuple[int, ...] = ()
    order: str = "GRB"  # the board applies this itself; DDP data is always RGB(W)
    rgbw: bool = False  # the LEDs have a white chip, driven by the pattern's w
    name: str | None = None  # the segment's name on the board
    segment: int | None = None  # WLED segment id (None when built from an LED output)
    white_start: int | None = None  # linked white segment (Govee): its LEDs show the pattern's w
    white_length: int = 0
    white_segment: int | None = None

    @property
    def linked(self) -> bool:
        return self.white_start is not None

    @property
    def bytes_per_pixel(self) -> int:
        """4 (r, g, b, w) when the channel can show white, else 3."""
        return 4 if self.rgbw or self.linked else 3

    @property
    def ranges(self) -> list[tuple[int, int]]:
        """(start, length) of every LED range the channel covers."""
        out = [(self.start, self.length)]
        if self.linked:
            out.append((self.white_start, self.white_length))
        return out

    @property
    def end(self) -> int:
        return max(s + n for s, n in self.ranges)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start,
            "length": self.length,
            "pins": list(self.pins),
            "order": self.order,
            "rgbw": self.rgbw,
            "name": self.name,
            "segment": self.segment,
            "linked": self.linked,
            "white": {"start": self.white_start, "length": self.white_length, "segment": self.white_segment}
            if self.linked else None,
        }


@dataclass(frozen=True)
class Segment:
    """A WLED segment, from ``/json/state`` ``seg``."""

    id: int
    start: int
    length: int
    name: str | None = None


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


def outputs_from_cfg(cfg: dict[str, Any]) -> list[Channel]:
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


def segments_from_state(state: dict[str, Any]) -> list[Segment]:
    """Segments from ``/json/state`` ``seg``, sorted by start LED. Empty and 2-D segments are left out."""
    out = []
    for s in state.get("seg") or []:
        if not isinstance(s, dict) or s.get("stopY") not in (None, 0, 1):
            continue
        start = int(s.get("start") or 0)
        length = int(s.get("len") or (int(s.get("stop") or 0) - start))
        if length > 0:
            name = s.get("n")
            out.append(Segment(int(s.get("id") or 0), start, length, name.strip() if isinstance(name, str) and name.strip() else None))
    return sorted(out, key=lambda s: (s.start, s.id))


_GOVEE_RE = re.compile(r"^govee\s*(rgb|white)\b\s*(.*)$", re.IGNORECASE)


def _govee_part(name: str | None) -> tuple[str, str] | None:
    """"Govee RGB 2" -> ("rgb", "2"); None if the name isn't a Govee segment."""
    m = _GOVEE_RE.match(name or "")
    return (m.group(1).lower(), m.group(2).strip().lower()) if m else None


def channels_from_segments(segments: list[Segment], outputs: list[Channel]) -> list[Channel]:
    """One channel per segment, with each Govee RGB/White pair merged into a linked channel.

    A segment takes pins, color order and RGBW from the LED output it starts in. A Govee white segment without a
    matching RGB segment stays a channel of its own.
    """
    def output_at(led: int) -> Channel | None:
        return next((o for o in outputs if o.start <= led < o.start + o.length), None)

    # suffix -> the first segment of each kind with that suffix
    first: dict[str, dict[str, Segment]] = {"rgb": {}, "white": {}}
    for seg in segments:
        if part := _govee_part(seg.name):
            first[part[0]].setdefault(part[1], seg)
    pairs = {first["rgb"][k].id: first["white"][k] for k in first["rgb"].keys() & first["white"].keys()}
    paired_whites = {w.id for w in pairs.values()}

    out: list[Channel] = []
    for seg in segments:
        if seg.id in paired_whites:
            continue  # part of its RGB segment's channel
        white = pairs.get(seg.id)
        o = output_at(seg.start)
        out.append(Channel(
            index=len(out),
            start=seg.start,
            length=seg.length,
            pins=o.pins if o else (),
            order=o.order if o else "GRB",
            rgbw=o.rgbw if o else False,
            name=seg.name,
            segment=seg.id,
            white_start=white.start if white else None,
            white_length=white.length if white else 0,
            white_segment=white.id if white else None,
        ))
    return out


def board_channels(cfg: dict[str, Any] | None, state: dict[str, Any] | None, led_count: int = 0) -> list[Channel]:
    """A board's channels: from its segments when it reports any, else its LED outputs, else one channel of
    ``led_count`` LEDs (firmware without ``hw.led.ins``)."""
    outputs = outputs_from_cfg(cfg or {})
    segments = segments_from_state(state or {})
    if segments:
        return channels_from_segments(segments, outputs)
    if outputs:
        return outputs
    return [Channel(index=0, start=0, length=led_count)] if led_count else []


def normalize_mac(mac: str | None) -> str:
    """``68:FE:71:a4:ad:b4`` / ``68fe71a4adb4`` -> ``68fe71a4adb4`` (empty if unknown)."""
    return "".join(c for c in (mac or "").lower() if c in "0123456789abcdef")
