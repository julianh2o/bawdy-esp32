"""Fallback identify blink sent straight from the web server, for when the daemon isn't running.

Normally the daemon does this (POST /api/identify) so the blink replaces the board's show
output instead of fighting it. Mirrors ``wled_engine.identify``; the web image doesn't
ship ``wled_engine``.
"""

from __future__ import annotations
import socket
import struct
import threading
import time

IDENTIFY_LEVEL = 200
IDENTIFY_HALF_PERIOD = 0.25
FRAME_INTERVAL = 0.05
DEFAULT_SECONDS = 6.0
DEFAULT_LEDS = 1500
MAX_LEDS = 4096
_CHUNK = 480 * 3  # bytes of pixel data per DDP packet

_active: dict[tuple[str, int], threading.Event] = {}
_lock = threading.Lock()


def ddp_packets(data: bytes, seq: int) -> list[bytes]:
    """DDP datagrams for one frame, push flag on the last one so the board shows it immediately."""
    packets = []
    for offset in range(0, len(data), _CHUNK):
        chunk = data[offset:offset + _CHUNK]
        last = offset + _CHUNK >= len(data)
        header = struct.pack(">BBBBIH", 0x41 if last else 0x40, seq, 0x01, 0x01, offset, len(chunk))
        packets.append(header + chunk)
    return packets


def _run(ip: str, port: int, led_start: int, led_count: int, seconds: float, stop: threading.Event) -> None:
    dark = bytes(led_start * 3)
    on, off = dark + bytes([IDENTIFY_LEVEL]) * (led_count * 3), bytes((led_start + led_count) * 3)
    start = time.monotonic()
    seq = 1
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            while not stop.is_set() and time.monotonic() - start < seconds:
                lit = int((time.monotonic() - start) / IDENTIFY_HALF_PERIOD) % 2 == 0
                for pkt in ddp_packets(on if lit else off, seq):
                    sock.sendto(pkt, (ip, port))
                seq = seq % 15 + 1
                stop.wait(FRAME_INTERVAL)
            for pkt in ddp_packets(off, seq):
                sock.sendto(pkt, (ip, port))
        except OSError:
            pass
    with _lock:
        if _active.get((ip, port)) is stop:
            del _active[(ip, port)]


def blink_direct(
    ip: str, port: int = 4048, led_count: int | None = None, seconds: float | None = None, led_start: int = 0,
) -> None:
    """Start (or restart) blinking a board (LEDs ``led_start`` onward) in a background thread."""
    led_start = max(0, min(MAX_LEDS - 1, int(led_start or 0)))
    led_count = max(1, min(MAX_LEDS - led_start, int(led_count or DEFAULT_LEDS)))
    seconds = max(0.5, min(60.0, float(seconds or DEFAULT_SECONDS)))
    stop = threading.Event()
    with _lock:
        previous = _active.get((ip, port))
        if previous:
            previous.set()
        _active[(ip, port)] = stop
    threading.Thread(target=_run, args=(ip, port, led_start, led_count, seconds, stop), daemon=True, name=f"Identify-{ip}").start()


def stop_direct(ip: str, port: int = 4048) -> None:
    with _lock:
        ev = _active.get((ip, port))
    if ev:
        ev.set()
