"""Parse MRF headers and locate sibling idx/data files."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from .paths import COMPRESSION_TO_DATA_EXT, COMPRESSION_TO_MIME, TILE_MAGIC

DATA_EXTS = {"pjg", "ppg", "ptf", "pvt", "lrc", "lerc"}

# Inverse of COMPRESSION_TO_DATA_EXT — used when the .mrf header doesn't
# declare <Compression> and we need to infer it from the sibling data file.
EXT_TO_COMPRESSION = {
    "pjg": "JPEG",
    "ppg": "PNG",
    "pjp": "JPNG",
    "ptf": "TIFF",
    "pvt": "PBF",
    "lrc": "LERC",
    "lerc": "LERC",
}


@dataclass
class MrfTriplet:
    header_path: Path
    idx_path: Path
    data_path: Path
    size_x: int
    size_y: int
    bands: int
    tile_size_x: int
    tile_size_y: int
    compression: str
    mime_type: str
    bbox: str | None
    base_name: str
    date_stamp: str | None
    # Actual on-disk format of the tile payload. For JPEG-family layers
    # this is "JPEG" or "BRUNSLI" — both have <Compression>JPEG</Compression>
    # in the header, so we peek at the first non-empty tile's magic bytes
    # to tell them apart. May also be "ZENJPEG", a NASA JPEG variant.
    stored_variant: str = ""

    @property
    def data_ext(self) -> str:
        return self.data_path.suffix.lstrip(".")


_DATESTAMP_RE = re.compile(r"^(?P<base>.+)-(?P<stamp>\d{13,14})$")


def parse_mrf(header_path: Path) -> MrfTriplet:
    """Parse an .mrf header and locate its sibling idx + data files."""
    header_path = header_path.resolve()
    if not header_path.is_file():
        raise FileNotFoundError(f"MRF header not found: {header_path}")
    if header_path.suffix != ".mrf":
        raise ValueError(f"Expected .mrf file, got: {header_path.name}")

    tree = ET.parse(header_path)
    root = tree.getroot()
    raster = root.find("Raster")
    if raster is None:
        raise ValueError(f"MRF header missing <Raster>: {header_path}")

    size_el = raster.find("Size")
    page_el = raster.find("PageSize")
    comp_el = raster.find("Compression")
    if size_el is None or page_el is None:
        raise ValueError(
            f"MRF header missing <Size> or <PageSize>: {header_path}"
        )

    size_x = int(size_el.get("x"))
    size_y = int(size_el.get("y"))
    bands = int(size_el.get("c", "1"))
    tile_x = int(page_el.get("x"))
    tile_y = int(page_el.get("y"))

    # <Compression> is optional in MRF — if absent, infer from the sibling
    # data file's extension (the tool that wrote the MRF picked the ext
    # consistent with the format).
    if comp_el is not None and comp_el.text:
        compression = comp_el.text.strip().upper()
    else:
        compression = _infer_compression_from_siblings(header_path)

    mime_type = COMPRESSION_TO_MIME.get(compression)
    if mime_type is None:
        raise ValueError(
            f"Unsupported compression '{compression}' in {header_path.name}"
        )

    bbox = None
    geo = root.find("GeoTags/BoundingBox")
    if geo is not None:
        bbox = (
            f"{geo.get('minx').strip()},{geo.get('miny').strip()},"
            f"{geo.get('maxx').strip()},{geo.get('maxy').strip()}"
        )

    stem = header_path.stem
    m = _DATESTAMP_RE.match(stem)
    if m:
        base_name = m.group("base")
        date_stamp = m.group("stamp")
    else:
        base_name = stem
        date_stamp = None

    idx_path = header_path.with_suffix(".idx")
    if not idx_path.is_file():
        raise FileNotFoundError(f"Index file not found beside header: {idx_path}")

    data_path = _find_data_file(header_path, compression)
    stored_variant = detect_stored_variant(idx_path, data_path, compression)

    return MrfTriplet(
        header_path=header_path,
        idx_path=idx_path,
        data_path=data_path,
        size_x=size_x,
        size_y=size_y,
        bands=bands,
        tile_size_x=tile_x,
        tile_size_y=tile_y,
        compression=compression,
        mime_type=mime_type,
        bbox=bbox,
        base_name=base_name,
        date_stamp=date_stamp,
        stored_variant=stored_variant,
    )


def detect_stored_variant(
    idx_path: Path, data_path: Path, compression: str
) -> str:
    """For JPEG-family MRFs, peek at the first populated tile's header
    bytes to distinguish standard JPEG from Brunsli (or similar variants
    that ride under the <Compression>JPEG</Compression> banner).

    Returns a descriptive variant label: 'JPEG', 'BRUNSLI', 'ZENJPEG',
    or simply the declared compression when we're not looking at a JPEG.
    """
    if compression not in {"JPEG", "JPG"}:
        return compression

    try:
        with open(idx_path, "rb") as f:
            idx_bytes = f.read()
    except OSError:
        return compression

    # Each idx entry is a 16-byte pair (offset, size), big-endian int64.
    # Scan for the first entry with non-zero size — that's our first real
    # tile payload.
    offset = size = 0
    for i in range(0, len(idx_bytes) - 15, 16):
        o, s = struct.unpack(">qq", idx_bytes[i : i + 16])
        if s > 0:
            offset, size = o, s
            break
    if size == 0:
        return compression  # no tiles to inspect

    try:
        with open(data_path, "rb") as f:
            f.seek(offset)
            head = f.read(max(len(v) for v in TILE_MAGIC.values()))
    except OSError:
        return compression

    if head.startswith(TILE_MAGIC["BRUNSLI"]):
        return "BRUNSLI"
    if head.startswith(TILE_MAGIC["JPEG"]):
        return "JPEG"
    # Anything else under a JPEG header is suspicious. Surface it as a
    # distinct label so the --diff guard can refuse without guessing.
    return "UNKNOWN_JPEG_VARIANT"


def _infer_compression_from_siblings(header_path: Path) -> str:
    """Pick the compression format from whichever MRF data file sits next
    to the header. Raises if nothing recognizable is there."""
    for ext, compression in EXT_TO_COMPRESSION.items():
        if header_path.with_suffix(f".{ext}").is_file():
            return compression
    raise ValueError(
        f"{header_path.name} has no <Compression> element and no "
        f"recognizable data file sibling ({', '.join(sorted(EXT_TO_COMPRESSION))})"
    )


def _find_data_file(header_path: Path, compression: str) -> Path:
    preferred = COMPRESSION_TO_DATA_EXT.get(compression)
    if preferred:
        candidate = header_path.with_suffix(f".{preferred}")
        if candidate.is_file():
            return candidate

    for ext in DATA_EXTS:
        candidate = header_path.with_suffix(f".{ext}")
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(
        f"No MRF data file ({', '.join(sorted(DATA_EXTS))}) found next to "
        f"{header_path.name}"
    )
