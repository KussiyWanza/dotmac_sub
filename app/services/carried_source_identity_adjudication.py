"""Dual-reviewed adjudication for pre-handoff Sub-native customers.

This owner never invents or assigns a Splynx identifier and never writes money.
It records the narrow decision that a customer existed natively in Dotmac Omni
before the fixed financial handoff. The billing opening-history resolver may
then use complete canonical Sub facts instead of demanding legacy BSS history.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from string import hexdigits
from typing import TypeVar
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import Invoice, Payment
from app.models.carried_source_identity import (
    CarriedSourceIdentityAdjudication,
    CarriedSourceIdentityDisposition,
)
from app.models.catalog import Subscription
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.schemas.audit import AuditEventCreate
from app.services import audit as audit_service
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.prepaid_funding_reconstruction import LEGACY_FINANCIAL_HANDOFF_AT

ResultT = TypeVar("ResultT")
OWNER = "billing.carried_source_identity_adjudication"
CONCERN = "reviewed pre-handoff native customer provenance adjudication"
SCOPE = OWNER
SOURCE_SYSTEM = "dotmac_omni"
_AUDIT_ACTION = "carried_source_identity_adjudicated"
_CRM_REFERENCE_KEYS = (
    "crm_person_id",
    "crm_project_id",
    "crm_quote_id",
    "crm_sales_order_id",
)


class CarriedSourceIdentityBlocker(StrEnum):
    """Closed reasons that keep a source-identity decision unresolved."""

    account_created_after_handoff = "account_created_after_handoff"
    existing_splynx_customer_id = "existing_splynx_customer_id"
    missing_crm_subscriber_provenance = "missing_crm_subscriber_provenance"
    incomplete_crm_creation_provenance = "incomplete_crm_creation_provenance"
    unsupported_source_system = "unsupported_source_system"
    splynx_service_evidence_present = "splynx_service_evidence_present"
    splynx_invoice_evidence_present = "splynx_invoice_evidence_present"
    splynx_payment_evidence_present = "splynx_payment_evidence_present"


class CarriedSourceIdentityAdjudicationError(DomainError):
    """Stable failure at the carried-source adjudication boundary."""


@dataclass(frozen=True, slots=True)
class CarriedSourceIdentityPreview:
    """PII-free exact evidence preview for one account."""

    account_id: UUID
    disposition: CarriedSourceIdentityDisposition | None
    account_created_at: datetime
    financial_handoff_at: datetime
    source_system: str
    crm_reference_kinds: tuple[str, ...]
    splynx_service_evidence_count: int
    splynx_invoice_evidence_count: int
    splynx_payment_evidence_count: int
    blockers: tuple[CarriedSourceIdentityBlocker, ...]
    fingerprint: str
    existing_decision_id: UUID | None

    @property
    def eligible(self) -> bool:
        return (
            self.disposition is CarriedSourceIdentityDisposition.native_before_handoff
            and not self.blockers
        )


@dataclass(frozen=True, slots=True)
class ConfirmCarriedSourceIdentityCommand:
    """Exact dual-reviewed confirmation of a fresh evidence preview."""

    context: CommandContext
    account_id: UUID
    expected_preview_fingerprint: str
    evidence_ref: str
    evidence_sha256: str
    reviewed_by_id: UUID
    approved_by_id: UUID


@dataclass(frozen=True, slots=True)
class ConfirmCarriedSourceIdentityOutcome:
    decision_id: UUID
    account_id: UUID
    disposition: CarriedSourceIdentityDisposition
    preview_fingerprint: str
    replayed: bool


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> CarriedSourceIdentityAdjudicationError:
    return CarriedSourceIdentityAdjudicationError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _definition(name: str) -> OwnerCommandDefinition:
    return OwnerCommandDefinition(owner=OWNER, concern=CONCERN, name=name)


def _execute(
    db: Session,
    *,
    context: CommandContext,
    name: str,
    operation: Callable[[], ResultT],
) -> ResultT:
    return execute_owner_command(
        db,
        definition=_definition(name),
        context=context,
        operation=operation,
    )


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _normalized_metadata(account: Subscriber) -> dict[str, object]:
    return account.metadata_ if isinstance(account.metadata_, dict) else {}


def _nonempty_metadata_keys(
    metadata: dict[str, object], keys: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(
        key
        for key in keys
        if metadata.get(key) is not None and str(metadata.get(key)).strip()
    )


def _reference_fingerprint(value: object) -> str | None:
    normalized = str(value or "").strip()
    return _digest({"reference": normalized}) if normalized else None


def _count_legacy_evidence(
    db: Session,
    account_id: UUID,
) -> tuple[int, int, int]:
    service_count = int(
        db.scalar(
            select(func.count(Subscription.id)).where(
                Subscription.subscriber_id == account_id,
                Subscription.splynx_service_id.is_not(None),
            )
        )
        or 0
    )
    invoice_count = int(
        db.scalar(
            select(func.count(Invoice.id)).where(
                Invoice.account_id == account_id,
                Invoice.splynx_invoice_id.is_not(None),
            )
        )
        or 0
    )
    payment_count = int(
        db.scalar(
            select(func.count(Payment.id)).where(
                Payment.account_id == account_id,
                Payment.splynx_payment_id.is_not(None),
            )
        )
        or 0
    )
    return service_count, invoice_count, payment_count


def _build_preview(
    db: Session,
    account: Subscriber,
) -> CarriedSourceIdentityPreview:
    created_at = _stored_utc(account.created_at)
    metadata = _normalized_metadata(account)
    source_system = str(metadata.get("source") or "").strip().lower()
    crm_reference_kinds = _nonempty_metadata_keys(metadata, _CRM_REFERENCE_KEYS)
    service_count, invoice_count, payment_count = _count_legacy_evidence(db, account.id)
    blockers: list[CarriedSourceIdentityBlocker] = []
    if created_at > LEGACY_FINANCIAL_HANDOFF_AT:
        blockers.append(CarriedSourceIdentityBlocker.account_created_after_handoff)
    if account.splynx_customer_id is not None:
        blockers.append(CarriedSourceIdentityBlocker.existing_splynx_customer_id)
    if account.crm_subscriber_id is None:
        blockers.append(CarriedSourceIdentityBlocker.missing_crm_subscriber_provenance)
    if len(crm_reference_kinds) != len(_CRM_REFERENCE_KEYS):
        blockers.append(CarriedSourceIdentityBlocker.incomplete_crm_creation_provenance)
    if source_system != SOURCE_SYSTEM:
        blockers.append(CarriedSourceIdentityBlocker.unsupported_source_system)
    if service_count:
        blockers.append(CarriedSourceIdentityBlocker.splynx_service_evidence_present)
    if invoice_count:
        blockers.append(CarriedSourceIdentityBlocker.splynx_invoice_evidence_present)
    if payment_count:
        blockers.append(CarriedSourceIdentityBlocker.splynx_payment_evidence_present)

    existing = db.scalars(
        select(CarriedSourceIdentityAdjudication).where(
            CarriedSourceIdentityAdjudication.account_id == account.id
        )
    ).one_or_none()
    disposition = (
        CarriedSourceIdentityDisposition.native_before_handoff if not blockers else None
    )
    canonical = {
        "account_id": str(account.id),
        "account_created_at": created_at.isoformat(),
        "crm_reference_kinds": list(crm_reference_kinds),
        "crm_reference_fingerprints": {
            key: _reference_fingerprint(metadata.get(key))
            for key in crm_reference_kinds
        },
        "crm_subscriber_provenance_present": account.crm_subscriber_id is not None,
        "crm_subscriber_provenance_fingerprint": _reference_fingerprint(
            account.crm_subscriber_id
        ),
        "disposition": disposition.value if disposition is not None else None,
        "financial_handoff_at": LEGACY_FINANCIAL_HANDOFF_AT.isoformat(),
        "source_system": source_system,
        "splynx_customer_id_present": account.splynx_customer_id is not None,
        "splynx_invoice_evidence_count": invoice_count,
        "splynx_payment_evidence_count": payment_count,
        "splynx_service_evidence_count": service_count,
        "blockers": [item.value for item in blockers],
    }
    return CarriedSourceIdentityPreview(
        account_id=account.id,
        disposition=disposition,
        account_created_at=created_at,
        financial_handoff_at=LEGACY_FINANCIAL_HANDOFF_AT,
        source_system=source_system,
        crm_reference_kinds=crm_reference_kinds,
        splynx_service_evidence_count=service_count,
        splynx_invoice_evidence_count=invoice_count,
        splynx_payment_evidence_count=payment_count,
        blockers=tuple(blockers),
        fingerprint=_digest(canonical),
        existing_decision_id=existing.id if existing is not None else None,
    )


def preview_carried_source_identity_adjudication(
    db: Session,
    account_id: UUID,
) -> CarriedSourceIdentityPreview:
    """Return a PII-free reviewed-decision preview without mutation."""

    account = db.get(Subscriber, account_id)
    if account is None:
        raise _error(
            "account_not_found",
            "The requested customer account does not exist.",
            account_id=str(account_id),
        )
    return _build_preview(db, account)


def _validate_digest(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if (
        len(normalized) != 64
        or any(char not in hexdigits for char in normalized)
        or not normalized.isascii()
    ):
        raise _error("invalid_evidence", f"{field} must be a SHA-256 digest.")
    return normalized


def _validate_context(context: CommandContext) -> str:
    if context.scope != SCOPE:
        raise _error("invalid_scope", "The adjudication command scope is invalid.")
    key = str(context.idempotency_key or "").strip()
    if not key:
        raise _error("missing_idempotency_key", "An idempotency key is required.")
    if len(key) > 200:
        raise _error("invalid_evidence", "The idempotency key is too long.")
    return key


def _serialize_key(db: Session, key: str) -> None:
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


def _active_reviewers(
    db: Session,
    reviewed_by_id: UUID,
    approved_by_id: UUID,
) -> None:
    if reviewed_by_id == approved_by_id:
        raise _error(
            "reviewer_conflict",
            "The reviewer and independent approver must be different staff users.",
        )
    rows = list(
        db.scalars(
            select(SystemUser)
            .where(SystemUser.id.in_((reviewed_by_id, approved_by_id)))
            .with_for_update()
        ).all()
    )
    active = {row.id for row in rows if row.is_active}
    missing = sorted({reviewed_by_id, approved_by_id} - active, key=str)
    if missing:
        raise _error(
            "reviewer_unavailable",
            "Both reviewers must be active staff principals.",
            reviewer_ids=[str(value) for value in missing],
        )


def _command_fingerprint(
    command: ConfirmCarriedSourceIdentityCommand,
    *,
    idempotency_key: str,
    evidence_ref: str,
    evidence_sha256: str,
) -> str:
    return _digest(
        {
            "account_id": str(command.account_id),
            "approved_by_id": str(command.approved_by_id),
            "evidence_ref": evidence_ref,
            "evidence_sha256": evidence_sha256,
            "expected_preview_fingerprint": command.expected_preview_fingerprint,
            "idempotency_key": idempotency_key,
            "reason": command.context.reason.strip(),
            "reviewed_by_id": str(command.reviewed_by_id),
        }
    )


def _outcome(
    decision: CarriedSourceIdentityAdjudication,
    *,
    replayed: bool,
) -> ConfirmCarriedSourceIdentityOutcome:
    return ConfirmCarriedSourceIdentityOutcome(
        decision_id=decision.id,
        account_id=decision.account_id,
        disposition=decision.disposition,
        preview_fingerprint=decision.preview_fingerprint,
        replayed=replayed,
    )


def _confirm(
    db: Session,
    command: ConfirmCarriedSourceIdentityCommand,
) -> ConfirmCarriedSourceIdentityOutcome:
    idempotency_key = _validate_context(command.context)
    preview_fingerprint = _validate_digest(
        command.expected_preview_fingerprint,
        field="expected_preview_fingerprint",
    )
    evidence_sha256 = _validate_digest(
        command.evidence_sha256,
        field="evidence_sha256",
    )
    evidence_ref = command.evidence_ref.strip()
    if not evidence_ref or len(evidence_ref) > 240:
        raise _error(
            "invalid_evidence",
            "The reviewed evidence reference must contain at most 240 characters.",
        )
    reason = command.context.reason.strip()
    if not reason:
        raise _error("invalid_evidence", "The adjudication reason is required.")
    if len(reason) > 1000:
        raise _error("invalid_evidence", "The adjudication reason is too long.")

    _serialize_key(db, f"{OWNER}:{idempotency_key}")
    fingerprint = _command_fingerprint(
        command,
        idempotency_key=idempotency_key,
        evidence_ref=evidence_ref,
        evidence_sha256=evidence_sha256,
    )
    replay = db.scalars(
        select(CarriedSourceIdentityAdjudication)
        .where(CarriedSourceIdentityAdjudication.idempotency_key == idempotency_key)
        .with_for_update()
    ).one_or_none()
    if replay is not None:
        if replay.command_fingerprint != fingerprint:
            raise _error(
                "idempotency_conflict",
                "The idempotency key was already used with different evidence.",
            )
        return _outcome(replay, replayed=True)

    account = db.scalars(
        select(Subscriber).where(Subscriber.id == command.account_id).with_for_update()
    ).one_or_none()
    if account is None:
        raise _error(
            "account_not_found",
            "The requested customer account does not exist.",
            account_id=str(command.account_id),
        )
    existing = db.scalars(
        select(CarriedSourceIdentityAdjudication)
        .where(CarriedSourceIdentityAdjudication.account_id == command.account_id)
        .with_for_update()
    ).one_or_none()
    if existing is not None:
        if existing.command_fingerprint == fingerprint:
            return _outcome(existing, replayed=True)
        raise _error(
            "decision_conflict",
            "The customer already has a different carried-source adjudication.",
            decision_id=str(existing.id),
        )

    preview = _build_preview(db, account)
    if not preview.eligible:
        raise _error(
            "ineligible_evidence",
            "The current evidence does not prove pre-handoff native provenance.",
            blockers=[item.value for item in preview.blockers],
        )
    if preview.fingerprint != preview_fingerprint:
        raise _error(
            "stale_preview",
            "The source-identity evidence changed after review.",
            current_preview_fingerprint=preview.fingerprint,
        )
    _active_reviewers(db, command.reviewed_by_id, command.approved_by_id)

    decision = CarriedSourceIdentityAdjudication(
        account_id=account.id,
        disposition=CarriedSourceIdentityDisposition.native_before_handoff,
        source_system=SOURCE_SYSTEM,
        financial_handoff_at=LEGACY_FINANCIAL_HANDOFF_AT,
        account_created_at=preview.account_created_at,
        preview_fingerprint=preview.fingerprint,
        evidence_ref=evidence_ref,
        evidence_sha256=evidence_sha256,
        reviewed_by_id=command.reviewed_by_id,
        approved_by_id=command.approved_by_id,
        reason=reason,
        idempotency_key=idempotency_key,
        command_fingerprint=fingerprint,
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
    )
    db.add(decision)
    db.flush()

    audit_service.audit_events.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.service,
            actor_id=command.context.actor,
            action=_AUDIT_ACTION,
            entity_type="subscriber",
            entity_id=str(account.id),
            status_code=200,
            is_success=True,
            request_id=str(command.context.correlation_id),
            metadata_={
                "decision_id": str(decision.id),
                "disposition": decision.disposition.value,
                "evidence_ref": evidence_ref,
                "evidence_sha256": evidence_sha256,
                "preview_fingerprint": preview.fingerprint,
                "reviewed_by_id": str(command.reviewed_by_id),
                "approved_by_id": str(command.approved_by_id),
                "reason": reason,
            },
        ),
    )
    emit_event(
        db,
        EventType.carried_source_identity_adjudicated,
        {
            "account_id": str(account.id),
            "decision_id": str(decision.id),
            "disposition": decision.disposition.value,
            "preview_fingerprint": preview.fingerprint,
        },
        actor=command.context.actor,
        subscriber_id=account.id,
    )
    db.flush()
    return _outcome(decision, replayed=False)


def confirm_carried_source_identity_adjudication(
    db: Session,
    command: ConfirmCarriedSourceIdentityCommand,
) -> ConfirmCarriedSourceIdentityOutcome:
    """Persist one exact reviewed decision in the owner transaction."""

    return _execute(
        db,
        context=command.context,
        name="confirm_carried_source_identity_adjudication",
        operation=lambda: _confirm(db, command),
    )


__all__ = [
    "CarriedSourceIdentityAdjudicationError",
    "CarriedSourceIdentityBlocker",
    "CarriedSourceIdentityPreview",
    "ConfirmCarriedSourceIdentityCommand",
    "ConfirmCarriedSourceIdentityOutcome",
    "confirm_carried_source_identity_adjudication",
    "preview_carried_source_identity_adjudication",
]
