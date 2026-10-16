"""Tests for the dashboard: area sliders and scenes against fake Home Assistant and lightd servers."""

import json
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from wled_sequencer.dashboard import Dashboard, load_config
from wled_sequencer.server import SequencerAppServer

TOKEN = "test-token"

CONFIG = {
    "areas": [
        {"id": "west", "name": "West", "lights": ["light.a", "light.b"], "strips": ["wled_01/1"]},
        {"id": "office", "name": "Office", "lights": ["light.c"], "strips": []},
        {"id": "pool", "name": "Pool", "lights": [], "strips": ["govee_03/1"]},
    ],
    "area_scenes": {
        "bright": {"label": "Bright", "brightness": 100, "kelvin": 2700},
        "dim": {"label": "Dim", "brightness": 25, "kelvin": 2700},
        "party": {"label": "Party", "brightness": 100, "party": True},
    },
    "house_scenes": [{"id": "dim", "label": "Dim", "entity": "scene.dim"}],
}


def _fake(record, routes):
    """A handler class that records (method, path, auth, body) and answers from routes[(method, path)]."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _handle(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else None
            record.append((self.command, self.path, self.headers.get("Authorization"), body))
            status, payload = routes.get((self.command, self.path), (200, []))
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = _handle

    return Handler


class TestDashboard(unittest.TestCase):
    def setUp(self) -> None:
        self.ha_calls, self.engine_calls = [], []
        self.ha_routes = {
            ("GET", "/api/states"): (200, [
                {"entity_id": "light.a", "state": "on", "attributes": {"brightness": 255}},
                {"entity_id": "light.b", "state": "off", "attributes": {}},
                {"entity_id": "light.c", "state": "unavailable", "attributes": {}},
                {"entity_id": "light.not_ours", "state": "on", "attributes": {"brightness": 10}},
            ]),
        }
        self.engine_routes = {
            ("GET", "/api/controllers"): (200, {"controllers": [
                {"id": "wled_01", "channels": [{"target": "wled_01/1", "brightness": 1.0}]},
                {"id": "govee_03", "channels": [{"target": "govee_03/1", "brightness": 0.4}]},
            ]}),
            ("GET", "/api/runs"): (200, {"runs": [{"targets": ["wled_01/1"], "pattern": "smooth"}]}),
        }
        self.ha = ThreadingHTTPServer(("127.0.0.1", 0), _fake(self.ha_calls, self.ha_routes))
        self.engine = ThreadingHTTPServer(("127.0.0.1", 0), _fake(self.engine_calls, self.engine_routes))
        for srv in (self.ha, self.engine):
            Thread(target=srv.serve_forever, daemon=True).start()
        engine_url = f"http://127.0.0.1:{self.engine.server_port}"
        dashboard = Dashboard(CONFIG, f"http://127.0.0.1:{self.ha.server_port}", TOKEN, engine_url, timeout=3)
        self.app = SequencerAppServer(host="127.0.0.1", port=0, engine_url=engine_url, dashboard=dashboard)
        self.app.start()

    def tearDown(self) -> None:
        self.app.stop()
        for srv in (self.ha, self.engine):
            srv.shutdown()
            srv.server_close()

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"http://127.0.0.1:{self.app.port}{path}", data=data, method=method)
        r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def services(self):
        return [(path, body) for method, path, _, body in self.ha_calls if path.startswith("/api/services/")]

    def test_describe_hides_entities_and_token(self) -> None:
        status, body = self.req("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertTrue(body["ha"])
        self.assertEqual([a["id"] for a in body["areas"]], ["west", "office", "pool"])
        self.assertEqual([s["id"] for s in body["area_scenes"]], ["bright", "dim", "party"])
        self.assertEqual(body["house_scenes"], [{"id": "dim", "label": "Dim"}])
        self.assertNotIn(TOKEN, json.dumps(body))

    def test_state(self) -> None:
        status, body = self.req("GET", "/api/dashboard/state")
        self.assertEqual(status, 200)
        areas = body["areas"]
        self.assertEqual(areas["west"], {"brightness": 100, "on": True, "available": 2, "lights": 2})  # off bulb isn't counted
        self.assertIsNone(areas["office"]["brightness"])  # only light unavailable, no strips
        self.assertEqual(areas["pool"]["brightness"], 40)  # no bulbs: strips
        self.assertEqual(self.ha_calls[0][2], f"Bearer {TOKEN}")

    def test_brightness_drives_ha_lights_and_strips(self) -> None:
        status, body = self.req("PUT", "/api/dashboard/areas/west/brightness", {"brightness": 42.4})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "warnings": []})
        self.assertEqual(self.services(), [("/api/services/light/turn_on",
                                            {"entity_id": ["light.a", "light.b"], "brightness_pct": 42})])
        self.assertIn(("PUT", "/api/brightness", None, {"targets": ["wled_01/1"], "brightness": 0.42}), self.engine_calls)
        self.assertFalse([c for c in self.engine_calls if c[0] == "POST"])  # strip already playing: left alone

    def test_brightness_starts_idle_strips_and_zero_turns_off(self) -> None:
        self.req("PUT", "/api/dashboard/areas/pool/brightness", {"brightness": 50})
        self.assertIn(("POST", "/api/runs", None,
                       {"targets": ["govee_03/1"], "pattern": "white", "params": {"kelvin": 2700}}), self.engine_calls)
        self.req("PUT", "/api/dashboard/areas/office/brightness", {"brightness": 0})
        self.assertEqual(self.services()[-1], ("/api/services/light/turn_off", {"entity_id": ["light.c"]}))

    def test_brightness_validation(self) -> None:
        self.assertEqual(self.req("PUT", "/api/dashboard/areas/west/brightness", {"brightness": "loud"})[0], 400)
        self.assertEqual(self.req("PUT", "/api/dashboard/areas/nope/brightness", {"brightness": 5})[0], 404)
        self.req("PUT", "/api/dashboard/areas/office/brightness", {"brightness": 400})
        self.assertEqual(self.services()[-1][1]["brightness_pct"], 100)

    def test_area_scene_dim(self) -> None:
        status, _ = self.req("POST", "/api/dashboard/areas/west/scenes/dim")
        self.assertEqual(status, 200)
        self.assertEqual(self.services(), [("/api/services/light/turn_on", {
            "entity_id": ["light.a", "light.b"], "brightness_pct": 25, "color_temp_kelvin": 2700})])
        self.assertIn(("POST", "/api/runs", None,
                       {"targets": ["wled_01/1"], "pattern": "white", "params": {"kelvin": 2700}}), self.engine_calls)
        self.assertIn(("PUT", "/api/brightness", None, {"targets": ["wled_01/1"], "brightness": 0.25}), self.engine_calls)

    def test_area_scene_party_gives_each_bulb_its_own_color(self) -> None:
        self.req("POST", "/api/dashboard/areas/west/scenes/party")
        calls = sorted(self.services(), key=lambda c: c[1]["entity_id"])
        self.assertEqual([c[1]["entity_id"] for c in calls], ["light.a", "light.b"])
        hues = [c[1]["hs_color"][0] for c in calls]
        self.assertTrue(all(c[1]["hs_color"][1] == 100 for c in calls))
        self.assertAlmostEqual(abs(hues[0] - hues[1]) % 360, 180, delta=0.01)
        self.assertIn(("POST", "/api/runs", None, {"targets": ["wled_01/1"], "pattern": "rainbow", "params": {}}),
                      self.engine_calls)

    def test_house_scene(self) -> None:
        status, _ = self.req("POST", "/api/dashboard/scenes/dim")
        self.assertEqual(status, 200)
        self.assertEqual(self.services(), [("/api/services/scene/turn_on", {"entity_id": "scene.dim"})])
        self.assertEqual(self.req("POST", "/api/dashboard/scenes/nope")[0], 404)
        self.assertEqual(self.req("POST", "/api/dashboard/areas/west/scenes/nope")[0], 404)

    def test_partial_failure_is_a_warning_total_failure_is_an_error(self) -> None:
        self.engine_routes[("PUT", "/api/brightness")] = (500, {"message": "boom"})
        status, body = self.req("PUT", "/api/dashboard/areas/west/brightness", {"brightness": 30})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["warnings"]), 1)
        self.assertIn("lightd", body["warnings"][0])
        status, body = self.req("PUT", "/api/dashboard/areas/pool/brightness", {"brightness": 30})
        self.assertEqual(status, 502)

    def test_without_home_assistant(self) -> None:
        dash = Dashboard(CONFIG, "", "", "http://127.0.0.1:9")
        self.assertFalse(dash.ha_enabled)
        self.assertFalse(dash.describe()["ha"])
        self.app.server.dashboard = dash
        status, body = self.req("PUT", "/api/dashboard/areas/office/brightness", {"brightness": 30})
        self.assertEqual(status, 503)
        self.assertIn("HA_URL", body["message"])

    def test_from_env_accepts_hass_names(self) -> None:
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"HASS_SERVER": "http://ha:8123", "HASS_TOKEN": "t"}, clear=True):
            dash = Dashboard.from_env("http://engine")
        self.assertEqual((dash.ha_url, dash.ha_token, dash.ha_enabled), ("http://ha:8123", "t", True))

    def test_unknown_dashboard_path(self) -> None:
        self.assertEqual(self.req("GET", "/api/dashboard/nope")[0], 404)


class TestBundledConfig(unittest.TestCase):
    def test_bundled_areas(self) -> None:
        config = load_config()
        self.assertEqual([a["id"] for a in config["areas"]], ["west", "east", "office", "red_room", "pool"])
        everything = [e for a in config["areas"] for e in a["lights"]]
        self.assertEqual(len(everything), len(set(everything)), "a light is in two areas")
        self.assertEqual(config["areas"][3]["lights"], ["light.c4"])
        self.assertFalse([e for e in everything if e.startswith("light.l")], "smart plugs stay off the sliders")
        self.assertEqual(set(config["area_scenes"]), {"bright", "dim", "party"})


if __name__ == "__main__":
    unittest.main()
