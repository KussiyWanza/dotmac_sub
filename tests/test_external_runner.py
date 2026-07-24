"""ExternalOciRunner marshals the ConnectorRunner protocol over a transport.

Phase 3 of ADR 0005, marshalling half. Exercised with an in-memory transport,
so the security and failure semantics are verified without a container runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from app.services.integrations.external_runner import (
    ExternalOciRunner,
    RunnerTimeout,
    RunnerTransportError,
)
from app.services.integrations.manifest import (
    CapabilityManifest,
    CapabilityMode,
    ConnectorManifest,
    ConnectorRuntimeType,
    RuntimeManifest,
)
from app.services.integrations.runner_protocol import (
    RunnerRequest,
    RunnerResponse,
    RunnerVerb,
)
from app.services.integrations.runtime import (
    HealthResult,
    OperationEnvelope,
    OperationResult,
    OperationStatus,
    OperationTrigger,
    ValidationResult,
)

SECRET_VALUE = "sk_live_must_never_reach_the_request"
IMAGE = "ghcr.io/dotmac/connector-example"
DIGEST = "sha256:" + "a" * 64

MANIFEST = ConnectorManifest(
    key="example",
    name="Example",
    version="1.0.0",
    connector_type="payment",
    description="Example connector.",
    runtime=RuntimeManifest(
        type=ConnectorRuntimeType.external_oci, image=IMAGE, digest=DIGEST
    ),
    capabilities=(
        CapabilityManifest(
            id="payments.intent.v1", modes=(CapabilityMode.interactive,)
        ),
    ),
)
CONFIG = {"base_url": "https://api.example.test", "timeout_seconds": 30}
SECRETS = {"gateway_credentials": SECRET_VALUE}


class FakeTransport:
    """Captures each exchange and returns a scripted response.

    `respond` is a callable given the request; it returns a RunnerResponse, or
    raises to simulate a transport failure or timeout.
    """

    def __init__(self, respond):
        self._respond = respond
        self.calls: list[dict[str, Any]] = []

    def exchange(
        self,
        *,
        request: RunnerRequest,
        image_ref: str,
        secret_material: Mapping[str, str],
        deadline_at: datetime,
    ) -> RunnerResponse:
        self.calls.append(
            {
                "request": request,
                "image_ref": image_ref,
                "secret_material": dict(secret_material),
                "deadline_at": deadline_at,
            }
        )
        return self._respond(request)


def _runner(respond) -> tuple[ExternalOciRunner, FakeTransport]:
    transport = FakeTransport(respond)
    return ExternalOciRunner(MANIFEST, transport), transport


def _envelope(**overrides) -> OperationEnvelope:
    payload = {
        "operation_id": uuid4(),
        "correlation_id": "corr-1",
        "installation_id": uuid4(),
        "capability_binding_id": uuid4(),
        "capability_id": "payments.intent.v1",
        "connector_key": MANIFEST.key,
        "connector_version": MANIFEST.version,
        "manifest_digest": MANIFEST.digest,
        "config_revision_id": uuid4(),
        "trigger": OperationTrigger.interactive,
        "idempotency_key": "idem-1",
        "deadline_at": datetime.now(UTC) + timedelta(seconds=30),
        "payload": {"action": "initialize", "params": {}},
    }
    payload.update(overrides)
    return OperationEnvelope(**payload)


# --- construction -----------------------------------------------------------


def test_runner_refuses_a_non_external_manifest():
    builtin = MANIFEST.model_copy(
        update={
            "runtime": RuntimeManifest(
                type=ConnectorRuntimeType.builtin_worker, module="x"
            )
        }
    )
    with pytest.raises(ValueError, match="not an external_oci connector"):
        ExternalOciRunner(builtin, FakeTransport(lambda r: None))


# --- the secret-material guarantee ------------------------------------------


def test_secret_material_is_delivered_out_of_band_not_in_the_request():
    runner, transport = _runner(
        lambda r: RunnerResponse(
            verb=RunnerVerb.execute,
            operation=OperationResult(
                operation_id=r.envelope.operation_id,
                status=OperationStatus.succeeded,
            ),
        )
    )
    envelope = _envelope()
    runner.execute(envelope, config=CONFIG, secret_material=SECRETS)

    call = transport.calls[0]
    # Secret travels beside the request, for the transport to inject.
    assert call["secret_material"] == SECRETS
    # ...and never inside it.
    assert SECRET_VALUE not in call["request"].model_dump_json()
    assert "sk_live" not in call["request"].model_dump_json()


def test_the_pinned_image_reference_is_digest_addressed():
    runner, transport = _runner(
        lambda r: RunnerResponse(
            verb=RunnerVerb.health, health=HealthResult(status="ok")
        )
    )
    runner.health(manifest=MANIFEST, config=CONFIG, secret_material=SECRETS)
    assert transport.calls[0]["image_ref"] == f"{IMAGE}@{DIGEST}"


# --- verb marshalling -------------------------------------------------------


def test_validate_unwraps_the_validation_result():
    runner, _ = _runner(
        lambda r: RunnerResponse(
            verb=RunnerVerb.validate,
            validation=ValidationResult(valid=True, details={"checked": "bank"}),
        )
    )
    result = runner.validate(manifest=MANIFEST, config=CONFIG, secret_material=SECRETS)
    assert result.valid is True
    assert result.details == {"checked": "bank"}


def test_execute_unwraps_the_operation_result():
    envelope = _envelope()
    runner, _ = _runner(
        lambda r: RunnerResponse(
            verb=RunnerVerb.execute,
            operation=OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.succeeded,
                output={"authorization_url": "https://pay.example/x"},
            ),
        )
    )
    result = runner.execute(envelope, config=CONFIG, secret_material=SECRETS)
    assert result.status is OperationStatus.succeeded
    assert result.output["authorization_url"] == "https://pay.example/x"


def test_health_and_cancel_unwrap_their_results():
    runner, _ = _runner(
        lambda r: RunnerResponse(
            verb=RunnerVerb.health, health=HealthResult(status="healthy")
        )
    )
    assert (
        runner.health(manifest=MANIFEST, config=CONFIG, secret_material=SECRETS).status
        == "healthy"
    )

    runner, transport = _runner(
        lambda r: RunnerResponse(verb=RunnerVerb.cancel, canceled=True)
    )
    op = uuid4()
    assert runner.cancel(op) is True
    assert transport.calls[0]["request"].operation_id == op


# --- failure semantics ------------------------------------------------------


def test_execute_timeout_becomes_reconciliation_required_not_a_retry():
    """The design forbids blindly repeating an ambiguous remote outcome."""

    def respond(_request):
        raise RunnerTimeout("deadline exceeded")

    envelope = _envelope()
    runner, _ = _runner(respond)
    result = runner.execute(envelope, config=CONFIG, secret_material=SECRETS)
    assert result.status is OperationStatus.reconciliation_required
    assert result.error_code == "runner_timeout"
    assert result.operation_id == envelope.operation_id


def test_execute_transport_error_is_retryable():
    def respond(_request):
        raise RunnerTransportError("container exited 1")

    runner, _ = _runner(respond)
    result = runner.execute(_envelope(), config=CONFIG, secret_material=SECRETS)
    assert result.status is OperationStatus.retryable
    assert result.error_code == "runner_transport_error"


def test_validate_timeout_and_transport_error_map_to_invalid():
    runner, _ = _runner(lambda r: (_ for _ in ()).throw(RunnerTimeout("x")))
    timed = runner.validate(manifest=MANIFEST, config=CONFIG, secret_material=SECRETS)
    assert timed.valid is False
    assert timed.error_codes == ("runner_timeout",)

    runner, _ = _runner(lambda r: (_ for _ in ()).throw(RunnerTransportError("x")))
    errored = runner.validate(manifest=MANIFEST, config=CONFIG, secret_material=SECRETS)
    assert errored.valid is False
    assert errored.error_codes == ("runner_transport_error",)


def test_health_timeout_is_unknown_and_transport_error_is_unavailable():
    runner, _ = _runner(lambda r: (_ for _ in ()).throw(RunnerTimeout("x")))
    assert (
        runner.health(manifest=MANIFEST, config=CONFIG, secret_material=SECRETS).status
        == "unknown"
    )

    runner, _ = _runner(lambda r: (_ for _ in ()).throw(RunnerTransportError("x")))
    assert (
        runner.health(manifest=MANIFEST, config=CONFIG, secret_material=SECRETS).status
        == "unavailable"
    )


def test_cancel_transport_error_is_false():
    runner, _ = _runner(lambda r: (_ for _ in ()).throw(RunnerTransportError("x")))
    assert runner.cancel(uuid4()) is False


# --- defensive checks against a misbehaving connector -----------------------


def test_a_wrong_verb_response_fails_closed_without_crashing_sub():
    """A misbehaving connector must not be able to raise into caller code.

    A health response to a validate request is a protocol violation; the runner
    maps it to a clean invalid result rather than accepting it or propagating an
    exception a worker would have to catch.
    """
    runner, _ = _runner(
        lambda r: RunnerResponse(
            verb=RunnerVerb.health, health=HealthResult(status="ok")
        )
    )
    result = runner.validate(manifest=MANIFEST, config=CONFIG, secret_material=SECRETS)
    assert result.valid is False


def test_execute_wrong_operation_id_becomes_reconciliation_required():
    """The container responded and may have acted, so the outcome is ambiguous.

    A protocol-violating execute response must not be retried (it could double
    an effect) nor raised; it goes to reconciliation like an ambiguous timeout.
    """
    envelope = _envelope()
    runner, _ = _runner(
        lambda r: RunnerResponse(
            verb=RunnerVerb.execute,
            operation=OperationResult(
                operation_id=uuid4(),  # not the one we sent
                status=OperationStatus.succeeded,
            ),
        )
    )
    result = runner.execute(envelope, config=CONFIG, secret_material=SECRETS)
    assert result.status is OperationStatus.reconciliation_required
    assert result.error_code == "runner_protocol_error"
    assert result.operation_id == envelope.operation_id


def test_execute_wrong_verb_also_becomes_reconciliation_required():
    envelope = _envelope()
    runner, _ = _runner(
        lambda r: RunnerResponse(
            verb=RunnerVerb.health, health=HealthResult(status="ok")
        )
    )
    result = runner.execute(envelope, config=CONFIG, secret_material=SECRETS)
    assert result.status is OperationStatus.reconciliation_required
    assert result.error_code == "runner_protocol_error"


def test_runner_refuses_a_manifest_for_a_different_connector():
    other = MANIFEST.model_copy(update={"key": "other", "version": "2.0.0"})
    runner, _ = _runner(
        lambda r: RunnerResponse(
            verb=RunnerVerb.health, health=HealthResult(status="ok")
        )
    )
    with pytest.raises(RunnerTransportError, match="pinned to example"):
        runner.health(manifest=other, config=CONFIG, secret_material=SECRETS)
