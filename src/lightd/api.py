"""lightd REST API (JSON over HTTP). Trusted-LAN use: no auth."""

from __future__ import annotations
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from wled import WledController, WledError

from lightd.config import ConfigError, ControllerStore
from lightd.lua import RUNTIME_PATH, PatternError, Pattern
from lightd.patterns import LibraryError, PatternLibrary
from lightd.registry import Registry
from lightd.runs import RunManager
from lightd.scenes import SceneError, SceneStore, is_active
from lightd.targets import Target, TargetError

log = logging.getLogger(__name__)

MAX_BODY = 256 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.extra = extra


class Lightd:
    """Everything the API needs, wired together."""

    def __init__(
        self, store: ControllerStore, registry: Registry, library: PatternLibrary, runs: RunManager, loop: Any,
        scenes: SceneStore | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.library = library
        self.runs = runs
        self.loop = loop
        self.scenes = scenes if scenes is not None else SceneStore(None, library)

    @property
    def browser(self) -> Any:
        return self.registry.browser


def _route(app: Lightd, method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    parts = [unquote(p) for p in path.strip("/").split("/")]
    if parts[:1] != ["api"]:
        raise ApiError(404, f"Not found: {path}")
    parts = parts[1:]
    head = parts[0] if parts else ""

    if head == "status" and method == "GET":
        entries = app.registry.entries()
        runs = app.runs.runs()
        return 200, {
            "status": "ok",
            "fps": app.loop.fps,
            "actual_fps": round(app.loop.actual_fps, 1),
            "last_frame_ms": round(app.loop.last_frame_ms, 2),
            "controllers": {"total": len(entries), "online": sum(1 for e in entries if e.online)},
            "runs": len(runs),
            "output": app.runs.output,
            "errors": [{"run": r.id, **r.error} for r in runs if r.error],
        }

    if head == "runtime.lua" and method == "GET":
        return 200, RUNTIME_PATH.read_text(encoding="utf-8")

    if head == "controllers" and len(parts) == 1 and method == "GET":
        return 200, {"controllers": _controllers(app)}

    if head == "controllers" and parts[1:] == ["refresh"] and method == "POST":
        # Re-read every board's info, outputs and segments now instead of waiting for the next periodic refresh.
        app.registry.refresh_all()
        return 200, {"status": "ok", "controllers": _controllers(app)}

    if head == "output" and len(parts) == 1:
        # Master switch: off = send nothing, so the boards show their own WLED effects.
        if method in ("PUT", "POST"):
            app.runs.set_output(body.get("enabled"))
        if method in ("GET", "PUT", "POST"):
            return 200, {"enabled": app.runs.output}

    if head == "brightness" and len(parts) == 1:
        # Per-channel output level, 0..1. Channels not listed are at 1.
        if method == "GET":
            return 200, {"brightness": app.runs.brightness_levels()}
        if method in ("PUT", "POST"):
            app.runs.set_brightness(body.get("targets"), body.get("brightness"))
            return 200, {"status": "ok", "brightness": app.runs.brightness_levels()}

    if head == "config":
        # controllers.json itself: read, replace (validated, saved, applied at once), or re-read from disk.
        if len(parts) == 1 and method == "GET":
            return 200, _config_state(app)
        if len(parts) == 1 and method == "PUT":
            if "controllers" not in body:
                raise ApiError(400, "Expected {'controllers': {...}}")
            data = app.store.save(body["controllers"])
            app.registry.apply(data)
            return 200, {"status": "ok", **_config_state(app)}
        if parts[1:] == ["reload"] and method == "POST":
            app.registry.load()
            return 200, {"status": "ok", **_config_state(app)}

    if head == "discovered":
        browser = app.browser
        if len(parts) == 1 and method == "GET":
            if browser is None:
                return 200, {"enabled": False, "listening": False, "error": None, "boards": []}
            return 200, {"enabled": True, "listening": browser.listening, "error": browser.error, "boards": browser.boards()}
        if browser is None:
            raise ApiError(409, "Discovery is disabled")
        if parts[1:] == ["rescan"] and method == "POST":
            browser.rescan()
            return 200, {"status": "ok"}
        if parts[1:] == ["probe"] and method == "POST":
            try:
                return 200, browser.probe(str(body.get("ip") or "").strip())
            except ValueError as e:
                raise ApiError(400, str(e))

    if head == "identify" and method == "POST":
        return 200, _identify(app, body)

    if head == "patterns":
        if len(parts) == 1 and method == "GET":
            return 200, {"patterns": app.library.list()}
        if parts[1:] == ["check"] and method == "POST":
            p = Pattern(_source(body))
            return 200, {"status": "ok", "params": p.schema}
        if len(parts) == 2:
            name = parts[1]
            if method == "GET":
                return 200, app.library.get(name)
            if method == "PUT":
                return 200, app.library.save(name, _source(body))
            if method == "DELETE":
                app.library.delete(name)
                return 200, {"status": "ok"}

    if head == "runs":
        if len(parts) == 1:
            if method == "GET":
                return 200, {"runs": [r.to_dict() for r in app.runs.runs()]}
            if method == "POST":
                run = app.runs.start(body.get("targets"), pattern=body.get("pattern"), source=body.get("source"),
                                     params=body.get("params"))
                return 200, run.to_dict()
        if len(parts) == 2:
            try:
                if method == "GET":
                    return 200, app.runs.get(parts[1]).to_dict()
                if method == "PATCH":
                    return 200, app.runs.update(parts[1], body.get("params", {})).to_dict()
                if method == "DELETE":
                    app.runs.stop(parts[1])
                    return 200, {"status": "ok"}
            except KeyError:
                raise ApiError(404, f"No run '{parts[1]}'")

    if head == "scenes":
        if len(parts) == 1 and method == "GET":
            assignments = app.runs.assignments()
            return 200, {"scenes": [{**s, "active": is_active(s["name"], s["entries"], assignments)}
                                    for s in app.scenes.list()]}
        if len(parts) >= 2:
            name = parts[1]
            if len(parts) == 2:
                if method == "GET":
                    scene = app.scenes.get(name)
                    return 200, {**scene, "active": is_active(name, scene["entries"], app.runs.assignments())}
                if method == "PUT":
                    return 200, app.scenes.save(name, body.get("entries"))
                if method == "DELETE":
                    app.scenes.delete(name)
                    return 200, {"status": "ok"}
            if parts[2:] == ["activate"] and method == "POST":
                started, errors = app.scenes.activate(name, app.runs)
                if errors and not started:
                    raise ApiError(400, "; ".join(errors), errors=errors)
                return 200, {"status": "error" if errors else "ok", "runs": [r.to_dict() for r in started],
                             "errors": errors}

    if head == "blackout" and method == "POST":
        app.runs.stop_all()
        return 200, {"status": "ok"}

    raise ApiError(404, f"No endpoint {method} {path}")


def _controllers(app: Lightd) -> list[dict[str, Any]]:
    """Live state: board info, channels as the board reports them (plus brightness), online, current IP."""
    controllers = [e.to_dict() for e in app.registry.entries()]
    for c in controllers:
        for ch in c["channels"]:
            ch["brightness"] = app.runs.brightness(Target(c["id"], ch["index"]))
    return controllers


def _config_state(app: Lightd) -> dict[str, Any]:
    """controllers.json as stored, with where it lives and whether lightd can write it."""
    try:
        controllers, error = app.store.load(), None
    except ConfigError as e:
        controllers, error = {}, str(e)
    return {"path": str(app.store.path), "writable": app.store.writable, "error": error, "controllers": controllers}


def _source(body: dict[str, Any]) -> str:
    source = body.get("source")
    if not isinstance(source, str):
        raise ApiError(400, "Expected 'source': Lua code")
    return source


def _identify(app: Lightd, body: dict[str, Any]) -> dict[str, Any]:
    """Blink ``{target}``, ``{controller_id[, channel]}`` or ``{ip[, port, led_start, led_count | ranges]}``.

    ``ranges`` is ``[[start, count], ...]``, for channels that span more than one LED range (linked segments).
    """
    seconds = float(body.get("seconds") or 6.0)
    channel: int | None = None
    if body.get("target"):
        t = Target.parse(body["target"])
        entry, channel = app.registry.get(t.controller_id), t.channel
        if entry is None:
            raise ApiError(404, f"No controller '{t.controller_id}'")
        controller = entry.controller
    elif body.get("controller_id"):
        entry = app.registry.get(str(body["controller_id"]))
        if entry is None:
            raise ApiError(404, f"No controller '{body['controller_id']}'")
        controller = entry.controller
        channel = int(body["channel"]) - 1 if body.get("channel") else None
    elif body.get("ip"):
        port = int(body.get("port") or 4048)
        entry = app.registry.find_by_ip(str(body["ip"]), port)
        controller = entry.controller if entry else WledController(str(body["ip"]), port=port, transport=app.registry.transport)
    else:
        raise ApiError(400, "Expected 'target', 'controller_id' or 'ip'")

    if body.get("stop"):
        controller.stop_identify()
        return {"status": "ok", "stopped": True}
    ranges = body.get("ranges")
    if ranges is not None:
        try:
            ranges = [(int(start), int(count)) for start, count in ranges]
        except (TypeError, ValueError):
            raise ApiError(400, "Expected 'ranges': [[start, count], ...]")
    controller.identify(
        channel=channel, seconds=seconds,
        led_start=body.get("led_start"), led_count=body.get("led_count"), ranges=ranges or None,
    )
    return {"status": "ok", "ip": controller.host, "port": controller.port, "via": "lightd"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "lightd"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, payload: Any) -> None:
        if isinstance(payload, str):
            data, ctype = payload.encode("utf-8"), "text/plain; charset=utf-8"
        else:
            data, ctype = json.dumps(payload).encode("utf-8"), "application/json"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "Request body too large")
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            raise ApiError(400, "Body must be JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "Body must be a JSON object")
        return body

    def _handle(self, method: str) -> None:
        try:
            body = self._body() if method in ("POST", "PUT", "PATCH") else {}
            status, payload = _route(self.server.app, method, urlparse(self.path).path, body)
        except ApiError as e:
            status, payload = e.status, {"status": "error", "message": str(e), **e.extra}
        except PatternError as e:
            status, payload = 400, {"status": "error", **e.to_dict()}
        except (LibraryError, SceneError) as e:
            status, payload = e.status, {"status": "error", "message": str(e)}
        except (ConfigError, TargetError, WledError, ValueError, TypeError) as e:
            status, payload = 400, {"status": "error", "message": str(e)}
        except OSError as e:
            status, payload = 500, {"status": "error", "message": f"I/O error: {e}"}
        self._send(status, payload)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()


class ApiServer:
    def __init__(self, app: Lightd, host: str = "0.0.0.0", port: int = 8765) -> None:
        self.httpd = ThreadingHTTPServer((host, port), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = app  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="lightd-api")
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
