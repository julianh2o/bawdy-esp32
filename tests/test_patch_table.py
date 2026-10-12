"""Unit tests for PatchSegment, color permutations, and PatchTable routing."""

from __future__ import annotations
import tempfile
import unittest
from pathlib import Path

from wled_engine.patch.target import (
    PatchSegment,
    ColorOrder,
    OutputProtocol,
    permute_color,
)
from wled_engine.patch.patch_table import PatchTable


class TestPatchTarget(unittest.TestCase):
    def test_permute_color_orders(self) -> None:
        r, g, b = 255, 128, 64
        # GRB (WS2812B standard)
        self.assertEqual(permute_color(r, g, b, ColorOrder.GRB), (128, 255, 64))
        # RGB (WS2811 standard)
        self.assertEqual(permute_color(r, g, b, ColorOrder.RGB), (255, 128, 64))
        # BGR
        self.assertEqual(permute_color(r, g, b, ColorOrder.BGR), (64, 128, 255))
        # RGBW (SK6812)
        self.assertEqual(permute_color(r, g, b, ColorOrder.RGBW, w=200), (255, 128, 64, 200))
        # GRBW
        self.assertEqual(permute_color(r, g, b, ColorOrder.GRBW, w=200), (128, 255, 64, 200))

    def test_patch_segment_bounds_and_serialization(self) -> None:
        seg = PatchSegment(
            fixture_id="roof_arch",
            pixel_start=100,
            pixel_count=150,
            controller_id="wled_01",
            channel_index=2,
            port_offset=50,
            reversed=True,
            color_order=ColorOrder.GRB,
            protocol=OutputProtocol.DDP,
        )
        self.assertEqual(seg.pixel_end, 250)
        self.assertEqual(seg.port_end, 200)

        d = seg.to_dict()
        restored = PatchSegment.from_dict(d)
        self.assertEqual(restored.fixture_id, "roof_arch")
        self.assertEqual(restored.pixel_count, 150)
        self.assertTrue(restored.reversed)
        self.assertEqual(restored.color_order, ColorOrder.GRB)


class TestPatchTable(unittest.TestCase):
    def test_patch_table_routing(self) -> None:
        table = PatchTable("House Patch")

        # Full fixture on Board 1, Port 0
        table.patch_fixture("garage_door", 270, controller_id="wled_01", channel_index=0)

        # Long fixture (600 LEDs) split across Board 1 Port 1 and Board 2 Port 0
        table.patch_segment("long_roof", pixel_start=0, pixel_count=300, controller_id="wled_01", channel_index=1)
        table.patch_segment("long_roof", pixel_start=300, pixel_count=300, controller_id="wled_02", channel_index=0)

        self.assertEqual(table.get_controllers(), ["wled_01", "wled_02"])
        self.assertEqual(len(table.get_segments_for_controller("wled_01")), 2)
        self.assertEqual(len(table.get_segments_for_controller("wled_02")), 1)

        self.assertEqual(table.get_channel_pixel_length("wled_01", 0), 270)
        self.assertEqual(table.get_channel_pixel_length("wled_01", 1), 300)
        self.assertEqual(table.get_channel_pixel_length("wled_02", 0), 300)

        # Validation should pass cleanly
        errors = table.validate()
        self.assertEqual(errors, [])

    def test_patch_collision_detection(self) -> None:
        table = PatchTable("Collision Test")
        # Overlapping port ranges on wled_01 Channel 0!
        table.patch_fixture("strip_a", 100, controller_id="wled_01", channel_index=0, port_offset=0)
        table.patch_fixture("strip_b", 100, controller_id="wled_01", channel_index=0, port_offset=50)

        errors = table.validate()
        self.assertEqual(len(errors), 1)
        self.assertIn("Port Collision on Controller 'wled_01' Channel 0", errors[0])

    def test_patch_table_json_persistence(self) -> None:
        table = PatchTable("Production Patch")
        table.patch_fixture("strip_1", 150, "controller_1", channel_index=0)
        table.patch_fixture("matrix_1", 256, "controller_2", channel_index=1, color_order=ColorOrder.RGB)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "patch.json"
            table.save_json(path)
            loaded = PatchTable.load_json(path)

            self.assertEqual(loaded.name, "Production Patch")
            self.assertEqual(len(loaded.segments), 2)
            self.assertEqual(loaded.get_controllers(), ["controller_1", "controller_2"])

    def test_unpack_controller_buffers_to_fixtures(self) -> None:
        table = PatchTable("Unpack Test")
        # Fixture 1: 2 LEDs on wled_01 Ch0 (GRB)
        # Fixture 2: 2 LEDs on wled_01 Ch1 (RGB, reversed)
        table.patch_fixture("fix_grb", 2, "wled_01", channel_index=0, color_order=ColorOrder.GRB)
        table.patch_fixture("fix_rev", 2, "wled_01", channel_index=1, color_order=ColorOrder.RGB, reversed=True)

        # Build simulated controller buffers:
        # Ch0: LED 0 = Red (GRB: 0, 255, 0), LED 1 = Blue (GRB: 0, 0, 255)
        # Ch1: LED 0 = Yellow (RGB: 255, 255, 0), LED 1 = Cyan (RGB: 0, 255, 255)
        ch0_bytes = bytearray([0, 255, 0, 0, 0, 255])
        ch1_bytes = bytearray([255, 255, 0, 0, 255, 255])

        buffers = {"wled_01": [ch0_bytes, ch1_bytes]}
        unpacked = table.unpack_controller_buffers_to_fixtures(buffers)

        # fix_grb should unpermute to: Red [255, 0, 0], Blue [0, 0, 255]
        self.assertEqual(list(unpacked["fix_grb"]), [255, 0, 0, 0, 0, 255])

        # fix_rev was reversed: so controller LED 0 (Yellow) is at fixture pixel 1,
        # and controller LED 1 (Cyan) is at fixture pixel 0!
        self.assertEqual(list(unpacked["fix_rev"]), [0, 255, 255, 255, 255, 0])


if __name__ == "__main__":
    unittest.main()



class TestDeclaredChannelLengths(unittest.TestCase):
    """channel_lengths keeps each channel's data on its physical WLED output."""

    def make(self) -> "PatchTable":
        from wled_engine.patch.patch_table import PatchTable

        table = PatchTable.from_dict({
            "segments": [
                {"fixture_id": "a", "pixel_count": 100, "controller_id": "c", "channel_index": 0},
                {"fixture_id": "b", "pixel_count": 10, "controller_id": "c", "channel_index": 1},
            ],
            "channel_lengths": {"c": [270, 270]},
        })
        return table

    def test_partly_mapped_channel_keeps_declared_length(self) -> None:
        table = self.make()
        self.assertEqual(table.get_channel_pixel_length("c", 0), 270)
        self.assertEqual(table.to_dict()["channel_lengths"], {"c": [270, 270]})

    def test_sampler_places_second_channel_at_declared_offset(self) -> None:
        from wled_app.domain.color import Color
        from wled_engine.spatial.fixtures import LinearStripFixture
        from wled_engine.spatial.coordinates import Point3D
        from wled_engine.spatial.sampler import SpatialSampler
        from wled_engine.spatial.universe import SpatialUniverse

        uni = SpatialUniverse()
        for fid, n in (("a", 100), ("b", 10)):
            uni.add_fixture(LinearStripFixture(fid, fid, n, [Point3D(0, 0, 0), Point3D(1, 0, 0)]))
        bufs = SpatialSampler(uni, self.make()).route_fixture_colors_to_controllers(
            {"a": [Color(0, 0, 0)] * 100, "b": [Color(255, 255, 255)] * 10}
        )
        self.assertEqual([len(b) for b in bufs["c"]], [270 * 3, 270 * 3])

    def test_validate_reports_overflow(self) -> None:
        table = self.make()
        table.channel_lengths["c"] = [50, 270]
        self.assertTrue(any("Channel Overflow" in e for e in table.validate()))
