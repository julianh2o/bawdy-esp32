"""Web UI server: the pattern editor (/) and setup page (/config) as static files, plus /engine/* proxied to lightd."""

from __future__ import annotations
import json
import mimetypes
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


class SequencerHandler(BaseHTTPRequestHandler):
    """Serves the static UI and proxies /engine/* to lightd."""

    server: SequencerServer

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stderr HTTP request logging for quiet operation."""
        pass

    def _send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status: int, data: Any) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._send_cors()
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, status: int, msg: str) -> None:
        self._send_json(status, {"status": "error", "message": msg})

    def _proxy_to_engine(self) -> None:
        """Forward /engine/<path> to the configured daemon so browsers only talk to this server."""
        parsed = urlparse(self.path)
        target = self.server.engine_url.rstrip("/") + parsed.path[len("/engine"):]
        if parsed.query:
            target += "?" + parsed.query
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(target, data=body, method=self.command)
        if body is not None:
            req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                status, payload, ctype = resp.status, resp.read(), resp.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            status, payload, ctype = e.code, e.read(), e.headers.get("Content-Type")
        except Exception as e:
            self._send_error(502, f"Engine unreachable at {self.server.engine_url}: {e}")
            return
        self.send_response(status)
        self.send_header("Content-Type", ctype or "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _try_proxy(self) -> bool:
        if urlparse(self.path).path.startswith("/engine/"):
            self._proxy_to_engine()
            return True
        return False

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_PUT(self) -> None:
        if not self._try_proxy():
            self._send_error(404, f"Unknown endpoint: {urlparse(self.path).path}")

    def do_GET(self) -> None:
        if self._try_proxy():
            return
        self._serve_static(unquote(urlparse(self.path).path.rstrip("/")))

    def do_POST(self) -> None:
        if not self._try_proxy():
            self._send_error(404, f"Unknown endpoint: {urlparse(self.path).path}")

    def do_PATCH(self) -> None:
        if not self._try_proxy():
            self._send_error(404, f"Unknown endpoint: {urlparse(self.path).path}")

    def do_DELETE(self) -> None:
        if not self._try_proxy():
            self._send_error(404, f"Unknown endpoint: {urlparse(self.path).path}")

    def _serve_static(self, path: str) -> None:
        static_dir = self.server.static_dir
        if path in ("", "/", "/patterns"):
            target = static_dir / "patterns.html"  # the pattern editor, with runs and scenes
        elif path == "/config":
            target = static_dir / "config.html"  # controller setup
        else:
            rel = path.lstrip("/")
            target = static_dir / rel

        # Security check against directory traversal
        try:
            target = target.resolve()
            static_dir = static_dir.resolve()
            if not str(target).startswith(str(static_dir)):
                self._send_error(403, "Access denied")
                return
        except Exception:
            self._send_error(404, "File not found")
            return

        if not target.is_file():
            self._send_error(404, "File not found")
            return

        mime_type, _ = mimetypes.guess_type(str(target))
        mime_type = mime_type or "application/octet-stream"

        with open(target, "rb") as f:
            content = f.read()

        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(content)))
        self._send_cors()
        self.end_headers()
        self.wfile.write(content)


class SequencerServer(ThreadingHTTPServer):
    """Threading HTTP server for the static UI and the lightd proxy."""

    def __init__(self, server_address: tuple[str, int], static_dir: Path, engine_url: str = "http://127.0.0.1:8765") -> None:
        self.static_dir = static_dir
        self.engine_url = engine_url
        super().__init__(server_address, SequencerHandler)


class SequencerAppServer:
    """Starts and stops the web UI server in a background thread."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8081,
        static_dir: Path | str | None = None,
        engine_url: str = "http://127.0.0.1:8765",
    ) -> None:
        self.host = host
        self.port = port
        self.engine_url = engine_url
        self.static_dir = Path(static_dir) if static_dir else Path(__file__).resolve().parent / "static"

        self.server: SequencerServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self.server is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self.server = SequencerServer((self.host, self.port), self.static_dir, self.engine_url)
        self.port = self.server.server_port
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="WledSequencerServer")
        self._thread.start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None


def main() -> None:
    import argparse
    import time
    from rich.console import Console

    parser = argparse.ArgumentParser(description="Lights web UI server")
    parser.add_argument("--port", type=int, default=8081, help="Web port (default: 8081)")
    parser.add_argument("--host", default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    args = parser.parse_args()

    console = Console()
    app = SequencerAppServer(host=args.host, port=args.port)
    app.start()
    console.print(f"[bold green]⚡ Lights web UI running on http://{args.host}:{app.port}[/bold green]")
    console.print("Press [bold yellow]Ctrl+C[/bold yellow] to stop.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()


if __name__ == "__main__":
    main()

