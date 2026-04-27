"""Decode Brunsli tile bytes into JPEG by shelling out to python3 inside
the onearth-tile-services container, which has libbrunslidec-c installed.

No Python Brunsli binding exists on PyPI, and we don't want to add a
host-side native dep. The container already links mod_brunsli against
/usr/lib64/libbrunslidec-c.so, so ctypes-calling it there gets us a
decoder 'for free'.

We keep the script embedded rather than copying a file in, so there are
no filesystem artifacts in the container.
"""

from __future__ import annotations

import subprocess

from . import paths as P

# Runs inside onearth-tile-services. Reads a stream of
# [4-byte big-endian length N][N Brunsli bytes] records from stdin,
# emits [4-byte big-endian length M][M JPEG bytes] for each, or a
# length of 0 to signal a decode error for that tile.
_DECODER_SCRIPT = r'''
import sys, ctypes, struct

lib = ctypes.CDLL("/usr/lib64/libbrunslidec-c.so")
Sink = ctypes.CFUNCTYPE(
    ctypes.c_size_t,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint8),
    ctypes.c_size_t,
)
lib.DecodeBrunsli.argtypes = [
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_uint8),
    ctypes.c_void_p,
    Sink,
]
lib.DecodeBrunsli.restype = ctypes.c_int

stdin = sys.stdin.buffer
stdout = sys.stdout.buffer

while True:
    header = stdin.read(4)
    if len(header) < 4:
        break
    (n,) = struct.unpack(">I", header)
    if n == 0:
        break  # caller signaled end-of-stream
    data = stdin.read(n)
    if len(data) != n:
        # truncated input — emit zero-length tile and stop
        stdout.write(struct.pack(">I", 0))
        break
    buf = (ctypes.c_uint8 * n).from_buffer_copy(data)
    out = bytearray()

    def cb(_ctx, ptr, size):
        out.extend(ctypes.string_at(ptr, size))
        return size

    ok = lib.DecodeBrunsli(n, buf, None, Sink(cb))
    if ok:
        stdout.write(struct.pack(">I", len(out)))
        stdout.write(bytes(out))
    else:
        stdout.write(struct.pack(">I", 0))
    stdout.flush()
'''


class BrunsliDecoder:
    """Spawn a single python3 inside the container and stream many tiles
    through it, so we pay the docker-exec startup cost once per --diff
    invocation instead of once per tile."""

    def __init__(self, container: str = P.TILE_SERVICES):
        self._proc = subprocess.Popen(
            [
                "docker",
                "exec",
                "-u",
                "0",
                "-i",
                container,
                "python3",
                "-c",
                _DECODER_SCRIPT,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def decode(self, brunsli_bytes: bytes) -> bytes:
        import struct

        if not brunsli_bytes:
            raise ValueError("decode() got empty input")
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._proc.stdin.write(struct.pack(">I", len(brunsli_bytes)))
        self._proc.stdin.write(brunsli_bytes)
        self._proc.stdin.flush()
        header = self._proc.stdout.read(4)
        if len(header) < 4:
            stderr = self._proc.stderr.read() if self._proc.stderr else b""
            raise RuntimeError(
                f"brunsli decoder closed unexpectedly: "
                f"{stderr.decode(errors='replace')}"
            )
        (n,) = struct.unpack(">I", header)
        if n == 0:
            raise RuntimeError("brunsli decode returned 0 bytes")
        out = b""
        while len(out) < n:
            chunk = self._proc.stdout.read(n - len(out))
            if not chunk:
                raise RuntimeError("brunsli decoder stream truncated")
            out += chunk
        return out

    def close(self) -> None:
        import struct

        if self._proc.stdin:
            try:
                self._proc.stdin.write(struct.pack(">I", 0))
                self._proc.stdin.close()
            except BrokenPipeError:
                pass
        self._proc.wait(timeout=5)

    def __enter__(self) -> "BrunsliDecoder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
