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
        dashboard = Dashboard(CONFIG, f"http://127.0.0.1:{self.ha.server_port}", TOKEN, engine_url, timeout=3,
                              confirm_timeout=0.3)
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
        self.engine_routes[("GET", "/api/runs")] = (200, {"runs": [
            {"targets": ["wled_01/1"], "pattern": "smooth"},
            {"targets": ["govee_03/1"], "pattern": "rainbow"},
        ]})
        status, body = self.req("GET", "/api/dashboard/state")
        self.assertEqual(status, 200)
        areas = body["areas"]
        self.assertEqual({k: areas["west"][k] for k in ("brightness", "on", "available", "lights", "dimmable")},
                         {"brightness": 100, "on": True, "available": 2, "lights": 2, "dimmable": True})  # off bulb isn't counted
        self.assertIsNone(areas["office"]["brightness"])  # only light unavailable, no strips
        self.assertEqual(areas["pool"]["brightness"], 40)  # no bulbs: strips
        self.assertEqual(self.ha_calls[0][2], f"Bearer {TOKEN}")

    def test_pool_state_averages_ha_lights_and_strips(self) -> None:
        cfg = {
            **CONFIG,
            "areas": [
                {"id": "pool", "name": "Pool", "lights": ["light.a", "light.b"], "strips": ["govee_03/1", "govee_pergola/1"]},
            ],
        }
        self.engine_routes[("GET", "/api/controllers")] = (200, {"controllers": [
            {"id": "govee_03", "channels": [{"target": "govee_03/1", "brightness": 0.4}]},
            {"id": "govee_pergola", "channels": [{"target": "govee_pergola/1", "brightness": 0.6}]},
        ]})
        self.engine_routes[("GET", "/api/runs")] = (200, {"runs": [
            {"targets": ["govee_03/1", "govee_pergola/1"], "pattern": "rainbow"},
        ]})
        self.app.server.dashboard = Dashboard(cfg, f"http://127.0.0.1:{self.ha.server_port}", TOKEN,
                                              f"http://127.0.0.1:{self.engine.server_port}", timeout=3)
        status, body = self.req("GET", "/api/dashboard/state")
        self.assertEqual(status, 200)
        # light.a is 100%, govee_03/1 is 40%, govee_pergola/1 is 60% -> mean(100, 40, 60) = 67
        self.assertEqual(body["areas"]["pool"]["brightness"], 67)
        self.assertTrue(body["areas"]["pool"]["on"])

    def test_idle_strips_reats_solid_black_as_idle_and_keeps_running_patterns(self) -> None:
        cfg = {
            **CONFIG,
            "areas": [
                {"id": "pool", "name": "Pool", "lights": [], "strips": ["govee_03/1", "govee_pergola/1"]},
            ],
        }
        self.engine_routes[("GET", "/api/runs")] = (200, {"runs": [
            {"targets": ["govee_03/1"], "pattern": "solid", "params": {"color": "#000000"}},
            {"targets": ["govee_pergola/1"], "pattern": "rainbow", "params": {}},
        ]})
        self.app.server.dashboard = Dashboard(cfg, f"http://127.0.0.1:{self.ha.server_port}", TOKEN,
                                              f"http://127.0.0.1:{self.engine.server_port}", timeout=3)
        self.req("PUT", "/api/dashboard/areas/pool/brightness", {"brightness": 50})
        # govee_03/1 was solid #000000 (off), so it gets warm white; govee_pergola/1 was rainbow, so it is left alone
        self.assertIn(("POST", "/api/runs", None,
                       {"targets": ["govee_03/1"], "pattern": "white", "params": {"kelvin": 2700}}), self.engine_calls)
        self.assertIn(("PUT", "/api/brightness", None,
                       {"targets": ["govee_03/1", "govee_pergola/1"], "brightness": 0.5}), self.engine_calls)

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
        status, body = self.req("POST", "/api/dashboard/scenes/dim")
        self.assertEqual(status, 200)
        self.assertFalse(body["confirmed"])  # fake HA never reports a new timestamp
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


    # ---------- #3 active scene chip, #4 readout of on/off-only and mid-transition lights ----------

    @staticmethod
    def _ts(offset: float = 0.0) -> str:
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()

    def test_house_scene_confirms_when_ha_and_lightd_agree(self) -> None:
        self.ha_routes[("GET", "/api/states/scene.dim")] = (200, {"entity_id": "scene.dim", "state": self._ts(0.5)})
        self.engine_routes[("GET", "/api/scenes/dim")] = (200, {"name": "dim", "active": True})
        status, body = self.req("POST", "/api/dashboard/scenes/dim")
        self.assertEqual(status, 200)
        self.assertEqual((body["confirmed"], body["active_scene"], body["lightd_active"]), (True, "dim", True))

    def test_house_scene_not_confirmed_when_lightd_not_active(self) -> None:
        self.ha_routes[("GET", "/api/states/scene.dim")] = (200, {"entity_id": "scene.dim", "state": self._ts(0.5)})
        self.engine_routes[("GET", "/api/scenes/dim")] = (200, {"name": "dim", "active": False})
        body = self.req("POST", "/api/dashboard/scenes/dim")[1]
        self.assertFalse(body["confirmed"])
        self.assertIsNone(body["active_scene"])

    def test_house_scene_with_stale_ha_timestamp_is_not_confirmed(self) -> None:
        self.ha_routes[("GET", "/api/states/scene.dim")] = (200, {"entity_id": "scene.dim", "state": self._ts(-3600)})
        self.engine_routes[("GET", "/api/scenes/dim")] = (200, {"name": "dim", "active": True})
        self.assertFalse(self.req("POST", "/api/dashboard/scenes/dim")[1]["confirmed"])

    def _with_house_scenes(self, states, lightd_active):
        cfg = {**CONFIG, "house_scenes": [{"id": "bright", "label": "Bright", "entity": "scene.bright"},
                                          {"id": "dim", "label": "Dim", "entity": "scene.dim"}]}
        self.ha_routes[("GET", "/api/states")] = (200, states)
        self.engine_routes[("GET", "/api/scenes")] = (200, {"scenes": [{"name": n, "active": True} for n in lightd_active]})
        self.app.server.dashboard = Dashboard(cfg, f"http://127.0.0.1:{self.ha.server_port}", TOKEN,
                                              f"http://127.0.0.1:{self.engine.server_port}", timeout=3)
        return self.req("GET", "/api/dashboard/state")[1]

    def test_state_active_scene_is_newest_ha_scene_lightd_reports_active(self) -> None:
        states = [{"entity_id": "scene.bright", "state": self._ts(-60)}, {"entity_id": "scene.dim", "state": self._ts(-10)}]
        self.assertEqual(self._with_house_scenes(states, ["bright", "dim"])["active_scene"], "dim")
        # dim is newer in HA but something replaced it on the strips: lightd only has bright active
        self.assertEqual(self._with_house_scenes(states, ["bright"])["active_scene"], "bright")
        self.assertIsNone(self._with_house_scenes(states, [])["active_scene"])

    def test_area_scene_marks_area_until_slider_moves(self) -> None:
        self.assertEqual(self.req("POST", "/api/dashboard/areas/west/scenes/dim")[1]["active_scene"], "dim")
        self.assertEqual(self.req("GET", "/api/dashboard/state")[1]["areas"]["west"]["active_scene"], "dim")
        self.req("PUT", "/api/dashboard/areas/west/brightness", {"brightness": 60})
        self.assertIsNone(self.req("GET", "/api/dashboard/state")[1]["areas"]["west"]["active_scene"])

    def _office(self, lights):
        cfg = {**CONFIG, "areas": [{"id": "office", "name": "Office", "lights": [e["entity_id"] for e in lights], "strips": []}]}
        self.ha_routes[("GET", "/api/states")] = (200, lights)
        self.app.server.dashboard = Dashboard(cfg, f"http://127.0.0.1:{self.ha.server_port}", TOKEN,
                                              f"http://127.0.0.1:{self.engine.server_port}", timeout=3)
        return self.req("GET", "/api/dashboard/state")[1]["areas"]["office"]

    def test_office_set_to_40_reads_40(self) -> None:
        ct = {"supported_color_modes": ["color_temp"], "color_mode": "color_temp", "brightness": 102}
        st = self._office([{"entity_id": "light.c1", "state": "on", "attributes": ct},
                           {"entity_id": "light.n1", "state": "on", "attributes": ct}])
        self.assertEqual((st["brightness"], st["dimmable"]), (40, True))

    def test_lit_light_without_brightness_is_not_read_as_100(self) -> None:
        st = self._office([
            {"entity_id": "light.c1", "state": "on", "attributes": {"supported_color_modes": ["xy"], "brightness": 102}},
            {"entity_id": "light.n1", "state": "on", "attributes": {"supported_color_modes": ["color_temp"]}},  # mid-transition
            {"entity_id": "light.sw", "state": "on", "attributes": {"supported_color_modes": ["onoff"]}},  # relay
        ])
        self.assertEqual(st["brightness"], 40)

    def test_area_of_only_onoff_lights_is_a_toggle(self) -> None:
        onoff = {"supported_color_modes": ["onoff"], "color_mode": "onoff"}
        st = self._office([{"entity_id": "light.sw", "state": "on", "attributes": onoff}])
        self.assertEqual((st["dimmable"], st["on"], st["brightness"]), (False, True, 100))
        st = self._office([{"entity_id": "light.sw", "state": "off", "attributes": onoff}])
        self.assertEqual((st["dimmable"], st["on"], st["brightness"]), (False, False, 0))


class TestBundledConfig(unittest.TestCase):
    def test_bundled_areas(self) -> None:
        config = load_config()
        self.assertEqual([a["id"] for a in config["areas"]], ["west", "east", "office", "red_room", "pool"])
        everything = [e for a in config["areas"] for e in a["lights"]]
        self.assertEqual(len(everything), len(set(everything)), "a light is in two areas")
        self.assertEqual(config["areas"][3]["lights"], ["light.c4"])
        self.assertFalse([e for e in everything if e.startswith("light.l")], "smart plugs stay off the sliders")
        self.assertEqual(set(config["area_scenes"]), {"bright", "dim", "party"})
        pool = next(a for a in config["areas"] if a["id"] == "pool")
        self.assertEqual(pool["strips"], ["govee_03/1", "govee_pergola/1"])
        self.assertNotIn("wled_02/1", pool["strips"])
        targets = config.get("targets", {})
        self.assertEqual(targets["wled_02/1"], {"name": "Over Garage Door", "zone": "inside"})
        self.assertEqual(targets["govee_03/1"]["zone"], "outside")
        self.assertEqual(targets["govee_pergola/1"]["zone"], "outside")


if __name__ == "__main__":
    unittest.main()
