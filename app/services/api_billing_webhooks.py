"""Thin signature and HTTP adapter for the payment-webhook coordinator."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from uuid import UUID

from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.integrations import inbox as integration_inbox
from app.services.integrations import payment_capability
from app.services.integrations.inbox import (
    InboxError,
    InboxLeaseLost,
    ProviderEventIdentityCollision,
)
from app.services.integrations.installations import InstallationError
from app.services.owner_commands import CommandContext
from app.services.payment_webhook_commands import (
    PROCESS_SCOPE,
    PaymentWebhookError,
    PaymentWebhookProvider,
    ProcessClaimedPaymentWebhookCommand,
    identify_verified_payment_webhook,
    process_claimed_payment_webhook,
)

logger = logging.getLogger(__name__)


def _record_ingress_outcome(provider: PaymentWebhookProvider, outcome: str) -> None:
    try:
        from app.metrics import PAYMENT_PROVIDER_WEBHOOK_INGRESS

        PAYMENT_PROVIDER_WEBHOOK_INGRESS.labels(provider.value, outcome).inc()
    except Exception:
        logger.debug("payment_provider_webhook_metric_failed", exc_info=True)


@dataclass(frozen=True, slots=True)
class _ErrorMapping:
    http_status: int
    response_status: str
    inbox_code: str
    max_attempts: int
    expose_detail: bool = False


def _map_payment_webhook_error(error: DomainError) -> _ErrorMapping:
    suffix = error.code.rsplit(".", 1)[-1]
    if suffix == "provider_not_configured":
        return _ErrorMapping(
            http_status=503,
            response_status="provider not configured",
            inbox_code="payment_provider_not_configured",
            max_attempts=10,
        )
    if suffix in {
        "payload_invalid",
        "receipt_not_found",
        "receipt_not_claimed",
        "receipt_provider_mismatch",
        "topup_intent_mismatch",
        "deposit_rejected",
        "provider_event_rejected",
        "dispute_resolution_unrecognized",
    }:
        return _ErrorMapping(
            http_status=400 if suffix == "payload_invalid" else 409,
            response_status="rejected",
            inbox_code="payment_event_rejected",
            max_attempts=1,
            expose_detail=True,
        )
    if suffix == "settlement_unlinked":
        return _ErrorMapping(
            http_status=500,
            response_status="error",
            inbox_code="payment_settlement_unlinked",
            max_attempts=10,
        )
    if suffix == "provider_event_unresolved":
        return _ErrorMapping(
            http_status=500,
            response_status="error",
            inbox_code="payment_provider_event_unresolved",
            max_attempts=10,
        )
    return _ErrorMapping(
        http_status=500,
        response_status="error",
        inbox_code="payment_event_processing_failed",
        max_attempts=10,
    )


def _response_for_error(error: DomainError, mapping: _ErrorMapping) -> JSONResponse:
    payload: dict[str, object] = {"status": mapping.response_status}
    if mapping.expose_detail:
        payload["detail"] = error.message
    return JSONResponse(payload, status_code=mapping.http_status)


def _processed_response(consequence: dict[str, object]) -> JSONResponse:
    raw_status = consequence.get("http_status", 200)
    status_code = raw_status if isinstance(raw_status, int) else 200
    return JSONResponse(
        {"status": str(consequence.get("status") or "ok")},
        status_code=status_code,
    )


def _log_processed_webhook(
    *,
    provider: PaymentWebhookProvider,
    source_provider_event_id: str,
    receipt_id: UUID,
    consequence: dict[str, object],
    replayed: bool,
) -> None:
    """Emit non-secret trace evidence after the financial consequence commits."""

    logger.info(
        "payment_webhook_consequence_confirmed",
        extra={
            "event": "payment_webhook_consequence_confirmed",
            "provider": provider.value,
            "source_provider_event_id": source_provider_event_id,
            "receipt_id": str(receipt_id),
            "provider_event_id": consequence.get("provider_event_id"),
            "payment_id": consequence.get("payment_id"),
            "consequence_status": consequence.get("status"),
            "replayed": replayed,
        },
    )


def _record_processing_failure(
    db: Session,
    *,
    receipt_id: UUID,
    error_code: str,
    error_detail: str,
    max_attempts: int,
    claimed_attempt: int | None = None,
) -> None:
    try:
        integration_inbox.fail_claimed_consequence(
            db,
            receipt_id=receipt_id,
            error_code=error_code,
            error_detail=error_detail,
            max_attempts=max_attempts,
            claimed_attempt=claimed_attempt,
        )
    except InboxLeaseLost:
        # We are already recording a failure for OUR claim when we learn the
        # receipt was reclaimed out from under us in between. The current
        # owner's state is the one that matters now; do not stamp our stale
        # attempt's failure over it.
        logger.warning(
            "payment webhook failure recording superseded (receipt_id=%s)",
            receipt_id,
        )


def _process_webhook(
    *,
    db: Session,
    body: bytes,
    signature: str,
    provider: PaymentWebhookProvider,
) -> JSONResponse:
    try:
        binding, signature_valid = payment_capability.verify_webhook_signature(
            db,
            provider_type=provider.value,
            body=body,
            signature=signature,
        )
        binding_id = binding.id
    except (InstallationError, payment_capability.PaymentCapabilityError) as exc:
        logger.error("%s webhook capability unavailable: %s", provider.value, exc)
        _record_ingress_outcome(provider, "capability_unavailable")
        return JSONResponse({"status": "provider not configured"}, status_code=503)
    if not signature_valid:
        logger.warning("Invalid %s webhook signature", provider.value)
        _record_ingress_outcome(provider, "invalid_signature")
        return JSONResponse({"status": "invalid signature"}, status_code=400)
    db_session_adapter.release_read_transaction(db)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        _record_ingress_outcome(provider, "invalid_payload")
        return JSONResponse({"status": "invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        _record_ingress_outcome(provider, "invalid_payload")
        return JSONResponse({"status": "invalid JSON"}, status_code=400)
    try:
        identity = identify_verified_payment_webhook(provider, payload)
    except PaymentWebhookError as exc:
        _record_ingress_outcome(provider, "rejected")
        mapping = _map_payment_webhook_error(exc)
        return _response_for_error(exc, mapping)

    logger.info("%s webhook: %s", provider.value, identity.event_type)
    try:
        receipt, should_process = integration_inbox.receive_and_claim_verified(
            db,
            capability_binding_id=binding_id,
            provider_event_id=identity.provider_event_id,
            event_type=identity.event_type,
            payload=payload,
            headers={"provider": provider.value},
            legacy_provider_event_id=identity.legacy_provider_event_id,
        )
        receipt_id = receipt.id
        receipt_state = receipt.state
        claimed_attempt = receipt.attempt_count
        consequence = dict(receipt.consequence_json or {})
        db_session_adapter.release_read_transaction(db)
    except ProviderEventIdentityCollision as exc:
        logger.error("%s webhook identity rejected: %s", provider.value, exc)
        _record_ingress_outcome(provider, "identity_conflict")
        return JSONResponse({"status": "event identity conflict"}, status_code=409)
    except InboxError as exc:
        logger.error("%s webhook receipt unavailable: %s", provider.value, exc)
        _record_ingress_outcome(provider, "receipt_unavailable")
        return JSONResponse({"status": "event requires replay"}, status_code=500)

    if not should_process:
        if receipt_state == "processing":
            # A live claim: we have decided nothing yet. Answering 200 here
            # would tell the provider delivery succeeded when no consequence
            # exists, and it would stop retrying — exactly the bug this PR
            # fixes. Keep it retrying instead. Once auto-reclaim is wired in
            # above, an EXPIRED lease never reaches this branch: it is
            # reclaimed and processed in the same request instead.
            _record_ingress_outcome(provider, "claim_in_progress")
            return JSONResponse({"status": "processing"}, status_code=409)
        _record_ingress_outcome(provider, "duplicate")
        _log_processed_webhook(
            provider=provider,
            source_provider_event_id=identity.provider_event_id,
            receipt_id=receipt_id,
            consequence=consequence,
            replayed=True,
        )
        return _processed_response(consequence)

    context = CommandContext.system(
        actor=f"provider:{provider.value}",
        scope=PROCESS_SCOPE,
        reason="Process signature-verified payment webhook receipt",
        idempotency_key=identity.provider_event_id,
    )
    try:
        result = process_claimed_payment_webhook(
            db,
            ProcessClaimedPaymentWebhookCommand(
                receipt_id=receipt_id,
                provider=provider,
                claimed_attempt=claimed_attempt,
            ),
            context=context,
        )
    except InboxLeaseLost:
        # This claim was reclaimed by someone else before we finished (see
        # inbox.InboxLeaseLost). The transaction already rolled back any
        # consequence we were about to commit, so there is nothing of ours to
        # record — the receipt now belongs to whichever claimant holds the
        # current attempt. Do not touch it: writing a failure here with our
        # stale attempt would either be rejected by the same fence or, worse,
        # stamp over the current owner's state.
        logger.warning(
            "%s webhook claim superseded before completion (receipt_id=%s)",
            provider.value,
            receipt_id,
        )
        _record_ingress_outcome(provider, "claim_superseded")
        return JSONResponse({"status": "processing"}, status_code=409)
    except DomainError as exc:
        mapping = _map_payment_webhook_error(exc)
        logger.warning(
            "%s webhook command rejected (%s): %s",
            provider.value,
            exc.code,
            exc.message,
        )
        _record_processing_failure(
            db,
            receipt_id=receipt_id,
            error_code=mapping.inbox_code,
            error_detail=exc.message,
            max_attempts=mapping.max_attempts,
            claimed_attempt=claimed_attempt,
        )
        _record_ingress_outcome(provider, "rejected")
        return _response_for_error(exc, mapping)
    except Exception as exc:
        logger.error(
            "%s webhook processing failed: %s",
            provider.value,
            type(exc).__name__,
            exc_info=True,
        )
        _record_processing_failure(
            db,
            receipt_id=receipt_id,
            error_code="payment_event_processing_failed",
            error_detail=type(exc).__name__,
            max_attempts=10,
            claimed_attempt=claimed_attempt,
        )
        _record_ingress_outcome(provider, "processing_error")
        return JSONResponse({"status": "error"}, status_code=500)

    consequence = result.consequence()
    _record_ingress_outcome(provider, "processed")
    _log_processed_webhook(
        provider=provider,
        source_provider_event_id=identity.provider_event_id,
        receipt_id=receipt_id,
        consequence=consequence,
        replayed=result.replayed,
    )
    return _processed_response(consequence)


def process_paystack_webhook(
    *, db: Session, body: bytes, signature: str
) -> JSONResponse:
    return _process_webhook(
        db=db,
        body=body,
        signature=signature,
        provider=PaymentWebhookProvider.PAYSTACK,
    )


def process_flutterwave_webhook(
    *, db: Session, body: bytes, signature: str
) -> JSONResponse:
    return _process_webhook(
        db=db,
        body=body,
        signature=signature,
        provider=PaymentWebhookProvider.FLUTTERWAVE,
    )
