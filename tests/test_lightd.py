"""Tests for lightd: config, targets, Lua runtime, library, runs, render loop and REST API."""

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
from lightd.targets import Target, TargetError, parse_targets
from test_wled import FakeHttp, payloads

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

    def test_params_schema_and_coercion(self):
        p = Pattern('params = { speed = { type = "number", default = 2, min = 0, max = 5 }, c = "#ff0000" }\n' + RED)
        self.assertEqual(p.schema["speed"], {"type": "number", "default": 2, "min": 0, "max": 5})
        self.assertEqual(p.schema["c"]["type"], "color")
        p.set_params({"speed": 99, "level": 0.5})
        self.assertEqual(p.params()["speed"], 5)
        self.assertEqual(p.params()["level"], 0.5)  # unknown keys pass through (future live inputs)
        self.assertEqual(p.params()["c"], {"r": 1.0, "g": 0.0, "b": 0.0})

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
        names = [n for n, _ in lib.names()]
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
        self.assertFalse(items["mine"]["builtin"])
        self.assertTrue(items["rainbow"]["builtin"])
        self.assertEqual(self.lib.get("mine")["source"], RED)
        self.lib.delete("mine")
        with self.assertRaises(LibraryError):
            self.lib.get("mine")

    def test_rejects(self):
        with self.assertRaises(LibraryError):
            self.lib.save("rainbow", RED)  # built-in name
        with self.assertRaises(LibraryError):
            self.lib.save("Bad Name", RED)
        with self.assertRaises(PatternError):
            self.lib.save("broken", "x = = 1")
        with self.assertRaises(LibraryError):
            self.lib.delete("rainbow")


def make_registry(tmp: Path, controllers: dict, transport: FakeTransport, fail: bool = False) -> Registry:
    path = tmp / "controllers.json"
    path.write_text(json.dumps(controllers))
    factory = lambda ip, port, transport: WledController(ip, port=port, transport=transport, http=FakeHttp(fail=fail))
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

    def test_error_keeps_last_good_frame_and_reports(self):
        run = self.runs.start(["a/1"], source="function pixel(n, t, p) if t > 5 then error('late') end return 1, 0, 0 end")
        self.loop.step(now=run.started)
        self.tx.clear()
        self.loop.step(now=run.started + 10)
        self.assertEqual(payloads(self.tx.sent)[:3], b"\xff\x00\x00")
        self.assertIn("late", run.error["message"])
        self.assertEqual(run.error_count, 1)

    def test_unreachable_board_gets_nothing(self):
        tx = FakeTransport()
        reg = make_registry(self.dir, {"b": {"ip": "10.0.0.2"}}, tx, fail=True)
        RenderLoop(reg, self.runs).step()
        self.assertEqual(tx.sent, [])
        self.assertFalse(reg.get("b").online)


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
        self.assertEqual(self.call("PUT", "/api/patterns/rainbow", {"source": RED})[0], 409)
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

    def test_discovery_disabled(self):
        status, res = self.call("GET", "/api/discovered")
        self.assertEqual((status, res["enabled"], res["boards"]), (200, False, []))
        self.assertEqual(self.call("POST", "/api/discovered/probe", {"ip": "10.0.0.1"})[0], 409)


if __name__ == "__main__":
    unittest.main()
