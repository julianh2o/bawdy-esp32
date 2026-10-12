"""Tests for the /config page backend: controllers.json store, WLED discovery, and API routes."""

import json
import os
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from wled_sequencer.controller_config import (
    ConfigError,
    ControllerConfigStore,
    WledDiscovery,
    validate_controllers,
)
from wled_sequencer.server import SequencerAppServer

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestValidate(unittest.TestCase):
    def test_defaults_port_and_keeps_extra_keys(self) -> None:
        out = validate_controllers({"wled_01": {"ip": " 10.0.0.5 ", "mac": "aa:bb"}, "wled_02": "10.0.0.6"})
        self.assertEqual(out["wled_01"], {"ip": "10.0.0.5", "port": 4048, "mac": "aa:bb"})
        self.assertEqual(out["wled_02"], {"ip": "10.0.0.6", "port": 4048})

    def test_rejects_bad_input(self) -> None:
        for bad in (
            {"bad id!": {"ip": "10.0.0.1"}},
            {"wled_01": {}},
            {"wled_01": {"ip": "10.0.0.1", "port": 70000}},
            {"wled_01": {"ip": "10.0.0.1"}, "wled_02": {"ip": "10.0.0.1"}},  # same endpoint twice
            {"wled_01": {"ip": "10.0.0.1", "channels": "Truss"}},
            {"wled_01": {"ip": "10.0.0.1", "channels": ["Truss"]}},
            {"wled_01": {"ip": "10.0.0.1", "channels": [{"name": 5}]}},
            {"wled_01": {"ip": "10.0.0.1", "channels": [{"name": "x" * 65}]}},
            [],
        ):
            with self.assertRaises(ConfigError, msg=bad):
                validate_controllers(bad)

    def test_channel_names(self) -> None:
        out = validate_controllers({"wled_01": {"ip": "10.0.0.5", "channels": [{"name": " Truss L "}, {"name": "", "x": 1}]}})
        self.assertEqual(out["wled_01"]["channels"], [{"name": "Truss L"}, {"name": "", "x": 1}])

    def test_output_is_readable_by_engine(self) -> None:
        from wled_engine.controllers import parse_controllers

        ips, ports = parse_controllers(validate_controllers({"wled_01": {"ip": "10.0.0.5", "name": "x", "mac": "y", "channels": [{"name": "a"}]}}))
        self.assertEqual(ips, {"wled_01": "10.0.0.5"})
        self.assertEqual(ports, {"wled_01": 4048})


class TestStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_save_creates_file_and_round_trips(self) -> None:
        store = ControllerConfigStore(self.dir / "controllers.json")
        self.assertEqual(store.load(), {})
        store.save({"wled_01": {"ip": "10.0.0.5", "port": 4048, "name": "Front"}})
        self.assertEqual(json.loads((self.dir / "controllers.json").read_text())["wled_01"]["name"], "Front")
        self.assertEqual(store.load()["wled_01"]["ip"], "10.0.0.5")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["controllers.json"])  # no temp files left

    def test_saved_file_keeps_mode_and_is_readable(self) -> None:
        path = self.dir / "controllers.json"
        store = ControllerConfigStore(path)
        store.save({"wled_01": {"ip": "10.0.0.1"}})
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)  # not mkstemp's 0600
        os.chmod(path, 0o666)
        store.save({"wled_01": {"ip": "10.0.0.2"}})
        self.assertEqual(path.stat().st_mode & 0o777, 0o666)

    def test_preserves_controllers_wrapper(self) -> None:
        path = self.dir / "controllers.json"
        path.write_text(json.dumps({"note": "keep me", "controllers": {"wled_01": "10.0.0.1"}}))
        store = ControllerConfigStore(path)
        self.assertEqual(store.load(), {"wled_01": {"ip": "10.0.0.1"}})
        store.save({"wled_02": {"ip": "10.0.0.2"}})
        data = json.loads(path.read_text())
        self.assertEqual(data["note"], "keep me")
        self.assertEqual(data["controllers"], {"wled_02": {"ip": "10.0.0.2", "port": 4048}})

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_falls_back_to_in_place_write_when_dir_read_only(self) -> None:
        path = self.dir / "controllers.json"
        path.write_text("{}")
        os.chmod(self.dir, 0o555)
        try:
            ControllerConfigStore(path).save({"wled_01": {"ip": "10.0.0.1"}})
        finally:
            os.chmod(self.dir, 0o755)
        self.assertIn("wled_01", json.loads(path.read_text()))

    def test_patch_expectations_match_engine_packing(self) -> None:
        from wled_engine.patch.patch_table import PatchTable

        patch_path = REPO_ROOT / "config" / "patch.json.example"
        store = ControllerConfigStore(self.dir / "controllers.json", patch_path)
        exp = store.patch_expectations()
        table = PatchTable.load_json(patch_path)
        self.assertEqual(set(exp), set(table.get_controllers()))
        for cid, e in exp.items():
            engine_channels = [table.get_channel_pixel_length(cid, ch) for ch in range(len(e["channels"]))]
            self.assertEqual(e["channels"], engine_channels, cid)
        self.assertEqual(exp["wled_01"]["channels"], [300, 300, 200])
        self.assertEqual(exp["wled_01"]["total_leds"], 800)


def wait_for(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


FAKE_INFO = {"name": "Truss Front", "mac": "aabbccddeeff", "led_count": 800}


class TestDiscoveryTable(unittest.TestCase):
    """Node bookkeeping, driven directly (no network)."""

    def setUp(self) -> None:
        self.probed: list[str] = []

        def prober(ip: str) -> dict:
            self.probed.append(ip)
            if ip == "10.0.0.66":
                raise OSError("timed out")
            return dict(FAKE_INFO)

        self.disc = WledDiscovery(prober=prober)
        self.name = "wled-truss._wled._tcp.local."

    def test_service_is_recorded_and_probed_once(self) -> None:
        self.disc.record_service(self.name, ip="10.0.0.5", hostname="wled-truss.local", mac="aabbccddeeff")
        self.disc.record_service(self.name, ip="10.0.0.5", hostname="wled-truss.local", mac="aabbccddeeff")
        self.assertTrue(wait_for(lambda: self.disc.nodes()[0]["info"] is not None))
        node = self.disc.nodes()[0]
        self.assertEqual(node["instance"], "wled-truss")
        self.assertEqual(node["hostname"], "wled-truss.local")
        self.assertTrue(node["online"])
        self.assertEqual(node["info"]["led_count"], 800)
        self.assertEqual(self.probed, ["10.0.0.5"])

    def test_ip_change_reprobes_and_removal_marks_offline(self) -> None:
        self.disc.record_service(self.name, ip="10.0.0.5")
        wait_for(lambda: len(self.probed) == 1)
        self.disc.record_service(self.name, ip="10.0.0.6")
        self.assertTrue(wait_for(lambda: self.probed == ["10.0.0.5", "10.0.0.6"]))
        self.disc.mark_gone(self.name)
        nodes = self.disc.nodes()
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["ip"], "10.0.0.6")
        self.assertFalse(nodes[0]["online"])

    def test_manual_probe_merges_into_later_mdns_entry(self) -> None:
        node = self.disc.probe("10.0.0.5")
        self.assertEqual(node["source"], "manual")
        self.assertTrue(node["online"])
        self.disc.record_service(self.name, ip="10.0.0.5", hostname="wled-truss.local")
        nodes = self.disc.nodes()
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["source"], "mdns")
        self.assertEqual(nodes[0]["info"]["mac"], "aabbccddeeff")

    def test_probe_failure_and_validation(self) -> None:
        node = self.disc.probe("10.0.0.66")
        self.assertIsNone(node["info"])
        self.assertFalse(node["online"])
        self.assertEqual(node["probe_error"], "timed out")
        with self.assertRaises(ConfigError):
            self.disc.probe("not-an-ip")


try:
    import zeroconf  # noqa: F401
    HAVE_ZEROCONF = True
except ImportError:
    HAVE_ZEROCONF = False


@unittest.skipUnless(HAVE_ZEROCONF, "zeroconf not installed")
class TestMdnsDiscovery(unittest.TestCase):
    """Round trip over real multicast: advertise a fake WLED board, then browse for it."""

    def test_finds_advertised_board(self) -> None:
        from zeroconf import IPVersion, ServiceInfo, Zeroconf

        # A private service type, so real boards on the LAN don't interfere.
        stype = f"_wledtest{os.getpid() % 10000}._tcp.local."
        name = f"wled-fake.{stype}"
        info = ServiceInfo(
            stype, name, port=80, addresses=[socket.inet_aton("127.0.0.1")],
            properties={"mac": "aabbccddeeff"}, server="wled-fake.local.",
        )
        publisher = Zeroconf(ip_version=IPVersion.V4Only)
        disc = WledDiscovery(prober=lambda ip: dict(FAKE_INFO), service_type=stype)
        try:
            publisher.register_service(info)
            disc.start()
            self.assertTrue(disc.listening, disc.error)
            found = wait_for(lambda: any(n.get("info") for n in disc.nodes()), timeout=10.0)
            if not found:
                self.skipTest("no multicast loopback on this host")
            node = disc.nodes()[0]
            self.assertEqual(node["instance"], "wled-fake")
            self.assertEqual(node["hostname"], "wled-fake.local")
            self.assertEqual(node["ip"], "127.0.0.1")
            self.assertEqual(node["mac"], "aabbccddeeff")

            publisher.unregister_service(info)
            self.assertTrue(wait_for(lambda: not disc.nodes()[0]["online"], timeout=5.0))
        finally:
            disc.stop()
            publisher.close()


class TestConfigApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "patch.json").write_text(
            json.dumps({"segments": [{"fixture_id": "a", "pixel_count": 60, "controller_id": "wled_01"}]})
        )
        self.app = SequencerAppServer(
            host="127.0.0.1",
            port=0,
            sequences_dir=self.dir / "sequences",
            controllers_file=self.dir / "controllers.json",
            patch_file=self.dir / "patch.json",
            discovery=True,
        )
        self.app.discovery.prober = lambda ip: {"name": "Board", "mac": "aabbccddeeff", "led_count": 60}
        self.app.start()
        self.base = f"http://127.0.0.1:{self.app.port}"

    def tearDown(self) -> None:
        self.app.stop()
        self.tmp.cleanup()

    def request(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_get_save_and_validation(self) -> None:
        status, state = self.request("GET", "/api/config/controllers")
        self.assertEqual(status, 200)
        self.assertFalse(state["exists"])
        self.assertTrue(state["writable"])
        self.assertEqual(state["patch"]["wled_01"]["total_leds"], 60)

        status, res = self.request("PUT", "/api/config/controllers", {"controllers": {"wled_01": {"ip": "10.0.0.5", "mac": "aa"}}})
        self.assertEqual(status, 200, res)
        saved = json.loads((self.dir / "controllers.json").read_text())
        self.assertEqual(saved, {"wled_01": {"ip": "10.0.0.5", "port": 4048, "mac": "aa"}})

        status, res = self.request("PUT", "/api/config/controllers", {"controllers": {"wled_01": {"ip": ""}}})
        self.assertEqual(status, 400)
        self.assertIn("valid 'ip'", res["message"])

    def test_probe_and_discovered(self) -> None:
        status, node = self.request("POST", "/api/config/probe", {"ip": "10.0.0.7"})
        self.assertEqual(status, 200, node)
        self.assertEqual(node["info"]["mac"], "aabbccddeeff")
        status, disc = self.request("GET", "/api/config/discovered")
        self.assertTrue(disc["enabled"])
        self.assertIn("10.0.0.7", [n["ip"] for n in disc["nodes"]])

        status, disc = self.request("POST", "/api/config/rescan", {})
        self.assertEqual(status, 200)

        status, res = self.request("POST", "/api/config/probe", {"ip": "not-an-ip"})
        self.assertEqual(status, 400)

    def test_config_page_is_served(self) -> None:
        with urllib.request.urlopen(self.base + "/config", timeout=3) as resp:
            self.assertIn(b"Lighting setup", resp.read())
        with urllib.request.urlopen(self.base + "/js/config.js", timeout=3) as resp:
            self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()


class TestPatchEditing(unittest.TestCase):
    """patch.json validation/storage and the combined /api/config endpoints."""

    FIXTURES = {"truss": 600, "lamp": 1}

    def seg(self, **kw):
        base = {"fixture_id": "truss", "pixel_start": 0, "pixel_count": 270, "controller_id": "wled_01", "channel_index": 0}
        return {**base, **kw}

    def test_validate_normalizes_and_trims_channel_lengths(self) -> None:
        from wled_sequencer.controller_config import validate_patch

        out = validate_patch({"name": "P", "segments": [self.seg()], "channel_lengths": {"wled_01": [270, 270, 0, 0]}}, self.FIXTURES)
        self.assertEqual(out["channel_lengths"], {"wled_01": [270, 270]})
        self.assertEqual(out["segments"][0]["color_order"], "GRB")
        self.assertEqual(out["segments"][0]["port_offset"], 0)

    def test_validate_rejects_mapping_errors(self) -> None:
        from wled_sequencer.controller_config import validate_patch

        cases = {
            "mapped to both": [self.seg(), self.seg(fixture_id="lamp", pixel_count=1, port_offset=100)],
            "past the fixture": [self.seg(pixel_start=500, pixel_count=200)],
            "isn't in universe": [self.seg(fixture_id="nope")],
            "channel has 270": [self.seg(port_offset=10)],
            "color order": [self.seg(color_order="XYZ")],
        }
        for needle, segs in cases.items():
            with self.assertRaises(ConfigError, msg=needle) as cm:
                validate_patch({"segments": segs, "channel_lengths": {"wled_01": [270]}}, self.FIXTURES)
            self.assertIn(needle, str(cm.exception))

    def test_channel_layout_honours_declared_lengths(self) -> None:
        from wled_sequencer.controller_config import channel_layout

        layout = channel_layout({
            "segments": [self.seg(pixel_count=100), self.seg(channel_index=2, pixel_count=50)],
            "channel_lengths": {"wled_01": [270, 270, 270]},
        })
        self.assertEqual(layout["wled_01"]["channels"], [270, 270, 270])
        self.assertEqual(layout["wled_01"]["total_leds"], 810)

    def test_store_keeps_unmanaged_keys(self) -> None:
        from wled_sequencer.controller_config import PatchStore

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "patch.json"
            path.write_text(json.dumps({"name": "P", "segments": [], "controllers": {"wled_01": "10.0.0.1"}}))
            PatchStore(path).save({"name": "P", "segments": [self.seg()], "controllers": {"wled_01": "10.0.0.1"}})
            data = json.loads(path.read_text())
            self.assertEqual(data["controllers"], {"wled_01": "10.0.0.1"})
            self.assertEqual(len(data["segments"]), 1)

    def test_combined_api(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "universe.json").write_text(json.dumps({"fixtures": [
                {"type": "linear_strip", "id": "truss", "name": "Truss", "pixel_count": 600, "group": "g",
                 "waypoints": [{"x": 0, "y": 0, "z": 0}, {"x": 1, "y": 0, "z": 0}]},
            ]}))
            (d / "patch.json").write_text(json.dumps({"name": "P", "segments": []}))
            app = SequencerAppServer(
                port=0, sequences_dir=d / "seq", controllers_file=d / "controllers.json",
                patch_file=d / "patch.json", universe_file=d / "universe.json", engine_url="http://127.0.0.1:9",
            )
            app.start()
            try:
                def call(method, body=None):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{app.port}/api/config", method=method,
                        data=json.dumps(body).encode() if body is not None else None,
                        headers={"Content-Type": "application/json"},
                    )
                    try:
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            return resp.status, json.loads(resp.read())
                    except urllib.error.HTTPError as e:
                        return e.code, json.loads(e.read())

                status, cfg = call("GET")
                self.assertEqual(status, 200)
                self.assertEqual(cfg["universe"]["fixtures"][0], {"id": "truss", "name": "Truss", "type": "linear_strip", "group": "g", "pixel_count": 600, "tags": []})
                self.assertTrue(cfg["patch"]["writable"])

                # An invalid patch writes neither file.
                status, res = call("PUT", {"controllers": {"wled_01": {"ip": "10.0.0.1"}},
                                           "patch": {"segments": [self.seg(pixel_count=700)]}})
                self.assertEqual(status, 400, res)
                self.assertFalse((d / "controllers.json").exists())

                status, res = call("PUT", {"controllers": {"wled_01": {"ip": "10.0.0.1"}},
                                           "patch": {"name": "P", "segments": [self.seg()], "channel_lengths": {"wled_01": [270]}}})
                self.assertEqual(status, 200, res)
                self.assertFalse(res["reload"]["ok"])  # no daemon in this test
                self.assertEqual(json.loads((d / "patch.json").read_text())["channel_lengths"], {"wled_01": [270]})
                self.assertIn("wled_01", json.loads((d / "controllers.json").read_text()))
            finally:
                app.stop()
