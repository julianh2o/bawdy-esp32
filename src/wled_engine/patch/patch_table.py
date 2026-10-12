"""PatchTable: Maps universe fixtures and segments to physical hardware controller ports."""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from wled_engine.patch.target import (
    PatchSegment,
    ColorOrder,
    OutputProtocol,
    unpermute_color,
)


class PatchTable:
    """Registry managing the mapping between logical spatial fixtures and physical controller channels."""

    def __init__(self, name: str = "Default Patch") -> None:
        self.name = name
        self._segments: list[PatchSegment] = []
        # Declared per-channel LED counts, e.g. {"wled_01": [270, 270, 270]}, matching the
        # board's physical outputs. A channel is never shorter than its furthest segment.
        self.channel_lengths: dict[str, list[int]] = {}

    def add_segment(self, segment: PatchSegment) -> None:
        """Add a patch segment to the table."""
        self._segments.append(segment)

    def patch_fixture(
        self,
        fixture_id: str,
        pixel_count: int,
        controller_id: str,
        channel_index: int = 0,
        port_offset: int = 0,
        reversed: bool = False,
        color_order: ColorOrder = ColorOrder.GRB,
        protocol: OutputProtocol = OutputProtocol.DDP,
        brightness_scale: float = 1.0,
    ) -> PatchSegment:
        """Convenience method to patch an entire fixture to a physical port."""
        seg = PatchSegment(
            fixture_id=fixture_id,
            pixel_start=0,
            pixel_count=pixel_count,
            controller_id=controller_id,
            channel_index=channel_index,
            port_offset=port_offset,
            reversed=reversed,
            color_order=color_order,
            protocol=protocol,
            brightness_scale=brightness_scale,
        )
        self.add_segment(seg)
        return seg

    def patch_segment(
        self,
        fixture_id: str,
        pixel_start: int,
        pixel_count: int,
        controller_id: str,
        channel_index: int = 0,
        port_offset: int = 0,
        reversed: bool = False,
        color_order: ColorOrder = ColorOrder.GRB,
        protocol: OutputProtocol = OutputProtocol.DDP,
        brightness_scale: float = 1.0,
    ) -> PatchSegment:
        """Patch a sub-segment of a fixture (e.g. for long strips split across boards)."""
        seg = PatchSegment(
            fixture_id=fixture_id,
            pixel_start=pixel_start,
            pixel_count=pixel_count,
            controller_id=controller_id,
            channel_index=channel_index,
            port_offset=port_offset,
            reversed=reversed,
            color_order=color_order,
            protocol=protocol,
            brightness_scale=brightness_scale,
        )
        self.add_segment(seg)
        return seg

    @property
    def segments(self) -> list[PatchSegment]:
        return list(self._segments)

    def clear(self) -> None:
        self._segments.clear()

    def get_controllers(self) -> list[str]:
        """Return sorted list of all unique controller IDs referenced in patch."""
        return sorted(list({s.controller_id for s in self._segments}))

    def get_segments_for_controller(self, controller_id: str) -> list[PatchSegment]:
        """Return all segments routed to a specific controller."""
        return [s for s in self._segments if s.controller_id == controller_id]

    def get_segments_for_fixture(self, fixture_id: str) -> list[PatchSegment]:
        """Return all segments assigned to a specific fixture ID."""
        return [s for s in self._segments if s.fixture_id == fixture_id]

    def get_channel_indices(self, controller_id: str) -> list[int]:
        """Return sorted list of distinct channel indices patched on a controller."""
        return sorted(list({s.channel_index for s in self.get_segments_for_controller(controller_id)}))

    def get_channel_pixel_length(self, controller_id: str, channel_index: int) -> int:
        """Pixel length of a controller channel: its declared length, or its furthest segment if longer.

        Channels are packed back to back into one DDP stream, so this is also what puts each
        channel's data at the right offset. Without a declared length, a partly-mapped channel
        shrinks and shifts every later channel off its physical output.
        """
        declared = self.channel_lengths.get(controller_id, [])
        length = declared[channel_index] if channel_index < len(declared) else 0
        segs = [s for s in self._segments if s.controller_id == controller_id and s.channel_index == channel_index]
        return max([length] + [s.port_end for s in segs])

    def validate(self) -> list[str]:
        """Validate patch table for hardware collisions and overlap errors.

        Returns a list of error warning strings. Empty list indicates clean patch.
        """
        errors: list[str] = []

        # Check for port collisions: two segments on same (controller, channel) sharing offset range
        port_buckets: dict[tuple[str, int], list[PatchSegment]] = {}
        for s in self._segments:
            key = (s.controller_id, s.channel_index)
            port_buckets.setdefault(key, []).append(s)

        for (cid, ch_idx), segs in port_buckets.items():
            for i in range(len(segs)):
                for j in range(i + 1, len(segs)):
                    s1, s2 = segs[i], segs[j]
                    # Overlap check
                    if max(s1.port_offset, s2.port_offset) < min(s1.port_end, s2.port_end):
                        errors.append(
                            f"Port Collision on Controller '{cid}' Channel {ch_idx}: "
                            f"Fixture '{s1.fixture_id}' [{s1.port_offset}..{s1.port_end}] overlaps with "
                            f"Fixture '{s2.fixture_id}' [{s2.port_offset}..{s2.port_end}]"
                        )

        for s in self._segments:
            declared = self.channel_lengths.get(s.controller_id, [])
            if s.channel_index < len(declared) and declared[s.channel_index] and s.port_end > declared[s.channel_index]:
                errors.append(
                    f"Channel Overflow on Controller '{s.controller_id}' Channel {s.channel_index}: "
                    f"Fixture '{s.fixture_id}' ends at {s.port_end} but the channel has {declared[s.channel_index]} LEDs"
                )
        return errors

    def unpack_controller_buffers_to_fixtures(
        self,
        controller_buffers: dict[str, Any],
        fixture_pixel_counts: dict[str, int] | None = None,
        channel_offsets: dict[tuple[str, int], int] | None = None,
    ) -> dict[str, bytearray]:
        """Unpack raw controller wire byte buffers back into fixture RGB bytearrays.

        Args:
            controller_buffers: Map of controller_id -> bytes/bytearray (flat)
                                or controller_id -> list[bytes/bytearray] (per-channel).
            fixture_pixel_counts: Optional fixture_id -> total_pixel_count map to pre-allocate buffers.
            channel_offsets: Optional map of (controller_id, channel_index) -> base_led_offset
                             for flat controller buffers.

        Returns:
            dict mapping fixture_id -> bytearray of length (pixel_count * 3) in RGB order.
        """
        fixture_bytes: dict[str, bytearray] = {}
        if fixture_pixel_counts:
            for fid, count in fixture_pixel_counts.items():
                fixture_bytes[fid] = bytearray(count * 3)

        for seg in self._segments:
            cid = seg.controller_id
            if cid not in controller_buffers:
                continue

            raw = controller_buffers[cid]
            bpp = 4 if seg.color_order in (ColorOrder.RGBW, ColorOrder.GRBW) else 3

            if isinstance(raw, (list, tuple)):
                if seg.channel_index >= len(raw):
                    continue
                ch_buf = raw[seg.channel_index]
                src_base_byte = seg.port_offset * bpp
            else:
                ch_buf = raw
                if channel_offsets and (cid, seg.channel_index) in channel_offsets:
                    base_led = channel_offsets[(cid, seg.channel_index)]
                else:
                    base_led = sum(self.get_channel_pixel_length(cid, ch) for ch in range(seg.channel_index))
                src_base_byte = (base_led + seg.port_offset) * bpp

            if seg.fixture_id not in fixture_bytes:
                max_px = seg.pixel_end
                fixture_bytes[seg.fixture_id] = bytearray(max_px * 3)
            else:
                needed = seg.pixel_end * 3
                if len(fixture_bytes[seg.fixture_id]) < needed:
                    fixture_bytes[seg.fixture_id].extend(b"\x00" * (needed - len(fixture_bytes[seg.fixture_id])))

            dest_buf = fixture_bytes[seg.fixture_id]

            for i in range(seg.pixel_count):
                src_off = src_base_byte + i * bpp
                if src_off + bpp > len(ch_buf):
                    break

                c0 = ch_buf[src_off]
                c1 = ch_buf[src_off + 1]
                c2 = ch_buf[src_off + 2]
                c3 = ch_buf[src_off + 3] if bpp == 4 else 0

                r, g, b = unpermute_color(c0, c1, c2, c3, order=seg.color_order)

                px_idx = (seg.pixel_end - 1 - i) if seg.reversed else (seg.pixel_start + i)
                dst_off = px_idx * 3
                if dst_off + 3 <= len(dest_buf):
                    dest_buf[dst_off] = r
                    dest_buf[dst_off + 1] = g
                    dest_buf[dst_off + 2] = b

        return fixture_bytes

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "segments": [s.to_dict() for s in self._segments],
        }
        if self.channel_lengths:
            data["channel_lengths"] = {cid: list(lengths) for cid, lengths in self.channel_lengths.items()}
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PatchTable:
        table = cls(name=data.get("name", "Default Patch"))
        for s_data in data.get("segments", []):
            table.add_segment(PatchSegment.from_dict(s_data))
        for cid, lengths in (data.get("channel_lengths") or {}).items():
            table.channel_lengths[str(cid)] = [max(0, int(n)) for n in lengths]
        return table

    def save_json(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load_json(cls, path: Path | str) -> PatchTable:
        p = Path(path)
        with open(p, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
