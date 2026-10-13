"""The render loop: at a fixed rate, render every run on its channels and send each board one frame."""

from __future__ import annotations
import threading
import time

from wled import WledError

from lightd.lua import PatternError
from lightd.registry import Registry
from lightd.runs import RunManager
from lightd.targets import Target

DEFAULT_FPS = 30.0


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

    def step(self, now: float | None = None) -> None:
        """Render and send one frame to every board whose channels are known. Idle channels are black."""
        now = time.monotonic() if now is None else now
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
                    data = run.pattern.render(ch.length, run.elapsed(now), entry.reversed(ch.index))
                    run.last_frames[target] = data
                    run.error = None
                except PatternError as e:
                    run.record_error(e)
                    data = run.last_frames.get(target)  # keep showing the last good frame
                if data:
                    buffers[ch.index] = data
            try:
                controller.send_channels(buffers)
            except WledError:
                pass
        self.frames += 1

    def _run(self) -> None:
        period = 1.0 / self.fps
        next_tick = time.monotonic()
        window_start, window_frames = next_tick, 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            self.step(t0)
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
        if blackout:
            for entry in self.registry.entries():
                if entry.controller.channels is not None:
                    entry.controller.blackout()
