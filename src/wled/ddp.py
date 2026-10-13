"""DDP (Distributed Display Protocol) packets, as WLED receives them on UDP 4048.

Header (10 bytes): flags, sequence, data type, destination id, data offset (bytes, uint32),
data length (uint16). WLED reads RGB (3 bytes/pixel) for data type 0x01 and RGBW (4 bytes/pixel) for
0x1A, and places the data at ``offset / bytes per pixel`` in its LED index space; the push flag on the
last packet shows the frame.
"""

from __future__ import annotations
import struct

DDP_PORT = 4048
MAX_DATA_PER_PACKET = 1440  # 480 RGB or 360 RGBW pixels; a whole number of either

FLAG_VERSION_1 = 0x40
FLAG_PUSH = 0x01
DATA_TYPE_RGB8 = 0x01
DATA_TYPE_RGBW8 = 0x1A  # WLED takes 0x1A and 0x1B as RGBW
DEST_DEFAULT = 0x01


def ddp_packets(data: bytes, seq: int = 1, offset: int = 0, push: bool = True, rgbw: bool = False) -> list[bytes]:
    """Split RGB (or RGBW) ``data`` (starting at byte ``offset``) into DDP datagrams; push flag on the last."""
    dtype = DATA_TYPE_RGBW8 if rgbw else DATA_TYPE_RGB8
    if not data:
        return []
    packets = []
    for pos in range(0, len(data), MAX_DATA_PER_PACKET):
        chunk = data[pos:pos + MAX_DATA_PER_PACKET]
        last = pos + MAX_DATA_PER_PACKET >= len(data)
        flags = FLAG_VERSION_1 | (FLAG_PUSH if push and last else 0)
        header = struct.pack(">BBBBIH", flags, seq & 0x0F, dtype, DEST_DEFAULT, offset + pos, len(chunk))
        packets.append(header + chunk)
    return packets


def parse_header(packet: bytes) -> dict[str, int | bool]:
    """Decode a DDP header (for tests and debugging)."""
    flags, seq, dtype, dest, offset, length = struct.unpack(">BBBBIH", packet[:10])
    return {"push": bool(flags & FLAG_PUSH), "seq": seq, "type": dtype, "dest": dest, "offset": offset, "length": length}
