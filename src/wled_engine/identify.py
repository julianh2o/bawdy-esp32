"""Identify blink: flash a whole board so you can tell which physical controller is which."""

from __future__ import annotations
import threading
from dataclasses import dataclass

# Every byte the same value, so the frame looks identical whatever the board's color
# order or RGB/RGBW setting. Kept below 255 to go easy on the power supply.
IDENTIFY_LEVEL = 200
IDENTIFY_HALF_PERIOD = 0.25  # seconds on, then seconds off (2 Hz)
DEFAULT_IDENTIFY_SECONDS = 6.0
DEFAULT_IDENTIFY_LEDS = 1500  # WLED ignores data past its LED count, so overshooting is harmless
MAX_IDENTIFY_LEDS = 4096


@dataclass
class _Target:
    start: float
    until: float
    led_count: int
    led_start: int = 0

    def frame(self, lit: bool) -> bytes:
        # LEDs before led_start stay dark so a single output's range can be picked out.
        return bytes(self.led_start * 3) + bytes([IDENTIFY_LEVEL if lit else 0]) * (self.led_count * 3)


class IdentifyScheduler:
    """Tracks boards being identified and produces each one's frame bytes per tick."""

    def __init__(self) -> None:
        self._targets: dict[tuple[str, int], _Target] = {}
        self._lock = threading.Lock()

    def start(self, ip: str, port: int, led_count: int, seconds: float, now: float, led_start: int = 0) -> None:
        led_start = max(0, min(MAX_IDENTIFY_LEDS - 1, int(led_start)))
        led_count = max(1, min(MAX_IDENTIFY_LEDS - led_start, int(led_count)))
        seconds = max(0.5, min(60.0, float(seconds)))
        with self._lock:
            self._targets[(ip, port)] = _Target(start=now, until=now + seconds, led_count=led_count, led_start=led_start)

    def cancel(self, ip: str, port: int) -> None:
        with self._lock:
            target = self._targets.get((ip, port))
            if target:
                target.until = 0.0  # emits one final black frame next tick

    @property
    def active(self) -> list[tuple[str, int]]:
        with self._lock:
            return list(self._targets)

    def frames(self, now: float) -> dict[tuple[str, int], bytes]:
        """Frame bytes for every target this tick. An expired target gets one black frame, then is dropped."""
        out: dict[tuple[str, int], bytes] = {}
        with self._lock:
            for key, t in list(self._targets.items()):
                if now >= t.until:
                    out[key] = t.frame(False)
                    del self._targets[key]
                    continue
                lit = int((now - t.start) / IDENTIFY_HALF_PERIOD) % 2 == 0
                out[key] = t.frame(lit)
        return out
