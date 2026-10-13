"""Tests for lightd: config, targets, Lua runtime, library, runs, brightness, scenes, render loop and REST API."""

import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from wled import FakeTransport, WledController

from lightd import ApiServer, Lightd
from lightd.config import ConfigError, ControllerStore, validate_controllers
from lightd.loop import RenderLoop
from lightd.lua import Pattern, PatternError
from lightd.patterns import LibraryError, PatternLibrary
from lightd.registry import Registry
from lightd.runs import RunManager
from lightd.scenes import SceneError, SceneStore, is_active
from lightd.targets import Target, TargetError, parse_targets
from test_wled import CFG, GOVEE_CFG, GOVEE_STATE, FakeHttp, payloads

RGB_CFG = {"hw": {"led": {"ins": [dict(o, type=22) for o in CFG["hw"]["led"]["ins"]]}}}

REPO = Path(__file__).resolve().parent.parent
RED = "function pixel(n, t, p) return 1, 0, 0 end"
RAMP = "function pixel(n, t, p) return n / 255, 0, 0 end"


class TestConfig(unittest.TestCase):
    def test_channels_and_reversed(self):
        out = validate_controllers({"controllers": {"a": {"ip": "10.0.0.1", "channels": [{"name": " x ", "reversed": True}]}}})
        self.assertEqual(out["a"]["channels"], [{"name": "x", "reversed": True}])
        self.assertEqual(out["a"]["port"], 4048)
        for bad in ({"a": {"ip": "10.0.0.1", "channels": [{"reversed": "yes"}]}}, {"a": {}}, {"bad id": {"ip": "1.2.3.4"}}):
            with self.assertRaises(ConfigError):
                validate_controllers(bad)



class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "controllers.json"

    def tearDown(self):
        os.chmod(self.dir, 0o755)
        self.tmp.cleanup()

    def test_round_trip_leaves_no_temp_files(self):
        store = ControllerStore(self.path)
        self.assertEqual(store.load(), {})
        store.save({"a": {"ip": "10.0.0.1"}})
        self.assertEqual(store.load()["a"]["ip"], "10.0.0.1")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["controllers.json"])

    def test_keeps_file_mode(self):
        store = ControllerStore(self.path)
        store.save({"a": {"ip": "10.0.0.1"}})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)  # not mkstemp's 0600
        os.chmod(self.path, 0o666)
        store.save({"a": {"ip": "10.0.0.2"}})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o666)

    def test_preserves_controllers_wrapper(self):
        self.path.write_text(json.dumps({"note": "keep me", "controllers": {"a": "10.0.0.1"}}))
        store = ControllerStore(self.path)
        self.assertEqual(store.load(), {"a": {"ip": "10.0.0.1", "port": 4048}})
        store.save({"b": {"ip": "10.0.0.2"}})
        data = json.loads(self.path.read_text())
        self.assertEqual((data["note"], data["controllers"]), ("keep me", {"b": {"ip": "10.0.0.2", "port": 4048}}))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_rewrites_in_place_when_dir_is_read_only(self):
        self.path.write_text("{}")
        os.chmod(self.dir, 0o555)
        ControllerStore(self.path).save({"a": {"ip": "10.0.0.1"}})
        self.assertIn("a", json.loads(self.path.read_text()))

    def test_invalid_json(self):
        self.path.write_text("{nope")
        with self.assertRaises(ConfigError):
            ControllerStore(self.path).load()


class TestTargets(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(Target.parse("wled_01/2"), Target("wled_01", 1))
        self.assertEqual(str(Target("wled_01", 1)), "wled_01/2")
        self.assertEqual(parse_targets(["a/1", "a/1", "b/3"]), [Target("a", 0), Target("b", 2)])
        for bad in ("wled_01", "wled_01/0", "wled_01/x", "a/b/c"):
            with self.assertRaises(TargetError):
                Target.parse(bad)
        with self.assertRaises(TargetError):
            parse_targets([])


class TestLua(unittest.TestCase):
    def test_render_reversed_and_count(self):
        p = Pattern("function pixel(n, t, p) return n / (COUNT - 1), 0, t end")
        self.assertEqual(p.render(3, 1.0), bytes([0, 0, 255, 128, 0, 255, 255, 0, 255]))
        self.assertEqual(p.render(3, 0.0, reversed=True)[::3], bytes([255, 128, 0]))

    def test_w_is_optional_and_added_to_rgb_unless_rgbw(self):
        p = Pattern("function pixel(n, t, p) return 0.2, 0, 0, n == 1 and 0.5 or nil end")
        self.assertEqual(p.render(2, 0), bytes([51, 0, 0, 179, 128, 128]))
        self.assertEqual(p.render(2, 0, rgbw=True), bytes([51, 0, 0, 0, 51, 0, 0, 128]))
        with self.assertRaises(PatternError) as cm:
            Pattern("function pixel(n, t, p) return 1, 0, 0, 'x' end").render(1, 0)
        self.assertIn("optionally w", str(cm.exception))

    def test_white_is_extracted_on_white_capable_leds(self):
        p = Pattern("function pixel(n, t, p) return 1, 0.6, 0.2 end")
        self.assertEqual(p.render(1, 0), bytes([255, 153, 51]))  # RGB strip: unchanged
        self.assertEqual(p.render(1, 0, rgbw=True), bytes([204, 102, 0, 51]))  # common 0.2 moves to w
        sat = Pattern("function pixel(n, t, p) return hsv(0.3, 1, 1) end")
        self.assertEqual(sat.render(1, 0, rgbw=True)[3], 0)  # saturated color: no white
        own = Pattern("function pixel(n, t, p) return 1, 1, 1, 0 end")
        self.assertEqual(own.render(1, 0, rgbw=True), bytes([255, 255, 255, 0]))  # explicit w: left alone
        flag = Pattern("function pixel(n, t, p) return WHITE and 1 or 0, 0, 0, 0 end")
        self.assertEqual((flag.render(1, 0)[0], flag.render(1, 0, rgbw=True)[0]), (0, 255))

    def test_white_pattern_uses_the_white_chip(self):
        lib = PatternLibrary(REPO / "patterns", None)
        p = Pattern(lib.get("white")["source"])
        rgb = p.render(1, 0)
        self.assertEqual(rgb[0], 255)
        p.set_params({"kelvin": 4000, "white_led": 4000})
        self.assertEqual(p.render(1, 0, rgbw=True), bytes([0, 0, 0, 255]))  # matches the chip: all white
        p.set_params({"kelvin": 2700, "white_led": 4000})
        r, g, b, w = p.render(1, 0, rgbw=True)
        self.assertTrue(w > 100 and r > g >= b == 0)  # white chip tinted warmer with red

    def test_params_schema_and_coercion(self):
        p = Pattern('params = { speed = { type = "number", default = 2, min = 0, max = 5 }, c = "#ff0000" }\n' + RED)
        self.assertEqual(p.schema["speed"], {"type": "number", "default": 2, "min": 0, "max": 5})
        self.assertEqual(p.schema["c"]["type"], "color")
        p.set_params({"speed": 99, "level": 0.5})
        self.assertEqual(p.params()["speed"], 99)  # min/max are slider hints, not limits
        self.assertEqual(p.params()["level"], 0.5)  # unknown keys pass through (future live inputs)
        self.assertEqual(p.params()["c"], {"r": 1.0, "g": 0.0, "b": 0.0})
        p.set_params({"speed": "1.5"})
        self.assertEqual(p.params()["speed"], 1.5)
        p.set_params({"speed": "fast"})
        self.assertEqual(p.params()["speed"], 2)  # not a number: default

    def test_errors_and_sandbox(self):
        with self.assertRaises(PatternError) as cm:
            Pattern("x = = 1")
        self.assertEqual(cm.exception.line, 1)
        with self.assertRaises(PatternError):
            Pattern("x = 1")  # no pixel()
        for src in ("os.exit()", "io.open('/etc/passwd')", "require('os')", "load('return 1')()", "python.eval('1')"):
            with self.assertRaises(PatternError, msg=src):
                Pattern(src + "\n" + RED)
        bad = Pattern("function pixel(n, t, p)\n  if n == 2 then error('boom') end\n  return 0, 0, 0\nend")
        with self.assertRaises(PatternError) as cm:
            bad.render(5, 0)
        self.assertEqual(cm.exception.line, 2)

    def test_budget_stops_endless_loops(self):
        p = Pattern("function pixel(n, t, p) while true do end end", budget=100_000)
        t0 = time.monotonic()
        with self.assertRaises(PatternError) as cm:
            p.render(10, 0)
        self.assertIn("budget", str(cm.exception))
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_builtin_patterns_compile_and_render(self):
        lib = PatternLibrary(REPO / "patterns", None)
        names = lib.names()
        self.assertIn("rainbow", names)
        for name in names:
            data = Pattern(lib.source(name)).render(50, 1.5)
            self.assertEqual(len(data), 150, name)


class TestLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = PatternLibrary(REPO / "patterns", Path(self.tmp.name) / "user")

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_list_delete(self):
        self.lib.save("mine", RED)
        items = {p["name"]: p for p in self.lib.list()}
        self.assertEqual((items["mine"]["builtin"], items["mine"]["modified"]), (False, False))
        self.assertEqual((items["rainbow"]["builtin"], items["rainbow"]["modified"]), (True, False))
        self.assertEqual(self.lib.get("mine")["source"], RED)
        self.lib.delete("mine")
        with self.assertRaises(LibraryError):
            self.lib.get("mine")

    def test_edit_and_revert_builtin(self):
        original = self.lib.source("rainbow")
        self.assertEqual(self.lib.save("rainbow", RED), {"name": "rainbow", "builtin": True, "modified": True})
        self.assertEqual(self.lib.get("rainbow")["source"], RED)  # the edited copy is used
        self.assertTrue({p["name"]: p for p in self.lib.list()}["rainbow"]["modified"])
        self.assertEqual((REPO / "patterns" / "rainbow.lua").read_text(), original)  # built-in untouched
        self.lib.delete("rainbow")  # revert
        self.assertEqual(self.lib.get("rainbow"), {"name": "rainbow", "builtin": True, "modified": False, "source": original})
        with self.assertRaises(LibraryError):
            self.lib.delete("rainbow")  # nothing left to revert

    def test_rejects(self):
        with self.assertRaises(LibraryError):
            self.lib.save("Bad Name", RED)
        with self.assertRaises(PatternError):
            self.lib.save("broken", "x = = 1")
        with self.assertRaises(LibraryError):
            self.lib.delete("rainbow")


def make_registry(tmp: Path, controllers: dict, transport: FakeTransport, fail: bool = False, **http) -> Registry:
    """Every board gets 3 RGB channels of 270 LEDs unless ``http`` says otherwise (cfg=..., state=...)."""
    path = tmp / "controllers.json"
    path.write_text(json.dumps(controllers))
    http.setdefault("cfg", RGB_CFG)
    factory = lambda ip, port, transport: WledController(ip, port=port, transport=transport, http=FakeHttp(fail=fail, **http))
    reg = Registry(ControllerStore(path), transport=transport, controller_factory=factory)
    reg.load()
    for e in reg.entries():
        reg.refresh(e)
    return reg


class RunsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tx = FakeTransport()
        self.registry = make_registry(self.dir, {"a": {"ip": "10.0.0.1", "channels": [{}, {"reversed": True}]}}, self.tx)
        self.lib = PatternLibrary(REPO / "patterns", self.dir / "user")
        self.runs = RunManager(self.lib, self.dir / "state.json")
        self.loop = RenderLoop(self.registry, self.runs)

    def tearDown(self):
        self.tmp.cleanup()


class TestRuns(RunsTest):
    def test_new_run_cuts_overlapping_targets(self):
        r1 = self.runs.start(["a/1", "a/2"], source=RED)
        r2 = self.runs.start(["a/2"], pattern="solid")
        self.assertEqual([str(t) for t in r1.targets], ["a/1"])
        r3 = self.runs.start(["a/1"], source=RED)
        self.assertEqual({r.id for r in self.runs.runs()}, {r2.id, r3.id})  # r1 lost all targets

    def test_params_update_live_and_persist(self):
        run = self.runs.start(["a/1"], pattern="solid", params={"color": "#0000ff"})
        self.runs.update(run.id, {"brightness": 0.5})
        self.assertEqual(run.values, {"color": "#0000ff", "brightness": 0.5})
        run.apply_pending()
        self.assertEqual(run.pattern.params()["brightness"], 0.5)

        restored = RunManager(self.lib, self.dir / "state.json")
        self.assertEqual(restored.restore(), [])
        (r,) = restored.runs()
        self.assertEqual((r.name, [str(t) for t in r.targets], r.values), ("solid", ["a/1"], run.values))

    def test_brightness_per_channel_persists(self):
        self.runs.set_brightness(["a/1", "a/2"], 0.25)
        self.runs.set_brightness("a/2", 1)  # back to full: no longer stored
        self.assertEqual(self.runs.brightness_levels(), {"a/1": 0.25})
        self.assertEqual(self.runs.brightness(Target("a", 2)), 1.0)
        for bad in (1.5, -0.1, "0.5", True, None):
            with self.assertRaises(ValueError):
                self.runs.set_brightness(["a/1"], bad)

        restored = RunManager(self.lib, self.dir / "state.json")
        self.assertEqual(restored.restore(), [])
        self.assertEqual(restored.brightness(Target("a", 0)), 0.25)

    def test_bad_requests(self):
        with self.assertRaises(ValueError):
            self.runs.start(["a/1"])
        with self.assertRaises(LibraryError):
            self.runs.start(["a/1"], pattern="nope")
        with self.assertRaises(PatternError):
            self.runs.start(["a/1"], source="x = = 1")


class TestLoop(RunsTest):
    def test_renders_runs_and_blacks_idle_channels(self):
        self.runs.start(["a/2"], source=RAMP)
        self.loop.step()
        frame = payloads(self.tx.sent)
        self.assertEqual(frame[:270 * 3], bytes(270 * 3))  # ch 1 idle
        ch2 = frame[270 * 3:540 * 3]
        self.assertEqual(ch2[0], 255)  # reversed: physical first LED shows n = 269 (clamped)
        self.assertEqual(ch2[-3], 0)  # last LED shows n = 0
        self.assertEqual(frame[540 * 3:], bytes(270 * 3))

    def test_t_is_unix_time_shared_by_every_run(self):
        src = "function pixel(n, t, p) return (t % 100) / 100, t > 1e9 and 1 or 0, 0 end"
        self.runs.start(["a/1"], source=src)
        time.sleep(0.05)
        self.runs.start(["a/3"], source=src)  # started later, same t
        self.loop.step(now=1_700_000_042.0)
        frame = payloads(self.tx.sent)
        self.assertEqual(frame[:3], bytes([107, 255, 0]))  # 0.42 * 255
        self.assertEqual(frame[540 * 3:540 * 3 + 3], frame[:3])
        self.tx.clear()
        self.loop.step()  # defaults to the current time
        self.assertEqual(payloads(self.tx.sent)[1], 255)

    def test_brightness_scales_output(self):
        self.runs.start(["a/1", "a/2"], source=RED)
        self.runs.set_brightness(["a/1"], 0.5)
        self.loop.step()
        frame = payloads(self.tx.sent)
        self.assertEqual(frame[:3], b"\x80\x00\x00")
        self.assertEqual(frame[270 * 3:270 * 3 + 3], b"\xff\x00\x00")  # a/2 untouched
        self.runs.set_brightness(["a/1"], 0)
        self.tx.clear()
        self.loop.step()
        self.assertEqual(payloads(self.tx.sent)[:270 * 3], bytes(270 * 3))

    def test_rgbw_and_linked_govee_channels_get_w(self):
        tx = FakeTransport()
        reg = make_registry(self.dir, {"g": {"ip": "10.0.0.2"}}, tx, cfg=GOVEE_CFG, state=GOVEE_STATE)
        loop = RenderLoop(reg, self.runs)
        self.runs.start(["g/1"], source="function pixel(n, t, p) return 1, 0, 0, 0.5 end")
        loop.step()
        frame = payloads(tx.sent)
        self.assertEqual(frame, b"\xff\x00\x00\x00" * 10 + b"\x80" * 8)  # RGB LEDs, then the white LEDs
        tx.clear()
        reg = make_registry(self.dir, {"r": {"ip": "10.0.0.3"}}, tx, cfg=CFG)  # channel 3 is RGBW
        self.runs.start(["r/1", "r/3"], source="function pixel(n, t, p) return 1, 0, 0, 0.5 end")
        RenderLoop(reg, self.runs).step()
        frame = payloads(tx.sent)
        self.assertEqual(frame[:4], b"\xff\x80\x80\x00")  # RGB channel: w added to r, g, b
        self.assertEqual(frame[540 * 4:540 * 4 + 4], b"\xff\x00\x00\x80")

    def test_output_off_sends_nothing_and_releases_boards(self):
        self.runs.start(["a/1"], source=RED)
        self.runs.set_output(False)
        self.loop.step()
        self.loop.step()
        self.assertEqual(self.tx.sent, [])
        board = self.registry.get("a").controller
        for _ in range(50):  # released in a background thread
            if board.http.posted:
                break
            time.sleep(0.01)
        self.assertEqual(board.http.posted, [{"live": False}])  # once, not every frame

        restored = RunManager(self.lib, self.dir / "state.json")
        restored.restore()
        self.assertFalse(restored.output)
        with self.assertRaises(ValueError):
            self.runs.set_output("off")

        self.runs.set_output(True)
        self.loop.step()
        self.assertEqual(payloads(self.tx.sent)[:3], b"\xff\x00\x00")

    def test_error_keeps_last_good_frame_and_reports(self):
        run = self.runs.start(["a/1"], source="function pixel(n, t, p) if t > 1005 then error('late') end return 1, 0, 0 end")
        self.loop.step(now=1000.0)
        self.tx.clear()
        self.loop.step(now=1010.0)
        self.assertEqual(payloads(self.tx.sent)[:3], b"\xff\x00\x00")
        self.assertIn("late", run.error["message"])
        self.assertEqual(run.error_count, 1)

    def test_unreachable_board_gets_nothing(self):
        tx = FakeTransport()
        reg = make_registry(self.dir, {"b": {"ip": "10.0.0.2"}}, tx, fail=True)
        RenderLoop(reg, self.runs).step()
        self.assertEqual(tx.sent, [])
        self.assertFalse(reg.get("b").online)


class TestScenes(RunsTest):
    def setUp(self):
        super().setUp()
        self.scenes = SceneStore(self.dir / "scenes.json", self.lib)

    def test_save_validates(self):
        ok = {"targets": ["a/1"], "pattern": "solid"}
        for entries, msg in [
            ([], "non-empty"),
            ([{"targets": ["a/1"], "pattern": "nope"}], "no pattern named"),
            ([ok, {"targets": ["a/2", "a/1"], "pattern": "fire"}], "entries 1 and 2"),
            ([{**ok, "brightness": 2}], "brightness"),
            ([{**ok, "params": [1]}], "params"),
            ([{"targets": ["a"], "pattern": "solid"}], "Entry 1"),
        ]:
            with self.assertRaisesRegex(SceneError, msg):
                self.scenes.save("party", entries)
        with self.assertRaises(SceneError):
            self.scenes.save("Bad Name", [ok])
        saved = self.scenes.save("party", [ok])
        self.assertEqual(saved["entries"], [{"targets": ["a/1"], "pattern": "solid", "params": {}}])
        self.assertEqual(SceneStore(self.dir / "scenes.json", self.lib).get("party"), saved)  # on disk
        self.scenes.delete("party")
        with self.assertRaises(SceneError):
            self.scenes.get("party")

    def test_activate_starts_runs_and_tracks_active(self):
        other = self.runs.start(["a/1", "a/3"], source=RED)
        entries = [
            {"targets": ["a/1", "a/2"], "pattern": "rainbow", "params": {"speed": 1}},
            {"targets": ["a/3"], "pattern": "solid", "params": {"color": "#00ff00"}, "brightness": 0.3},
        ]
        self.scenes.save("party", entries)
        started, errors = self.scenes.activate("party", self.runs)
        self.assertEqual(errors, [])
        self.assertEqual([(r.name, r.scene, [str(t) for t in r.targets]) for r in started],
                         [("rainbow", "party", ["a/1", "a/2"]), ("solid", "party", ["a/3"])])
        self.assertNotIn(other.id, {r.id for r in self.runs.runs()})  # cut off every target
        self.assertEqual(self.runs.brightness(Target("a", 2)), 0.3)
        self.assertTrue(is_active("party", entries, self.runs.assignments()))

        restored = RunManager(self.lib, self.dir / "state.json")
        restored.restore()
        self.assertTrue(is_active("party", entries, restored.assignments()))

        self.runs.start(["a/2"], pattern="fire")
        self.assertFalse(is_active("party", entries, self.runs.assignments()))

    def test_activate_skips_entries_that_fail(self):
        (self.dir / "user").mkdir()
        self.lib.save("mine", RED)
        self.scenes.save("s", [{"targets": ["a/1"], "pattern": "mine"}, {"targets": ["a/2"], "pattern": "solid"}])
        self.lib.delete("mine")
        started, errors = self.scenes.activate("s", self.runs)
        self.assertEqual([r.name for r in started], ["solid"])
        self.assertIn("mine on a/1", errors[0])


class TestApi(RunsTest):
    def setUp(self):
        super().setUp()
        app = Lightd(self.registry.store, self.registry, self.lib, self.runs, self.loop)
        self.server = ApiServer(app, "127.0.0.1", 0)
        self.server.start()

    def tearDown(self):
        self.server.stop()
        super().tearDown()

    def call(self, method, path, body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server.port}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                text = resp.read().decode()
                return resp.status, json.loads(text) if resp.headers.get("Content-Type") == "application/json" else text
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_controllers_and_targets(self):
        status, res = self.call("GET", "/api/controllers")
        self.assertEqual(status, 200)
        (c,) = res["controllers"]
        self.assertTrue(c["online"])
        self.assertEqual([ch["target"] for ch in c["channels"]], ["a/1", "a/2", "a/3"])
        self.assertEqual(c["channels"][1]["reversed"], True)
        self.assertEqual(c["channels"][0]["length"], 270)
        self.assertEqual((c["channels"][0]["linked"], c["channels"][0]["segment_name"]), (False, None))

    def test_controllers_refresh(self):
        board = self.registry.get("a").controller
        board.channels = None  # forget what the board reported
        status, res = self.call("POST", "/api/controllers/refresh")
        self.assertEqual(status, 200, res)
        self.assertIsNotNone(board.channels)
        self.assertEqual([ch["length"] for ch in res["controllers"][0]["channels"]], [270, 270, 270])

    def test_run_lifecycle(self):
        status, run = self.call("POST", "/api/runs", {"targets": ["a/1"], "pattern": "chase", "params": {"width": 3}})
        self.assertEqual(status, 200, run)
        self.assertEqual(run["params"], {"width": 3})
        status, run = self.call("PATCH", f"/api/runs/{run['id']}", {"params": {"speed": 10}})
        self.assertEqual(run["params"], {"width": 3, "speed": 10})
        self.assertEqual(len(self.call("GET", "/api/runs")[1]["runs"]), 1)
        self.assertEqual(self.call("DELETE", f"/api/runs/{run['id']}")[0], 200)
        self.assertEqual(self.call("DELETE", f"/api/runs/{run['id']}")[0], 404)

    def test_errors_carry_lines(self):
        status, res = self.call("POST", "/api/runs", {"targets": ["a/1"], "source": "\nx = = 1"})
        self.assertEqual(status, 400)
        self.assertEqual(res["line"], 2)
        self.assertEqual(self.call("POST", "/api/runs", {"targets": ["a"], "source": RED})[0], 400)
        self.assertEqual(self.call("POST", "/api/patterns/check", {"source": RED})[0], 200)

    def test_patterns_and_runtime(self):
        status, res = self.call("PUT", "/api/patterns/mine", {"source": RED})
        self.assertEqual(status, 200, res)
        names = [p["name"] for p in self.call("GET", "/api/patterns")[1]["patterns"]]
        self.assertIn("mine", names)
        self.assertEqual(self.call("PUT", "/api/patterns/rainbow", {"source": RED})[1]["modified"], True)
        self.assertEqual(self.call("DELETE", "/api/patterns/rainbow")[0], 200)
        self.assertEqual(self.call("DELETE", "/api/patterns/rainbow")[0], 409)
        status, text = self.call("GET", "/api/runtime.lua")
        self.assertIn("function M.render_hex", text)

    def test_blackout_status_and_identify(self):
        self.call("POST", "/api/runs", {"targets": ["a/1"], "source": RED})
        self.assertEqual(self.call("POST", "/api/blackout")[0], 200)
        self.assertEqual(self.runs.runs(), [])
        status, s = self.call("GET", "/api/status")
        self.assertEqual(s["controllers"], {"total": 1, "online": 1})
        status, res = self.call("POST", "/api/identify", {"target": "a/2", "seconds": 0.5})
        self.assertEqual(status, 200, res)
        self.assertTrue(self.registry.get("a").controller.identifying)
        self.call("POST", "/api/identify", {"target": "a/2", "stop": True})
        status, res = self.call("POST", "/api/identify", {"ip": "10.0.0.1", "ranges": [[0, 4], [10, 2]], "seconds": 0.5})
        self.assertEqual(status, 200, res)
        self.call("POST", "/api/identify", {"ip": "10.0.0.1", "stop": True})
        self.assertEqual(self.call("POST", "/api/identify", {"ip": "10.0.0.1", "ranges": [[0]]})[0], 400)

    def test_config_get_put_and_reload(self):
        status, res = self.call("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(res["writable"])
        self.assertIsNone(res["error"])
        self.assertEqual(res["controllers"]["a"]["channels"][1], {"name": "", "reversed": True})

        new = {"a": {"ip": "10.0.0.1", "channels": [{"name": "Truss"}]}, "b": {"ip": "10.0.0.2"}}
        status, res = self.call("PUT", "/api/config", {"controllers": new})
        self.assertEqual(status, 200, res)
        self.assertEqual(sorted(res["controllers"]), ["a", "b"])
        self.assertEqual(self.registry.get("a").channel_config(0)["name"], "Truss")  # applied at once
        self.assertIsNotNone(self.registry.get("a").controller.channels)  # same IP: board info kept
        self.assertIsNotNone(self.registry.get("b"))
        self.assertEqual(json.loads(self.registry.store.path.read_text())["b"]["port"], 4048)

        bad = {"a": {"ip": "10.0.0.1"}, "c": {"ip": "10.0.0.1"}}  # same endpoint twice
        status, res = self.call("PUT", "/api/config", {"controllers": bad})
        self.assertEqual(status, 400)
        self.assertIn("both use", res["message"])
        self.assertIsNone(self.registry.get("c"))  # nothing applied
        self.assertEqual(self.call("PUT", "/api/config", {"a": {}})[0], 400)

        self.registry.store.path.write_text(json.dumps({"z": {"ip": "10.0.0.9"}}))  # edited by hand
        status, res = self.call("POST", "/api/config/reload")
        self.assertEqual(list(res["controllers"]), ["z"])
        self.assertEqual([e.id for e in self.registry.entries()], ["z"])

    def test_config_reports_unreadable_file(self):
        self.registry.store.path.write_text("{nope")
        status, res = self.call("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertIn("not valid JSON", res["error"])
        self.assertEqual(self.call("POST", "/api/config/reload")[0], 400)

    def test_brightness(self):
        status, res = self.call("PUT", "/api/brightness", {"targets": ["a/2"], "brightness": 0.4})
        self.assertEqual((status, res["brightness"]), (200, {"a/2": 0.4}))
        self.assertEqual(self.call("GET", "/api/brightness")[1]["brightness"], {"a/2": 0.4})
        channels = self.call("GET", "/api/controllers")[1]["controllers"][0]["channels"]
        self.assertEqual([ch["brightness"] for ch in channels], [1.0, 0.4, 1.0])
        self.assertEqual(self.call("PUT", "/api/brightness", {"targets": ["a/2"], "brightness": 3})[0], 400)
        self.assertEqual(self.call("PUT", "/api/brightness", {"brightness": 0.5})[0], 400)

    def test_output_switch(self):
        self.assertEqual(self.call("GET", "/api/output")[1], {"enabled": True})
        self.assertEqual(self.call("PUT", "/api/output", {"enabled": False})[1], {"enabled": False})
        self.assertFalse(self.call("GET", "/api/status")[1]["output"])
        self.assertEqual(self.call("PUT", "/api/output", {})[0], 400)

    def test_scenes(self):
        entries = [{"targets": ["a/1"], "pattern": "chase", "params": {"width": 3}, "brightness": 0.5}]
        status, res = self.call("PUT", "/api/scenes/party", {"entries": entries})
        self.assertEqual(status, 200, res)
        self.assertEqual(self.call("PUT", "/api/scenes/bad", {"entries": [{"targets": ["a/1"], "pattern": "x"}]})[0], 400)
        (scene,) = self.call("GET", "/api/scenes")[1]["scenes"]
        self.assertEqual((scene["name"], scene["active"]), ("party", False))

        status, res = self.call("POST", "/api/scenes/party/activate")
        self.assertEqual((status, res["status"], res["runs"][0]["scene"]), (200, "ok", "party"))
        self.assertTrue(self.call("GET", "/api/scenes/party")[1]["active"])
        self.assertEqual(self.call("GET", "/api/brightness")[1]["brightness"], {"a/1": 0.5})

        self.assertEqual(self.call("POST", "/api/scenes/nope/activate")[0], 404)
        self.assertEqual(self.call("DELETE", "/api/scenes/party")[0], 200)
        self.assertEqual(self.call("GET", "/api/scenes/party")[0], 404)

    def test_discovery_disabled(self):
        status, res = self.call("GET", "/api/discovered")
        self.assertEqual((status, res["enabled"], res["boards"]), (200, False, []))
        self.assertEqual(self.call("POST", "/api/discovered/probe", {"ip": "10.0.0.1"})[0], 409)


if __name__ == "__main__":
    unittest.main()
