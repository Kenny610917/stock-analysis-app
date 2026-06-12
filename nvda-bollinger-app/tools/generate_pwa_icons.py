#!/usr/bin/env python3
"""Generate simple PNG icons for the Stock analysis PWA."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = ROOT / "static" / "icons"


def lerp(a: int, b: int, t: float) -> int:
    return round(a + (b - a) * t)


def set_pixel(pixels: bytearray, size: int, x: int, y: int, color: tuple[int, int, int, int]) -> None:
    if x < 0 or y < 0 or x >= size or y >= size:
        return
    index = (y * size + x) * 4
    pixels[index : index + 4] = bytes(color)


def draw_disc(pixels: bytearray, size: int, cx: int, cy: int, radius: int, color: tuple[int, int, int, int]) -> None:
    radius_sq = radius * radius
    for y in range(cy - radius, cy + radius + 1):
        for x in range(cx - radius, cx + radius + 1):
            if (x - cx) * (x - cx) + (y - cy) * (y - cy) <= radius_sq:
                set_pixel(pixels, size, x, y, color)


def draw_line(
    pixels: bytearray,
    size: int,
    start: tuple[int, int],
    end: tuple[int, int],
    width: int,
    color: tuple[int, int, int, int],
) -> None:
    x0, y0 = start
    x1, y1 = end
    dx = abs(x1 - x0)
    sx = 1 if x0 < x1 else -1
    dy = -abs(y1 - y0)
    sy = 1 if y0 < y1 else -1
    err = dx + dy

    while True:
      draw_disc(pixels, size, x0, y0, width, color)
      if x0 == x1 and y0 == y1:
          break
      e2 = 2 * err
      if e2 >= dy:
          err += dy
          x0 += sx
      if e2 <= dx:
          err += dx
          y0 += sy


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def write_png(path: Path, size: int, pixels: bytearray) -> None:
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw.extend(pixels[y * stride : (y + 1) * stride])

    body = b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)),
            png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)),
            png_chunk(b"IEND", b""),
        ]
    )
    path.write_bytes(body)


def make_icon(size: int) -> None:
    pixels = bytearray(size * size * 4)
    radius = round(size * 0.22)
    dark = (15, 118, 110)
    light = (23, 114, 69)

    for y in range(size):
        for x in range(size):
            corner_dx = min(x, size - 1 - x)
            corner_dy = min(y, size - 1 - y)
            if corner_dx < radius and corner_dy < radius:
                if (radius - corner_dx) ** 2 + (radius - corner_dy) ** 2 > radius * radius:
                    continue

            t = (x + y) / max(1, (size - 1) * 2)
            r = lerp(dark[0], light[0], t)
            g = lerp(dark[1], light[1], t)
            b = lerp(dark[2], light[2], t)
            set_pixel(pixels, size, x, y, (r, g, b, 255))

    grid = (255, 255, 255, 44)
    for fraction in (0.28, 0.5, 0.72):
        x = round(size * fraction)
        y = round(size * fraction)
        draw_line(pixels, size, (round(size * 0.18), y), (round(size * 0.82), y), max(1, size // 160), grid)
        draw_line(pixels, size, (x, round(size * 0.18)), (x, round(size * 0.82)), max(1, size // 160), grid)

    line = (255, 255, 255, 245)
    accent = (163, 230, 53, 255)
    width = max(4, size // 42)
    points = [
        (round(size * 0.2), round(size * 0.66)),
        (round(size * 0.34), round(size * 0.55)),
        (round(size * 0.48), round(size * 0.62)),
        (round(size * 0.63), round(size * 0.38)),
        (round(size * 0.82), round(size * 0.30)),
    ]
    for start, end in zip(points, points[1:]):
        draw_line(pixels, size, start, end, width, line)
    for point in points:
        draw_disc(pixels, size, point[0], point[1], width + 2, accent)

    write_png(ICON_DIR / f"icon-{size}.png", size, pixels)


def main() -> int:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        make_icon(size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
