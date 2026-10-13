"""Tests for the low-level wled package (no network: fake HTTP and in-memory UDP)."""

import os
import socket
import time
import unittest

from wled import FakeTransport, WledBrowser, WledController, WledError, ddp_packets
from wled.ddp import parse_header
from wled.models import channels_from_cfg, info_from_json, normalize_mac

INFO = {"name": "Stage left", "mac": "68fe71a4adb4", "ver": "0.15.0", "arch": "esp32", "leds": {"count": 810}}
CFG = {"hw": {"led": {"ins": [
    {"start": 270, "len": 270, "pin": [4], "order": 1, "type": 22},
    {"start": 0, "len": 270, "pin": [5], "order": 0, "type": 22},
    {"start": 540, "len": 270, "pin": [12], "order": 0, "type": 30},
]}}}


class FakeHttp:
    def __init__(self, info=INFO, cfg=CFG, fail=False):
        self.host = "fake"
        self._info, self._cfg, self.fail = info, cfg, fail
        self.posted = []

    def info(self):
        if self.fail:
            raise WledError("unreachable")
        return self._info

    def cfg(self):
        if self.fail:
            raise WledError("unreachable")
        return self._cfg

    def state(self):
        return {"on": True}

    def set_state(self, **state):
        self.posted.append(state)
        return {"success": True}


def payloads(sent):
    """Reassemble a frame from DDP packets: bytes placed at their offsets."""
    out = bytearray()
    for _host, _port, pkt in sent:
        h = parse_header(pkt)
        end = h["offset"] + h["length"]
        if len(out) < end:
            out.extend(bytes(end - len(out)))
        out[h["offset"]:end] = pkt[10:]
    return bytes(out)


class TestDdp(unittest.TestCase):
    def test_splits_at_480_pixels_and_pushes_last(self):
        pkts = ddp_packets(bytes(600 * 3), seq=3, offset=30)
        self.assertEqual(len(pkts), 2)
        first, last = parse_header(pkts[0]), parse_header(pkts[1])
        self.assertEqual((first["offset"], first["length"], first["push"]), (30, 1440, False))
        self.assertEqual((last["offset"], last["length"], last["push"]), (30 + 1440, 360, True))
        self.assertEqual(first["seq"], 3)
        self.assertEqual(ddp_packets(b""), [])


class TestModels(unittest.TestCase):
    def test_channels_sorted_with_orders_and_rgbw(self):
        chans = channels_from_cfg(CFG)
        self.assertEqual([(c.index, c.start, c.length) for c in chans], [(0, 0, 270), (1, 270, 270), (2, 540, 270)])
        self.assertEqual([c.order for c in chans], ["GRB", "RGB", "GRB"])
        self.assertEqual([c.rgbw for c in chans], [False, False, True])
        self.assertEqual(chans[0].pins, (5,))
        self.assertEqual(channels_from_cfg({}), [])

    def test_info_and_mac(self):
        info = info_from_json(INFO)
        self.assertEqual((info.name, info.led_count), ("Stage left", 810))
        self.assertEqual(normalize_mac("68:FE:71:A4:AD:B4"), "68fe71a4adb4")


class TestController(unittest.TestCase):
    def setUp(self):
        self.tx = FakeTransport()
        self.board = WledController("10.0.0.5", transport=self.tx, http=FakeHttp())

    def test_refresh_loads_channels(self):
        self.board.refresh()
        self.assertEqual(len(self.board.channels), 3)
        self.assertEqual(self.board.pixel_count, 810)
        self.assertEqual(self.board.info.name, "Stage left")

    def test_refresh_failure(self):
        board = WledController("10.0.0.6", transport=self.tx, http=FakeHttp(fail=True))
        with self.assertRaises(WledError):
            board.refresh()
        self.assertIsNone(board.channels)
        self.assertIn("unreachable", board.last_error)

    def test_send_channels_places_each_channel_at_its_start(self):
        with self.assertRaises(WledError):
            self.board.send_channels({0: b"\x01\x02\x03"})
        self.board.refresh()
        self.board.send_channels({1: b"\xff\x00\x00" * 300, 2: b"\x00\x00\x09"})  # ch 2 too long -> cut
        frame = payloads(self.tx.sent)
        self.assertEqual(len(frame), 810 * 3)
        self.assertEqual(frame[:270 * 3], bytes(270 * 3))  # idle channel is black
        self.assertEqual(frame[270 * 3:540 * 3], b"\xff\x00\x00" * 270)
        self.assertEqual(frame[540 * 3:540 * 3 + 3], b"\x00\x00\x09")
        self.assertTrue(parse_header(self.tx.sent[-1][2])["push"])
        self.assertTrue(all(h == "10.0.0.5" and p == 4048 for h, p, _ in self.tx.sent))

    def test_identify_channel_blinks_only_that_range_and_mutes_frames(self):
        self.board.refresh()
        self.board.identify(channel=1, seconds=0.5)
        self.assertTrue(self.board.identifying)
        time.sleep(0.05)
        self.tx.clear()
        self.board.send_frame(b"\x07" * 30)  # ignored while blinking
        time.sleep(0.6)
        self.assertFalse(self.board.identifying)
        frames = [p for _, _, p in self.tx.sent]
        self.assertNotIn(b"\x07" * 30, [f[10:] for f in frames])
        lit = next(f for f in frames if any(f[10:]))
        data = lit[10:]
        self.assertEqual(data[:270 * 3], bytes(270 * 3))
        self.assertEqual(set(data[270 * 3:]), {200})
        self.assertEqual(frames[-1][10:], bytes(len(frames[-1]) - 10))  # ends black

    def test_host_change_follows_into_http(self):
        self.board.host = "10.0.0.9"
        self.assertEqual(self.board.http.host, "10.0.0.9")

    def test_release(self):
        self.board.release()
        self.assertEqual(self.board.http.posted, [{"live": False}])


class TestBrowser(unittest.TestCase):
    def test_mdns_records_probe_and_merge(self):
        seen = []
        browser = WledBrowser(on_change=seen.append, prober=lambda ip: {"name": "B", "mac": "aa", "channels": []})
        manual = browser.probe("10.0.0.7")
        self.assertEqual(manual["source"], "manual")
        self.assertTrue(manual["online"])
        browser.record_service("wled-x._wled._tcp.local.", ip="10.0.0.7", hostname="wled-x.local")
        time.sleep(0.1)
        boards = browser.boards()
        self.assertEqual(len(boards), 1)  # manual entry merged into the mDNS one
        self.assertEqual(boards[0]["instance"], "wled-x")
        self.assertEqual(boards[0]["info"]["name"], "B")
        browser.mark_gone("wled-x._wled._tcp.local.")
        self.assertFalse(browser.boards()[0]["online"])
        self.assertTrue(seen)
        with self.assertRaises(ValueError):
            browser.probe("not-an-ip")

    def test_ip_change_reprobes_and_service_is_probed_once(self):
        probed = []
        browser = WledBrowser(prober=lambda ip: probed.append(ip) or {"mac": "aa"})
        name = "wled-truss._wled._tcp.local."
        browser.record_service(name, ip="10.0.0.5")
        self.assertTrue(wait_for(lambda: probed == ["10.0.0.5"]))
        browser.record_service(name, ip="10.0.0.5")  # same IP, already probed
        browser.record_service(name, ip="10.0.0.6")
        self.assertTrue(wait_for(lambda: probed == ["10.0.0.5", "10.0.0.6"]))
        self.assertEqual([b["ip"] for b in browser.boards()], ["10.0.0.6"])

    def test_failed_probe_is_offline(self):
        def fail(ip):
            raise WledError("no answer")

        board = WledBrowser(prober=fail).probe("10.0.0.66")
        self.assertIsNone(board["info"])
        self.assertFalse(board["online"])
        self.assertEqual(board["probe_error"], "no answer")


def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class TestMdns(unittest.TestCase):
    """Round trip over real multicast: advertise a fake WLED board, then browse for it."""

    def test_finds_advertised_board(self):
        from zeroconf import IPVersion, ServiceInfo, Zeroconf

        stype = f"_wledtest{os.getpid() % 10000}._tcp.local."  # private type: real boards don't interfere
        info = ServiceInfo(
            stype, f"wled-fake.{stype}", port=80, addresses=[socket.inet_aton("127.0.0.1")],
            properties={"mac": "aabbccddeeff"}, server="wled-fake.local.",
        )
        publisher = Zeroconf(ip_version=IPVersion.V4Only)
        browser = WledBrowser(prober=lambda ip: {"name": "Fake", "mac": "aabbccddeeff", "channels": []}, service_type=stype)
        try:
            publisher.register_service(info)
            browser.start()
            self.assertTrue(browser.listening, browser.error)
            if not wait_for(lambda: any(b.get("info") for b in browser.boards()), timeout=10.0):
                self.skipTest("no multicast loopback on this host")
            board = browser.boards()[0]
            self.assertEqual((board["instance"], board["hostname"], board["ip"], board["mac"]),
                             ("wled-fake", "wled-fake.local", "127.0.0.1", "aabbccddeeff"))
            publisher.unregister_service(info)
            self.assertTrue(wait_for(lambda: not browser.boards()[0]["online"], timeout=5.0))
        finally:
            browser.stop()
            publisher.close()


if __name__ == "__main__":
    unittest.main()
