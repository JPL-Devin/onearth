"""Generate the layer-config YAML consumed by oe2_wmts_configure.py."""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from .mrf import MrfTriplet


@dataclass
class LayerSpec:
    layer_id: str
    projection: str
    profile: str
    tilematrixset: str
    title: str | None
    static: bool
    time_config: str | None

    @property
    def projection_dir(self) -> str:
        """Directory token used in container paths (e.g. 'epsg4326')."""
        return self.projection.replace(":", "").lower()


def build_layer_yaml(spec: LayerSpec, mrf: MrfTriplet) -> str:
    # Brunsli layers declare <Compression>JPEG</Compression> in the header
    # but carry Brunsli-encoded bytes. oe2_wmts_configure.py keys off the
    # image/x-j mime to load mod_brunsli and attach the DBRUNSLI output
    # filter that decodes to standard JPEG before tiles hit the client.
    mime_type = (
        "image/x-j" if mrf.stored_variant == "BRUNSLI" else mrf.mime_type
    )
    empty_tile = (
        "/etc/onearth/empty_tiles/Blank_RGB_512.jpg"
        if mime_type in ("image/jpeg", "image/x-j")
        else "/etc/onearth/empty_tiles/transparent.png"
    )
    # Static vs temporal have different idx_path/data_file_uri conventions:
    #   - Temporal: directory paths; mod_wmts_wrapper substitutes
    #     ${prefix}/${filename}.<ext> at request time using layer + date.
    #   - Static:   full file paths; there's no date, so nothing to fill in.
    data_ext = mrf.data_ext
    if spec.static:
        idx_path = (
            f"/onearth/idx/{spec.projection_dir}/{spec.layer_id}/"
            f"{spec.layer_id}.idx"
        )
        data_file_uri = (
            f"/onearth/layers/{spec.projection_dir}/{spec.layer_id}/"
            f"{spec.layer_id}.{data_ext}"
        )
    else:
        idx_path = f"/onearth/idx/{spec.projection_dir}"
        data_file_uri = f"/onearth/layers/{spec.projection_dir}"

    doc: dict = {
        "layer_id": spec.layer_id,
        "layer_title": spec.title or spec.layer_id,
        "layer_name": f"{spec.layer_id} tileset",
        "projection": spec.projection,
        "tilematrixset": spec.tilematrixset,
        "mime_type": mime_type,
        "static": spec.static,
        "abstract": f"{spec.layer_id} imported via onearth-import",
        "metadata": [{}],
        "source_mrf": {
            "size_x": mrf.size_x,
            "size_y": mrf.size_y,
            "bands": mrf.bands,
            "tile_size_x": mrf.tile_size_x,
            "tile_size_y": mrf.tile_size_y,
            "idx_path": idx_path,
            "data_file_uri": data_file_uri,
            "year_dir": False,
            "bbox": mrf.bbox or _default_bbox(spec.projection),
            "empty_tile": empty_tile,
        },
    }

    if not spec.static and spec.time_config:
        doc["time_config"] = spec.time_config

    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def _default_bbox(projection: str) -> str:
    return {
        "EPSG:4326": "-180,-90,180,90",
        "EPSG:3857": "-20037508.34,-20037508.34,20037508.34,20037508.34",
        "EPSG:3031": "-4194304,-4194304,4194304,4194304",
        "EPSG:3413": "-4194304,-4194304,4194304,4194304",
    }.get(projection.upper(), "-180,-90,180,90")
