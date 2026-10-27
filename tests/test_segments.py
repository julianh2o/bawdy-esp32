"""Tests for #6: known-good segment snapshots, reset detection and restore (fake WLED boards, no hardware)."""

import copy
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from wled import FakeTransport, WledController

from lightd import ApiServer, Lightd
from lightd.config import ControllerStore
from lightd.loop import RenderLoop
from lightd.patterns import PatternLibrary
from lightd.registry import Registry
from lightd.runs import RunManager
from lightd.segments import SegmentStore, clean_segments, layout
from test_wled import GOVEE_INTERLEAVED_CFG, GOVEE_INTERLEAVED_STATE, FakeHttp, payloads

REPO = Path(__file__).resolve().parent.parent
# What WLED falls back to after a power cycle: one segment over the whole strip.
DEFAULT_STATE = {"seg": [{"id": 0, "start": 0, "stop": 90, "len": 90, "grp": 1, "spc": 0, "of": 0}]}


class FakeBoard(FakeHttp):
    """A FakeHttp whose /json/state segments change when they're POSTed, like a real board."""

    def __init__(self, state, cfg=GOVEE_INTERLEAVED_CFG):
        super().__init__(cfg=cfg, state=copy.deepcopy(state))
        self.cfg_posted = []

    def set_state(self, **state):
        self.posted.append(state)
        if "seg" in state:
            segs = {s["id"]: s for s in self._state.get("seg", [])}
            for s in state["seg"]:
                if s.get("stop") == 0:
                    segs.pop(s["id"], None)
                else:
                    segs[s["id"]] = {**segs.get(s["id"], {}), **s}
            self._state = {**self._state, "seg": sorted(segs.values(), key=lambda s: s["id"])}
        return {"success": True}

    def set_cfg(self, cfg):
        self.cfg_posted.append(cfg)
        return {"success": True}


class SegmentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tx = FakeTransport()
        self.boards = {"10.0.0.1": FakeBoard(GOVEE_INTERLEAVED_STATE),
                       "10.0.0.2": FakeBoard({"seg": [{"id": 0, "start": 0, "stop": 270, "len": 270}]},
                                             cfg={"hw": {"led": {"ins": [{"start": 0, "len": 270, "pin": [5], "type": 22}]}}})}
        path = self.dir / "controllers.json"
        path.write_text(json.dumps({"govee_01": {"ip": "10.0.0.1"}, "wled_01": {"ip": "10.0.0.2"}}))
        factory = lambda ip, port, transport: WledController(ip, port=port, transport=transport, http=self.boards[ip])
        self.segments = SegmentStore(self.dir / "segments")
        self.registry = Registry(ControllerStore(path), transport=self.tx, controller_factory=factory, segments=self.segments)
        self.registry.load()
        self.registry.refresh_all()
        self.lib = PatternLibrary(REPO / "patterns", None)
        self.runs = RunManager(self.lib)
        self.loop = RenderLoop(self.registry, self.runs)
        self.entry = self.registry.get("govee_01")

    def tearDown(self):
        self.tmp.cleanup()

    def power_cycle(self):
        self.boards["10.0.0.1"]._state = copy.deepcopy(DEFAULT_STATE)
        self.registry.refresh(self.entry)


class TestSegmentStore(SegmentsTest):
    def test_snapshots_a_linked_board_only(self):
        saved = json.loads((self.dir / "segments" / "govee_01.json").read_text())
        self.assertEqual(saved["reason"], "auto")
        self.assertEqual(layout(saved["seg"]), layout(clean_segments(GOVEE_INTERLEAVED_STATE["seg"])))
        self.assertEqual(self.entry.segment_status, {"saved": True, "match": True, "reset": False})
        self.assertFalse((self.dir / "segments" / "wled_01.json").exists())  # nothing linked: no snapshot
        self.assertEqual(self.registry.get("wled_01").segment_status["reset"], False)

    def test_power_cycle_is_flagged_as_reset(self):
        self.power_cycle()
        self.assertFalse(self.entry.controller.channels[0].linked)
        self.assertEqual(self.entry.segment_status, {"saved": True, "match": False, "reset": True})
        d = self.entry.to_dict()
        self.assertEqual(d["warnings"], ["segments reset"])
        # the snapshot isn't overwritten by the broken layout
        saved = self.segments.load("govee_01")
        self.assertEqual(layout(saved["seg"]), layout(clean_segments(GOVEE_INTERLEAVED_STATE["seg"])))

    def test_restore_puts_segments_back_and_run_resumes(self):
        self.runs.start(["govee_01/1"], source="function pixel(n, t, p) return 1, 0, 0, 0.5 end")
        self.power_cycle()
        result = self.segments.restore(self.entry, self.registry.refresh)
        self.assertTrue(result["linked"])
        self.assertIsNone(result["boot_preset"])
        self.assertEqual(self.entry.segment_status["reset"], False)
        self.assertTrue(self.entry.controller.channels[0].linked)
        self.loop.step()
        frame = payloads([p for p in self.tx.sent if p[0] == "10.0.0.1"])
        self.assertEqual(frame[4:8], b"\xff\x00\x00\x00")  # LED 1 (RGB, odd) red again
        self.assertEqual(frame[0:4], b"\x80" * 4)  # LED 0 (white, even) gets w

    def test_restore_deletes_extra_segments_and_can_set_boot_preset(self):
        board = self.boards["10.0.0.1"]
        board._state = {"seg": DEFAULT_STATE["seg"] + [{"id": 5, "start": 0, "stop": 10, "len": 10}]}
        self.registry.refresh(self.entry)
        result = SegmentStore(self.dir / "segments", preset_id=42).restore(self.entry, self.registry.refresh, boot_preset=True)
        self.assertEqual(result["boot_preset"], 42)
        sent = board.posted[0]["seg"]
        self.assertIn({"id": 5, "stop": 0}, sent)
        self.assertEqual(board.posted[1], {"psave": 42, "n": "lightd segments", "ib": True, "sb": True})
        self.assertEqual(board.cfg_posted, [{"def": {"ps": 42, "on": True}}])

    def test_no_boot_preset_when_restore_fails(self):
        board = self.boards["10.0.0.1"]
        board.set_state = lambda **s: board.posted.append(s) or {}  # board ignores the write
        self.power_cycle()
        result = self.segments.restore(self.entry, self.registry.refresh, boot_preset=True)
        self.assertFalse(result["linked"])
        self.assertIsNone(result["boot_preset"])
        self.assertEqual(board.cfg_posted, [])


class TestSegmentsApi(SegmentsTest):
    def setUp(self):
        super().setUp()
        self.app = Lightd(self.registry.store, self.registry, self.lib, self.runs, self.loop)
        self.server = ApiServer(self.app, "127.0.0.1", 0)
        self.server.start()

    def tearDown(self):
        self.server.stop()
        super().tearDown()

    def call(self, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_fake_reset_warning_restore_flow(self):
        self.power_cycle()
        (c,) = [c for c in self.call("GET", "/api/controllers")[1]["controllers"] if c["id"] == "govee_01"]
        self.assertEqual((c["warnings"], c["segments"]["reset"]), (["segments reset"], True))
        status, res = self.call("GET", "/api/controllers/govee_01/segments")
        self.assertEqual((status, res["reset"], len(res["snapshot"]["seg"]), len(res["live"])), (200, True, 2, 1))
        status, res = self.call("POST", "/api/controllers/govee_01/segments/restore", {"boot_preset": False})
        self.assertEqual((status, res["linked"], res["segments"]["reset"]), (200, True, False))
        (c,) = [c for c in self.call("GET", "/api/controllers")[1]["controllers"] if c["id"] == "govee_01"]
        self.assertEqual(c["warnings"], [])
        self.assertTrue(c["channels"][0]["linked"])

    def test_manual_save_and_errors(self):
        status, res = self.call("POST", "/api/controllers/wled_01/segments/save")
        self.assertEqual((status, res["snapshot"]["reason"], res["snapshot"]["seg"][0]["stop"]), (200, "manual", 270))
        self.assertEqual(self.call("POST", "/api/controllers/nope/segments/restore")[0], 404)
        self.assertEqual(self.call("POST", "/api/controllers/govee_01/segments/restore", {"boot_preset": "yes"})[0], 400)
        self.assertEqual(self.call("DELETE", "/api/controllers/govee_01/segments")[0], 404)
        (self.dir / "segments" / "govee_01.json").unlink()
        self.assertEqual(self.call("POST", "/api/controllers/govee_01/segments/restore")[0], 404)

    def test_dry_run_refuses_restore(self):
        self.loop.release = False
        self.assertEqual(self.call("POST", "/api/controllers/govee_01/segments/restore")[0], 409)
        self.assertEqual(self.boards["10.0.0.1"].posted, [])


if __name__ == "__main__":
    unittest.main()
