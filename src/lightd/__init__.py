"""lightd: runs Lua patterns on WLED controller channels at a fixed frame rate."""

from __future__ import annotations
from pathlib import Path
from typing import Any

from wled import WledBrowser

from lightd.api import ApiServer, Lightd
from lightd.config import ControllerStore
from lightd.loop import DEFAULT_FPS, RenderLoop
from lightd.patterns import PatternLibrary
from lightd.registry import Registry
from lightd.runs import RunManager
from lightd.scenes import SceneStore


def build(
    controllers: Path | str,
    builtin_patterns: Path | str | None = None,
    user_patterns: Path | str | None = None,
    state: Path | str | None = None,
    scenes: Path | str | None = None,
    fps: float = DEFAULT_FPS,
    discovery: bool = True,
    transport: Any = None,
    dry_run: bool = False,
) -> Lightd:
    """Wire up a lightd instance (not started). ``dry_run`` also stops it changing board state over HTTP."""
    store = ControllerStore(controllers)
    browser = WledBrowser() if discovery else None
    registry = Registry(store, transport=transport, browser=browser)
    library = PatternLibrary(builtin_patterns, user_patterns)
    runs = RunManager(library, state)
    return Lightd(store, registry, library, runs, RenderLoop(registry, runs, fps=fps, release=not dry_run), SceneStore(scenes, library, registry))


__all__ = ["ApiServer", "Lightd", "build"]
