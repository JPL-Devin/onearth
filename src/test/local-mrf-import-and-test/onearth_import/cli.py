"""Argparse dispatch for onearth-import."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import commands


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="onearth-import",
        description=(
            "Import MRF layers into a running local OnEarth docker-compose "
            "stack."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    add = sub.add_parser(
        "add-layer",
        help="Register a new MRF layer",
        description=(
            "Register a new MRF layer. Missing --projection, --tilematrixset, "
            "or time mode will be prompted for when run in an interactive "
            "terminal; in scripts, pass all of them explicitly."
        ),
    )
    add.add_argument("mrf", type=Path, help="Path to the .mrf header file")
    add.add_argument(
        "--projection",
        help="e.g. EPSG:4326, EPSG:3857, EPSG:3031, EPSG:3413",
    )
    add.add_argument(
        "--profile",
        help="Endpoint profile to register into (default: std)",
    )
    add.add_argument(
        "--tilematrixset",
        help="Tile matrix set name, e.g. 250m, GoogleMapsCompatible_Level6",
    )
    add.add_argument(
        "--layer-id",
        help="Layer ID (defaults to the MRF filename stem, with any date "
        "stamp stripped)",
    )
    add.add_argument("--title", help="Human-readable title")
    time_group = add.add_mutually_exclusive_group()
    time_group.add_argument(
        "--static",
        action="store_true",
        help="Layer has no time dimension",
    )
    time_group.add_argument(
        "--time-config",
        help="Time config string, e.g. 'DETECT' or "
        "'2020-01-01/2020-12-31/P1D'",
    )

    verify = sub.add_parser(
        "verify",
        help="Hit GetCapabilities + one tile to confirm a layer is serving",
    )
    verify.add_argument("layer_id")
    verify.add_argument("--projection", required=True)
    verify.add_argument("--profile", default="std")
    verify.add_argument("--tilematrixset", required=True)

    sub.add_parser("doctor", help="Check that the stack is up and reachable")
    sub.add_parser("list", help="List layers registered in tile-services")

    rm = sub.add_parser("remove", help="Remove a previously imported layer")
    rm.add_argument("layer_id")
    rm.add_argument("--projection", required=True)
    rm.add_argument("--profile", default="std")

    st = sub.add_parser(
        "stitch",
        help=(
            "Fetch all tiles of a layer at a chosen TileMatrixSet level and "
            "compose them into a single image. Without --level, lists the "
            "available levels and their sizes."
        ),
    )
    st.add_argument("layer_id")
    st.add_argument("--projection", required=True)
    st.add_argument("--profile", default="std")
    st.add_argument("--tilematrixset", required=True)
    st.add_argument(
        "--level",
        type=int,
        help="TileMatrix level to stitch. Omit to list available levels.",
    )
    st.add_argument(
        "--out",
        type=Path,
        help="Output file path (default: <layer_id>_<tms>_L<N>.<ext>)",
    )
    st.add_argument(
        "--date",
        help="For temporal layers: YYYY-MM-DD to fetch (default: use default date)",
    )
    st.add_argument(
        "--max-tiles",
        type=int,
        default=1024,
        help="Refuse to stitch a level with more tiles than this (default 1024)",
    )
    st.add_argument(
        "--diff",
        action="store_true",
        help=(
            "Also stitch the same level directly from the source MRF "
            "(bypassing the tile server) and compare. Saves three files: "
            "<prefix>-served.<ext>, <prefix>-source.<ext>, <prefix>-diff.png; "
            "prints mean / max / %% differing pixels."
        ),
    )

    return p


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "add-layer":
        commands.add_layer(
            args.mrf,
            projection=args.projection,
            profile=args.profile,
            tilematrixset=args.tilematrixset,
            layer_id=args.layer_id,
            title=args.title,
            static=args.static,
            time_config=args.time_config,
        )
    elif args.command == "verify":
        commands.verify(
            args.layer_id,
            projection=args.projection,
            profile=args.profile,
            tilematrixset=args.tilematrixset,
        )
    elif args.command == "doctor":
        commands.doctor()
    elif args.command == "list":
        commands.list_layers()
    elif args.command == "remove":
        commands.remove_layer(
            args.layer_id,
            projection=args.projection,
            profile=args.profile,
        )
    elif args.command == "stitch":
        from . import stitch as stitch_mod

        stitch_mod.stitch(
            layer_id=args.layer_id,
            projection=args.projection,
            profile=args.profile,
            tilematrixset=args.tilematrixset,
            level=args.level,
            out=args.out,
            date=args.date,
            max_tiles=args.max_tiles,
            diff=args.diff,
        )


if __name__ == "__main__":
    main()
