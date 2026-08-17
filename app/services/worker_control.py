"""Controlled operations for background worker services."""

from __future__ import annotations

import http.client
import os
import re
import shlex
import socket
import subprocess  # nosec - command is built from a validated template/target.
from dataclasses import dataclass
from urllib.parse import quote

from app.services import infrastructure_health

_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_DEFAULT_RESTART_COMMAND = "docker compose restart {target}"
_DOCKER_SOCKET = "/var/run/docker.sock"
_DEFAULT_RESTART_CONTAINERS = {
    "celery-worker": "dotmac_sub_celery_worker",
    "celery-worker-notifications-immediate": (
        "dotmac_sub_celery_worker_notifications_immediate"
    ),
    "celery-worker-notifications": "dotmac_sub_celery_worker_notifications",
    "celery-worker-tr069": "dotmac_sub_celery_worker_tr069",
    "celery-worker-bandwidth": "dotmac_sub_celery_worker_bandwidth",
    "celery-worker-monitoring": "dotmac_sub_celery_worker_monitoring",
    "celery-worker-billing": "dotmac_sub_celery_worker_billing",
}


@dataclass(frozen=True)
class WorkerRestartResult:
    target: str
    ok: bool
    message: str
    returncode: int | None = None


def restart_enabled() -> bool:
    return infrastructure_health._celery_worker_restart_enabled()


def allowed_restart_targets() -> set[str]:
    return set(infrastructure_health._celery_queue_restart_targets().values())


def restart_containers() -> dict[str, str]:
    raw = os.getenv("CELERY_WORKER_RESTART_CONTAINERS", "")
    if not raw.strip():
        return dict(_DEFAULT_RESTART_CONTAINERS)

    containers: dict[str, str] = {}
    for item in raw.split(","):
        if "=" not in item:
            continue
        target, container = item.split("=", 1)
        target = target.strip()
        container = container.strip()
        if target and container:
            containers[target] = container
    return containers or dict(_DEFAULT_RESTART_CONTAINERS)


def _restart_command(target: str) -> list[str]:
    template = os.getenv("CELERY_WORKER_RESTART_COMMAND", "")
    if not template.strip():
        template = _DEFAULT_RESTART_COMMAND
    command = template.format(target=target)
    return shlex.split(command)


class _DockerUnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        self.sock = sock


def _restart_with_docker_api(target: str) -> WorkerRestartResult:
    container = restart_containers().get(target)
    if not container:
        return WorkerRestartResult(
            target=target,
            ok=False,
            message="No Docker container is mapped for this worker target.",
        )
    if not os.path.exists(_DOCKER_SOCKET):
        return WorkerRestartResult(
            target=target,
            ok=False,
            message="Docker socket is unavailable in the app container.",
        )

    conn = _DockerUnixHTTPConnection(_DOCKER_SOCKET)
    try:
        conn.request(
            "POST",
            f"/containers/{quote(container, safe='')}/restart?t=10",
        )
        response = conn.getresponse()
        body = response.read().decode("utf-8", errors="replace").strip()
    except OSError as exc:
        return WorkerRestartResult(
            target=target,
            ok=False,
            message=f"Docker restart failed: {exc}",
        )
    finally:
        conn.close()

    if response.status in {204, 304}:
        return WorkerRestartResult(
            target=target,
            ok=True,
            message=f"Restart requested for {target}.",
            returncode=0,
        )
    return WorkerRestartResult(
        target=target,
        ok=False,
        message=(body or f"Docker restart failed with HTTP {response.status}")[:300],
        returncode=response.status,
    )


def _restart_with_command(target: str) -> WorkerRestartResult:
    try:
        command = _restart_command(target)
    except (KeyError, ValueError):
        return WorkerRestartResult(
            target=target,
            ok=False,
            message="Worker restart command template is invalid.",
        )
    if any("{target}" in part for part in command):
        return WorkerRestartResult(
            target=target,
            ok=False,
            message="Worker restart command template is invalid.",
        )
    try:
        result = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=float(os.getenv("CELERY_WORKER_RESTART_TIMEOUT_SECONDS", "20")),
        )
    except FileNotFoundError as exc:
        return WorkerRestartResult(
            target=target,
            ok=False,
            message=f"Restart command is unavailable: {exc.filename}",
        )
    except subprocess.TimeoutExpired:
        return WorkerRestartResult(
            target=target,
            ok=False,
            message="Restart command timed out.",
        )
    except OSError as exc:
        return WorkerRestartResult(
            target=target,
            ok=False,
            message=f"Restart command failed: {exc}",
        )

    if result.returncode == 0:
        return WorkerRestartResult(
            target=target,
            ok=True,
            message=f"Restart requested for {target}.",
            returncode=result.returncode,
        )

    detail = (result.stderr or result.stdout or "Restart command failed.").strip()
    return WorkerRestartResult(
        target=target,
        ok=False,
        message=detail[:300],
        returncode=result.returncode,
    )


def restart_worker_target(target: str) -> WorkerRestartResult:
    target = target.strip()
    if not restart_enabled():
        return WorkerRestartResult(
            target=target,
            ok=False,
            message="Worker restart is disabled by configuration.",
        )
    if not _TARGET_RE.match(target):
        return WorkerRestartResult(
            target=target,
            ok=False,
            message="Invalid worker restart target.",
        )
    if target not in allowed_restart_targets():
        return WorkerRestartResult(
            target=target,
            ok=False,
            message="Worker restart target is not allowed.",
        )

    if os.getenv("CELERY_WORKER_RESTART_COMMAND", "").strip():
        return _restart_with_command(target)
    return _restart_with_docker_api(target)
