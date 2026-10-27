"""WledController: one WLED board — its info and channels, DDP output, identify and state."""

from __future__ import annotations
import threading
import time
from typing import Any

from wled.ddp import DDP_PORT, ddp_packets
from wled.http import WledHttp
from wled.models import (
    BoardInfo, Channel, Segment, WledError, board_channels, info_from_json, outputs_from_cfg, segments_from_state,
)
from wled.transport import UdpTransport

IDENTIFY_LEVEL = 200  # same value in every byte: looks alike whatever the color order; easy on the PSU
IDENTIFY_HALF_PERIOD = 0.25  # 2 Hz blink
IDENTIFY_FRAME_INTERVAL = 0.05
DEFAULT_IDENTIFY_SECONDS = 6.0
FALLBACK_LED_COUNT = 1500  # when the board's size is unknown; WLED ignores data past its LEDs
MAX_LEDS = 4096

_shared_transport: UdpTransport | None = None
_shared_lock = threading.Lock()


def _default_transport() -> UdpTransport:
    global _shared_transport
    with _shared_lock:
        if _shared_transport is None:
            _shared_transport = UdpTransport()
        return _shared_transport


class WledController:
    """A WLED board reached at ``host`` (IP or hostname).

    ``refresh()`` loads the board's info, LED outputs and segments over HTTP and builds its channels from them
    (see ``wled.models``). Frames go out over DDP, as RGBW when any channel can show white; while ``identify()`` is
    blinking, ``send_frame``/``send_channels`` are ignored.
    """

    def __init__(self, host: str, port: int = DDP_PORT, transport: Any = None, http: WledHttp | None = None) -> None:
        self._host = host
        self.port = port
        self.transport = transport if transport is not None else _default_transport()
        self.http = http or WledHttp(host)
        self.info: BoardInfo | None = None
        self.channels: list[Channel] | None = None
        self.outputs: list[Channel] = []  # LED outputs (buses), as /json/cfg lists them
        self.segments: list[Segment] = []
        self.raw_segments: list[dict[str, Any]] = []  # /json/state "seg" as the board sent it
        self.last_refresh: float | None = None
        self.last_error: str | None = None
        self._seq = 0
        self._send_lock = threading.Lock()
        self._identify_stop: threading.Event | None = None

    # ---------- identity ----------

    @property
    def host(self) -> str:
        return self._host

    @host.setter
    def host(self, value: str) -> None:
        self._host = value
        self.http.host = value

    def __repr__(self) -> str:
        return f"WledController({self._host!r}, port={self.port})"

    def refresh(self) -> WledController:
        """Load ``/json/info``, the LED outputs from ``/json/cfg`` and the segments from ``/json/state``.
        Raises ``WledError``."""
        try:
            info = info_from_json(self.http.info())
            cfg, state = self.http.cfg(), self.http.state()
        except WledError as e:
            self.last_error = str(e)
            raise
        self.outputs, self.segments = outputs_from_cfg(cfg), segments_from_state(state)
        self.raw_segments = [s for s in (state.get("seg") or []) if isinstance(s, dict)]
        self.info, self.channels = info, board_channels(cfg, state, info.led_count)
        self.last_refresh = time.time()
        self.last_error = None
        return self

    @property
    def pixel_count(self) -> int:
        """LEDs addressed by the channels (end of the last one)."""
        if self.channels:
            return max(c.end for c in self.channels)
        return self.info.led_count if self.info else 0

    def channel(self, index: int) -> Channel:
        if not self.channels or not 0 <= index < len(self.channels):
            raise WledError(f"{self._host} has no channel {index + 1}")
        return self.channels[index]

    # ---------- output ----------

    def _next_seq(self) -> int:
        self._seq = self._seq % 15 + 1
        return self._seq

    def _send(self, data: bytes, offset_leds: int = 0, rgbw: bool = False) -> None:
        bpp = 4 if rgbw else 3
        with self._send_lock:
            self.transport.send(self._host, self.port, ddp_packets(data, self._next_seq(), offset=offset_leds * bpp, rgbw=rgbw))

    @property
    def identifying(self) -> bool:
        return self._identify_stop is not None and not self._identify_stop.is_set()

    def send_frame(self, rgb: bytes, start_led: int = 0) -> None:
        """Send RGB bytes (3 per LED) starting at ``start_led``, pushed immediately."""
        if not self.identifying:
            self._send(rgb, start_led)

    def send_channels(self, buffers: dict[int, bytes]) -> None:
        """Send one frame covering every channel: given channels get their pixels, the rest black.

        Requires ``channels`` (call ``refresh()`` first). Each buffer has ``ch.bytes_per_pixel`` bytes per pixel:
        r, g, b for RGB channels; r, g, b, w for RGBW and linked ones. Buffers are cut or zero-padded to the
        channel's length, and placed per the channel's LED map (grouping, spacing, reverse). A linked channel's r, g, b
        go to its RGB LEDs and its w, spread over the white LEDs, to every byte of those. The frame goes out as RGBW when any channel can show white, else as RGB.
        """
        if self.channels is None:
            raise WledError(f"{self._host}: channels unknown, call refresh() first")
        bpp = max((ch.bytes_per_pixel for ch in self.channels), default=3)
        frame = bytearray(self.pixel_count * bpp)
        for ch in self.channels:
            data = buffers.get(ch.index)
            if not data:
                continue
            cb = ch.bytes_per_pixel
            n = min(len(data) // cb, ch.length)
            data = data[:n * cb]
            if ch.linked:  # r, g, b to the RGB LEDs, w to the white ones
                rgb = bytearray(n * 3)
                for k in range(3):
                    rgb[k::3] = data[k::4]
                ch.led_map.place(frame, bpp, bytes(rgb), 3)
                levels = _spread(data[3::4], ch.white.count)
                ch.white.place(frame, bpp, bytes(v for v in levels for _ in range(4)), 4)
            else:
                ch.led_map.place(frame, bpp, data, cb)
        if not self.identifying:
            self._send(bytes(frame), rgbw=bpp == 4)

    def blackout(self) -> None:
        count = self.pixel_count or FALLBACK_LED_COUNT
        self._send(bytes(count * 3))

    # ---------- identify ----------

    def identify(
        self,
        channel: int | None = None,
        seconds: float = DEFAULT_IDENTIFY_SECONDS,
        led_start: int | None = None,
        led_count: int | None = None,
        ranges: list[tuple[int, int]] | None = None,
    ) -> None:
        """Blink the whole board, one channel (0-based, every LED range it covers), an LED range, or several
        ``ranges`` of ``(start, count)``, in a background thread.

        Other LEDs stay dark. Normal frames are ignored until the blink ends with a black frame.
        """
        if channel is not None:
            ranges = self.channel(channel).ranges
        elif not ranges:
            start = int(led_start or 0)
            ranges = [(start, led_count or (self.pixel_count - start if self.pixel_count > start else FALLBACK_LED_COUNT))]
        clean = []
        for start, count in ranges:
            start = max(0, min(MAX_LEDS - 1, int(start)))
            clean.append((start, max(1, min(MAX_LEDS - start, int(count)))))
        seconds = max(0.5, min(60.0, float(seconds)))

        self.stop_identify()
        stop = threading.Event()
        self._identify_stop = stop
        threading.Thread(
            target=self._blink, args=(clean, seconds, stop), daemon=True, name=f"Identify-{self._host}",
        ).start()

    def stop_identify(self) -> None:
        if self._identify_stop is not None:
            self._identify_stop.set()

    def _blink(self, ranges: list[tuple[int, int]], seconds: float, stop: threading.Event) -> None:
        on = bytearray(max(s + n for s, n in ranges) * 3)
        for start, count in ranges:
            on[start * 3: (start + count) * 3] = bytes([IDENTIFY_LEVEL]) * (count * 3)
        on = bytes(on)
        off = bytes(len(on))
        began = time.monotonic()
        while not stop.is_set() and time.monotonic() - began < seconds:
            lit = int((time.monotonic() - began) / IDENTIFY_HALF_PERIOD) % 2 == 0
            self._send(on if lit else off)
            stop.wait(IDENTIFY_FRAME_INTERVAL)
        self._send(off)
        stop.set()
        if self._identify_stop is stop:
            self._identify_stop = None

    # ---------- JSON API state ----------

    def state(self) -> dict[str, Any]:
        return self.http.state()

    def set_state(self, **state: Any) -> dict[str, Any]:
        """POST ``/json/state``, e.g. ``set_state(on=True, bri=128)``."""
        return self.http.set_state(**state)

    def release(self) -> None:
        """Leave realtime mode now, so the board shows its own effects (instead of waiting for its timeout)."""
        self.http.set_state(live=False)


def _spread(levels: bytes, count: int) -> bytes:
    """``levels`` resampled to ``count`` values, each the mean of the source values it covers."""
    n = len(levels)
    if n == count or not n:
        return levels if n else bytes(count)
    out = bytearray(count)
    for j in range(count):
        a = j * n // count
        b = max(a + 1, (j + 1) * n // count)
        out[j] = round(sum(levels[a:b]) / (b - a))
    return bytes(out)
