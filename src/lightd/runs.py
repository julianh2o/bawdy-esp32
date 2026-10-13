"""Runs: a Lua pattern with params, playing on one or more targets. Persisted across restarts."""

from __future__ import annotations
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from lightd.config import write_json_atomic
from lightd.lua import DEFAULT_BUDGET, Pattern, PatternError
from lightd.patterns import PatternLibrary
from lightd.targets import Target, parse_targets

log = logging.getLogger(__name__)


class Run:
    def __init__(self, targets: list[Target], pattern: Pattern, name: str | None, params: dict[str, Any] | None) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.targets = list(targets)
        self.pattern = pattern
        self.name = name  # library pattern name, or None for ad-hoc source
        self.values: dict[str, Any] = dict(params or {})
        self.pending: dict[str, Any] | None = None
        pattern.set_params(self.values)
        self.started = time.monotonic()
        self.created = time.time()
        self.last_frames: dict[Target, bytes] = {}
        self.error: dict[str, Any] | None = None
        self.error_count = 0

    @property
    def source(self) -> str:
        return self.pattern.source

    def elapsed(self, now: float | None = None) -> float:
        return (time.monotonic() if now is None else now) - self.started

    def apply_pending(self) -> None:
        """Apply param changes; called from the render thread (Lua states aren't thread-safe)."""
        pending, self.pending = self.pending, None
        if pending is not None:
            self.pattern.set_params(pending)

    def record_error(self, e: PatternError) -> None:
        self.error = {**e.to_dict(), "at": time.time()}
        self.error_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "targets": [str(t) for t in self.targets],
            "pattern": self.name,
            "params": self.values,
            "schema": self.pattern.schema,
            "elapsed": round(self.elapsed(), 3),
            "error": self.error,
            "error_count": self.error_count,
        }

    def to_state(self) -> dict[str, Any]:
        return {"targets": [str(t) for t in self.targets], "pattern": self.name, "source": self.source, "params": self.values}


class RunManager:
    """Active runs. A channel plays at most one run: starting a run cuts others off its targets."""

    def __init__(self, library: PatternLibrary, state_path: Path | str | None = None, budget: int = DEFAULT_BUDGET) -> None:
        self.library = library
        self.state_path = Path(state_path) if state_path else None
        self.budget = budget
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def start(
        self,
        targets: Any,
        pattern: str | None = None,
        source: str | None = None,
        params: dict[str, Any] | None = None,
        save: bool = True,
    ) -> Run:
        """Start a run from a library pattern name or ad-hoc source. Raises TargetError/LibraryError/PatternError."""
        parsed = parse_targets(targets)
        if source is None:
            if not pattern:
                raise ValueError("Expected 'pattern' (a library name) or 'source' (Lua code)")
            source = self.library.source(pattern)
        if params is not None and not isinstance(params, dict):
            raise ValueError("'params' must be an object")
        run = Run(parsed, Pattern(source, budget=self.budget), pattern if pattern else None, params)
        with self._lock:
            for other in list(self._runs.values()):
                other.targets = [t for t in other.targets if t not in parsed]
                if not other.targets:
                    del self._runs[other.id]
            self._runs[run.id] = run
        if save:
            self.save()
        return run

    def update(self, run_id: str, params: dict[str, Any]) -> Run:
        if not isinstance(params, dict):
            raise ValueError("'params' must be an object")
        run = self.get(run_id)
        run.values = {**run.values, **params}
        run.pending = dict(run.values)
        self.save()
        return run

    def get(self, run_id: str) -> Run:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def stop(self, run_id: str) -> None:
        with self._lock:
            if self._runs.pop(run_id, None) is None:
                raise KeyError(run_id)
        self.save()

    def stop_all(self) -> None:
        with self._lock:
            self._runs.clear()
        self.save()

    def runs(self) -> list[Run]:
        with self._lock:
            return sorted(self._runs.values(), key=lambda r: r.created)

    def assignments(self) -> dict[Target, Run]:
        with self._lock:
            return {t: run for run in self._runs.values() for t in run.targets}

    # ---------- persistence ----------

    def save(self) -> None:
        if self.state_path is None:
            return
        try:
            write_json_atomic(self.state_path, {"runs": [r.to_state() for r in self.runs()]})
        except OSError as e:
            log.warning("Could not save run state to %s: %s", self.state_path, e)

    def restore(self) -> list[str]:
        """Start the runs saved in the state file. Returns error messages for runs that couldn't start."""
        if self.state_path is None or not self.state_path.is_file():
            return []
        try:
            doc = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return [f"{self.state_path}: {e}"]
        errors = []
        for item in doc.get("runs") or []:
            try:
                self.start(item.get("targets"), pattern=item.get("pattern"), source=item.get("source"),
                           params=item.get("params"), save=False)
            except (ValueError, PatternError) as e:
                errors.append(f"{item.get('pattern') or 'ad-hoc pattern'} on {item.get('targets')}: {e}")
        return errors
