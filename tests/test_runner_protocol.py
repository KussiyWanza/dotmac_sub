"""Conformance of the out-of-process runner wire contract.

Phase 2 of ADR 0005. These tests pin the properties a connector image can rely
on and that Sub must never regress: no secret material on the wire, a pinned
connector identity, and a response shape that matches the verb asked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.services.integrations.manifest import (
    ConnectorManifest,
    ConnectorRuntimeType,
    RuntimeManifest,
)
from app.services.integrations.runner_protocol import (
    RUNNER_CONTRACT_VERSION,
    ConnectorPin,
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

SECRET_VALUE = "sk_live_this_must_never_cross_the_boundary"

MANIFEST = ConnectorManifest(
    key="example",
    name="Example",
    version="1.0.0",
    connector_type="payment",
    description="Example connector.",
    runtime=RuntimeManifest(
        type=ConnectorRuntimeType.external_oci,
        image="ghcr.io/dotmac/connector-example",
        digest="sha256:" + "a" * 64,
    ),
)


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


def _execute_request(**overrides) -> RunnerRequest:
    payload = {
        "verb": RunnerVerb.execute,
        "connector": ConnectorPin.from_manifest(MANIFEST),
        "config": {"base_url": "https://api.example.test"},
        "envelope": _envelope(),
    }
    payload.update(overrides)
    return RunnerRequest(**payload)


def test_a_request_has_no_field_for_secret_material():
    """The guarantee the whole contract rests on."""
    assert "secret" not in " ".join(RunnerRequest.model_fields).lower()
    with pytest.raises(ValidationError):
        RunnerRequest(
            verb=RunnerVerb.health,
            connector=ConnectorPin.from_manifest(MANIFEST),
            secret_material={"gateway_credentials": SECRET_VALUE},
        )


def test_a_serialized_request_cannot_carry_a_credential():
    """Config and payload are operator-supplied; neither may smuggle a secret.

    Serialized requests are safe to persist as delivery evidence and to replay,
    which is only true if no credential can reach them.
    """
    request = _execute_request()
    wire = request.model_dump_json()
    assert SECRET_VALUE not in wire
    assert "sk_live" not in wire


def test_the_connector_pin_travels_with_every_request():
    request = _execute_request()
    assert request.connector.matches(MANIFEST)
    assert request.contract_version == RUNNER_CONTRACT_VERSION


def test_a_pin_rejects_a_manifest_with_a_different_digest():
    other = MANIFEST.model_copy(update={"version": "2.0.0"})
    pin = ConnectorPin.from_manifest(MANIFEST)
    assert pin.matches(other) is False


def test_execute_refuses_an_envelope_pinned_to_another_connector():
    """A digest mismatch must fail before the connector interprets a payload."""
    foreign = _envelope(manifest_digest="b" * 64)
    with pytest.raises(ValidationError) as excinfo:
        _execute_request(envelope=foreign)
    assert "does not match the pinned connector" in str(excinfo.value)


def test_execute_requires_an_envelope():
    with pytest.raises(ValidationError) as excinfo:
        RunnerRequest(
            verb=RunnerVerb.execute,
            connector=ConnectorPin.from_manifest(MANIFEST),
        )
    assert "requires an operation envelope" in str(excinfo.value)


def test_non_execute_verbs_reject_an_envelope():
    with pytest.raises(ValidationError) as excinfo:
        RunnerRequest(
            verb=RunnerVerb.health,
            connector=ConnectorPin.from_manifest(MANIFEST),
            envelope=_envelope(),
        )
    assert "does not take an operation envelope" in str(excinfo.value)


def test_cancel_requires_an_operation_id_and_others_reject_it():
    with pytest.raises(ValidationError):
        RunnerRequest(
            verb=RunnerVerb.cancel,
            connector=ConnectorPin.from_manifest(MANIFEST),
        )
    with pytest.raises(ValidationError):
        RunnerRequest(
            verb=RunnerVerb.validate,
            connector=ConnectorPin.from_manifest(MANIFEST),
            operation_id=uuid4(),
        )
    cancel = RunnerRequest(
        verb=RunnerVerb.cancel,
        connector=ConnectorPin.from_manifest(MANIFEST),
        operation_id=uuid4(),
    )
    assert cancel.operation_id is not None


def test_a_request_round_trips_through_json_unchanged():
    """A runner in another language must reconstruct exactly what was sent."""
    request = _execute_request()
    assert RunnerRequest.model_validate_json(request.model_dump_json()) == request


def test_an_unknown_contract_version_is_refused():
    payload = _execute_request().model_dump(mode="json")
    payload["contract_version"] = "dotmac.io/integrations/runner/v2"
    with pytest.raises(ValidationError):
        RunnerRequest.model_validate(payload)


def test_an_unknown_field_is_refused_rather_than_ignored():
    payload = _execute_request().model_dump(mode="json")
    payload["extra_field"] = "smuggled"
    with pytest.raises(ValidationError):
        RunnerRequest.model_validate(payload)


@pytest.mark.parametrize(
    ("verb", "field", "value"),
    [
        (RunnerVerb.validate, "validation", ValidationResult(valid=True)),
        (
            RunnerVerb.execute,
            "operation",
            OperationResult(operation_id=uuid4(), status=OperationStatus.succeeded),
        ),
        (RunnerVerb.health, "health", HealthResult(status="healthy")),
        (RunnerVerb.cancel, "canceled", True),
    ],
)
def test_each_verb_accepts_its_own_result(verb, field, value):
    response = RunnerResponse(verb=verb, **{field: value})
    assert getattr(response, field) == value


def test_a_response_must_answer_the_verb_that_was_asked():
    with pytest.raises(ValidationError) as excinfo:
        RunnerResponse(verb=RunnerVerb.execute, health=HealthResult(status="healthy"))
    assert "requires operation" in str(excinfo.value)


def test_a_response_must_not_carry_results_for_other_verbs():
    with pytest.raises(ValidationError) as excinfo:
        RunnerResponse(
            verb=RunnerVerb.health,
            health=HealthResult(status="healthy"),
            validation=ValidationResult(valid=True),
        )
    assert "must not carry" in str(excinfo.value)


def test_every_verb_has_a_defined_response_shape():
    """A new verb must not reach production without a response contract."""
    for verb in RunnerVerb:
        with pytest.raises(ValidationError):
            RunnerResponse(verb=verb)
