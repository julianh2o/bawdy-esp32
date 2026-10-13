"""Tests for per-controller network config, the /engine proxy, and shutdown blackout."""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from wled_app.network.udp_client import MockUdpClient
from wled_engine.adapters.rest_daemon import EngineDaemon
from wled_engine.controllers import load_controllers, parse_controllers
from wled_engine.core import LightingEngine
from wled_engine.spatial.venue import create_demo_venue
from wled_sequencer.server import SequencerAppServer


class TestControllerConfig(unittest.TestCase):
    def test_parse_defaults_port_to_4048(self) -> None:
        ips, ports = parse_controllers({"wled_01": {"ip": "192.168.1.50"}, "wled_02": {"ip": "10.0.0.2", "port": 5000}})
        self.assertEqual(ips, {"wled_01": "192.168.1.50", "wled_02": "10.0.0.2"})
        self.assertEqual(ports, {"wled_01": 4048, "wled_02": 5000})

    def test_parse_requires_ip(self) -> None:
        with self.assertRaises(ValueError):
            parse_controllers({"wled_01": {"port": 4048}})

    def test_file_overrides_patch_section(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            patch = Path(d) / "patch.json"
            patch.write_text(json.dumps({"controllers": {"a": {"ip": "1.1.1.1"}, "b": {"ip": "2.2.2.2"}}}))
            ctl = Path(d) / "controllers.json"
            ctl.write_text(json.dumps({"b": {"ip": "3.3.3.3", "port": 4049}}))
            ips, ports = load_controllers(ctl, patch)
        self.assertEqual(ips, {"a": "1.1.1.1", "b": "3.3.3.3"})
        self.assertEqual(ports, {"a": 4048, "b": 4049})

    def test_real_hardware_uses_port_4048_for_every_board(self) -> None:
        universe, patch = create_demo_venue()
        ids = patch.get_controllers()
        ips = {cid: f"192.168.1.{100 + i}" for i, cid in enumerate(ids)}
        engine = LightingEngine(dry_run=True)
        engine.setup_universe(universe, patch, broadcast_ip="192.168.1.255", controller_ips=ips, controller_ports={}, simulator=False)
        self.assertEqual(engine.dispatcher.broadcast_ip, "192.168.1.255")
        for i, node in enumerate(engine.dispatcher.nodes):
            self.assertEqual((node.ip, node.port), (f"192.168.1.{100 + i}", 4048))

    def test_missing_ip_without_simulator_raises(self) -> None:
        universe, patch = create_demo_venue()
        engine = LightingEngine(dry_run=True)
        with self.assertRaises(ValueError):
            engine.setup_universe(universe, patch, controller_ips={}, simulator=False)

    def test_simulator_layout_unchanged(self) -> None:
        universe, patch = create_demo_venue()
        engine = LightingEngine(dry_run=True)
        engine.setup_universe(universe, patch)
        for i, node in enumerate(engine.dispatcher.nodes):
            self.assertEqual((node.ip, node.port), ("127.0.0.1", 4048 + i))

    def test_stop_sends_blackout_before_closing(self) -> None:
        universe, patch = create_demo_venue()
        engine = LightingEngine(dry_run=True)
        engine.setup_universe(universe, patch)
        sender = MockUdpClient()
        engine.dispatcher._sender = sender
        engine.dispatcher._owns_sender = False
        engine.start()
        engine.stop()
        self.assertEqual(engine.dispatcher.nodes, [])  # closed after the blackout
        self.assertTrue(sender.sent_packets)


class TestEngineProxy(unittest.TestCase):
    def test_web_server_proxies_engine_requests(self) -> None:
        engine = LightingEngine(dry_run=True)
        daemon = EngineDaemon(engine=engine, host="127.0.0.1", port=0)
        daemon.start()
        with tempfile.TemporaryDirectory() as d:
            app = SequencerAppServer(
                host="127.0.0.1", port=0, engine_url=f"http://127.0.0.1:{daemon.port}"
            )
            app.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{app.port}/engine/api/status", timeout=5) as resp:
                    data = json.load(resp)
                self.assertEqual(data["status"], "ok")
            finally:
                app.stop()
                daemon.stop()

    def test_proxy_reports_502_when_engine_down(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            app = SequencerAppServer(host="127.0.0.1", port=0, engine_url="http://127.0.0.1:1")
            app.start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(f"http://127.0.0.1:{app.port}/engine/api/status", timeout=5)
                self.assertEqual(ctx.exception.code, 502)
            finally:
                app.stop()


if __name__ == "__main__":
    unittest.main()
