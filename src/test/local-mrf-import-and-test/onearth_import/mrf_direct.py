"""Read tiles directly from an MRF triplet on disk, so we can assemble
a reference image independent of the tile server and compare against it.

Based on the MRF read logic in /Users/jdrodrig/gibs/mrf/mrf_apps/mrf_read.py,
but reimplemented with simpler indexing:

    level 0 = finest/base (most tiles)
    level len(pyramid)-1 = top (1 tile)

The .idx file stores tile entries in that order: all base tiles first
(row-major), then the next pyramid level up, etc. Each entry is 16 bytes:
offset (big-endian int64) + size (big-endian int64).
"""

from __future__ import annotations

import io
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET


@dataclass
class MrfInfo:
    size_x: int
    size_y: int
    tile_w: int
    tile_h: int
    compression: str  # JPEG/PNG/TIFF/...
    pyramid: list[tuple[int, int]]  # [(w_tiles, h_tiles), ...] base → top


def parse_mrf_header(mrf_path: Path) -> MrfInfo:
    tree = ET.parse(str(mrf_path))
    raster = tree.getroot().find("Raster")
    if raster is None:
        raise ValueError(f"MRF header missing <Raster>: {mrf_path}")
    size = raster.find("Size")
    page = raster.find("PageSize")
    comp = raster.find("Compression")
    size_x = int(size.get("x"))
    size_y = int(size.get("y"))
    tile_w = int(page.get("x"))
    tile_h = int(page.get("y"))
    # <Compression> is optional; default to PNG if absent (matches
    # mrf_read.py's default and is the most common silent case).
    compression = (
        (comp.text or "").strip().upper() if comp is not None and comp.text
        else "PNG"
    )

    pyramid: list[tuple[int, int]] = []
    w = math.ceil(size_x / tile_w)
    h = math.ceil(size_y / tile_h)
    while True:
        pyramid.append((w, h))
        if w <= 1 and h <= 1:
            break
        w = max(1, math.ceil(w / 2))
        h = max(1, math.ceil(h / 2))

    return MrfInfo(size_x, size_y, tile_w, tile_h, compression, pyramid)


def read_tile_bytes(
    idx_path: Path,
    data_path: Path,
    info: MrfInfo,
    pyramid_level: int,
    row: int,
    col: int,
) -> bytes | None:
    """Return the raw compressed tile bytes (JPEG/PNG/etc.) or None if the
    entry is empty (offset=0, size=0)."""
    if pyramid_level < 0 or pyramid_level >= len(info.pyramid):
        return None
    # Tile entries in .idx are laid out base-first, then each pyramid level
    # up. Compute the starting index for the requested pyramid level.
    level_start = sum(w * h for (w, h) in info.pyramid[:pyramid_level])
    w, h = info.pyramid[pyramid_level]
    if row < 0 or col < 0 or row >= h or col >= w:
        return None
    tile_index = level_start + row * w + col

    with open(idx_path, "rb") as f:
        f.seek(16 * tile_index)
        entry = f.read(16)
    if len(entry) < 16:
        return None
    offset, size = struct.unpack(">qq", entry)
    if size <= 0:
        return None

    with open(data_path, "rb") as f:
        f.seek(offset)
        return f.read(size)


def stitch_from_mrf(
    mrf_path: Path,
    idx_path: Path,
    data_path: Path,
    pyramid_level: int,
    matrix_width: int,
    matrix_height: int,
    mode: str = "RGB",
    decode_tile=None,
):
    """Build a PIL.Image of the full grid at the given pyramid level by
    reading every tile directly out of the MRF data file. Tiles the MRF
    doesn't have at that grid position are left as canvas default
    (black for RGB, fully transparent for RGBA).

    decode_tile: optional callable(bytes) -> bytes that transforms each
    tile's raw payload before PIL ingests it. Use it to turn Brunsli
    tiles into JPEG, since PIL can't read Brunsli directly.
    """
    from PIL import Image

    info = parse_mrf_header(mrf_path)
    canvas = Image.new(
        mode,
        (matrix_width * info.tile_w, matrix_height * info.tile_h),
    )
    for row in range(matrix_height):
        for col in range(matrix_width):
            data = read_tile_bytes(
                idx_path, data_path, info, pyramid_level, row, col
            )
            if data is None:
                continue
            if decode_tile is not None:
                data = decode_tile(data)
            tile = Image.open(io.BytesIO(data)).convert(mode)
            canvas.paste(tile, (col * info.tile_w, row * info.tile_h))
    return canvas
