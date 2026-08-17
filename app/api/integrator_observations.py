"""Sub's `messaging.receive.v1` port for the independently deployed Integrator.

Adapter only. It authenticates, normalizes, records, delegates and answers. It
owns no business decision: if a reviewer can point at one inside this file, the
file is wrong.

## Why this authenticates the Integrator and not the provider

Sub's older inbound routes verify a provider HMAC over raw request bytes. That
is the right control when Sub is the party talking to the provider. Here Sub is
not: the Integrator is. By the time bytes arrive they have already been verified
once in the Integrator's deployment, then re-serialized into a provider-neutral
envelope. The provider's signature no longer covers them, so re-checking it here
would be checking a signature over a body that is not the signed body — a check
that looks like security and is not.

The caller is therefore authenticated as a machine principal: an ``ApiKey`` with
``system_user_id`` set, presented as ``X-Api-Key``, bearing a dedicated scope.
``ApiKey.scopes`` is already fail-closed on empty and ``revoked_at`` already
gives revocation, so no fourth credential shape is invented for one caller.

## What the credential does not grant

Authentication answers *who is calling*, never *where this lands*. The envelope
names no conversation, team, queue or subscriber, and the port would refuse them
if it did — the schema forbids extra fields. ``scope`` is the Integrator's own
binding scope and is recorded as provenance on the transport receipt; Sub's
routing owner decides where the message actually goes. This is Sub's half of the
destination invariant that ``dotmac_integration.destination_binding`` enforces on
the other side of the wire.

## The chain, and that every link is somebody else's

    authenticate  -> require_permission(INTEGRATOR_OBSERVATION_SCOPE)
    bind          -> integrations.runtime_execution.build_execution_context
    normalize     -> team_inbox_integrator_envelope.normalize
    receipt       -> integrations.inbox.receive_and_claim_verified
    the FACT      -> team_inbox_observations.record_provider_observation
    the CONSEQUENCE -> team_inbox_processing.process_provider_observation
    settle        -> integrations.inbox.complete_consequence / fail_consequence

Two dedup layers appear there and they are not redundant. The receipt is keyed
``(capability_binding_id, provider_event_id)`` and answers "have I accepted
these bytes on this binding?". The observation is keyed
``(provider, provider_account_scope, provider_event_id)`` and answers "have I
already recorded this fact?". Two bindings legitimately observing one upstream
event are two receipts and one observation.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.integrator_observation import (
    IntegratorMirrorFieldDisagreement,
    IntegratorMirrorReport,
    IntegratorObservationEnvelope,
    IntegratorObservationReceipt,
)
from app.services import (
    team_inbox_integrator_envelope as envelopes,
)
from app.services import (
    team_inbox_integrator_mirror as mirror,
)
from app.services import (
    team_inbox_observations,
    team_inbox_processing,
)
from app.services.auth_dependencies import require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.integrations import inbox as integration_inbox
from app.services.integrations.connectors.integrator_http import (
    INTEGRATOR_RECEIVE_CAPABILITY,
)
from app.services.integrations.runtime_execution import (
    RuntimeExecutionError,
    build_execution_context,
)
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/integration/observations", tags=["integrator-observations"])

#: The one scope this port requires. No existing scope is widened, and an
#: ApiKey without it is 401 before any row is read or written.
INTEGRATOR_OBSERVATION_SCOPE = "integration:observations:write"

#: The shadow scope. Strictly narrower: it produces parity evidence and can
#: record nothing, so a credential issued for the shadow window cannot become a
#: writer by accident — repointing traffic has to be a deliberate re-scoping.
INTEGRATOR_MIRROR_SCOPE = "integration:observations:mirror"


def _bind(db: Session, capability_binding_id: UUID, capability_id: str):
    """Resolve an enabled binding, or refuse without saying which it was."""

    try:
        execution = build_execution_context(
            db, capability_binding_id=capability_binding_id
        )
    except RuntimeExecutionError as exc:
        # A missing, disabled, quarantined or retired binding is reported as
        # not found rather than unavailable: an authenticated caller learning
        # which bindings exist and what state they are in is information it has
        # no need for.
        raise HTTPException(
            status_code=404, detail="Integrator observation binding not found"
        ) from exc
    if execution.binding.capability_id != capability_id:
        raise HTTPException(
            status_code=404, detail="Integrator observation binding not found"
        )
    return execution


def _refuse(exc: envelopes.IntegratorEnvelopeError) -> HTTPException:
    """Map one envelope rejection onto its transport status.

    An unknown capability is 404 and not 403, so an authenticated caller cannot
    enumerate what Sub accepts. An undeployed contract version is 409: it is a
    real disagreement about the contract, not a malformed request, and answering
    400 would invite the caller to retry a body that can never be accepted.
    """

    if isinstance(exc, envelopes.UnknownCapability):
        return HTTPException(status_code=404, detail={"code": exc.code})
    if isinstance(exc, envelopes.UnsupportedContractVersion):
        return HTTPException(
            status_code=409, detail={"code": exc.code, "message": exc.message}
        )
    return HTTPException(
        status_code=400, detail={"code": exc.code, "message": exc.message}
    )


@router.post("/{capability_binding_id}", response_model=IntegratorObservationReceipt)
def receive_integrator_observation(
    capability_binding_id: UUID,
    envelope: IntegratorObservationEnvelope,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_permission(INTEGRATOR_OBSERVATION_SCOPE)),
) -> IntegratorObservationReceipt:
    execution = _bind(db, capability_binding_id, INTEGRATOR_RECEIVE_CAPABILITY)
    installation_id = execution.binding.installation_id
    try:
        normalized = envelopes.normalize(
            envelope, context=envelopes.observation_context(envelope)
        )
    except envelopes.IntegratorEnvelopeError as exc:
        raise _refuse(exc) from exc

    try:
        receipt, should_process = integration_inbox.receive_and_claim_verified(
            db,
            capability_binding_id=execution.binding.id,
            provider_event_id=normalized.command.provider_event_id,
            event_type=INTEGRATOR_RECEIVE_CAPABILITY,
            payload=envelope.model_dump(mode="json"),
            headers={
                # Transport provenance, recorded because it is the only place
                # that survives: which Integrator binding scope believed this
                # belonged where. Sub's routing owner never reads it.
                "x-integrator-scope-kind": normalized.scope_kind,
                "x-integrator-scope-ref": normalized.scope_ref,
            },
        )
    except integration_inbox.ProviderEventIdentityCollision as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "integrator_observation_identity_collision",
                "message": str(exc),
            },
        ) from exc
    except integration_inbox.InboxError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "integrator_observation_receipt_conflict"},
        ) from exc

    if not should_process:
        consequence = dict(receipt.consequence_json or {})
        return IntegratorObservationReceipt(
            observation_id=str(consequence.get("observation_id") or ""),
            outcome=str(consequence.get("outcome") or "replayed"),
            processing_status=str(consequence.get("processing_status") or "processed"),
            replayed=True,
        )

    receipt_id = receipt.id
    db_session_adapter.release_read_transaction(db)
    try:
        recorded = team_inbox_observations.record_provider_observation(
            db, normalized.command
        )
        processed = team_inbox_processing.process_provider_observation(
            db,
            observation_id=recorded.observation_id,
            context=CommandContext.system(
                actor="system:team-inbox-observation-processor",
                scope="team-inbox:provider-consequence",
                reason="resolve Integrator-delivered inbound observation",
                idempotency_key=str(recorded.observation_id),
            ),
        )
    except team_inbox_observations.TeamInboxObservationError as exc:
        integration_inbox.fail_claimed_consequence(
            db,
            receipt_id=receipt_id,
            error_code=exc.code[:120],
            error_detail=exc.message,
        )
        # A reused provider identity carrying different evidence is a
        # collision, never a duplicate: deduplicating it would discard real
        # content on the assumption the producer is well-behaved. It escalates.
        collision = exc.code.endswith(".provider_event_identity_collision")
        raise HTTPException(
            status_code=409 if collision else 422,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except Exception as exc:
        integration_inbox.fail_claimed_consequence(
            db,
            receipt_id=receipt_id,
            error_code="integrator_observation_consequence_failed",
            # The CLASS NAME, never `str(exc)`. An arbitrary exception's message
            # is untrusted text of unbounded shape — it can carry a payload
            # fragment, a connection string, or a provider's own error body —
            # and `error_detail` is a durable column an operator reads. The
            # branch above may persist `exc.message` only because a DomainError
            # message is Sub-authored and operator-safe by that class's
            # contract. `dotmac-integration` 0.1.0a4 fixed this exact defect
            # class on the Integrator's side of the wire.
            error_detail=type(exc).__name__,
        )
        raise

    current = integration_inbox.get_receipt(db, receipt_id=receipt_id)
    integration_inbox.complete_consequence(
        db,
        receipt=current,
        consequence={
            "observation_id": str(processed.observation_id),
            "outcome": processed.outcome.value,
            "processing_status": processed.processing_status.value,
            "installation_id": str(installation_id),
        },
    )
    return IntegratorObservationReceipt(
        observation_id=str(processed.observation_id),
        outcome=processed.outcome.value,
        processing_status=processed.processing_status.value,
        replayed=False,
    )


@router.post(
    "/{capability_binding_id}/mirror",
    response_model=IntegratorMirrorReport,
)
def mirror_integrator_observation(
    capability_binding_id: UUID,
    envelope: IntegratorObservationEnvelope,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_permission(INTEGRATOR_MIRROR_SCOPE)),
) -> IntegratorMirrorReport:
    """Compare one envelope against what Sub's own receiver recorded.

    THE SHADOW PATH. This writes nothing — no receipt, no observation, no
    conversation — so the Integrator can be pointed at it while the provider
    callback still goes to Sub's existing webhook, and the two producers can be
    compared on live traffic without either one being repointed.

    It carries its own scope, deliberately narrower than the write scope: a
    credential that may only ever produce evidence cannot accidentally start
    recording facts.
    """

    _bind(db, capability_binding_id, INTEGRATOR_RECEIVE_CAPABILITY)
    try:
        report = mirror.compare_envelope(db, envelope=envelope)
    except envelopes.IntegratorEnvelopeError as exc:
        raise _refuse(exc) from exc
    return IntegratorMirrorReport(
        verdict=report.verdict,
        identity=":".join(report.identity),
        counterpart_identity=(
            ":".join(report.counterpart_identity)
            if report.counterpart_identity
            else None
        ),
        blocking_reasons=report.blocking_reasons,
        disagreements=tuple(
            IntegratorMirrorFieldDisagreement(
                field=item.field, integrator=item.integrator, sub=item.sub
            )
            for item in report.disagreements
        ),
        agrees=report.agrees,
    )
