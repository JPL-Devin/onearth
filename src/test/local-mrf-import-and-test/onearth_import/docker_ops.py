"""Thin wrappers around the docker CLI."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path


class DockerError(RuntimeError):
    pass


def _run(cmd: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=capture,
        text=True,
    )
    if check and result.returncode != 0:
        raise DockerError(
            f"`{' '.join(shlex.quote(c) for c in cmd)}` exited {result.returncode}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result


def container_running(name: str) -> bool:
    result = _run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def cp_to(container: str, src: Path, dest: str) -> None:
    """Copy a file into the container and make it world-readable.

    docker cp preserves the host file's uid/gid (often a numeric uid that
    doesn't exist in the container) and can land files with 0600 perms.
    Services in the container typically run as www-data, so without a
    chmod the copy is unreadable by the very processes meant to use it.
    """
    if not src.exists():
        raise FileNotFoundError(src)
    _run(["docker", "cp", str(src), f"{container}:{dest}"])
    exec_cmd(container, f"chmod 0644 {shlex.quote(dest)}")


def exec_cmd(
    container: str,
    cmd: str,
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    # -u 0 forces root inside the container. docker cp leaves files owned
    # by the host uid (often foreign to the container), so subsequent
    # chmod/mkdir/rm by the container's default user (typically www-data)
    # fail with EPERM. Running all CLI-initiated exec as root sidesteps
    # that uid tangle and matches what a user would do manually.
    return _run(
        ["docker", "exec", "-u", "0", container, "sh", "-c", cmd],
        check=check,
        capture=capture,
    )


def mkdir_p(container: str, path: str) -> None:
    exec_cmd(container, f"mkdir -p {shlex.quote(path)}")


def rm_rf(container: str, path: str) -> None:
    exec_cmd(container, f"rm -rf {shlex.quote(path)}")


def read_file(container: str, path: str) -> str:
    return exec_cmd(container, f"cat {shlex.quote(path)}").stdout


def list_dir(container: str, path: str) -> list[str]:
    result = exec_cmd(
        container,
        f"ls -1 {shlex.quote(path)} 2>/dev/null || true",
    )
    return [line for line in result.stdout.splitlines() if line]


def redis_cli(args: list[str]) -> str:
    quoted = " ".join(shlex.quote(a) for a in args)
    return exec_cmd(
        "onearth-time-service",
        f"redis-cli -n 0 {quoted}",
    ).stdout.strip()


def compose_ps_json(project_dir: Path) -> list[dict]:
    """Return `docker compose ps --format json` rows, parsed."""
    result = _run(
        ["docker", "compose", "ps", "--format", "json"],
        check=False,
    )
    if result.returncode != 0:
        return []
    rows: list[dict] = []
    text = result.stdout.strip()
    if not text:
        return rows
    # newer compose emits one JSON object per line; older emits a JSON array.
    if text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
