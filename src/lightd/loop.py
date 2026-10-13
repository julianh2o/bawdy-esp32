"""The render loop: at a fixed rate, render every run on its channels and send each board one frame.

Patterns get ``t`` = Unix time in seconds, so every channel and every run is in phase by definition, and a
pattern can tell the time of day. Each channel's output is then scaled by its brightness.
"""

from __future__ import annotations
import functools
import threading
import time
from typing import Any

from wled import WledError

from lightd.lua import PatternError
from lightd.registry import Registry
from lightd.runs import RunManager
from lightd.targets import Target

DEFAULT_FPS = 30.0


@functools.lru_cache(maxsize=64)
def _scale_table(level: float) -> bytes:
    return bytes(round(v * level) for v in range(256))


def scale(data: bytes, level: float) -> bytes:
    """Every byte multiplied by ``level`` (0..1)."""
    if level >= 1.0:
        return data
    if level <= 0.0:
        return bytes(len(data))
    return data.translate(_scale_table(level))


class RenderLoop:
    def __init__(self, registry: Registry, runs: RunManager, fps: float = DEFAULT_FPS) -> None:
        self.registry = registry
        self.runs = runs
        self.fps = max(1.0, min(120.0, float(fps)))
        self.actual_fps = 0.0
        self.frames = 0
        self.last_frame_ms = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sending = True

    def step(self, now: float | None = None) -> None:
        """Render and send one frame to every board whose channels are known. Idle channels are black.

        ``now`` is the Unix time patterns see as ``t`` (default: the current time). With output off, nothing is sent;
        on the frame output goes off, every board is told to leave realtime mode so it shows its own effects at once.
        """
        if not self.runs.output:
            if self._sending:
                self._sending = False
                self._release_all()
            return
        self._sending = True
        now = time.time() if now is None else now
        assignments = self.runs.assignments()
        for run in set(assignments.values()):
            run.apply_pending()

        for entry in self.registry.entries():
            controller = entry.controller
            if controller.channels is None:
                continue
            buffers: dict[int, bytes] = {}
            for ch in controller.channels:
                target = Target(entry.id, ch.index)
                run = assignments.get(target)
                if run is None:
                    continue
                try:
                    data = run.pattern.render(ch.length, now, entry.reversed(ch.index), rgbw=ch.bytes_per_pixel == 4)
                    run.last_frames[target] = data
                    run.error = None
                except PatternError as e:
                    run.record_error(e)
                    data = run.last_frames.get(target)  # keep showing the last good frame
                if data:
                    buffers[ch.index] = scale(data, self.runs.brightness(target))
            try:
                controller.send_channels(buffers)
            except WledError:
                pass
        self.frames += 1

    def _release_all(self) -> None:
        def release(controller: Any) -> None:
            try:
                controller.release()
            except WledError:
                pass

        for entry in self.registry.entries():
            if entry.online:
                threading.Thread(target=release, args=(entry.controller,), daemon=True, name=f"Release-{entry.id}").start()

    def _run(self) -> None:
        period = 1.0 / self.fps
        next_tick = time.monotonic()
        window_start, window_frames = next_tick, 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            self.step()
            self.last_frame_ms = (time.monotonic() - t0) * 1000
            window_frames += 1
            if t0 - window_start >= 1.0:
                self.actual_fps = window_frames / (t0 - window_start)
                window_start, window_frames = t0, 0
            next_tick += period
            delay = next_tick - time.monotonic()
            if delay < -period:  # fell behind: don't try to catch up with a burst
                next_tick = time.monotonic()
                delay = 0
            self._stop.wait(max(0.0, delay))

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="RenderLoop")
        self._thread.start()

    def stop(self, blackout: bool = True) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if blackout and self.runs.output:
            for entry in self.registry.entries():
                if entry.controller.channels is not None:
                    entry.controller.blackout()
