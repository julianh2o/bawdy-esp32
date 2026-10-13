"""WledController: one WLED board — its info and channels, DDP output, identify and state."""

from __future__ import annotations
import threading
import time
from typing import Any

from wled.ddp import DDP_PORT, ddp_packets
from wled.http import WledHttp
from wled.models import BoardInfo, Channel, WledError, channels_from_cfg, info_from_json
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

    ``refresh()`` loads the board's info and channels over HTTP. Frames go out over DDP;
    while ``identify()`` is blinking, ``send_frame``/``send_channels`` are ignored.
    """

    def __init__(self, host: str, port: int = DDP_PORT, transport: Any = None, http: WledHttp | None = None) -> None:
        self._host = host
        self.port = port
        self.transport = transport if transport is not None else _default_transport()
        self.http = http or WledHttp(host)
        self.info: BoardInfo | None = None
        self.channels: list[Channel] | None = None
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
        """Load ``/json/info`` and the LED outputs from ``/json/cfg``. Raises ``WledError``."""
        try:
            info = info_from_json(self.http.info())
            channels = channels_from_cfg(self.http.cfg())
        except WledError as e:
            self.last_error = str(e)
            raise
        if not channels and info.led_count:
            channels = [Channel(index=0, start=0, length=info.led_count)]  # firmware without hw.led.ins
        self.info, self.channels = info, channels
        self.last_refresh = time.time()
        self.last_error = None
        return self

    @property
    def pixel_count(self) -> int:
        """LEDs addressed by the channels (end of the last one)."""
        if self.channels:
            return max(c.start + c.length for c in self.channels)
        return self.info.led_count if self.info else 0

    def channel(self, index: int) -> Channel:
        if not self.channels or not 0 <= index < len(self.channels):
            raise WledError(f"{self._host} has no channel {index + 1}")
        return self.channels[index]

    # ---------- output ----------

    def _next_seq(self) -> int:
        self._seq = self._seq % 15 + 1
        return self._seq

    def _send(self, data: bytes, offset_leds: int = 0) -> None:
        with self._send_lock:
            self.transport.send(self._host, self.port, ddp_packets(data, self._next_seq(), offset=offset_leds * 3))

    @property
    def identifying(self) -> bool:
        return self._identify_stop is not None and not self._identify_stop.is_set()

    def send_frame(self, rgb: bytes, start_led: int = 0) -> None:
        """Send RGB bytes (3 per LED) starting at ``start_led``, pushed immediately."""
        if not self.identifying:
            self._send(rgb, start_led)

    def send_channels(self, buffers: dict[int, bytes]) -> None:
        """Send one frame covering every channel: given channels get their RGB bytes, the rest black.

        Requires ``channels`` (call ``refresh()`` first). Buffers are cut or zero-padded to each
        channel's length; RGBW outputs get W = 0 (WLED fills W from RGB per its own settings).
        """
        if self.channels is None:
            raise WledError(f"{self._host}: channels unknown, call refresh() first")
        frame = bytearray(self.pixel_count * 3)
        for ch in self.channels:
            data = buffers.get(ch.index)
            if data:
                n = min(len(data), ch.length * 3)
                frame[ch.start * 3: ch.start * 3 + n] = data[:n]
        self.send_frame(bytes(frame))

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
    ) -> None:
        """Blink the whole board, one channel (0-based), or an LED range, in a background thread.

        Other LEDs stay dark. Normal frames are ignored until the blink ends with a black frame.
        """
        if channel is not None:
            ch = self.channel(channel)
            led_start, led_count = ch.start, ch.length
        start = max(0, min(MAX_LEDS - 1, int(led_start or 0)))
        count = int(led_count or (self.pixel_count - start if self.pixel_count > start else FALLBACK_LED_COUNT))
        count = max(1, min(MAX_LEDS - start, count))
        seconds = max(0.5, min(60.0, float(seconds)))

        self.stop_identify()
        stop = threading.Event()
        self._identify_stop = stop
        threading.Thread(
            target=self._blink, args=(start, count, seconds, stop), daemon=True, name=f"Identify-{self._host}",
        ).start()

    def stop_identify(self) -> None:
        if self._identify_stop is not None:
            self._identify_stop.set()

    def _blink(self, start: int, count: int, seconds: float, stop: threading.Event) -> None:
        on = bytes(start * 3) + bytes([IDENTIFY_LEVEL]) * (count * 3)
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
