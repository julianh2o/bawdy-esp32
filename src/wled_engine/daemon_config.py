"""Where the daemon's universe, patch and controller endpoints come from, so they can be reloaded live."""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wled_engine.controllers import load_controllers
from wled_engine.patch.patch_table import PatchTable
from wled_engine.spatial.universe import SpatialUniverse

if TYPE_CHECKING:
    from wled_engine.core import LightingEngine


@dataclass
class LoadedConfig:
    universe: SpatialUniverse
    patch_table: PatchTable
    controller_ips: dict[str, str]
    controller_ports: dict[str, int]


@dataclass
class DaemonConfig:
    """The daemon's config sources. ``load`` reads them all; ``apply`` swaps them into a running engine."""

    universe_path: Path | None = None
    patch_path: Path | None = None
    controllers_path: Path | None = None
    preset: str | None = None
    venue: bool = False
    simulator: bool = False
    broadcast_ip: str = "255.255.255.255"

    def load(self) -> LoadedConfig:
        """Read and validate everything. Raises ``OSError``/``ValueError`` without touching the engine."""
        if self.preset:
            from wled_engine.spatial.venue import get_preset_venue
            universe, patch_table = get_preset_venue(self.preset)
        elif self.venue:
            from wled_engine.spatial.venue import create_demo_venue
            universe, patch_table = create_demo_venue()
        elif self.universe_path:
            universe = SpatialUniverse.load_json(self.universe_path)
            patch_table = PatchTable.load_json(self.patch_path or Path(self.universe_path).parent / "patch.json")
        else:
            raise ValueError("No universe configured")

        controllers_path = self.controllers_path
        if controllers_path and not Path(controllers_path).is_file():
            # Not created yet (fresh install, or the simulator image's default path): the
            # daemon runs with no boards addressed until the /config page writes it.
            controllers_path = None
        ips, ports = load_controllers(controllers_path, self.patch_path)
        return LoadedConfig(universe, patch_table, ips, ports)

    def apply(self, engine: LightingEngine, loaded: LoadedConfig) -> dict[str, Any]:
        """Swap ``loaded`` into the engine between frames; returns a summary for the API/logs."""
        engine.setup_universe(
            loaded.universe,
            loaded.patch_table,
            broadcast_ip=self.broadcast_ip,
            controller_ips=loaded.controller_ips,
            controller_ports=loaded.controller_ports,
            simulator=self.simulator,
            allow_missing=True,
        )
        engine.daemon_config = self
        return summarize(engine)


def summarize(engine: LightingEngine) -> dict[str, Any]:
    nodes = engine.dispatcher.nodes if engine.dispatcher else []
    return {
        "universe": engine.universe.name if engine.universe else None,
        "fixtures": len(engine.universe.fixtures) if engine.universe else 0,
        "pixels": engine.universe.total_pixels if engine.universe else 0,
        "controllers": {n.node_id: f"{n.ip}:{n.port}" for n in nodes},
        "unconfigured_controllers": list(engine.unconfigured_controllers),
    }
