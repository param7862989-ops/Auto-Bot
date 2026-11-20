"""A minimal PNG encoder for 8-bit RGB images, using only ``zlib``."""

from __future__ import annotations

import struct
import zlib


def bgra_to_rgb(bgra: bytes, width: int, height: int) -> bytes:
    """Drop the alpha channel and reorder blue-green-red to red-green-blue."""
    pixels = width * height
    if len(bgra) != pixels * 4:
        raise ValueError("pixel data does not match the size")
    rgb = bytearray(pixels * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    return bytes(rgb)


def encode_png(rgb: bytes, width: int, height: int, *, level: int = 6) -> bytes:
    """Encode RGB rows (top to bottom) as a PNG with no filtering."""
    if width <= 0 or height <= 0 or len(rgb) != width * height * 3:
        raise ValueError("pixel data does not match the size")
    stride = width * 3
    raw = bytearray()
    for row in range(height):
        raw.append(0)
        raw += rgb[row * stride : (row + 1) * stride]

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), level))
        + chunk(b"IEND", b"")
    )
