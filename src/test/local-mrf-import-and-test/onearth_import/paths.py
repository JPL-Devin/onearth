"""Known paths and commands inside OnEarth containers."""

TILE_SERVICES = "onearth-tile-services"
CAPABILITIES = "onearth-capabilities"
TIME_SERVICE = "onearth-time-service"

IDX_DIR = "/onearth/idx/{projection}/{layer_id}"
DATA_DIR = "/onearth/layers/{projection}/{layer_id}"
LAYER_YAML_DIR = "/etc/onearth/config/layers/{projection}/{profile}"
ENDPOINT_YAML = "/etc/onearth/config/endpoint/{projection}_{profile}.yaml"

WMTS_CONFIGURE = "python3 /usr/bin/oe2_wmts_configure.py"
GC_CONFIGURE = "lua /home/oe2/onearth/src/modules/gc_service/make_gc_endpoint.lua"
# `-k graceful` signals the running master to reread config (SIGUSR1) rather
# than fork a new parent. Avoids the three-stale-httpd-parents bug we saw
# when using `-k restart` repeatedly in this container.
HTTPD_RESTART = "/usr/sbin/httpd -k graceful"

MIME_TO_TILE_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tiff",
    "image/lerc": "lerc",
}

COMPRESSION_TO_MIME = {
    "JPEG": "image/jpeg",
    "JPG": "image/jpeg",
    "PNG": "image/png",
    "PPNG": "image/png",
    "EPNG": "image/png",
    "JPNG": "image/png",
    "TIFF": "image/tiff",
    "LERC": "image/lerc",
}

COMPRESSION_TO_DATA_EXT = {
    "JPEG": "pjg",
    "JPG": "pjg",
    "PNG": "ppg",
    "PPNG": "ppg",
    "EPNG": "ppg",
    "JPNG": "pjp",
    "TIFF": "ptf",
    "LERC": "lrc",
}

# JPEG-family variants detected by looking at the first tile's magic bytes
# rather than the MRF <Compression> element (which always says "JPEG").
# Used by --diff to refuse cleanly instead of feeding undecodable bytes
# to PIL. mod_brunsli / mod_convert transcode these to standard JPEG at
# serve time, so the HTTP path is unaffected.
DIFF_UNSUPPORTED_VARIANTS = {"BRUNSLI", "ZENJPEG"}

# First bytes of a tile payload that identify the stored format. JPEG is
# the canonical SOI marker; Brunsli's signature is documented in its spec.
TILE_MAGIC = {
    "BRUNSLI": bytes([0x0A, 0x04, 0x42, 0xD2, 0xD5, 0x4E]),
    "JPEG":    bytes([0xFF, 0xD8, 0xFF]),
    "PNG":     bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]),
}
