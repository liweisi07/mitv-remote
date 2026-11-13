#!/usr/bin/env python3
"""Generate icons for mitvremote (pure stdlib, no PIL).

Icons are written as RGBA PNGs (color type 6): fnOS App Center rejects
packages whose ICON files are not RGBA ("应用包不符合系统要求").

Design: dark navy background, a rounded remote-control body, a red
power button on top (white power symbol), and a 3x3 button grid below
with a blue OK button in the middle.
"""

import binascii
import math
import os
import struct
import zlib

BG = (16, 24, 40)
BODY = (35, 48, 68)
POWER = (217, 45, 32)
WHITE = (255, 255, 255)
BTN = (208, 213, 221)
OKBLUE = (23, 92, 211)


def _png_chunk(kind, data):
    chunk = struct.pack(">I", len(data)) + kind + data
    chunk += struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
    return chunk


def _in_round_rect(nx, ny, x0, y0, x1, y1, r):
    if x0 + r <= nx <= x1 - r and y0 <= ny <= y1:
        return True
    if y0 + r <= ny <= y1 - r and x0 <= nx <= x1:
        return True
    for cx, cy in ((x0 + r, y0 + r), (x1 - r, y0 + r),
                   (x0 + r, y1 - r), (x1 - r, y1 - r)):
        if (nx - cx) ** 2 + (ny - cy) ** 2 <= r * r:
            return True
    return False


def write_png(path, size):
    cx, cy = 0.5, 0.24  # power button center
    raw = bytearray()
    for py in range(size):
        raw.append(0)  # filter: none
        for px in range(size):
            nx = (px + 0.5) / size
            ny = (py + 0.5) / size
            color = BG
            if _in_round_rect(nx, ny, 0.28, 0.08, 0.72, 0.92, 0.08):
                color = BODY
            # button grid: 3x3, center is OK
            for gx in (0.38, 0.50, 0.62):
                for gy in (0.45, 0.58, 0.71):
                    dx = nx - gx
                    dy = ny - gy
                    rad = 0.042 if (gx == 0.50 and gy == 0.58) else 0.032
                    if dx * dx + dy * dy <= rad * rad:
                        color = OKBLUE if rad > 0.04 else BTN
            # power button: red circle + white power symbol
            dx = nx - cx
            dy = ny - cy
            dist = math.hypot(dx, dy)
            if dist <= 0.085:
                color = POWER
            if 0.040 <= dist <= 0.055:
                # power arc: ring with a gap at the top (line enters there)
                ang = math.degrees(math.atan2(dy, dx))
                if not (-120 <= ang <= -60):
                    color = WHITE
            if abs(dx) <= 0.008 and 0.145 <= ny <= 0.245:
                color = WHITE
            raw.extend(color + (255,))
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", struct.pack(
        ">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += _png_chunk(b"IDAT", zlib.compress(bytes(raw), level=9))
    png += _png_chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)
    print("wrote %s (%d bytes)" % (path, len(png)))


if __name__ == "__main__":
    root = os.path.join(os.path.dirname(__file__), "..")
    write_png(os.path.join(root, "ICON.PNG"), 64)
    write_png(os.path.join(root, "ICON_256.PNG"), 256)
    ui_images = os.path.join(root, "app", "ui", "images")
    os.makedirs(ui_images, exist_ok=True)
    write_png(os.path.join(ui_images, "icon_64.png"), 64)
    write_png(os.path.join(ui_images, "icon_256.png"), 256)
    static_icons = os.path.join(root, "app", "static", "icons")
    os.makedirs(static_icons, exist_ok=True)
    write_png(os.path.join(static_icons, "icon-192.png"), 192)
    write_png(os.path.join(static_icons, "icon-512.png"), 512)
