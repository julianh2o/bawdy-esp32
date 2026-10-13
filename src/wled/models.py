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
class Leds:
    """Where a strip's pixels are on the board: ``span`` LEDs from ``start``, in groups of ``group`` LEDs that show
    the same pixel, each group followed by ``spacing`` LEDs left dark (WLED's segment grouping and spacing).
    ``reverse`` runs the pixels from the far end."""

    start: int
    span: int
    group: int = 1
    spacing: int = 0
    reverse: bool = False

    @property
    def step(self) -> int:
        return self.group + self.spacing

    @property
    def count(self) -> int:
        """Pixels (WLED's virtual length)."""
        return (self.span + self.step - 1) // self.step

    @property
    def end(self) -> int:
        return self.start + self.span

    @property
    def contiguous(self) -> bool:
        return self.spacing == 0 and self.group == 1 and not self.reverse

    def runs(self) -> list[tuple[int, int]]:
        """(start, count) of each run of lit LEDs."""
        if self.spacing == 0:
            return [(self.start, self.span)]
        return [(self.start + i * self.step, min(self.group, self.span - i * self.step)) for i in range(self.count)]

    def place(self, frame: bytearray, bpp: int, pixels: bytes, cb: int) -> None:
        """Write ``pixels`` (``cb`` bytes each) into ``frame`` (``bpp`` bytes per LED). Extra pixels are dropped;
        when ``cb`` < ``bpp`` the missing bytes are left as they are (zero)."""
        n = min(len(pixels) // cb, self.count)
        if not n:
            return
        if self.contiguous and cb == bpp:
            frame[self.start * bpp: (self.start + n) * bpp] = pixels[:n * cb]
            return
        if self.reverse:  # pixel 0 at the far end, so pad a short buffer to full length first
            pixels, n = pixels[:n * cb] + bytes((self.count - n) * cb), self.count
            pixels = b"".join(pixels[i * cb:(i + 1) * cb] for i in range(n - 1, -1, -1))
        stride = self.step * bpp
        for j in range(self.group):
            m = min(n, (self.span - j + self.step - 1) // self.step)  # pixels whose j-th LED is inside the span
            if m <= 0:
                continue
            base = (self.start + j) * bpp
            for k in range(min(cb, bpp)):
                frame[base + k: base + k + m * stride: stride] = pixels[k: m * cb: cb]


@dataclass(frozen=True)
class Channel:
    """A strip lightd drives as one: a segment, a linked Govee pair, or an LED output.

    ``start``/``length`` are the first LED and the pixel count patterns see (``COUNT``). ``leds`` says where those
    pixels are when they aren't simply ``length`` LEDs from ``start`` (segment grouping/spacing/reverse).
    """

    index: int  # 0-based, in order of start LED
    start: int  # first LED in the board's LED index space
    length: int  # pixels
    pins: tuple[int, ...] = ()
    order: str = "GRB"  # the board applies this itself; DDP data is always RGB(W)
    rgbw: bool = False  # the LEDs have a white chip, driven by the pattern's w
    name: str | None = None  # the segment's name on the board
    segment: int | None = None  # WLED segment id (None when built from an LED output)
    leds: Leds | None = None
    white: Leds | None = None  # linked white segment (Govee): its LEDs show the pattern's w
    white_segment: int | None = None

    @property
    def led_map(self) -> Leds:
        return self.leds or Leds(self.start, self.length)

    @property
    def linked(self) -> bool:
        return self.white is not None

    @property
    def bytes_per_pixel(self) -> int:
        """4 (r, g, b, w) when the channel can show white, else 3."""
        return 4 if self.rgbw or self.linked else 3

    @property
    def ranges(self) -> list[tuple[int, int]]:
        """(start, count) of every run of LEDs the channel lights."""
        return self.led_map.runs() + (self.white.runs() if self.white else [])

    @property
    def end(self) -> int:
        return max(self.led_map.end, self.white.end if self.white else 0)

    def to_dict(self) -> dict[str, Any]:
        m = self.led_map
        return {
            "index": self.index,
            "start": self.start,
            "length": self.length,
            "span": m.span,
            "group": m.group,
            "spacing": m.spacing,
            "pins": list(self.pins),
            "order": self.order,
            "rgbw": self.rgbw,
            "name": self.name,
            "segment": self.segment,
            "linked": self.linked,
            "white": {"start": self.white.start, "length": self.white.count, "segment": self.white_segment}
            if self.white else None,
            "ranges": [list(r) for r in self.ranges],
        }


@dataclass(frozen=True)
class Segment:
    """A WLED segment, from ``/json/state`` ``seg``."""

    id: int
    start: int
    span: int  # LEDs from start to stop
    name: str | None = None
    group: int = 1
    spacing: int = 0
    reverse: bool = False

    @property
    def leds(self) -> Leds:
        return Leds(self.start, self.span, self.group, self.spacing, self.reverse)


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
        span = int(s.get("len") or (int(s.get("stop") or 0) - start))
        if span > 0:
            name = s.get("n")
            out.append(Segment(
                int(s.get("id") or 0), start, span,
                name=name.strip() if isinstance(name, str) and name.strip() else None,
                group=max(1, int(s.get("grp") or 1)),
                spacing=max(0, int(s.get("spc") or 0)),
                reverse=bool(s.get("rev")),
            ))
    return sorted(out, key=lambda s: (s.start, s.id))


_GOVEE_RE = re.compile(r"^govee\s*(rgb|white)\b\s*(.*)$", re.IGNORECASE)


def _govee_part(name: str | None) -> tuple[str, str] | None:
    """"Govee RGB 2" -> ("rgb", "2"); None if the name isn't a Govee segment."""
    m = _GOVEE_RE.match(name or "")
    return (m.group(1).lower(), m.group(2).strip().lower()) if m else None


def channels_from_segments(segments: list[Segment], outputs: list[Channel]) -> list[Channel]:
    """One channel per segment, with each Govee RGB/White pair merged into a linked channel.

    A segment takes pins, color order and RGBW from the LED output it starts in. Its grouping, spacing and reverse
    are honoured (Govee strips interleave the two: spacing 1, RGB on odd LEDs, white on even). A Govee white
    segment without a matching RGB segment stays a channel of its own.
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
            length=seg.leds.count,
            pins=o.pins if o else (),
            order=o.order if o else "GRB",
            rgbw=o.rgbw if o else False,
            name=seg.name,
            segment=seg.id,
            leds=None if seg.leds.contiguous else seg.leds,
            white=white.leds if white else None,
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


def channel_warnings(channels: list[Channel], outputs: list[Channel]) -> list[list[str]]:
    """Per channel, how its segment disagrees with the LED output it starts in: LEDs of the output no segment
    lights (e.g. the output was lengthened in WLED but its segment wasn't), or a segment running past the output's
    end. Each warning says how to fix it on the board. Only segment channels are checked."""
    warnings: list[list[str]] = [[] for _ in channels]
    if not outputs or not any(ch.segment is not None for ch in channels):
        return warnings
    lit = bytearray(max([o.start + o.length for o in outputs] + [ch.end for ch in channels]))
    for ch in channels:
        for start, count in ch.ranges:
            lit[start:start + count] = b"\x01" * count

    def where(o: Channel) -> str:
        return f"the output on GPIO {','.join(map(str, o.pins))}" if o.pins else f"output {o.index + 1}"

    for o in outputs:
        end = o.start + o.length
        for ch in channels:
            # each segment (a linked channel has two) against the output it starts in
            for seg, leds in ((ch.segment, ch.led_map), (ch.white_segment, ch.white)):
                if seg is None or leds is None or not o.start <= leds.start < end or leds.end <= end:
                    continue
                warnings[ch.index].append(
                    f"Segment {seg} runs to LED {leds.end - 1}, past the end of {where(o)} ({o.start}–{end - 1}, "
                    f"{o.length} LEDs). Fix: in WLED, open Segments and set segment {seg}'s Stop to {end}, or "
                    f"lengthen the output in Config → LED Preferences. Then press Refresh.")
        mine = [ch for ch in channels if ch.segment is not None and o.start <= ch.start < end]
        if not mine:
            continue
        gaps, i = [], o.start
        while i < end:
            if lit[i]:
                i += 1
                continue
            j = i
            while j < end and not lit[j]:
                j += 1
            gaps.append((i, j))
            i = j
        last = max(mine, key=lambda ch: ch.led_map.end)
        for a, b in gaps:
            text = (f"LEDs {a}–{b - 1} of {where(o)} ({o.start}–{end - 1}, {o.length} LEDs) aren't in any WLED "
                    f"segment, so lightd can't light them.")
            if b == end and a >= last.led_map.end:
                fix = f"set segment {last.segment}'s Stop to {end}"
            elif a == o.start and b <= min(ch.start for ch in mine):
                first = min(mine, key=lambda ch: ch.start)
                fix = f"set segment {first.segment}'s Start to {o.start}"
            else:
                fix = f"widen the segments so they cover LEDs {a}–{b - 1}"
            text += (f" Fix: in WLED, open Segments and {fix} (or change the output's length in Config → LED "
                     f"Preferences). Then press Refresh.")
            warnings[last.index if b == end else mine[0].index].append(text)
    return warnings
