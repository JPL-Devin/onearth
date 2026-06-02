"""Implementations of the CLI subcommands."""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import docker_ops as d
from . import paths as P
from . import prompts
from .layer_yaml import LayerSpec, build_layer_yaml
from .mrf import parse_mrf


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def _info(msg: str) -> None:
    print(msg)


def _base_url() -> str:
    """Honor the same USE_SSL / SERVER_NAME env vars the stack's own startup
    script uses, so the CLI's probe URLs match however the user is actually
    running the stack. Override: ONEARTH_BASE_URL."""
    override = os.environ.get("ONEARTH_BASE_URL")
    if override:
        return override.rstrip("/")
    use_ssl = os.environ.get("USE_SSL", "false").lower() in ("true", "1", "yes")
    host = os.environ.get("SERVER_NAME", "localhost")
    return f"{'https' if use_ssl else 'http'}://{host}"


def _default_date_from_time_config(time_config: str | None) -> str | None:
    """Extract the start date from a period string like
    '2020-01-01/2020-12-31/P1D' → '2020-01-01'. Returns None for 'DETECT'
    or unparseable input."""
    if not time_config:
        return None
    first = time_config.split("/")[0].strip()
    if not first or first.upper() == "DETECT":
        return None
    return first


def _datestamp_to_iso(date_stamp: str | None) -> str | None:
    """Parse OnEarth's YYYYDDDHHMMSS filename datestamp into YYYY-MM-DD.
    Example: '2004214000000' → '2004-08-01'."""
    if not date_stamp or len(date_stamp) < 7:
        return None
    try:
        from datetime import date, timedelta

        year = int(date_stamp[:4])
        day_of_year = int(date_stamp[4:7])
        d = date(year, 1, 1) + timedelta(days=day_of_year - 1)
        return d.isoformat()
    except (ValueError, IndexError):
        return None


def _endpoint_yaml_path(projection_dir: str, profile: str) -> str:
    return P.ENDPOINT_YAML.format(projection=projection_dir, profile=profile)


def _retarget_filename(original: str, old_base: str, new_base: str) -> str:
    """Rewrite filename base while preserving any trailing -datestamp and ext.

    Example:
        _retarget_filename("Raster_Status-2004214000000.idx",
                           "Raster_Status", "MyLayer")
        → "MyLayer-2004214000000.idx"
    """
    if old_base == new_base or not original.startswith(old_base):
        return original
    return new_base + original[len(old_base):]


def _read_time_service_keys(projection_dir: str, profile: str) -> list[str]:
    """Read the endpoint YAML from the tile-services container and return
    its time_service_keys list (empty if the field isn't set)."""
    import yaml

    endpoint = _endpoint_yaml_path(projection_dir, profile)
    raw = d.read_file(P.TILE_SERVICES, endpoint)
    doc = yaml.safe_load(raw) or {}
    keys = doc.get("time_service_keys") or []
    return [str(k) for k in keys]


def _redis_prefix(keys: list[str]) -> str:
    return "".join(f"{k}:" for k in keys)


def _require_stack(require_redis: bool = False) -> None:
    missing = []
    if not d.container_running(P.TILE_SERVICES):
        missing.append(P.TILE_SERVICES)
    if not d.container_running(P.CAPABILITIES):
        missing.append(P.CAPABILITIES)
    if require_redis and not d.container_running(P.TIME_SERVICE):
        missing.append(P.TIME_SERVICE)
    if missing:
        _die(
            "required containers are not running: "
            + ", ".join(missing)
            + ". Start the stack with `docker compose up -d` from docker/."
        )
    _repair_tilematrixsets_xml()


_TMS_XML_DEST = "/etc/onearth/config/conf/tilematrixsets.xml"
_TMS_XML_SRC = (
    "/home/oe2/onearth/src/modules/gc_service/conf/tilematrixsets.xml"
)


def _repair_tilematrixsets_xml() -> None:
    """The containers' startup scripts don't copy tilematrixsets.xml into
    /etc/onearth/config/conf/ — it's not bundled in sample_configs/conf/.
    Its authoritative copy lives in the gc_service module source inside the
    image. If missing, copy it in so every downstream operation that reads
    the TMS definitions works. No-op if already present."""
    for container in (P.TILE_SERVICES, P.CAPABILITIES):
        present = d.exec_cmd(
            container,
            f"test -f {shlex.quote(_TMS_XML_DEST)}",
            check=False,
        ).returncode == 0
        if present:
            continue
        have_src = d.exec_cmd(
            container,
            f"test -f {shlex.quote(_TMS_XML_SRC)}",
            check=False,
        ).returncode == 0
        if not have_src:
            _die(
                f"{container}: {_TMS_XML_DEST} is missing and no source "
                f"copy found at {_TMS_XML_SRC}. Cannot repair automatically."
            )
        _info(
            f"  (repair) copying tilematrixsets.xml into {container} "
            f"from {_TMS_XML_SRC}"
        )
        d.exec_cmd(
            container,
            f"cp {shlex.quote(_TMS_XML_SRC)} {shlex.quote(_TMS_XML_DEST)}",
        )


def _require_endpoint(projection_dir: str, profile: str) -> str:
    endpoint = _endpoint_yaml_path(projection_dir, profile)
    for container in (P.TILE_SERVICES, P.CAPABILITIES):
        result = d.exec_cmd(
            container,
            f"test -f {shlex.quote(endpoint)}",
            check=False,
        )
        if result.returncode != 0:
            _die(
                f"endpoint config {endpoint} not found in container "
                f"{container}. Create the endpoint config first."
            )
    return endpoint


def _existing_mrf_regexps() -> list[tuple[str, str]]:
    """Return (conf_file, pattern) tuples for every MRF_RegExp directive
    currently loaded in onearth-tile-services."""
    result = d.exec_cmd(
        P.TILE_SERVICES,
        "grep -H '^[[:space:]]*MRF_RegExp' /etc/httpd/conf.d/*.conf || true",
    )
    pairs: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        # format: /etc/httpd/conf.d/foo.conf:        MRF_RegExp pattern
        if ":" not in line:
            continue
        path, rest = line.split(":", 1)
        parts = rest.split()
        if len(parts) >= 2 and parts[0] == "MRF_RegExp":
            pairs.append((Path(path).name, parts[1]))
    return pairs


def _find_layer_id_conflicts(layer_id: str) -> list[tuple[str, str, str]]:
    """Return [(conf_file, pattern, reason), ...] for existing MRF_RegExp
    patterns that would collide with layer_id.

    mod_mrf's MRF_RegExp is unanchored. Any existing regex that is a
    substring of the new layer_id — or vice versa — will match cross-layer
    requests and route them to the wrong MRF config.
    """
    conflicts: list[tuple[str, str, str]] = []
    for conf_file, pattern in _existing_mrf_regexps():
        if pattern == layer_id:
            conflicts.append((conf_file, pattern, "duplicate"))
        elif pattern in layer_id:
            conflicts.append((conf_file, pattern, "existing is a substring"))
        elif layer_id in pattern:
            conflicts.append((conf_file, pattern, "new id is a substring"))
    return conflicts


def _format_conflicts(layer_id: str, conflicts: list[tuple[str, str, str]]) -> str:
    lines = [
        f"layer id '{layer_id}' would collide with existing "
        "MRF_RegExp patterns (mod_mrf regexes are unanchored, so "
        "overlapping names route requests to the wrong config):"
    ]
    for conf_file, pattern, reason in conflicts:
        lines.append(f"  - {conf_file}: '{pattern}' ({reason})")
    return "\n".join(lines)


def _resolve_layer_id(layer_id: str) -> str:
    """Ensure layer_id doesn't collide with any existing MRF_RegExp. On a
    TTY, prompt for a replacement until a clean id is entered; otherwise
    die with the full conflict list."""
    while True:
        conflicts = _find_layer_id_conflicts(layer_id)
        if not conflicts:
            return layer_id
        print(_format_conflicts(layer_id, conflicts), file=sys.stderr)
        if not prompts.is_interactive():
            sys.exit(1)
        new_id = prompts.free_text(
            "Pick a different layer id (or Ctrl-C to abort)"
        )
        if not new_id:
            _die("aborted: no layer id provided")
        layer_id = new_id


def _list_endpoints() -> list[dict]:
    """Parse each endpoint YAML and return [{file, projection, profile,
    epsg_code}, ...] for endpoints we can register into.

    Reproject-only and non-standard endpoints (no layer_config_source, no
    epsg_code) are filtered out.
    """
    import yaml

    endpoints: list[dict] = []
    names = d.list_dir(P.TILE_SERVICES, "/etc/onearth/config/endpoint/")
    for name in names:
        if not name.endswith(".yaml"):
            continue
        stem = name[:-5]
        # Skip reproject-only endpoints — they can't host raster MRFs.
        if stem.endswith("_reproject"):
            continue
        if "_" not in stem:
            # e.g. oe-status.yaml — no profile, skip for v1 since it's
            # not a typical import target.
            continue
        projection_dir, profile = stem.split("_", 1)
        try:
            raw = d.read_file(
                P.TILE_SERVICES, f"/etc/onearth/config/endpoint/{name}"
            )
            doc = yaml.safe_load(raw) or {}
        except Exception:
            continue
        if "reproject" in doc:
            continue
        epsg_code = doc.get("epsg_code") or projection_dir.upper().replace(
            "EPSG", "EPSG:"
        )
        endpoints.append(
            {
                "file": name,
                "projection_dir": projection_dir,
                "profile": profile,
                "epsg_code": epsg_code,
            }
        )
    return endpoints


# Map OnEarth epsg_code → the CRS strings found in tilematrixsets.xml.
_CRS_BY_EPSG = {
    "EPSG:4326": {"urn:ogc:def:crs:OGC:1.3:CRS84", "urn:ogc:def:crs:EPSG::4326"},
    "EPSG:3857": {"urn:ogc:def:crs:EPSG::3857"},
    "EPSG:3031": {"urn:ogc:def:crs:EPSG::3031"},
    "EPSG:3413": {"urn:ogc:def:crs:EPSG::3413"},
    "EPSG:3411": {"urn:ogc:def:crs:EPSG::3411"},
}


def _list_tilematrixsets(epsg_code: str) -> list[dict]:
    """Return [{name, levels, grids}] for TMSes whose SupportedCRS matches.
    grids is a list of (level, matrix_width, matrix_height) tuples."""
    from xml.etree import ElementTree as ET

    raw = d.read_file(P.TILE_SERVICES, "/etc/onearth/config/conf/tilematrixsets.xml")
    root = ET.fromstring(raw)
    ns = {"ows": "http://www.opengis.net/ows/1.1"}
    allowed = _CRS_BY_EPSG.get(epsg_code.upper(), set())
    out: list[dict] = []
    for tms in root.findall(".//TileMatrixSet"):
        ident = tms.find("ows:Identifier", ns)
        crs = tms.find("ows:SupportedCRS", ns)
        if ident is None or crs is None:
            continue
        if allowed and crs.text not in allowed:
            continue
        grids = []
        for m in tms.findall("TileMatrix"):
            try:
                grids.append((
                    int(m.find("ows:Identifier", ns).text),
                    int(m.find("MatrixWidth").text),
                    int(m.find("MatrixHeight").text),
                ))
            except (AttributeError, TypeError, ValueError):
                continue
        out.append({
            "name": ident.text,
            "levels": len(grids),
            "grids": grids,
            "crs": crs.text,
        })
    return out


def _warn_if_tms_mismatch(
    tms_name: str, tms_grids: list[tuple[int, int, int]],
    mrf_base_w: int, mrf_base_h: int,
) -> None:
    """If no TMS level's matrix exactly matches the MRF's native tile grid,
    print a warning with both so the user can course-correct before the
    tile server silently misbehaves."""
    exact = any(w == mrf_base_w and h == mrf_base_h for _, w, h in tms_grids)
    if exact:
        return
    _info("")
    _info(
        f"  ⚠ TMS/MRF size mismatch: your MRF at native resolution is "
        f"{mrf_base_w}×{mrf_base_h} tiles, but no level of '{tms_name}' "
        f"has a matrix that size."
    )
    _info("    Available levels in this TMS:")
    for lvl, w, h in tms_grids:
        marker = "  ← closest" if (w >= mrf_base_w and h >= mrf_base_h) else ""
        _info(f"      level {lvl}: {w}×{h} tiles{marker}")
    _info(
        "    The layer will register, but mod_mrf may fail to serve tiles "
        "or serve them at the wrong scale. Consider a TMS whose finest "
        "level matches your MRF's native tile grid."
    )


def _resolve_add_layer_args(
    *,
    projection: str | None,
    profile: str | None,
    tilematrixset: str | None,
    static: bool,
    time_config: str | None,
    mrf_has_datestamp: bool,
    datestamp_iso: str | None = None,
) -> tuple[str, str, str, bool, str | None]:
    """Fill in missing args interactively (TTY) or fail with a clear error.
    Returns (projection, profile, tilematrixset, static, time_config)."""
    interactive = prompts.is_interactive()

    # projection + profile
    if not projection:
        if not interactive:
            _die(
                "--projection is required (non-interactive mode). Example: "
                "--projection EPSG:4326 --profile std"
            )
        endpoints = _list_endpoints()
        if not endpoints:
            _die(
                "No projection endpoints are installed in the running stack."
            )
        choices = [
            prompts.Choice(
                value=f"{ep['epsg_code']}|{ep['profile']}",
                label=f"{ep['epsg_code']}  (profile: {ep['profile']})",
            )
            for ep in endpoints
        ]
        picked = prompts.pick_one(
            "Which projection endpoint do you want to register into?",
            choices,
            default=0 if len(endpoints) == 1 else None,
        )
        projection, profile = picked.split("|", 1)
    elif profile is None:
        profile = "std"

    # tilematrixset
    if not tilematrixset:
        if not interactive:
            _die("--tilematrixset is required (non-interactive mode).")
        tmses = _list_tilematrixsets(projection)
        if not tmses:
            _die(
                f"No TileMatrixSets found for {projection} in "
                "/etc/onearth/config/conf/tilematrixsets.xml"
            )
        choices = [
            prompts.Choice(
                value=t["name"], label=f"{t['name']}  ({t['levels']} levels)"
            )
            for t in tmses
        ]
        tilematrixset = prompts.pick_one(
            f"Which TileMatrixSet should this layer use? "
            f"(CRS: {projection})",
            choices,
            default=0 if len(tmses) == 1 else None,
        )

    # time mode: static vs temporal
    if not static and not time_config:
        if not interactive:
            _die(
                "Pass either --static or --time-config "
                "(non-interactive mode)."
            )
        is_temporal = prompts.yes_no(
            "Is this a temporal (time-varying) layer?",
            default=mrf_has_datestamp,
        )
        if is_temporal:
            # If the MRF filename encodes a date, build a one-day period
            # around it — that's immediately servable. DETECT would need a
            # separate periods.py scan to populate Redis, so warn about it.
            suggestion = (
                f"{datestamp_iso}/{datestamp_iso}/P1D"
                if datestamp_iso
                else "DETECT"
            )
            raw = prompts.free_text(
                "Time config (period string like "
                "'2020-01-01/2020-12-31/P1D', or 'DETECT' to scan idx files)",
                default=suggestion,
            )
            time_config = raw or suggestion
            if time_config.upper() == "DETECT":
                _info(
                    "  note: 'DETECT' requires a separate scan "
                    "(e.g. periods.py) to populate Redis time entries. "
                    "The layer will be registered but won't serve tiles "
                    "until that scan runs."
                )
        else:
            static = True

    return projection, profile or "std", tilematrixset, static, time_config


def _fetch_skipped_levels(
    projection_dir: str,
    profile: str,
    layer_id: str,
    tilematrixset: str,
) -> int:
    """Read the generated mod_mrf.config for the layer and return its
    SkippedLevels value (defaulting to 0)."""
    path = (
        f"/var/www/html/wmts/{projection_dir}/{profile}/"
        f"{layer_id}/default/{tilematrixset}/mod_mrf.config"
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


def add_layer(
    header_path: Path,
    *,
    projection: str | None,
    profile: str | None,
    tilematrixset: str | None,
    layer_id: str | None,
    title: str | None,
    static: bool,
    time_config: str | None,
) -> None:
    mrf = parse_mrf(header_path)
    resolved_id = layer_id or mrf.base_name

    # Anything required but missing → prompt when interactive, else die.
    # _require_stack must run first so the prompts can read container state.
    _require_stack()

    # Resolve collisions up-front so the user doesn't answer a bunch of
    # prompts only to be told their layer id won't work.
    resolved_id = _resolve_layer_id(resolved_id)

    projection, profile, tilematrixset, static, time_config = (
        _resolve_add_layer_args(
            projection=projection,
            profile=profile,
            tilematrixset=tilematrixset,
            static=static,
            time_config=time_config,
            mrf_has_datestamp=mrf.date_stamp is not None,
            datestamp_iso=_datestamp_to_iso(mrf.date_stamp),
        )
    )
    is_temporal = not static

    # Sanity-check: does this TMS even fit the MRF's native tile grid?
    import math as _math
    mrf_base_w = _math.ceil(mrf.size_x / mrf.tile_size_x)
    mrf_base_h = _math.ceil(mrf.size_y / mrf.tile_size_y)
    tms_matches = [
        t for t in _list_tilematrixsets(projection) if t["name"] == tilematrixset
    ]
    if tms_matches:
        _warn_if_tms_mismatch(
            tilematrixset, tms_matches[0]["grids"], mrf_base_w, mrf_base_h
        )

    if static and mrf.date_stamp:
        _info(
            f"note: filename contains a date stamp ({mrf.date_stamp}) but "
            "the layer is being stored as static."
        )

    spec = LayerSpec(
        layer_id=resolved_id,
        projection=projection.upper(),
        profile=profile,
        tilematrixset=tilematrixset,
        title=title,
        static=static,
        time_config=time_config,
    )

    if is_temporal and not d.container_running(P.TIME_SERVICE):
        _die(
            f"temporal layers require '{P.TIME_SERVICE}' to be running."
        )
    _require_endpoint(spec.projection_dir, profile)

    idx_dir = P.IDX_DIR.format(
        projection=spec.projection_dir, layer_id=resolved_id
    )
    data_dir = P.DATA_DIR.format(
        projection=spec.projection_dir, layer_id=resolved_id
    )
    yaml_dir = P.LAYER_YAML_DIR.format(
        projection=spec.projection_dir, profile=profile
    )
    yaml_name = f"{resolved_id}.yaml"

    _info(f"Importing layer '{resolved_id}' ({spec.projection} / {profile})")
    if mrf.stored_variant == "BRUNSLI":
        _info(
            "  detected Brunsli tile payload — layer YAML will use "
            "mime_type: image/x-j so oe2_wmts_configure.py wires up "
            "mod_brunsli's DBRUNSLI output filter."
        )
    elif mrf.stored_variant == "UNKNOWN_JPEG_VARIANT":
        _info(
            "  warning: tile payload isn't standard JPEG or Brunsli "
            "(may be ZenJPEG or similar). mime_type will default to "
            f"{mrf.mime_type}; you may need to edit the layer YAML."
        )

    # Rename files to {layer_id}[-{datestamp}].{ext} so Apache's filename
    # template matches (mod_wmts_wrapper sets ${filename} = layer_id-datestamp).
    idx_target = _retarget_filename(mrf.idx_path.name, mrf.base_name, resolved_id)
    data_target = _retarget_filename(mrf.data_path.name, mrf.base_name, resolved_id)
    mrf_target = _retarget_filename(
        mrf.header_path.name, mrf.base_name, resolved_id
    )

    _info(f"  → staging .idx to {P.TILE_SERVICES}:{idx_dir}/{idx_target}")
    d.mkdir_p(P.TILE_SERVICES, idx_dir)
    d.cp_to(P.TILE_SERVICES, mrf.idx_path, f"{idx_dir}/{idx_target}")

    _info(f"  → staging data + header to {P.TILE_SERVICES}:{data_dir}")
    d.mkdir_p(P.TILE_SERVICES, data_dir)
    d.cp_to(P.TILE_SERVICES, mrf.data_path, f"{data_dir}/{data_target}")
    d.cp_to(P.TILE_SERVICES, mrf.header_path, f"{data_dir}/{mrf_target}")

    # Write the layer YAML into both tile-services and capabilities.
    layer_yaml = build_layer_yaml(spec, mrf)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False
    ) as fh:
        fh.write(layer_yaml)
        tmp_yaml = Path(fh.name)
    try:
        for container in (P.TILE_SERVICES, P.CAPABILITIES):
            _info(f"  → writing layer YAML to {container}:{yaml_dir}/{yaml_name}")
            d.mkdir_p(container, yaml_dir)
            d.cp_to(container, tmp_yaml, f"{yaml_dir}/{yaml_name}")
    finally:
        tmp_yaml.unlink(missing_ok=True)

    # Seed Redis for temporal layers.
    if is_temporal and time_config and time_config.upper() != "DETECT":
        ts_keys = _read_time_service_keys(spec.projection_dir, profile)
        prefix = _redis_prefix(ts_keys)
        _info(
            f"  → seeding Redis time entries for {resolved_id} "
            f"(key prefix: '{prefix}')"
        )
        default_date = time_config.split("/")[0]
        d.redis_cli(["SET", f"{prefix}layer:{resolved_id}:default", default_date])
        d.redis_cli(
            [
                "ZADD",
                f"{prefix}layer:{resolved_id}:periods",
                "0",
                time_config,
            ]
        )
    elif is_temporal:
        _info(
            "  → skipping Redis seed (time_config=DETECT — time service will "
            "scan the idx files)"
        )

    # Regenerate configs in both containers and restart Apache.
    endpoint = _endpoint_yaml_path(spec.projection_dir, profile)
    _info(f"  → regenerating configs in {P.TILE_SERVICES}")
    d.exec_cmd(
        P.TILE_SERVICES,
        f"{P.WMTS_CONFIGURE} {shlex.quote(endpoint)}",
    )
    _info(f"  → regenerating GC endpoint in {P.CAPABILITIES}")
    d.exec_cmd(
        P.CAPABILITIES,
        f"{P.GC_CONFIGURE} {shlex.quote(endpoint)}",
    )

    for container in (P.TILE_SERVICES, P.CAPABILITIES):
        _info(f"  → restarting Apache in {container}")
        d.exec_cmd(container, P.HTTPD_RESTART)

    _post_import_check(spec, mrf, resolved_id, time_config)


def _post_import_check(
    spec: LayerSpec,
    mrf,
    layer_id: str,
    time_config: str | None,
) -> None:
    """Fetch tile 0/0/0 and report pass/fail. Always prints the URLs the
    user can use to probe further."""
    tile_ext = {
        "image/jpeg": "jpg",
        "image/png": "png",
    }.get(mrf.mime_type, "bin")
    base = f"{_base_url()}/wmts/{spec.projection_dir}/{spec.profile}"
    # Use the explicit default date for temporal layers — the time service's
    # 'default' keyword handling is unreliable for freshly-seeded layers
    # (observed: returns 'Invalid Layer' even when Redis has the right keys).
    default_date = _default_date_from_time_config(time_config)
    if spec.static:
        mid = "default"
    elif default_date:
        mid = f"default/{default_date}"
    else:
        mid = "default/default"  # fallback (e.g. DETECT mode)
    # TMS z-levels start at 0 regardless of mod_mrf's SkippedLevels setting
    # (SkippedLevels is internal: how many MRF pyramid levels sit above the
    # TMS's coarsest; the URL's z-level indexes the TMS, not the MRF).
    rest = (
        f"{base}/{layer_id}/{mid}/{spec.tilematrixset}/0/0/0.{tile_ext}"
    )
    gc = f"{base}/1.0.0/WMTSCapabilities.xml"

    _info("")
    _info("Checking tile 0/0/0...")
    _info(f"  {rest}")
    ok, msg = _http_get(rest, binary=True)
    if ok:
        _info(f"  PASS — {msg}")
    else:
        _info(f"  FAIL — {msg}")
        _info(
            "  The layer is registered but not serving. "
            "Check `docker exec onearth-tile-services tail /etc/httpd/logs/error_log`."
        )

    _info("")
    _info("URLs:")
    _info(f"  REST tile:       {rest}")
    _info(f"  GetCapabilities: {gc}")
    if not spec.static and default_date:
        _info(f"  (date segment is the layer's default: {default_date})")
    _info("")
    profile_flag = f" --profile {spec.profile}" if spec.profile != "std" else ""
    _info(
        f"To stitch all tiles into one image:\n"
        f"  onearth-import stitch {layer_id} --projection {spec.projection}"
        f"{profile_flag} --tilematrixset {spec.tilematrixset}"
    )


def verify(layer_id: str, projection: str, profile: str, tilematrixset: str) -> None:
    _require_stack()
    projection_dir = projection.replace(":", "").lower()
    profile = _resolve_layer_profile_for_cli(projection_dir, profile, layer_id)
    base = f"{_base_url()}/wmts/{projection_dir}/{profile}"
    # Look up the layer's default date from Redis so we can use it explicitly
    # (the 'default' keyword in the URL is unreliable for new layers).
    default_date = _layer_default_date(projection_dir, profile, layer_id)
    times = [default_date, "default"] if default_date else ["default"]

    ok = False
    for t in times:
        for ext in ("jpg", "png"):
            for lvl in (1, 0):
                url = (
                    f"{base}/{layer_id}/default/{t}/{tilematrixset}/"
                    f"{lvl}/0/0.{ext}"
                )
                print(f"Fetching tile: {url}")
                tile_ok, msg = _http_get(url, binary=True)
                if tile_ok:
                    print(f"  OK — {msg}")
                    ok = True
                    break
                print(f"  fail — {msg}")
            if ok:
                break
        if ok:
            break

    sys.exit(0 if ok else 1)


def _resolve_layer_profile_for_cli(
    projection_dir: str, preferred: str, layer_id: str
) -> str:
    """Return the profile under which layer_id's YAML actually lives.
    Checks the preferred first, then scans sibling profiles. Exits with an
    error if nowhere or in more than one place."""
    base = f"/etc/onearth/config/layers/{projection_dir}"
    preferred_path = f"{base}/{preferred}/{layer_id}.yaml"
    if d.exec_cmd(
        P.TILE_SERVICES,
        f"test -f {shlex.quote(preferred_path)}",
        check=False,
    ).returncode == 0:
        return preferred

    result = d.exec_cmd(
        P.TILE_SERVICES,
        f"find {shlex.quote(base)} -maxdepth 2 -name {shlex.quote(layer_id + '.yaml')} -type f",
        check=False,
    )
    matches = [line for line in result.stdout.splitlines() if line.strip()]
    if not matches:
        _die(
            f"layer '{layer_id}' not found under {base}/*/. "
            f"Is it registered? Run `onearth-import list` to check."
        )
    if len(matches) > 1:
        profiles = sorted({m.split("/")[-2] for m in matches})
        _die(
            f"layer '{layer_id}' exists under multiple profiles "
            f"({', '.join(profiles)}). Specify --profile explicitly."
        )
    found_profile = matches[0].split("/")[-2]
    print(f"(auto-detected profile: {found_profile})", file=sys.stderr)
    return found_profile


def _layer_default_date(
    projection_dir: str, profile: str, layer_id: str
) -> str | None:
    """Return the layer's default date by reading the Redis key that the
    time service consults. None if it's not set (static layer or DETECT)."""
    ts_keys = _read_time_service_keys(projection_dir, profile)
    prefix = _redis_prefix(ts_keys)
    result = d.exec_cmd(
        P.TIME_SERVICE,
        f"redis-cli -n 0 GET {shlex.quote(prefix + 'layer:' + layer_id + ':default')}",
        check=False,
    )
    val = result.stdout.strip()
    return val or None


def _http_get(
    url: str, binary: bool = False
) -> tuple[bool, str]:
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=10, context=ctx) as resp:
            body = resp.read()
            if binary:
                return True, f"HTTP {resp.status}, {len(body)} bytes"
            return True, body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return False, f"URL error: {e.reason}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def doctor() -> None:
    status = {
        P.TILE_SERVICES: d.container_running(P.TILE_SERVICES),
        P.CAPABILITIES: d.container_running(P.CAPABILITIES),
        P.TIME_SERVICE: d.container_running(P.TIME_SERVICE),
    }
    for name, running in status.items():
        print(f"  {name}: {'running' if running else 'NOT running'}")

    if not all(status.values()):
        print("\nOne or more containers are not running.")
        sys.exit(1)

    print("\nRepairing missing config files (if any):")
    _repair_tilematrixsets_xml()

    print("\nApache reachability:")
    url = f"{_base_url()}/oe-status/Raster_Status/default/2004-08-01/16km/0/0/0.jpeg"
    ok, msg = _http_get(url, binary=True)
    print(f"  {_base_url()}/  →  {'OK' if ok else 'FAIL'}  ({msg})")


def list_layers() -> None:
    _require_stack()
    base = "/etc/onearth/config/layers"
    result = d.exec_cmd(
        P.TILE_SERVICES,
        f"find {base} -name '*.yaml' -type f | sort",
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        print("(no layers registered)")
        return
    for line in result.stdout.splitlines():
        rel = line[len(base) + 1 :]  # strip "/etc/onearth/config/layers/"
        # rel looks like: epsg4326/std/MODIS_....yaml
        parts = rel.split("/")
        if len(parts) >= 3:
            projection_dir, profile, fname = parts[0], parts[1], parts[-1]
            print(
                f"  {fname[:-5]:<60}  {projection_dir}/{profile}"
            )
        else:
            print(f"  {rel}")


def remove_layer(
    layer_id: str, projection: str, profile: str
) -> None:
    _require_stack(require_redis=True)
    projection_dir = projection.replace(":", "").lower()
    idx_dir = P.IDX_DIR.format(projection=projection_dir, layer_id=layer_id)
    data_dir = P.DATA_DIR.format(projection=projection_dir, layer_id=layer_id)
    yaml_path = (
        P.LAYER_YAML_DIR.format(projection=projection_dir, profile=profile)
        + f"/{layer_id}.yaml"
    )

    _info(f"Removing layer '{layer_id}' ({projection.upper()} / {profile})")
    d.rm_rf(P.TILE_SERVICES, idx_dir)
    d.rm_rf(P.TILE_SERVICES, data_dir)
    d.rm_rf(P.TILE_SERVICES, yaml_path)
    d.rm_rf(P.CAPABILITIES, yaml_path)
    ts_keys = _read_time_service_keys(projection_dir, profile)
    prefix = _redis_prefix(ts_keys)
    d.redis_cli(["DEL", f"{prefix}layer:{layer_id}:default"])
    d.redis_cli(["DEL", f"{prefix}layer:{layer_id}:periods"])

    endpoint = _endpoint_yaml_path(projection_dir, profile)
    # Best-effort regen; ignore errors if the endpoint no longer has any layers.
    d.exec_cmd(
        P.TILE_SERVICES,
        f"{P.WMTS_CONFIGURE} {shlex.quote(endpoint)}",
        check=False,
    )
    d.exec_cmd(
        P.CAPABILITIES,
        f"{P.GC_CONFIGURE} {shlex.quote(endpoint)}",
        check=False,
    )
    for container in (P.TILE_SERVICES, P.CAPABILITIES):
        d.exec_cmd(container, P.HTTPD_RESTART)
    _info("Done.")
