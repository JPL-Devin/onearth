"""Stitch all tiles at a chosen TileMatrixSet level into a single image."""

from __future__ import annotations

import io
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from . import docker_ops as d
from . import paths as P
from . import prompts

_OWS = {"ows": "http://www.opengis.net/ows/1.1"}
TMS_XML_PATH = "/etc/onearth/config/conf/tilematrixsets.xml"
MRF_CONFIG_PATH = (
    "/var/www/html/wmts/{projection}/{profile}/"
    "{layer_id}/default/{tilematrixset}/mod_mrf.config"
)


@dataclass
class TmsLevel:
    level: int
    matrix_width: int
    matrix_height: int
    tile_width: int
    tile_height: int

    @property
    def image_px(self) -> tuple[int, int]:
        return (
            self.matrix_width * self.tile_width,
            self.matrix_height * self.tile_height,
        )

    @property
    def tile_count(self) -> int:
        return self.matrix_width * self.matrix_height


_CRS_BY_EPSG = {
    "EPSG:4326": {"urn:ogc:def:crs:OGC:1.3:CRS84", "urn:ogc:def:crs:EPSG::4326"},
    "EPSG:3857": {"urn:ogc:def:crs:EPSG::3857"},
    "EPSG:3031": {"urn:ogc:def:crs:EPSG::3031"},
    "EPSG:3413": {"urn:ogc:def:crs:EPSG::3413"},
    "EPSG:3411": {"urn:ogc:def:crs:EPSG::3411"},
}


def _read_tms(tilematrixset: str, projection: str) -> list[TmsLevel]:
    """Look up a TileMatrixSet by name AND projection — several TMSes
    share identifiers across CRSes (e.g. '1km' exists for EPSG:3031,
    EPSG:3413, and EPSG:4326 with very different grids)."""
    raw = d.read_file(P.TILE_SERVICES, TMS_XML_PATH)
    root = ET.fromstring(raw)
    allowed_crs = _CRS_BY_EPSG.get(projection.upper(), set())
    for tms in root.findall(".//TileMatrixSet"):
        ident = tms.find("ows:Identifier", _OWS)
        crs_el = tms.find("ows:SupportedCRS", _OWS)
        if ident is None or ident.text != tilematrixset:
            continue
        if allowed_crs and crs_el is not None and crs_el.text not in allowed_crs:
            continue
        levels: list[TmsLevel] = []
        for m in tms.findall("TileMatrix"):
            levels.append(
                TmsLevel(
                    level=int(m.find("ows:Identifier", _OWS).text),
                    matrix_width=int(m.find("MatrixWidth").text),
                    matrix_height=int(m.find("MatrixHeight").text),
                    tile_width=int(m.find("TileWidth").text),
                    tile_height=int(m.find("TileHeight").text),
                )
            )
        return sorted(levels, key=lambda x: x.level)
    raise ValueError(
        f"TileMatrixSet '{tilematrixset}' not found for projection "
        f"{projection}"
    )


def _read_skipped_levels(
    projection_dir: str, profile: str, layer_id: str, tilematrixset: str
) -> int:
    path = MRF_CONFIG_PATH.format(
        projection=projection_dir,
        profile=profile,
        layer_id=layer_id,
        tilematrixset=tilematrixset,
    )
    try:
        content = d.read_file(P.TILE_SERVICES, path)
    except d.DockerError:
        return 0
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("SkippedLevels"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return 0


def _resolve_layer_profile(
    projection_dir: str, preferred: str, layer_id: str
) -> str:
    """Pick the profile that actually contains this layer's YAML.

    If preferred has it, use that. Otherwise probe all sibling profile
    directories under the projection and pick the one that does. Dies with
    a clear message if it's nowhere (or somehow in multiple)."""
    base = f"/etc/onearth/config/layers/{projection_dir}"
    preferred_path = f"{base}/{preferred}/{layer_id}.yaml"
    if d.exec_cmd(
        P.TILE_SERVICES,
        f"test -f {preferred_path}",
        check=False,
    ).returncode == 0:
        return preferred

    result = d.exec_cmd(
        P.TILE_SERVICES,
        f"find {base} -maxdepth 2 -name {layer_id}.yaml -type f",
        check=False,
    )
    matches = [line for line in result.stdout.splitlines() if line.strip()]
    if not matches:
        print(
            f"error: layer '{layer_id}' not found under {base}/*/. "
            f"Is it registered? Run `onearth-import list` to check.",
            file=sys.stderr,
        )
        sys.exit(1)
    if len(matches) > 1:
        profiles = sorted({m.split("/")[-2] for m in matches})
        print(
            f"error: layer '{layer_id}' exists under multiple profiles "
            f"({', '.join(profiles)}). Specify --profile.",
            file=sys.stderr,
        )
        sys.exit(1)
    found_profile = matches[0].split("/")[-2]
    print(
        f"(auto-detected profile: {found_profile})",
        file=sys.stderr,
    )
    return found_profile


def _layer_is_temporal(projection_dir: str, profile: str, layer_id: str) -> bool:
    import yaml

    path = (
        f"/etc/onearth/config/layers/{projection_dir}/{profile}/{layer_id}.yaml"
    )
    raw = d.read_file(P.TILE_SERVICES, path)
    doc = yaml.safe_load(raw) or {}
    return not doc.get("static", True)


def _mime_to_ext(projection_dir: str, profile: str, layer_id: str) -> str:
    import yaml

    path = (
        f"/etc/onearth/config/layers/{projection_dir}/{profile}/{layer_id}.yaml"
    )
    raw = d.read_file(P.TILE_SERVICES, path)
    doc = yaml.safe_load(raw) or {}
    mime = doc.get("mime_type", "image/jpeg")
    # image/x-j is the Brunsli MIME; the URL still uses .jpg because
    # mod_brunsli's DBRUNSLI filter transcodes to standard JPEG before
    # the tile leaves Apache.
    return {
        "image/jpeg": "jpg",
        "image/x-j": "jpg",
        "image/png": "png",
    }.get(mime, "bin")


def _fetch(url: str) -> bytes:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(url, timeout=30, context=ctx) as resp:
        return resp.read()


def _level_is_available(
    base_url: str,
    layer_id: str,
    mid_path: str,
    tilematrixset: str,
    level: int,
    ext: str,
) -> bool:
    url = (
        f"{base_url}/{layer_id}/{mid_path}/"
        f"{tilematrixset}/{level}/0/0.{ext}"
    )
    try:
        _fetch(url)
        return True
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def stitch(
    layer_id: str,
    projection: str,
    profile: str,
    tilematrixset: str,
    level: int | None,
    out: Path | None,
    date: str | None,
    max_tiles: int,
    diff: bool = False,
) -> None:
    from . import commands

    commands._require_stack()
    projection_dir = projection.replace(":", "").lower()
    profile = _resolve_layer_profile(projection_dir, profile, layer_id)
    tms_levels = _read_tms(tilematrixset, projection)
    skipped = _read_skipped_levels(
        projection_dir, profile, layer_id, tilematrixset
    )
    temporal = _layer_is_temporal(projection_dir, profile, layer_id)
    ext = _mime_to_ext(projection_dir, profile, layer_id)
    base_url = f"{commands._base_url()}/wmts/{projection_dir}/{profile}"
    # Explicit date, not 'default' — the time service's default-keyword
    # handling is unreliable for freshly-seeded layers.
    resolved_date: str | None = None
    if temporal:
        resolved_date = date or commands._layer_default_date(
            projection_dir, profile, layer_id
        )
    # Middle path for the WMTS REST URL. Temporal layers have
    # /{style}/{time}/ segments; static layers have just /{style}/.
    if temporal:
        mid_path = f"default/{resolved_date}" if resolved_date else "default/default"
    else:
        mid_path = "default"

    # TMS z-levels start at 0 regardless of mod_mrf's SkippedLevels
    # (SkippedLevels is internal: how many MRF pyramid levels sit above the
    # TMS's coarsest, and don't participate in URL-level numbering). Probe
    # each TMS level to confirm it actually serves a tile — some TMSes go
    # further than the MRF's data, so higher levels may 4xx.
    confirmed: list[TmsLevel] = []
    for lvl in tms_levels:
        if _level_is_available(
            base_url, layer_id, mid_path, tilematrixset, lvl.level, ext
        ):
            confirmed.append(lvl)

    if not confirmed:
        print(
            f"No tiles are being served for layer '{layer_id}' in "
            f"tilematrixset '{tilematrixset}'. Is the layer registered?",
            file=sys.stderr,
        )
        sys.exit(1)

    if level is None:
        print(f"Available levels for layer '{layer_id}' in {tilematrixset}:")
        for lvl in confirmed:
            w, h = lvl.image_px
            marker = (
                "  (would exceed --max-tiles)"
                if lvl.tile_count > max_tiles
                else ""
            )
            print(
                f"  level {lvl.level}: {lvl.matrix_width} × "
                f"{lvl.matrix_height} tiles  →  {w} × {h} px{marker}"
            )
        print("")
        if prompts.is_interactive():
            eligible = [l for l in confirmed if l.tile_count <= max_tiles]
            if not eligible:
                print(
                    "All levels exceed --max-tiles; re-run with a higher "
                    "--max-tiles to stitch."
                )
                return
            choices = [
                prompts.Choice(
                    value=str(l.level),
                    label=f"level {l.level}  ({l.tile_count} tiles, "
                    f"{l.image_px[0]}×{l.image_px[1]} px)",
                )
                for l in eligible
            ]
            picked = prompts.pick_one(
                "Pick a level to stitch:",
                choices,
                default=len(choices) - 1,
            )
            level = int(picked)
        else:
            print(
                f"Run again with --level N to stitch. "
                f"(--max-tiles={max_tiles}; override if you need more.)"
            )
            return

    chosen = next((l for l in confirmed if l.level == level), None)
    if chosen is None:
        print(
            f"Level {level} is not available for this layer. Available: "
            + ", ".join(str(l.level) for l in confirmed),
            file=sys.stderr,
        )
        sys.exit(1)

    if chosen.tile_count > max_tiles:
        print(
            f"Level {level} requires {chosen.tile_count} tiles, exceeding "
            f"--max-tiles={max_tiles}. Re-run with --max-tiles to override.",
            file=sys.stderr,
        )
        sys.exit(1)

    if diff:
        prefix = out or Path(f"{layer_id}_{tilematrixset}_L{level}")
        served_path = prefix.with_name(prefix.name + f"-served.{ext}")
        source_path = prefix.with_name(prefix.name + f"-source.{ext}")
        diff_path = prefix.with_name(prefix.name + "-diff.png")
        _do_stitch(
            base_url=base_url,
            layer_id=layer_id,
            mid_path=mid_path,
            tilematrixset=tilematrixset,
            ext=ext,
            level=chosen,
            out_path=served_path,
        )
        _diff_against_source(
            projection_dir=projection_dir,
            profile=profile,
            layer_id=layer_id,
            date=resolved_date,
            tms_level=chosen,
            num_pyramid_levels=_num_pyramid_levels(
                projection_dir, profile, layer_id, tilematrixset
            ),
            skipped_levels=skipped,
            served_path=served_path,
            source_path=source_path,
            diff_path=diff_path,
            ext=ext,
        )
        return

    out_path = out or Path(f"{layer_id}_{tilematrixset}_L{level}.{ext}")
    _do_stitch(
        base_url=base_url,
        layer_id=layer_id,
        mid_path=mid_path,
        tilematrixset=tilematrixset,
        ext=ext,
        level=chosen,
        out_path=out_path,
    )


def _num_pyramid_levels(
    projection_dir: str, profile: str, layer_id: str, tilematrixset: str
) -> int:
    """Compute the number of pyramid levels in the MRF by parsing the
    header the tile-services container holds for this layer."""
    from . import mrf_direct

    # The header lives at /onearth/layers/{proj}/{layer}/{layer_id}*.mrf
    import shlex as _sh

    result = d.exec_cmd(
        P.TILE_SERVICES,
        f"ls /onearth/layers/{_sh.quote(projection_dir)}/{_sh.quote(layer_id)}/*.mrf 2>/dev/null | head -1",
        check=False,
    )
    paths = [line for line in result.stdout.splitlines() if line.strip()]
    if not paths:
        raise RuntimeError(
            f"no .mrf header found in container at "
            f"/onearth/layers/{projection_dir}/{layer_id}/"
        )
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        local_mrf = Path(td) / Path(paths[0]).name
        d.exec_cmd(
            P.TILE_SERVICES, f"cat {_sh.quote(paths[0])}", check=True
        ).stdout
        # cat via exec gives text; simpler: docker cp
        from . import docker_ops

        import subprocess

        subprocess.run(
            [
                "docker",
                "cp",
                f"{P.TILE_SERVICES}:{paths[0]}",
                str(local_mrf),
            ],
            check=True,
            capture_output=True,
        )
        info = mrf_direct.parse_mrf_header(local_mrf)
        return len(info.pyramid)


def _diff_against_source(
    *,
    projection_dir: str,
    profile: str,
    layer_id: str,
    date: str | None,  # explicit date (for temporal), None for static
    tms_level: TmsLevel,
    num_pyramid_levels: int,
    skipped_levels: int,
    served_path: Path,
    source_path: Path,
    diff_path: Path,
    ext: str,
) -> None:
    """docker cp the MRF triplet out, stitch same level from source,
    diff against served image, save all three, print stats."""
    import shlex as _sh
    import subprocess
    import tempfile

    from PIL import Image, ImageChops, ImageStat

    from . import mrf_direct

    # Locate and copy out the MRF triplet.
    layer_dir = f"/onearth/layers/{projection_dir}/{layer_id}"
    idx_dir = f"/onearth/idx/{projection_dir}/{layer_id}"
    result = d.exec_cmd(
        P.TILE_SERVICES,
        f"ls {_sh.quote(layer_dir)}",
        check=True,
    )
    data_names = [n for n in result.stdout.splitlines() if n.strip()]
    mrf_name = next((n for n in data_names if n.endswith(".mrf")), None)
    if not mrf_name:
        raise RuntimeError(f"no .mrf header in {layer_dir}")
    base = mrf_name[: -len(".mrf")]
    data_name = next(
        (n for n in data_names if n.startswith(base) and not n.endswith((".mrf",))),
        None,
    )
    if not data_name:
        raise RuntimeError(f"no data file matching {base} in {layer_dir}")

    idx_result = d.exec_cmd(P.TILE_SERVICES, f"ls {_sh.quote(idx_dir)}")
    idx_names = [
        n for n in idx_result.stdout.splitlines()
        if n.startswith(base) and n.endswith(".idx")
    ]
    if not idx_names:
        raise RuntimeError(f"no .idx file for {base} in {idx_dir}")
    idx_name = idx_names[0]

    # Map TMS level → MRF pyramid index (0 = finest/base).
    # mod_mrf's SkippedLevels accounts for MRF pyramid levels above what the
    # TMS exposes (e.g. the 1×1 top). So the number of MRF levels served
    # by the TMS is (num_pyramid_levels - skipped_levels), and the deepest
    # served TMS level maps to MRF pyramid index 0 (the finest).
    served_levels = num_pyramid_levels - skipped_levels
    pyramid_level = served_levels - 1 - tms_level.level

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        local_mrf = td_path / mrf_name
        local_data = td_path / data_name
        local_idx = td_path / idx_name
        for src_path, dst_path in (
            (f"{layer_dir}/{mrf_name}", local_mrf),
            (f"{layer_dir}/{data_name}", local_data),
            (f"{idx_dir}/{idx_name}", local_idx),
        ):
            subprocess.run(
                ["docker", "cp", f"{P.TILE_SERVICES}:{src_path}", str(dst_path)],
                check=True,
                capture_output=True,
            )

        # Brunsli MRFs declare <Compression>JPEG</Compression> in the
        # header, so we peek at the first tile's magic bytes. For Brunsli
        # payloads PIL can't decode directly; we stream each tile through
        # libbrunslidec-c inside the container to get JPEG bytes back,
        # then PIL takes over. ZenJPEG / other unknown JPEG variants
        # (we don't have their signatures yet) still refuse.
        from . import brunsli_decode, mrf as mrf_mod

        info = mrf_direct.parse_mrf_header(local_mrf)
        stored_variant = mrf_mod.detect_stored_variant(
            local_idx, local_data, info.compression
        )
        if stored_variant == "UNKNOWN_JPEG_VARIANT" or (
            stored_variant in P.DIFF_UNSUPPORTED_VARIANTS
            and stored_variant != "BRUNSLI"
        ):
            print(
                f"\nerror: --diff isn't supported for {stored_variant} "
                "payloads — no decoder available. "
                f"The served stitch was saved to {served_path}; "
                "drop --diff to skip the source comparison.",
                file=sys.stderr,
            )
            sys.exit(2)

        canvas_mode = "RGBA" if ext.lower() == "png" else "RGB"
        if stored_variant == "BRUNSLI":
            print(
                "Decoding Brunsli tiles via libbrunslidec-c in the "
                f"container ({tms_level.tile_count} tiles)..."
            )
            with brunsli_decode.BrunsliDecoder() as decoder:
                source_img = mrf_direct.stitch_from_mrf(
                    mrf_path=local_mrf,
                    idx_path=local_idx,
                    data_path=local_data,
                    pyramid_level=pyramid_level,
                    matrix_width=tms_level.matrix_width,
                    matrix_height=tms_level.matrix_height,
                    mode=canvas_mode,
                    decode_tile=decoder.decode,
                )
        else:
            print(f"Stitching same level directly from MRF at {local_mrf.name}...")
            source_img = mrf_direct.stitch_from_mrf(
                mrf_path=local_mrf,
                idx_path=local_idx,
                data_path=local_data,
                pyramid_level=pyramid_level,
                matrix_width=tms_level.matrix_width,
                matrix_height=tms_level.matrix_height,
                mode=canvas_mode,
            )
        source_img.save(source_path)
        print(f"Saved {source_path}")

    served_img = Image.open(served_path).convert(canvas_mode)

    # Crop/resize guard: the source canvas is a tile-aligned grid,
    # possibly slightly larger than the MRF's actual pixel dimensions
    # if size isn't tile-aligned. served image is the same grid. Same shape.
    if source_img.size != served_img.size:
        raise RuntimeError(
            f"size mismatch: served {served_img.size} vs source "
            f"{source_img.size} — cannot diff"
        )

    diff_img = ImageChops.difference(served_img, source_img)
    diff_img.save(diff_path)
    stat = ImageStat.Stat(diff_img)
    mean = sum(stat.mean) / len(stat.mean)
    extrema = diff_img.convert("L").getextrema()
    max_delta = extrema[1]
    # Count of pixels where any channel differs (by converting diff to
    # grayscale and checking nonzero).
    import itertools

    gray = diff_img.convert("L")
    total_pixels = gray.size[0] * gray.size[1]
    # PIL doesn't have a fast nonzero count without numpy; histogram is fine.
    hist = gray.histogram()
    nonzero_px = total_pixels - hist[0]
    pct = (nonzero_px / total_pixels) * 100 if total_pixels else 0

    print("")
    print("Diff stats (served vs. source-from-MRF):")
    print(f"  mean per-channel delta : {mean:.3f}")
    print(f"  max channel delta      : {max_delta}")
    print(
        f"  differing pixels       : {nonzero_px}/{total_pixels} ({pct:.3f}%)"
    )
    print("")
    print("Files written:")
    print(f"  served: {served_path}")
    print(f"  source: {source_path}")
    print(f"  diff:   {diff_path}  (grayscale: brighter = larger delta)")


def _do_stitch(
    *,
    base_url: str,
    layer_id: str,
    mid_path: str,
    tilematrixset: str,
    ext: str,
    level: TmsLevel,
    out_path: Path,
) -> None:
    from PIL import Image

    w_px, h_px = level.image_px
    # PNG tiles may carry alpha (RGBA or paletted with transparency); for
    # those we want an RGBA canvas so transparent regions stay transparent
    # instead of getting flattened to black. JPEG has no alpha → stay RGB.
    canvas_mode = "RGBA" if ext.lower() == "png" else "RGB"
    canvas = Image.new(canvas_mode, (w_px, h_px))
    total = level.tile_count
    fetched = 0
    print(
        f"Stitching {total} tiles at level {level.level} "
        f"({level.matrix_width}×{level.matrix_height}) → {w_px}×{h_px} px"
    )
    for row in range(level.matrix_height):
        for col in range(level.matrix_width):
            url = (
                f"{base_url}/{layer_id}/{mid_path}/"
                f"{tilematrixset}/{level.level}/{row}/{col}.{ext}"
            )
            try:
                data = _fetch(url)
            except Exception as e:  # noqa: BLE001
                print(
                    f"  warn: tile {level.level}/{row}/{col} failed "
                    f"({type(e).__name__}: {e}); leaving empty",
                    file=sys.stderr,
                )
                continue
            tile = Image.open(io.BytesIO(data)).convert(canvas_mode)
            canvas.paste(tile, (col * level.tile_width, row * level.tile_height))
            fetched += 1
    canvas.save(out_path)
    print(f"Saved {out_path} ({fetched}/{total} tiles populated)")
