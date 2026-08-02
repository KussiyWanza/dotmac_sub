"""Live PodmanTransport against a real rootless container.

Runs the example echo connector through the real transport, so the mechanics
the argv unit tests cannot cover — stdin/stdout marshalling, exit codes, the
deadline kill, and out-of-band secret delivery — are exercised end to end.

Skipped where Podman is unavailable, so CI without a container runtime stays
green; it is meant to run on seabone (and any host with rootless Podman).
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.services.integrations.external_runner import (
    RunnerTimeout,
    RunnerTransportError,
)
from app.services.integrations.podman_transport import PodmanTransport
from app.services.integrations.runner_protocol import (
    ConnectorPin,
    RunnerRequest,
    RunnerResponse,
    RunnerVerb,
)
from app.services.integrations.runtime import OperationEnvelope, OperationTrigger

pytestmark = pytest.mark.skipif(
    shutil.which("podman") is None, reason="rootless Podman not available"
)

_ECHO_DIR = Path(__file__).resolve().parents[1] / "examples" / "connectors" / "echo"
_IMAGE_TAG = "localhost/dotmac-echo-connector:pytest"
SECRET_VALUE = "sk_live_never_appears_in_any_output"
PIN = ConnectorPin(key="echo", version="1.0.0", manifest_digest="a" * 64)


@pytest.fixture(scope="module")
def echo_image() -> str:
    build = subprocess.run(
        ["podman", "build", "-t", _IMAGE_TAG, "-f", "Containerfile", "."],
        cwd=_ECHO_DIR,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if build.returncode != 0:
        pytest.skip(f"could not build example connector image: {build.stderr[-500:]}")
    return _IMAGE_TAG


def _envelope(action: str, *, deadline_seconds: int = 30) -> OperationEnvelope:
    return OperationEnvelope(
        operation_id=uuid4(),
        correlation_id="corr-1",
        installation_id=uuid4(),
        capability_binding_id=uuid4(),
        capability_id="payments.intent.v1",
        connector_key="echo",
        connector_version="1.0.0",
        manifest_digest="a" * 64,
        config_revision_id=uuid4(),
        trigger=OperationTrigger.interactive,
        idempotency_key="idem-1",
        deadline_at=datetime.now(UTC) + timedelta(seconds=deadline_seconds),
        payload={"action": action, "params": {"amount": 100}},
    )


def _execute_request(action: str, *, deadline_seconds: int = 30) -> RunnerRequest:
    return RunnerRequest(
        verb=RunnerVerb.execute,
        connector=PIN,
        config={"base_url": "https://example.test"},
        envelope=_envelope(action, deadline_seconds=deadline_seconds),
    )


def _transport() -> PodmanTransport:
    # Default egress is deny-all, so the echo connector runs with no network —
    # the tightest confinement, which is also what it needs.
    return PodmanTransport()


def _deadline(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def test_execute_round_trips_through_a_real_container(echo_image):
    request = _execute_request("echo_me")
    response = _transport().exchange(
        request=request,
        image_ref=echo_image,
        secret_material={},
        deadline_at=request.envelope.deadline_at,
    )
    assert isinstance(response, RunnerResponse)
    assert response.verb is RunnerVerb.execute
    assert response.operation is not None
    assert response.operation.operation_id == request.envelope.operation_id
    assert response.operation.output["echo"]["action"] == "echo_me"


def test_a_default_deny_container_has_no_network(echo_image):
    """The connector runs with --network=none, so it can reach nothing.

    The echo image reports whether it can open a socket to a public host; with
    default-deny egress that must fail.
    """
    request = _execute_request("probe_network")
    response = _transport().exchange(
        request=request,
        image_ref=echo_image,
        secret_material={},
        deadline_at=request.envelope.deadline_at,
    )
    assert response.operation is not None
    assert response.operation.output["network_reachable"] is False


def test_secret_is_delivered_by_name_and_its_value_never_appears(echo_image):
    request = RunnerRequest(verb=RunnerVerb.validate, connector=PIN, config={})
    response = _transport().exchange(
        request=request,
        image_ref=echo_image,
        secret_material={"gateway_credentials": SECRET_VALUE},
        deadline_at=_deadline(30),
    )
    assert response.validation is not None
    # The connector saw the binding by name...
    assert "gateway_credentials" in response.validation.details["secrets_seen"]
    # ...and its value is nowhere in what came back across the boundary.
    assert SECRET_VALUE not in response.model_dump_json()


def test_a_container_that_overruns_its_deadline_raises_timeout(echo_image):
    request = _execute_request("sleep_forever", deadline_seconds=2)
    with pytest.raises(RunnerTimeout):
        _transport().exchange(
            request=request,
            image_ref=echo_image,
            secret_material={},
            deadline_at=_deadline(2),
        )


def test_a_crashing_container_raises_a_transport_error(echo_image):
    request = _execute_request("crash")
    with pytest.raises(RunnerTransportError):
        _transport().exchange(
            request=request,
            image_ref=echo_image,
            secret_material={},
            deadline_at=request.envelope.deadline_at,
        )


def test_the_secret_env_file_is_removed_after_the_exchange(echo_image, tmp_path):
    request = _execute_request("echo_me")
    PodmanTransport(runtime_dir=str(tmp_path)).exchange(
        request=request,
        image_ref=echo_image,
        secret_material={"gateway_credentials": SECRET_VALUE},
        deadline_at=request.envelope.deadline_at,
    )
    assert list(tmp_path.glob("dm-runner-secret-*.env")) == []
