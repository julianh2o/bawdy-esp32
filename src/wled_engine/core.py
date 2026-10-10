"""Core Lighting Engine: Headless platform coordinating layers, fleet, and pacing."""

from __future__ import annotations
import threading
import time
from typing import Callable
from dataclasses import dataclass

from wled_app.domain.color import Color
from wled_app.domain.device import DeviceConfig
from wled_app.domain.frame import FrameBuffer
from wled_app.network.udp_client import UdpSender
from wled_app.protocols.base import ProtocolEmitter
from wled_app.patterns.base import Pattern, PatternConfig
from wled_engine.blend import BlendMode
from wled_engine.clock import FrameClock
from wled_engine.compositor import LayerStack
from wled_engine.events import EventBus
from wled_engine.fleet import DeviceFleet, DeviceNode
from wled_engine.fleet.dispatcher import MultiNodeDispatcher
from wled_engine.layer import Layer
from wled_engine.sequence_runner import SequenceRunner
from wled_engine.patch.patch_table import PatchTable
from wled_engine.spatial.coordinates import Point3D
from wled_engine.spatial.sampler import SpatialSampler
from wled_engine.spatial.universe import SpatialUniverse


@dataclass
class LayerSnapshot:
    """Snapshot of an individual layer's configuration for Cues."""
    index: int
    name: str
    pattern_id: str | None
    opacity: float
    blend_mode: BlendMode
    channel_ids: set[int] | None
    enabled: bool


class LightingEngine:
    """Headless Real-Time Lighting Compositing Engine.

    Coordinates an ordered 10-layer compositor (Layers 0..9), a multi-device
    hardware fleet, drift-free frame clocking, event reactivity, and background execution.
    """

    def __init__(
        self,
        target_fps: float = 30.0,
        dry_run: bool = False,
        on_frame: Callable[[DeviceConfig, FrameBuffer, float, int], None] | None = None,
        on_universe_frame: Callable[[dict[str, list[Color]], float, int], None] | None = None,
    ) -> None:
        self.dry_run = dry_run
        self.on_frame = on_frame
        self.on_universe_frame = on_universe_frame

        self.clock = FrameClock(target_fps=target_fps)
        self.layers = LayerStack()
        self.fleet = DeviceFleet(dry_run=dry_run)
        self.events = EventBus(engine=self)
        self.sequence = SequenceRunner(engine=self)

        # Spatial Universe & Multi-Node Dispatcher
        self.universe: SpatialUniverse | None = None
        self.patch_table: PatchTable | None = None
        self.dispatcher: MultiNodeDispatcher | None = None
        self.spatial_sampler: SpatialSampler | None = None
        self.spatial_pattern_func: Callable[[Point3D], Color] | None = None

        self._tick: int = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cues: dict[str, list[LayerSnapshot]] = {}

    @property
    def is_running(self) -> bool:
        return not self._stop_event.is_set() and (
            self._thread is not None and self._thread.is_alive()
        )

    @property
    def current_tick(self) -> int:
        return self._tick

    @property
    def actual_fps(self) -> float:
        return self.clock.actual_fps

    @property
    def master_brightness(self) -> float:
        return self.layers.master_brightness

    @master_brightness.setter
    def master_brightness(self, value: float) -> None:
        self.layers.master_brightness = value

    def layer(self, index: int) -> Layer:
        """Access indexed layer (0..9)."""
        return self.layers.get_layer(index)

    def set_layer_pattern(
        self,
        index: int,
        pattern: Pattern | None,
        pattern_config: PatternConfig | None = None,
        opacity: float = 1.0,
        blend_mode: BlendMode = BlendMode.OVERWRITE,
        channel_mask: set[int] | None = None,
    ) -> Layer:
        """Configure pattern, blend mode, and channel mask for layer (0..9)."""
        l = self.layers.get_layer(index)
        l.pattern = pattern
        if pattern_config is not None:
            l.pattern_config = pattern_config
        l.opacity = opacity
        l.blend_mode = blend_mode
        l.channel_ids = channel_mask
        l.enabled = True
        return l

    def fade_layer(self, index: int, target_opacity: float, duration_sec: float) -> None:
        """Smoothly fade a layer's opacity over a duration."""
        l = self.layers.get_layer(index)
        l.fade_to(target_opacity, duration_sec)

    def add_device(
        self,
        device: DeviceConfig,
        emitter: ProtocolEmitter | None = None,
        sender: UdpSender | None = None,
    ) -> DeviceNode:
        """Register a physical or simulated controller target."""
        return self.fleet.add_device(device, emitter=emitter, sender=sender)

    def remove_device(self, device_id: str) -> DeviceNode | None:
        """Remove a device target by ID."""
        return self.fleet.remove_device(device_id)

    def setup_universe(
        self,
        universe: SpatialUniverse,
        patch_table: PatchTable,
        broadcast_ip: str = "255.255.255.255",
        sync_port: int = 4048,
        controller_ips: dict[str, str] | None = None,
        controller_ports: dict[str, int] | None = None,
        dry_run: bool | None = None,
        simulator: bool | None = None,
    ) -> None:
        """Register spatial universe, patch table, and configure Cat6 multi-node dispatcher.

        Controllers listed in ``controller_ips`` are addressed at that IP on
        ``controller_ports`` (default 4048, the real WLED DDP port). In simulator
        mode, unlisted controllers go to 127.0.0.1 on ports 4048+idx. ``simulator``
        defaults to True when no ``controller_ips`` are given (legacy behaviour);
        otherwise a controller without an IP raises ``ValueError``.
        """
        self.universe = universe
        self.patch_table = patch_table
        self.spatial_sampler = SpatialSampler(universe, patch_table)

        use_dry = self.dry_run if dry_run is None else dry_run
        self.dispatcher = MultiNodeDispatcher(
            broadcast_ip=broadcast_ip,
            sync_port=sync_port,
            dry_run=use_dry,
        )

        ips = controller_ips or {}
        ports = controller_ports or {}
        if simulator is None:
            simulator = not ips
        controllers = patch_table.get_controllers()
        missing = [cid for cid in controllers if cid not in ips]
        if missing and not simulator:
            raise ValueError(f"No IP configured for controller(s): {', '.join(missing)}")
        for idx, cid in enumerate(controllers):
            if cid in ips:
                ip, port = ips[cid], ports.get(cid, 4048)
            else:
                ip, port = "127.0.0.1", ports.get(cid, 4048 + idx)
            self.dispatcher.add_node(node_id=cid, ip=ip, port=port)

    # Backward compatibility alias
    configure_universe = setup_universe

    def set_spatial_pattern(self, func: Callable[[Point3D], Any] | None) -> None:
        """Set continuous spatial 3D color field evaluator for universe sampling."""
        self.spatial_pattern_func = func

    def step(
        self,
        custom_dt: float | None = None,
        tick: int | None = None,
    ) -> dict[str, FrameBuffer]:
        """Execute a single compositing and transmission frame across all devices and universe fixtures."""
        if tick is not None:
            self._tick = tick
        dt = self.clock.tick() if custom_dt is None else custom_dt
        self.sequence.update(dt)
        current_tick = self._tick

        frames: dict[str, FrameBuffer] = {}

        # 1. Multi-node Spatial Universe sampling & Cat6 broadcast sync
        if (
            self.universe is not None
            and self.spatial_sampler is not None
            and self.dispatcher is not None
            and self.spatial_pattern_func is not None
        ):
            fixture_colors = self.spatial_sampler.sample_field_to_fixtures(self.spatial_pattern_func)
            controller_buffers = self.spatial_sampler.route_fixture_colors_to_controllers(
                fixture_colors,
                master_brightness=self.master_brightness,
            )
            self.dispatcher.dispatch_channel_buffers(controller_buffers, sync=True)
            if self.on_universe_frame:
                self.on_universe_frame(fixture_colors, self.clock.actual_fps, current_tick)

        # 2. Legacy / Individual Device Fleet composition
        for node in self.fleet._nodes.values():
            self.layers.composite(
                tick=current_tick,
                dt=dt,
                device=node.device,
                out_frame=node.frame,
            )
            self.fleet.transmit_node(node)
            frames[node.device.id] = node.frame

            if self.on_frame:
                self.on_frame(node.device, node.frame, self.clock.actual_fps, current_tick)

        if tick is None:
            self._tick += 1

        return frames

    def run(self, max_frames: int | None = None, close_on_exit: bool = True) -> None:
        """Run engine synchronously until stopped or max_frames reached.

        ``start()`` passes ``close_on_exit=False`` so ``stop()`` can still send
        a blackout frame over the open sockets before closing them.
        """
        self._stop_event.clear()
        frames_run = 0

        try:
            while not self._stop_event.is_set():
                if max_frames is not None and frames_run >= max_frames:
                    break

                start_time = time.perf_counter()
                self.step()
                frames_run += 1
                self.clock.sleep_until_next_frame(start_time)
        finally:
            if close_on_exit:
                self.close()

    def start(self) -> None:
        """Start engine loop asynchronously in background daemon thread."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.run, kwargs={"close_on_exit": False}, daemon=True, name="WledEngineClock"
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal engine to stop and wait for background thread completion."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        self.blackout()
        self.close()

    def save_cue(self, name: str) -> None:
        """Capture current Layer 0..9 state as a named cue."""
        snapshots = [
            LayerSnapshot(
                index=i,
                name=l.name,
                pattern_id=l.pattern.id if l.pattern else None,
                opacity=l.opacity,
                blend_mode=l.blend_mode,
                channel_ids=set(l.channel_ids) if l.channel_ids else None,
                enabled=l.enabled,
            )
            for i, l in enumerate(self.layers._layers)
        ]
        self._cues[name] = snapshots

    def transition_to_cue(self, name: str, duration_sec: float = 1.0) -> bool:
        """Transition layer opacities smoothly to a saved cue."""
        snapshots = self._cues.get(name)
        if not snapshots:
            return False

        for snap in snapshots:
            layer = self.layers.get_layer(snap.index)
            layer.fade_to(snap.opacity, duration_sec=duration_sec)
        return True

    def blackout(self) -> None:
        """Atomically clear all layers 0..9, clear spatial patterns, stop sequence, and flush black frames to all hardware."""
        self.sequence.stop()
        self.spatial_pattern_func = None
        for layer in self.layers._layers:
            layer.reset()

        # 1. Flush black frame to MultiNodeDispatcher (Cat6 20-node universe)
        if (
            self.universe is not None
            and self.spatial_sampler is not None
            and self.dispatcher is not None
        ):
            black_fixture_colors = {
                f.id: [Color.black() for _ in range(f.pixel_count)]
                for f in self.universe.fixtures
            }
            controller_buffers = self.spatial_sampler.route_fixture_colors_to_controllers(
                black_fixture_colors,
                master_brightness=0.0,
            )
            self.dispatcher.dispatch_channel_buffers(controller_buffers, sync=True)
            if self.on_universe_frame:
                self.on_universe_frame(black_fixture_colors, self.clock.actual_fps, self._tick)

        # 2. Flush black frame to individual device Fleet nodes
        for node in self.fleet._nodes.values():
            node.frame.clear()
            self.fleet.transmit_node(node)
            if self.on_frame:
                self.on_frame(node.device, node.frame, self.clock.actual_fps, self._tick)

    def close(self) -> None:
        """Release fleet and multi-node dispatcher network resources."""
        self.fleet.close()
        if self.dispatcher is not None:
            self.dispatcher.close()
