"""REST Daemon Adapter providing an HTTP JSON API for headless LightingEngine control."""

from __future__ import annotations
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from wled_app.domain.color import Color
from wled_app.domain.palette import PALETTES
from wled_app.patterns.base import PatternConfig
from wled_app.patterns.registry import _REGISTERED_PATTERNS, get_pattern_by_id
from wled_engine.blend import BlendMode
from wled_engine.core import LightingEngine
from wled_engine.patch.patch_table import PatchTable
from wled_engine.spatial.coordinates import Point3D


def handle_rest_request(
    engine: LightingEngine,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | list[Any]]:
    """Pure request router and handler for the LightingEngine REST API.

    Decoupled from network socket I/O to enable hermetic testing and embedding.
    """
    clean_path = urlparse(path).path.rstrip("/")
    method = method.upper()
    body = body or {}

    if method == "GET":
        if clean_path == "/api/status" or clean_path == "" or clean_path == "/api":
            devices_info = [
                {
                    "id": node.device.id,
                    "name": node.device.name,
                    "ip": node.device.ip,
                    "port": node.device.port,
                    "protocol": node.device.protocol.value,
                    "channels": len(node.device.active_channels),
                    "total_leds": node.device.total_leds,
                }
                for node in engine.fleet._nodes.values()
            ]
            return 200, {
                "status": "ok",
                "running": engine.is_running,
                "target_fps": engine.clock.target_fps,
                "actual_fps": round(engine.actual_fps, 2),
                "tick": engine.current_tick,
                "master_brightness": round(engine.master_brightness, 3),
                "devices": devices_info,
                "cues": list(engine._cues.keys()),
            }

        if clean_path == "/api/layers":
            layers_data = []
            for i in range(10):
                l = engine.layer(i)
                layers_data.append(
                    {
                        "index": l.index,
                        "name": l.name,
                        "pattern": l.pattern.id if l.pattern else None,
                        "opacity": round(l.opacity, 3),
                        "blend_mode": l.blend_mode.name,
                        "channel_ids": sorted(list(l.channel_ids)) if l.channel_ids else None,
                        "enabled": l.enabled,
                    }
                )
            return 200, layers_data

        if clean_path.startswith("/api/layers/"):
            parts = clean_path.split("/")
            if len(parts) == 4 and parts[3].isdigit():
                idx = int(parts[3])
                if not 0 <= idx <= 9:
                    return 400, {"status": "error", "message": f"Layer index {idx} out of range (0..9)"}
                l = engine.layer(idx)
                return 200, {
                    "status": "ok",
                    "index": l.index,
                    "name": l.name,
                    "pattern": l.pattern.id if l.pattern else None,
                    "opacity": round(l.opacity, 3),
                    "blend_mode": l.blend_mode.name,
                    "channel_ids": sorted(list(l.channel_ids)) if l.channel_ids else None,
                    "enabled": l.enabled,
                }

        if clean_path == "/api/capabilities":
            return 200, {
                "status": "ok",
                "patterns": list(_REGISTERED_PATTERNS.keys()),
                "palettes": list(PALETTES.keys()),
                "blend_modes": [m.name for m in BlendMode],
                "max_layers": 10,
            }

        if clean_path == "/api/sequence/status":
            return 200, {
                "status": "ok",
                **engine.sequence.get_status(),
            }

        if clean_path == "/api/universe":
            if not engine.universe:
                return 200, {"status": "ok", "universe": None}
            return 200, {
                "status": "ok",
                "universe": engine.universe.to_visualizer_dict(),
            }

        if clean_path == "/api/universe/fixtures":
            if not engine.universe:
                return 200, {"status": "ok", "fixtures": []}
            return 200, {
                "status": "ok",
                "fixtures": [f.to_dict() for f in engine.universe.fixtures],
            }

        if clean_path == "/api/universe/presets":
            from wled_engine.spatial.venue import list_preset_names
            return 200, {
                "status": "ok",
                "presets": list_preset_names(),
            }

        if clean_path.startswith("/api/universe/fixtures/"):
            parts = clean_path.split("/")
            if len(parts) == 5:
                fid = parts[4]
                if not engine.universe:
                    return 404, {"status": "error", "message": "No active universe"}
                f = engine.universe.get_fixture(fid)
                if not f:
                    return 404, {"status": "error", "message": f"Fixture '{fid}' not found"}
                return 200, {"status": "ok", "fixture": f.to_dict()}

        if clean_path == "/api/patch":
            if not engine.patch_table:
                return 200, {"status": "ok", "patch": None, "controllers": [], "validation": []}
            return 200, {
                "status": "ok",
                "patch": engine.patch_table.to_dict(),
                "controllers": engine.patch_table.get_controllers(),
                "validation": engine.patch_table.validate(),
            }

        if clean_path == "/api/config":
            from wled_engine.daemon_config import summarize
            return 200, {
                "status": "ok",
                "reloadable": engine.daemon_config is not None,
                **summarize(engine),
                "identifying": [f"{ip}:{port}" for ip, port in engine.identify.active],
            }

        if clean_path == "/api/fleet/nodes":
            if not engine.dispatcher:
                return 200, {"status": "ok", "nodes": {}}
            return 200, {
                "status": "ok",
                "nodes": engine.dispatcher.get_telemetry(),
            }

        return 404, {"status": "error", "message": f"Endpoint not found: {clean_path}"}

    if method in ("POST", "PUT"):
        if clean_path == "/api/start":
            engine.start()
            return 200, {"status": "ok", "running": True}

        if clean_path == "/api/stop":
            engine.stop()
            return 200, {"status": "ok", "running": False}

        if clean_path == "/api/blackout":
            engine.blackout()
            return 200, {"status": "ok", "blackout": True}

        if clean_path == "/api/sync":
            if engine.dispatcher:
                engine.dispatcher.emit_broadcast_sync()
            return 200, {"status": "ok", "sync_emitted": True}

        if clean_path == "/api/sequence/play":
            seq = body.get("sequence", body) if body else None
            dilation = body.get("time_dilation") if body else None
            if not engine.is_running:
                engine.start()
            engine.sequence.play(
                data=seq if isinstance(seq, dict) and "steps" in seq else None,
                time_dilation=dilation,
            )
            return 200, {"status": "ok", "playing": True, "status_info": engine.sequence.get_status()}

        if clean_path == "/api/sequence/pause":
            engine.sequence.pause()
            return 200, {"status": "ok", "paused": True}

        if clean_path == "/api/sequence/stop":
            engine.sequence.stop()
            engine.blackout()
            return 200, {"status": "ok", "stopped": True}

        if clean_path == "/api/sequence/seek":
            time_val = float(body.get("time", body.get("time_sec", 0.0)))
            engine.sequence.seek(time_val)
            return 200, {"status": "ok", "seek": time_val, "status_info": engine.sequence.get_status()}

        if clean_path == "/api/master_brightness":
            val = float(body.get("brightness", 1.0))
            engine.master_brightness = val
            return 200, {"status": "ok", "master_brightness": engine.master_brightness}

        if clean_path == "/api/cues":
            name = str(body.get("name", "")).strip()
            if not name:
                return 400, {"status": "error", "message": "Missing required 'name' field for cue"}
            engine.save_cue(name)
            return 200, {"status": "ok", "cue_saved": name}

        if clean_path.startswith("/api/cues/") and clean_path.endswith("/transition"):
            parts = clean_path.split("/")
            cue_name = parts[3]
            duration = float(body.get("duration_sec", 1.0))
            success = engine.transition_to_cue(cue_name, duration_sec=duration)
            if success:
                return 200, {"status": "ok", "cue": cue_name, "transitioning": True}
            else:
                return 404, {"status": "error", "message": f"Cue not found: {cue_name}"}

        if clean_path.startswith("/api/layers/") and clean_path.endswith("/fade"):
            parts = clean_path.split("/")
            if parts[3].isdigit():
                idx = int(parts[3])
                if not 0 <= idx <= 9:
                    return 400, {"status": "error", "message": f"Layer index {idx} out of bounds (0..9)"}
                target_opacity = float(body.get("target_opacity", 1.0))
                duration = float(body.get("duration_sec", 1.0))
                engine.fade_layer(idx, target_opacity, duration)
                return 200, {"status": "ok", "layer": idx, "fading": True}

        if clean_path.startswith("/api/layers/"):
            parts = clean_path.split("/")
            if len(parts) == 4 and parts[3].isdigit():
                idx = int(parts[3])
                if not 0 <= idx <= 9:
                    return 400, {"status": "error", "message": f"Layer index {idx} out of bounds (0..9)"}

                layer = engine.layer(idx)

                if "pattern" in body:
                    pid = body["pattern"]
                    if pid is None or pid == "":
                        layer.pattern = None
                        layer.enabled = False
                    else:
                        pattern_obj = get_pattern_by_id(pid)
                        if pattern_obj is None:
                            return 400, {"status": "error", "message": f"Unknown pattern id: {pid}"}
                        layer.pattern = pattern_obj
                        layer.enabled = True

                pcfg = layer.pattern_config
                speed = float(body.get("speed", pcfg.speed))
                brightness = float(body.get("brightness", pcfg.brightness))
                color = Color.from_hex(body["color"]) if "color" in body and body["color"] else pcfg.primary_color
                layer.pattern_config = PatternConfig(speed=speed, brightness=brightness, primary_color=color)

                if "opacity" in body:
                    layer.opacity = float(body["opacity"])
                if "enabled" in body:
                    layer.enabled = bool(body["enabled"])
                if "blend_mode" in body:
                    try:
                        layer.blend_mode = BlendMode[body["blend_mode"].upper()]
                    except KeyError:
                        return 400, {"status": "error", "message": f"Invalid blend mode: {body['blend_mode']}"}
                if "channels" in body:
                    ch_val = body["channels"]
                    layer.channel_ids = set(ch_val) if ch_val is not None else None

                return 200, {
                    "status": "ok",
                    "layer": {
                        "index": layer.index,
                        "pattern": layer.pattern.id if layer.pattern else None,
                        "opacity": round(layer.opacity, 3),
                        "blend_mode": layer.blend_mode.name,
                        "channel_ids": sorted(list(layer.channel_ids)) if layer.channel_ids else None,
                        "enabled": layer.enabled,
                    },
                }

        if clean_path == "/api/events":
            name = str(body.get("name", "")).strip()
            if not name:
                return 400, {"status": "error", "message": "Missing required 'name' for event"}
            data = body.get("data", {})
            actions = engine.events.publish(name, **data)
            return 200, {"status": "ok", "event": name, "executed_actions": actions}

        if clean_path == "/api/universe":
            if "preset" in body and body["preset"]:
                from wled_engine.spatial.venue import get_preset_venue
                preset_name = str(body["preset"])
                try:
                    u, p = get_preset_venue(preset_name)
                    engine.setup_universe(u, p, dry_run=engine.dry_run)
                    return 200, {
                        "status": "ok",
                        "preset": preset_name,
                        "universe": u.to_dict(),
                        "patch": p.to_dict(),
                    }
                except KeyError as e:
                    return 400, {"status": "error", "message": str(e)}
            elif "universe" in body and isinstance(body["universe"], dict):
                from wled_engine.spatial.universe import SpatialUniverse
                u = SpatialUniverse.from_dict(body["universe"])
                p = engine.patch_table or PatchTable("Universe Patch")
                engine.setup_universe(u, p, dry_run=engine.dry_run)
                return 200, {"status": "ok", "universe": u.to_dict()}
            elif "path" in body:
                from wled_engine.spatial.universe import SpatialUniverse
                u = SpatialUniverse.load_json(body["path"])
                p = engine.patch_table or PatchTable("Universe Patch")
                engine.setup_universe(u, p, dry_run=engine.dry_run)
                return 200, {"status": "ok", "universe": u.to_dict()}
            return 400, {"status": "error", "message": "Expected 'preset', 'path', or 'universe' dict"}

        if clean_path == "/api/patch":
            if "patch" in body and isinstance(body["patch"], dict):
                from wled_engine.patch.patch_table import PatchTable
                p = PatchTable.from_dict(body["patch"])
                if engine.universe:
                    engine.setup_universe(engine.universe, p, dry_run=engine.dry_run)
                else:
                    engine.patch_table = p
                return 200, {"status": "ok", "patch": p.to_dict(), "validation": p.validate()}
            elif "path" in body:
                from wled_engine.patch.patch_table import PatchTable
                p = PatchTable.load_json(body["path"])
                if engine.universe:
                    engine.setup_universe(engine.universe, p, dry_run=engine.dry_run)
                else:
                    engine.patch_table = p
                return 200, {"status": "ok", "patch": p.to_dict(), "validation": p.validate()}
            return 400, {"status": "error", "message": "Expected 'path' or 'patch' dict"}

        if clean_path == "/api/config/reload":
            config = engine.daemon_config
            if config is None:
                return 409, {"status": "error", "message": "Daemon was not started from config files; nothing to reload"}
            try:
                loaded = config.load()
            except (OSError, ValueError, KeyError) as e:
                # Keep running the previous configuration.
                return 400, {"status": "error", "message": f"Config not reloaded: {e}"}
            return 200, {"status": "ok", **config.apply(engine, loaded)}

        if clean_path == "/api/identify":
            ip = str(body.get("ip") or "").strip()
            port = body.get("port")
            led_count = body.get("led_count")
            cid = body.get("controller_id")
            if cid:
                node = engine.dispatcher.get_node(str(cid)) if engine.dispatcher else None
                if node is None:
                    return 404, {"status": "error", "message": f"Controller '{cid}' has no configured IP"}
                ip, port = node.ip, node.port
                if not led_count and engine.patch_table:
                    led_count = sum(
                        engine.patch_table.get_channel_pixel_length(node.node_id, ch)
                        for ch in range(max(engine.patch_table.get_channel_indices(node.node_id), default=-1) + 1)
                    )
            if not ip:
                return 400, {"status": "error", "message": "Expected 'ip' or 'controller_id'"}
            try:
                port = int(port or 4048)
                led_count = int(led_count) if led_count else None
                led_start = int(body.get("led_start") or 0)
                seconds = float(body["seconds"]) if body.get("seconds") else None
            except (TypeError, ValueError) as e:
                return 400, {"status": "error", "message": f"Invalid identify request: {e}"}
            if body.get("stop"):
                engine.identify.cancel(ip, port)
                return 200, {"status": "ok", "ip": ip, "port": port, "stopped": True}
            engine.start_identify(ip, port, led_count=led_count, seconds=seconds, led_start=led_start)
            return 200, {"status": "ok", "ip": ip, "port": port, "led_count": led_count, "running": engine.is_running}

        if clean_path == "/api/spatial/pattern":
            pat_name = str(body.get("pattern", "sweep")).lower()
            speed = float(body.get("speed", 1.0))
            freq = float(body.get("frequency", 2.0))
            time_val = float(body.get("time_sec", time.time()))

            from wled_engine.spatial.patterns import (
                spatial_angle_sweep,
                spatial_radial_pulse,
                spatial_linear_gradient,
                spatial_rainbow_cloud,
            )

            if pat_name == "sweep":
                angle = float(body.get("angle", 45.0))
                col_a = Color.from_hex(body.get("color_a", "#ff007f"))
                col_b = Color.from_hex(body.get("color_b", "#00c8ff"))
                func = spatial_angle_sweep(angle_deg=angle, speed=speed, time_sec=time_val, frequency=freq, color_a=col_a, color_b=col_b)
            elif pat_name == "pulse":
                cx = float(body.get("center_x", 0.5))
                cy = float(body.get("center_y", 0.5))
                col_c = Color.from_hex(body.get("color_center", "#ffdc32"))
                col_e = Color.from_hex(body.get("color_edge", "#0a1450"))
                func = spatial_radial_pulse(center=Point3D(cx, cy, 0.0), speed=speed, time_sec=time_val, frequency=freq, color_center=col_c, color_edge=col_e)
            elif pat_name == "rainbow":
                func = spatial_rainbow_cloud(time_sec=time_val, speed=speed, scale=freq)
            elif pat_name == "gradient":
                col_s = Color.from_hex(body.get("color_start", "#ff3c00"))
                col_e = Color.from_hex(body.get("color_end", "#7800ff"))
                func = spatial_linear_gradient(color_start=col_s, color_end=col_e)
            elif pat_name in ("off", "none", "clear"):
                func = None
            else:
                return 400, {"status": "error", "message": f"Unknown spatial pattern: {pat_name}"}

            engine.set_spatial_pattern(func)
            return 200, {"status": "ok", "spatial_pattern": pat_name}

        return 404, {"status": "error", "message": f"Endpoint not found: {clean_path}"}

    return 405, {"status": "error", "message": f"Method {method} not allowed"}


class RestDaemonHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler routing JSON REST requests to LightingEngine."""

    server: RestDaemonServer

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stderr HTTP request logging."""
        pass

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status_code: int, data: dict[str, Any] | list[Any]) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        status, data = handle_rest_request(self.server.engine, "GET", self.path)
        self._send_json(status, data)

    def do_POST(self) -> None:
        try:
            body = self._read_json_body()
        except Exception as e:
            self._send_json(400, {"status": "error", "message": f"Invalid JSON payload: {e}"})
            return
        status, data = handle_rest_request(self.server.engine, "POST", self.path, body)
        self._send_json(status, data)

    do_PUT = do_POST


class RestDaemonServer(ThreadingHTTPServer):
    """Threading HTTP Server holding reference to target LightingEngine."""

    def __init__(self, server_address: tuple[str, int], engine: LightingEngine) -> None:
        self.engine = engine
        super().__init__(server_address, RestDaemonHandler)


class EngineDaemon:
    """Manages lifecycle and thread execution of the HTTP REST Daemon."""

    def __init__(self, engine: LightingEngine, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.engine = engine
        self.host = host
        self.port = port
        self.server: RestDaemonServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self.server is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start daemon HTTP server in a background thread."""
        if self.is_running:
            return
        self.server = RestDaemonServer((self.host, self.port), self.engine)
        self.port = self.server.server_port
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="WledRestDaemon")
        self._thread.start()

    def stop(self) -> None:
        """Shutdown and close daemon server."""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
