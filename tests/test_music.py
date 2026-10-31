"""Tests for the dashboard music proxy (/api/dashboard/music*) against a fake telemeter."""

import json
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from wled_sequencer.dashboard import Dashboard
from wled_sequencer.music import Music
from wled_sequencer.server import SequencerAppServer

TOKEN = "telemeter-secret-token"

STATE = {
    "on_screen": True, "player_user": "bawdyshop", "state": "playing",
    "track": "Song", "artist": "Artist", "album": "Album", "artwork_url": "https://i.scdn.co/image/x",
    "position": 12.3, "duration": 201.0, "volume": 47, "muted": False,
    "speaker": {"name": "Polk MagniFi Max AX", "address": "94:4F:4C:07:90:9B", "connected": True},
    "busy": None,
    "jam": {"wanted": True, "active": True, "url": "https://spotify.link/abc", "extension_online": True,
            "error": None, "session": {"join_session_token": "secret-ish"}},
    "idle_teardown_at": None,
    "console_user": "bawdyshop",
}


def _fake(record, routes):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _handle(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else None
            record.append((self.command, self.path, self.headers.get("Authorization"), body))
            status, payload = routes.get((self.command, self.path), (200, STATE))
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = _handle

    return Handler


class MusicProxyBase(unittest.TestCase):
    music_url = None  # set per test class; None = fake telemeter

    def setUp(self) -> None:
        self.calls, self.routes = [], {}
        self.tele = ThreadingHTTPServer(("127.0.0.1", 0), _fake(self.calls, self.routes))
        Thread(target=self.tele.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{self.tele.server_port}"
        self.app = SequencerAppServer(host="127.0.0.1", port=0, dashboard=Dashboard({"areas": []}),
                                      music=self.make_music(url))
        self.app.start()

    def make_music(self, url):
        return Music(url, TOKEN, timeout=3)

    def tearDown(self) -> None:
        self.app.stop()
        self.tele.shutdown()
        self.tele.server_close()

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"http://127.0.0.1:{self.app.port}{path}", data=data, method=method)
        r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class TestMusicProxy(MusicProxyBase):
    def test_describe_reports_music_enabled(self) -> None:
        status, body = self.req("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertTrue(body["music"])

    def test_state_sends_bearer_and_whitelists_fields(self) -> None:
        status, body = self.req("GET", "/api/dashboard/music")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls[0][:3], ("GET", "/music", f"Bearer {TOKEN}"))
        self.assertEqual(body["track"], "Song")
        self.assertEqual(body["speaker"], {"name": "Polk MagniFi Max AX", "connected": True})
        self.assertEqual(body["jam"], {"active": True, "url": "https://spotify.link/abc", "extension_online": True})
        for hidden in ("player_user", "console_user"):
            self.assertNotIn(hidden, body)
        text = json.dumps(body)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("94:4F:4C", text)
        self.assertNotIn("secret-ish", text)

    def test_actions_hit_matching_upstream_paths(self) -> None:
        for action in ("play", "pause", "next", "previous"):
            status, _ = self.req("POST", f"/api/dashboard/music/{action}", {})
            self.assertEqual(status, 200, action)
        self.assertEqual([(m, p) for m, p, _, _ in self.calls],
                         [("POST", "/music/play"), ("POST", "/music/pause"), ("POST", "/music/next"),
                          ("POST", "/music/previous")])

    def test_upstream_error_passes_through(self) -> None:
        msg = "Couldn't connect to the Polk MagniFi Max AX. Is it on, or connected to someone's phone?"
        self.routes[("POST", "/music/play")] = (409, {"error": msg, "code": "speaker_unavailable"})
        status, body = self.req("POST", "/api/dashboard/music/play", {})
        self.assertEqual(status, 409)
        self.assertEqual(body["message"], msg)
        self.assertEqual(body["code"], "speaker_unavailable")

    def test_volume_valid(self) -> None:
        status, _ = self.req("PUT", "/api/dashboard/music/volume", {"volume": 50})
        self.assertEqual(status, 200)
        self.assertEqual(self.calls[-1][0:2], ("PUT", "/music/volume"))
        self.assertEqual(self.calls[-1][3], {"volume": 50})

    def test_volume_invalid_never_reaches_upstream(self) -> None:
        for bad in ("x", -1, 101, 50.5, None, True):
            status, body = self.req("PUT", "/api/dashboard/music/volume", {"volume": bad})
            self.assertEqual(status, 400, bad)
            self.assertEqual(body["code"], "bad_request")
        self.assertEqual(self.calls, [])

    def test_unknown_music_route_404(self) -> None:
        status, _ = self.req("POST", "/api/dashboard/music/shuffle", {})
        self.assertEqual(status, 404)
        self.assertEqual(self.calls, [])


class TestMusicUnreachable(MusicProxyBase):
    def make_music(self, url):
        return Music("http://127.0.0.1:9", TOKEN, timeout=1)  # discard port: connection refused

    def test_unreachable_is_502(self) -> None:
        status, body = self.req("GET", "/api/dashboard/music")
        self.assertEqual(status, 502)
        self.assertEqual(body["code"], "telemeter_unreachable")
        self.assertNotIn(TOKEN, json.dumps(body))


class TestMusicUnconfigured(MusicProxyBase):
    def make_music(self, url):
        return Music("", "")

    def test_unconfigured(self) -> None:
        status, body = self.req("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertFalse(body["music"])
        status, body = self.req("GET", "/api/dashboard/music")
        self.assertEqual(status, 503)
        self.assertEqual(body["code"], "not_configured")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
