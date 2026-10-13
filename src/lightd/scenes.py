"""Scenes: named sets of library patterns (with params) on targets, started together with one call.

Format of ``scenes.json``::

    {"scenes": {"party": {"entries": [
        {"targets": ["wled_01/1", "wled_01/2"], "pattern": "rainbow", "params": {"speed": 0.5}},
        {"targets": ["wled_02/1"], "pattern": "strobe", "params": {}, "brightness": 0.6}
    ]}}}

Activating a scene starts one run per entry (cutting whatever played on those targets) and sets the brightness of
entries that give one. Channels the scene doesn't mention are left alone. A scene is *active* while every one of its
targets is still playing a run that the scene started.
"""

from __future__ import annotations
import json
import threading
from pathlib import Path
from typing import Any

from lightd.config import write_json_atomic
from lightd.lua import PatternError
from lightd.patterns import NAME_RE, LibraryError, PatternLibrary
from lightd.runs import Run, RunManager, parse_brightness
from lightd.targets import TargetError, parse_targets

MAX_ENTRIES = 64


class SceneError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def validate_entries(entries: Any, library: PatternLibrary) -> list[dict[str, Any]]:
    """Normalize a scene's entries. Each target may appear in only one entry; patterns must be in the library."""
    if not isinstance(entries, list) or not entries:
        raise SceneError("Expected 'entries': a non-empty list of {targets, pattern, params}")
    if len(entries) > MAX_ENTRIES:
        raise SceneError(f"A scene can have at most {MAX_ENTRIES} entries")
    out: list[dict[str, Any]] = []
    owner: dict[str, int] = {}
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise SceneError(f"Entry {i} must be an object")
        try:
            targets = [str(t) for t in parse_targets(entry.get("targets"))]
        except TargetError as e:
            raise SceneError(f"Entry {i}: {e}")
        for t in targets:
            if t in owner:
                raise SceneError(f"{t} is in entries {owner[t]} and {i}; a channel can only play one pattern")
            owner[t] = i
        pattern = entry.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise SceneError(f"Entry {i} needs a 'pattern' from the library")
        try:
            library.get(pattern)
        except LibraryError:
            raise SceneError(f"Entry {i}: no pattern named '{pattern}'")
        params = entry.get("params") or {}
        if not isinstance(params, dict):
            raise SceneError(f"Entry {i}: 'params' must be an object")
        item: dict[str, Any] = {"targets": targets, "pattern": pattern, "params": params}
        if entry.get("brightness") is not None:
            try:
                item["brightness"] = parse_brightness(entry["brightness"])
            except ValueError as e:
                raise SceneError(f"Entry {i}: {e}")
        out.append(item)
    return out


def is_active(name: str, entries: list[dict[str, Any]], assignments: dict[Any, Run]) -> bool:
    for entry in entries:
        for t in parse_targets(entry["targets"]):
            run = assignments.get(t)
            if run is None or run.scene != name:
                return False
    return True


class SceneStore:
    """Scenes saved in a JSON file (or only in memory when ``path`` is None)."""

    def __init__(self, path: Path | str | None, library: PatternLibrary) -> None:
        self.path = Path(path) if path else None
        self.library = library
        self._memory: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self.path is None:
            return dict(self._memory)
        if not self.path.is_file():
            return {}
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise SceneError(f"{self.path} is not valid JSON: {e}", 500)
        scenes = doc.get("scenes") if isinstance(doc, dict) else None
        return dict(scenes) if isinstance(scenes, dict) else {}

    def _write(self, scenes: dict[str, dict[str, Any]]) -> None:
        if self.path is None:
            self._memory = scenes
        else:
            write_json_atomic(self.path, {"scenes": scenes})

    def get(self, name: str) -> dict[str, Any]:
        scene = self._load().get(name)
        if scene is None:
            raise SceneError(f"No scene named '{name}'", 404)
        return {"name": name, "entries": scene.get("entries") or []}

    def list(self) -> list[dict[str, Any]]:
        return [{"name": name, "entries": s.get("entries") or []} for name, s in sorted(self._load().items())]

    def save(self, name: str, entries: Any) -> dict[str, Any]:
        if not NAME_RE.match(name):
            raise SceneError("Scene names use lowercase letters, digits, '-' and '_' (max 64)")
        entries = validate_entries(entries, self.library)
        with self._lock:
            scenes = self._load()
            scenes[name] = {"entries": entries}
            self._write(scenes)
        return {"name": name, "entries": entries}

    def delete(self, name: str) -> None:
        with self._lock:
            scenes = self._load()
            if scenes.pop(name, None) is None:
                raise SceneError(f"No scene named '{name}'", 404)
            self._write(scenes)

    def activate(self, name: str, runs: RunManager) -> tuple[list[Run], list[str]]:
        """Start every entry. Entries that fail (e.g. a pattern deleted since) are skipped and reported."""
        started: list[Run] = []
        errors: list[str] = []
        for entry in self.get(name)["entries"]:
            try:
                started.append(runs.start(entry.get("targets"), pattern=entry.get("pattern"), params=entry.get("params"),
                                          scene=name, save=False))
                if entry.get("brightness") is not None:
                    runs.set_brightness(entry["targets"], entry["brightness"], save=False)
            except (ValueError, PatternError) as e:
                errors.append(f"{entry.get('pattern')} on {', '.join(map(str, entry.get('targets') or []))}: {e}")
        runs.save()
        return started, errors
