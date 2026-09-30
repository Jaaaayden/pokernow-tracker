"""Draw the extension's icons: a poker chip with a bar chart on it.

    python scripts/make_icons.py     # -> pnt/extension/icons/icon{16,32,48,128}.png

Plain Python, so the icons can be redrawn anywhere this repo runs: shapes are
described as functions of a point in the unit square, each pixel is sampled 4x4
for smooth edges, and the PNG is written with zlib. The colours are the side
panel's own.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "pnt" / "extension" / "icons"
SIZES = (16, 32, 48, 128)
SAMPLES = 4

BLUE = (42, 120, 214)
DARK = (20, 20, 19)
WHITE = (255, 255, 255)


def colour_at(x: float, y: float) -> tuple[tuple[int, int, int], float] | None:
    """The colour at a point of the unit square, or None where the icon is clear."""
    dx, dy = x - 0.5, y - 0.5
    r = math.hypot(dx, dy)
    if r > 0.48:
        return None
    if r > 0.34:
        # The chip's edge: blue, with six white inserts round it.
        angle = (math.degrees(math.atan2(dy, dx)) + 360) % 60
        return (WHITE if 22 < angle < 38 else BLUE), 1.0
    if r > 0.30:
        return WHITE, 1.0
    # The face: dark, with three rising bars.
    for left, top in ((0.33, 0.56), (0.455, 0.44), (0.58, 0.32)):
        if left <= x <= left + 0.09 and top <= y <= 0.68:
            return WHITE, 1.0
    return DARK, 1.0


def render(size: int) -> bytes:
    rows = []
    for py in range(size):
        row = bytearray([0])  # PNG filter: none
        for px in range(size):
            acc = [0.0, 0.0, 0.0]
            cover = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    hit = colour_at((px + (sx + 0.5) / SAMPLES) / size, (py + (sy + 0.5) / SAMPLES) / size)
                    if hit:
                        (cr, cg, cb), _ = hit
                        acc[0] += cr
                        acc[1] += cg
                        acc[2] += cb
                        cover += 1
            if cover:
                row += bytes(round(c / cover) for c in acc) + bytes([round(255 * cover / SAMPLES**2)])
            else:
                row += bytes(4)
        rows.append(bytes(row))
    return png(size, b"".join(rows))


def png(size: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        (OUT / f"icon{size}.png").write_bytes(render(size))
        print(OUT / f"icon{size}.png")


if __name__ == "__main__":
    main()
