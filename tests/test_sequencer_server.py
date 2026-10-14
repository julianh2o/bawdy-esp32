"""Tests for the web UI server: static pages and the /engine proxy."""

import json
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from wled_sequencer.server import SequencerAppServer


class _Engine(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        payload = json.dumps({"path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class TestWebServer(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ThreadingHTTPServer(("127.0.0.1", 0), _Engine)
        Thread(target=self.engine.serve_forever, daemon=True).start()
        self.app = SequencerAppServer(host="127.0.0.1", port=0, engine_url=f"http://127.0.0.1:{self.engine.server_port}")
        self.app.start()

    def tearDown(self) -> None:
        self.app.stop()
        self.engine.shutdown()
        self.engine.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.app.port}{path}", timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_pattern_editor_at_root_and_setup_page(self) -> None:
        for path in ("/", "/patterns"):
            status, html = self.get(path)
            self.assertEqual(status, 200)
            self.assertIn("/js/patterns.js", html)
        status, html = self.get("/config")
        self.assertEqual(status, 200)
        self.assertIn("/js/config.js", html)
        self.assertEqual(self.get("/js/patterns.js")[0], 200)

    def test_no_traversal_or_unknown_files(self) -> None:
        self.assertEqual(self.get("/index.html")[0], 404)
        self.assertIn(self.get("/../server.py")[0], (403, 404))

    def test_engine_proxy(self) -> None:
        status, body = self.get("/engine/api/status?x=1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"path": "/api/status?x=1"})

    def test_clock(self) -> None:
        import time

        before = time.time()
        status, body = self.get("/clock")
        self.assertEqual(status, 200)
        self.assertRegex(body, r"^\d+\.\d{2}$")
        self.assertAlmostEqual(float(body), before, delta=2)
        self.assertRegex(self.get("/clock/")[1], r"^\d+\.\d{2}$")  # trailing slash works too


if __name__ == "__main__":
    unittest.main()
