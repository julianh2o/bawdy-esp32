"""Tests for the old engine daemon's live config reload and identify blink."""

import json
import tempfile
import unittest
from pathlib import Path

from wled_app.domain.color import Color
from wled_engine.adapters.rest_daemon import EngineDaemon, handle_rest_request
from wled_engine.core import LightingEngine
from wled_engine.daemon_config import DaemonConfig
from wled_engine.identify import IDENTIFY_HALF_PERIOD, IDENTIFY_LEVEL, IdentifyScheduler

REPO_ROOT = Path(__file__).resolve().parent.parent

UNIVERSE = {
    "name": "Test",
    "fixtures": [
        {"type": "linear_strip", "id": "a", "pixel_count": 10, "waypoints": [{"x": 0, "y": 0, "z": 0}, {"x": 1, "y": 0, "z": 0}]},
        {"type": "linear_strip", "id": "b", "pixel_count": 5, "waypoints": [{"x": 0, "y": 1, "z": 0}, {"x": 1, "y": 1, "z": 0}]},
    ],
}
PATCH = {
    "segments": [
        {"fixture_id": "a", "pixel_count": 10, "controller_id": "wled_01", "channel_index": 0},
        {"fixture_id": "b", "pixel_count": 5, "controller_id": "wled_02", "channel_index": 0},
        {"fixture_id": "b", "pixel_count": 5, "controller_id": "wled_02", "channel_index": 1},
    ]
}


def ddp_payloads(packets, ip=None):
    """Concatenate DDP payloads (header is 10 bytes) of packets sent to ``ip``."""
    return b"".join(p[10:] for (dst, _port, p) in packets if ip is None or dst == ip)


class ConfigDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "universe.json").write_text(json.dumps(UNIVERSE))
        (self.dir / "patch.json").write_text(json.dumps(PATCH))
        self.controllers = self.dir / "controllers.json"
        self.config = DaemonConfig(
            universe_path=self.dir / "universe.json",
            patch_path=self.dir / "patch.json",
            controllers_path=self.controllers,
        )
        self.engine = LightingEngine(dry_run=True)

    def tearDown(self) -> None:
        self.engine.close()
        self.tmp.cleanup()

    def write_controllers(self, data) -> None:
        self.controllers.write_text(json.dumps(data))


class TestReload(ConfigDirTest):
    def test_missing_controllers_file_starts_with_nothing_addressed(self) -> None:
        summary = self.config.apply(self.engine, self.config.load())
        self.assertEqual(summary["controllers"], {})
        self.assertEqual(summary["unconfigured_controllers"], ["wled_01", "wled_02"])

    def test_reload_applies_new_ips_and_closes_old_dispatcher(self) -> None:
        self.write_controllers({"wled_01": {"ip": "10.0.0.1"}})
        self.config.apply(self.engine, self.config.load())
        old = self.engine.dispatcher
        self.assertEqual(self.engine.unconfigured_controllers, ["wled_02"])

        self.write_controllers({"wled_01": {"ip": "10.0.0.11"}, "wled_02": {"ip": "10.0.0.2", "port": 5000}})
        status, res = handle_rest_request(self.engine, "POST", "/api/config/reload")
        self.assertEqual(status, 200, res)
        self.assertEqual(res["controllers"], {"wled_01": "10.0.0.11:4048", "wled_02": "10.0.0.2:5000"})
        self.assertEqual(res["unconfigured_controllers"], [])
        self.assertIsNot(self.engine.dispatcher, old)
        self.assertEqual(old.nodes, [])  # closed

    def test_invalid_file_keeps_previous_config(self) -> None:
        self.write_controllers({"wled_01": {"ip": "10.0.0.1"}})
        self.config.apply(self.engine, self.config.load())
        dispatcher = self.engine.dispatcher
        self.controllers.write_text("{not json")
        status, res = handle_rest_request(self.engine, "POST", "/api/config/reload")
        self.assertEqual(status, 400)
        self.assertIn("not reloaded", res["message"])
        self.assertIs(self.engine.dispatcher, dispatcher)
        self.assertEqual(dispatcher.get_node("wled_01").ip, "10.0.0.1")

    def test_reload_without_config_source(self) -> None:
        status, _ = handle_rest_request(LightingEngine(dry_run=True), "POST", "/api/config/reload")
        self.assertEqual(status, 409)

    def test_reload_while_running(self) -> None:
        self.write_controllers({"wled_01": {"ip": "10.0.0.1"}, "wled_02": {"ip": "10.0.0.2"}})
        self.config.apply(self.engine, self.config.load())
        self.engine.set_spatial_pattern(lambda p: Color(255, 0, 0))
        self.engine.start()
        try:
            for i in range(20):
                self.write_controllers({"wled_01": {"ip": f"10.0.1.{i}"}, "wled_02": {"ip": "10.0.0.2"}})
                status, res = handle_rest_request(self.engine, "POST", "/api/config/reload")
                self.assertEqual(status, 200, res)
            self.assertTrue(self.engine.is_running)
            self.assertEqual(self.engine.dispatcher.get_node("wled_01").ip, "10.0.1.19")
        finally:
            self.engine.stop()


class TestIdentifyScheduler(unittest.TestCase):
    def test_blinks_then_sends_one_black_frame(self) -> None:
        s = IdentifyScheduler()
        s.start("10.0.0.5", 4048, led_count=4, seconds=1.0, now=100.0)
        on = s.frames(100.0)[("10.0.0.5", 4048)]
        self.assertEqual(on, bytes([IDENTIFY_LEVEL]) * 12)
        off = s.frames(100.0 + IDENTIFY_HALF_PERIOD)[("10.0.0.5", 4048)]
        self.assertEqual(off, bytes(12))
        self.assertEqual(s.frames(101.0), {("10.0.0.5", 4048): bytes(12)})
        self.assertEqual(s.frames(101.1), {})

    def test_led_start_keeps_earlier_leds_dark(self) -> None:
        s = IdentifyScheduler()
        s.start("10.0.0.5", 4048, led_count=2, seconds=1.0, now=0.0, led_start=3)
        self.assertEqual(s.frames(0.0)[("10.0.0.5", 4048)], bytes(9) + bytes([IDENTIFY_LEVEL]) * 6)
        self.assertEqual(s.frames(1.0)[("10.0.0.5", 4048)], bytes(15))

    def test_cancel(self) -> None:
        s = IdentifyScheduler()
        s.start("10.0.0.5", 4048, led_count=2, seconds=10, now=0.0)
        s.cancel("10.0.0.5", 4048)
        self.assertEqual(s.frames(0.1), {("10.0.0.5", 4048): bytes(6)})
        self.assertEqual(s.active, [])


class TestEngineIdentify(ConfigDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.write_controllers({"wled_01": {"ip": "10.0.0.1"}, "wled_02": {"ip": "10.0.0.2"}})
        self.config.apply(self.engine, self.config.load())

    def test_overrides_show_output_for_that_board_only(self) -> None:
        self.engine.set_spatial_pattern(lambda p: Color(255, 0, 0))
        handle_rest_request(self.engine, "POST", "/api/identify", {"controller_id": "wled_02"})
        self.engine.step(custom_dt=0.0)
        sent = self.engine.dispatcher._sender.sent_packets
        # wled_02 (2 channels x 5 px) shows the blink across its whole patched length.
        self.assertEqual(ddp_payloads(sent, "10.0.0.2"), bytes([IDENTIFY_LEVEL]) * 30)
        # wled_01 still shows the show (red, GRB on the wire).
        self.assertEqual(ddp_payloads(sent, "10.0.0.1")[:3], bytes([0, 255, 0]))
        self.assertIsNone(self.engine._identify_sender)  # nothing needed a separate send

    def test_idle_engine_and_unpatched_board_get_direct_frames(self) -> None:
        status, res = handle_rest_request(self.engine, "POST", "/api/identify", {"ip": "10.9.9.9", "led_count": 600})
        self.assertEqual(status, 200, res)
        self.engine.step(custom_dt=0.0)
        sent = self.engine._identify_sender.sent_packets
        self.assertEqual(len(sent), 2)  # 600 px = 480 + 120 per DDP packet
        self.assertEqual(sent[-1][2][0], 0x41)  # push flag on the last packet
        self.assertEqual(ddp_payloads(sent), bytes([IDENTIFY_LEVEL]) * 1800)

    def test_bad_requests(self) -> None:
        self.assertEqual(handle_rest_request(self.engine, "POST", "/api/identify", {})[0], 400)
        self.assertEqual(handle_rest_request(self.engine, "POST", "/api/identify", {"controller_id": "nope"})[0], 404)


if __name__ == "__main__":
    unittest.main()
