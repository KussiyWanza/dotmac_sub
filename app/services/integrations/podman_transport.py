"""Rootless-Podman transport for the external connector runtime.

Phase 3 of ADR 0005, transport half. Implements ``RunnerTransport`` by running
the connector's digest-pinned image as a short-lived, hardened, rootless Podman
container: the ``RunnerRequest`` JSON goes in on stdin, the ``RunnerResponse``
JSON comes back on stdout, the container is killed at the deadline and removed.

Security posture (ADR 0005 resolved decision 1). Every container runs
``--read-only`` with ``--cap-drop=ALL``, ``--security-opt=no-new-privileges``,
no host mounts, and bounded memory, CPU, and pids. Rootless execution maps
container-root to an unprivileged host uid, so an escape lands unprivileged.
Secret material is written to a tmpfs env file readable only by this user,
passed with ``--env-file`` (never on argv, where ``ps`` would expose it), and
deleted in a ``finally``.

Egress is default-deny (Phase 4). The transport takes an ``EgressPolicy`` derived
from the connector's manifest: a connector with no declared hosts runs with
``--network=none``; one that declares hosts requires an allowlist gateway that
confines outbound traffic to exactly those hosts, and until such a gateway
exists (Phase 4b) the transport refuses to run it rather than grant an open
network.

``_build_argv`` is a pure function so the exact command — the security flags in
particular — is unit-tested without invoking Podman. The live exchange is
covered by an integration test that runs a real example connector on a host
where Podman is present.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from app.config import settings
from app.services.integrations.egress_gateway import EgressGateway, EgressGatewayError
from app.services.integrations.egress_policy import EgressPolicy
from app.services.integrations.external_runner import (
    RunnerTimeout,
    RunnerTransportError,
)
from app.services.integrations.runner_protocol import RunnerRequest, RunnerResponse

# Connectors read each secret binding from an env var under this prefix. Config
# is not here — it travels in the request on stdin. Only secrets go by env, so
# they never share a channel with replayable request data.
SECRET_ENV_PREFIX = "DM_SECRET_"

_DEFAULT_MEMORY = "256m"
_DEFAULT_PIDS_LIMIT = 128
# CPU limiting is opt-in, not defaulted. `--cpus` needs the cpu cgroup
# controller delegated to the rootless user, which is not the default on
# Ubuntu 22.04 (only memory and pids are delegated out of the box). Applying it
# where the controller is absent makes every operation fail, so the transport
# only sets it when a deployment explicitly opts in on a host with delegation
# configured (see the Phase 3 deployment note in ADR 0005). memory and pids —
# the OOM and fork-bomb controls — work rootless everywhere and stay on.
_DEFAULT_CPUS: str | None = None
_MIN_DEADLINE_SECONDS = 1
# How much longer than the authoritative app-side deadline Podman's own
# --timeout runs, so conmon reaps a container orphaned by killing podman run.
_CONTAINER_REAP_GRACE_SECONDS = 5


def _secret_env_name(binding: str) -> str:
    return SECRET_ENV_PREFIX + binding.upper()


def _build_argv(
    image_ref: str,
    *,
    deadline_seconds: int,
    env_file: str,
    network: str | None = None,
    memory: str = _DEFAULT_MEMORY,
    cpus: str | None = _DEFAULT_CPUS,
    pids_limit: int = _DEFAULT_PIDS_LIMIT,
) -> list[str]:
    """Build the hardened ``podman run`` argv for one operation.

    Pure and side-effect free so the security flags are asserted in isolation.
    Carries no secret value: credentials arrive through ``env_file``, never as
    an argument.
    """

    if deadline_seconds < _MIN_DEADLINE_SECONDS:
        deadline_seconds = _MIN_DEADLINE_SECONDS
    argv = [
        "podman",
        "run",
        "--rm",
        "--interactive",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        # A read-only rootfs still needs a scratch area; keep it small, noexec,
        # and in memory rather than granting a writable layer.
        "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
        f"--memory={memory}",
        f"--pids-limit={pids_limit}",
        # conmon kills the container this many seconds after it starts; the
        # subprocess deadline below is the authoritative one.
        f"--timeout={deadline_seconds}",
        f"--env-file={env_file}",
    ]
    if cpus is not None:
        argv.append(f"--cpus={cpus}")
    if network is not None:
        argv.append(f"--network={network}")
    argv.append(image_ref)
    return argv


class PodmanTransport:
    """Carry one operation to a connector container over rootless Podman."""

    def __init__(
        self,
        *,
        egress: EgressPolicy = EgressPolicy(),
        egress_gateway: EgressGateway | None = None,
        memory: str = _DEFAULT_MEMORY,
        cpus: str | None = _DEFAULT_CPUS,
        pids_limit: int = _DEFAULT_PIDS_LIMIT,
        runtime_dir: str | None = None,
        podman_path: str = "podman",
    ) -> None:
        # Egress is default-deny. A connector with no declared hosts runs with
        # no network; one that declares hosts is confined by the gateway, and
        # refused outright when no gateway is configured — never run open.
        self._egress = egress
        self._egress_gateway = egress_gateway
        self._memory = memory
        self._cpus = cpus
        self._pids_limit = pids_limit
        # Secret env files live on tmpfs. The directory is a deployment input
        # owned by app.config, not read from the environment here: environment
        # reads outside the declared owners are a parallel decision source.
        self._runtime_dir = runtime_dir or (settings.connector_runtime_dir or None)
        self._podman_path = podman_path

    def _resolve_egress(self, connector_key: str) -> tuple[str, dict[str, str]]:
        """Resolve the container network and proxy environment, or fail closed.

        A connector that needs no egress runs with ``--network=none``. One that
        declares hosts is attached to a gateway that confines outbound traffic
        to exactly those hosts. With no gateway configured the transport refuses
        rather than grant an unrestricted network.
        """
        if not self._egress.requires_network:
            return "none", {}
        if self._egress_gateway is None:
            raise RunnerTransportError(
                "connector declares egress hosts "
                f"({', '.join(self._egress.hosts) or 'installation host'}) but no "
                "allowlist egress gateway is configured; refusing to run with "
                "unrestricted network (ADR 0005 Phase 4b)"
            )
        try:
            attachment = self._egress_gateway.attach(
                connector_key=connector_key, policy=self._egress
            )
        except EgressGatewayError as exc:
            # Without confinement the connector must not run at all.
            raise RunnerTransportError(
                f"egress gateway unavailable, refusing to run unconfined: {exc}"
            ) from exc
        return attachment.network, attachment.proxy_env()

    def _write_secret_env_file(
        self,
        secret_material: Mapping[str, str],
        extra_env: Mapping[str, str] | None = None,
    ) -> str:
        fd, path = tempfile.mkstemp(
            prefix="dm-runner-secret-", suffix=".env", dir=self._runtime_dir
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                entries = [
                    (_secret_env_name(name), value)
                    for name, value in secret_material.items()
                ]
                # Proxy variables ride the same file rather than argv. They are
                # not secret, but the file is already the delivery channel and
                # keeping one channel means one place to reason about.
                entries += list((extra_env or {}).items())
                for name, value in entries:
                    # env-file values are taken verbatim to end of line; a
                    # newline in a credential would corrupt the next entry.
                    if "\n" in value or "\r" in value:
                        raise RunnerTransportError(
                            f"secret {name!r} contains a newline and cannot be "
                            "delivered as an environment value"
                        )
                    handle.write(f"{name}={value}\n")
        except Exception:
            Path(path).unlink(missing_ok=True)
            raise
        return path

    def _deadline_seconds(self, deadline_at: datetime) -> int:
        remaining = (deadline_at - datetime.now(UTC)).total_seconds()
        return max(_MIN_DEADLINE_SECONDS, int(remaining))

    def _parse_response(
        self, stdout: bytes, *, request: RunnerRequest
    ) -> RunnerResponse:
        text = stdout.decode("utf-8", errors="replace").strip()
        if not text:
            raise RunnerTransportError("connector produced no response on stdout")
        # A connector may log to stdout before the response; take the last line.
        last = text.splitlines()[-1]
        try:
            return RunnerResponse.model_validate_json(last)
        except ValidationError as exc:
            raise RunnerTransportError(
                f"connector response did not match the wire contract: "
                f"{exc.error_count()} error(s)"
            ) from exc

    def exchange(
        self,
        *,
        request: RunnerRequest,
        image_ref: str,
        secret_material: Mapping[str, str],
        deadline_at: datetime,
    ) -> RunnerResponse:
        # Resolve egress before materializing a secret: a connector we refuse
        # to run on egress grounds must never have its credentials written out.
        network, proxy_env = self._resolve_egress(request.connector.key)
        deadline_seconds = self._deadline_seconds(deadline_at)
        env_file = self._write_secret_env_file(secret_material, proxy_env)
        # The subprocess deadline is authoritative: it raises TimeoutExpired
        # unambiguously, whereas Podman's own deadline kill exits 255 —
        # indistinguishable from a generic error. Podman's --timeout is set
        # longer, purely to reap the container if the podman process we killed
        # left conmon holding it.
        argv = self._argv(
            image_ref,
            deadline_seconds + _CONTAINER_REAP_GRACE_SECONDS,
            env_file,
            network,
        )
        try:
            completed = subprocess.run(
                argv,
                input=request.model_dump_json().encode("utf-8"),
                capture_output=True,
                timeout=deadline_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerTimeout(
                f"connector {image_ref} exceeded its {deadline_seconds}s deadline"
            ) from exc
        except FileNotFoundError as exc:
            raise RunnerTransportError(
                f"container runtime {self._podman_path!r} is not available"
            ) from exc
        finally:
            Path(env_file).unlink(missing_ok=True)

        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            # If Podman's backstop deadline fired first in a race, treat it as a
            # timeout so execute maps it to reconciliation, not a plain retry.
            if completed.returncode in (124, 137) or "timed out" in detail.lower():
                raise RunnerTimeout(f"connector {image_ref} was killed at its deadline")
            raise RunnerTransportError(
                f"connector {image_ref} exited {completed.returncode}: {detail[:500]}"
            )
        return self._parse_response(completed.stdout, request=request)

    def _argv(
        self, image_ref: str, deadline_seconds: int, env_file: str, network: str | None
    ) -> Sequence[str]:
        argv = _build_argv(
            image_ref,
            deadline_seconds=deadline_seconds,
            env_file=env_file,
            network=network,
            memory=self._memory,
            cpus=self._cpus,
            pids_limit=self._pids_limit,
        )
        if self._podman_path != "podman":
            argv[0] = self._podman_path
        return argv


__all__ = ["SECRET_ENV_PREFIX", "EgressPolicy", "PodmanTransport"]
