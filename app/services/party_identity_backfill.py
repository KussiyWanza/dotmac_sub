"""Guarded executor for an explicitly approved Subscriber-to-Party plan.

The read-only audit and adjudication planner remain the decision owners. This
service is the sole backfill adapter: it revalidates the exact plan, locks the
selected Subscriber accounts, calls ``party.registry`` commands, and records a
PII-free receipt. It never merges identities, repoints an existing binding,
assigns roles, copies contact points, or changes lifecycle/billing/access state.

The low-level plan executor never commits. The transactional command in this
module owns SERIALIZABLE isolation, fresh audit revalidation, commit, and
rollback so delivery scripts remain thin non-writers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.db import begin_serializable_write
from app.models.party import (
    Party,
    PartyIdentityBackfillReceipt,
    PartyIdentityStatus,
)
from app.models.subscriber import Subscriber
from app.services import party as party_registry
from app.services import party_identity_audit
from app.services.party_identity_adjudication import (
    PartyBackfillPlan,
    PartyIdentityDecision,
    build_party_backfill_plan,
    party_backfill_plan_digest,
)
from app.services.party_identity_audit import SubscriberIdentityAudit

_MAX_FUTURE_APPROVAL_SKEW = timedelta(minutes=5)
_MAX_APPROVAL_WINDOW = timedelta(hours=24)
_RECEIPT_METADATA_KEY = "identity_backfill"
MAX_PARTIES_PER_EXECUTION = 500
MAX_BINDINGS_PER_EXECUTION = 1000


class PartyIdentityBackfillError(ValueError):
    """Raised before an unsafe or stale identity backfill can write."""

    def __init__(self, errors: str | list[str] | tuple[str, ...]):
        self.errors = (errors,) if isinstance(errors, str) else tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class PartyBackfillExecutionApproval:
    """Protected human approval envelope for one exact plan and input file."""

    plan_digest: str
    audit_digest: str
    decision_file_sha256: str
    plan_file_sha256: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    reason: str
    maximum_parties: int
    maximum_bindings: int


@dataclass(frozen=True)
class PartyBackfillExecutionOutcome:
    receipt_id: UUID
    plan_digest: str
    parties_created: int
    bindings_created: int
    replayed: bool


def _set_serializable_read_write(db: Session) -> None:
    begin_serializable_write(db)


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()


def _required_text(value: str | None, field_name: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise PartyIdentityBackfillError(f"{field_name} is required")
    return cleaned


def _require_sha256(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if len(cleaned) != 64 or cleaned != cleaned.lower():
        raise PartyIdentityBackfillError(
            f"{field_name} must be a lowercase SHA-256 digest"
        )
    try:
        int(cleaned, 16)
    except ValueError as exc:
        raise PartyIdentityBackfillError(
            f"{field_name} must be a lowercase SHA-256 digest"
        ) from exc
    return cleaned


def _validate_approval(
    approval: PartyBackfillExecutionApproval,
    *,
    executed_at: datetime,
    decision_file_sha256: str,
    plan_file_sha256: str,
    approval_file_sha256: str,
) -> tuple[str, str]:
    errors: list[str] = []
    for digest_value, field_name in (
        (approval.plan_digest, "approval.plan_digest"),
        (approval.audit_digest, "approval.audit_digest"),
        (approval.decision_file_sha256, "approval.decision_file_sha256"),
        (approval.plan_file_sha256, "approval.plan_file_sha256"),
        (decision_file_sha256, "decision_file_sha256"),
        (plan_file_sha256, "plan_file_sha256"),
        (approval_file_sha256, "approval_file_sha256"),
    ):
        try:
            _require_sha256(digest_value, field_name)
        except PartyIdentityBackfillError as exc:
            errors.extend(exc.errors)
    approved_by = ""
    reason = ""
    try:
        approved_by = _required_text(approval.approved_by, "approval.approved_by")
        reason = _required_text(approval.reason, "approval.reason")
    except PartyIdentityBackfillError as exc:
        errors.extend(exc.errors)
    for timestamp, field_name in (
        (approval.approved_at, "approval.approved_at"),
        (approval.expires_at, "approval.expires_at"),
        (executed_at, "executed_at"),
    ):
        if timestamp.tzinfo is None:
            errors.append(f"{field_name} must be timezone-aware")
    if approval.maximum_parties < 0:
        errors.append("approval.maximum_parties must be nonnegative")
    if approval.maximum_bindings < 0:
        errors.append("approval.maximum_bindings must be nonnegative")
    if not errors:
        if approval.expires_at < approval.approved_at:
            errors.append("approval expires before it was approved")
        if approval.expires_at - approval.approved_at > _MAX_APPROVAL_WINDOW:
            errors.append("approval window cannot exceed 24 hours")
        if approval.approved_at > executed_at + _MAX_FUTURE_APPROVAL_SKEW:
            errors.append("approval.approved_at is in the future")
        if executed_at > approval.expires_at:
            errors.append("approval has expired")
        if approval.decision_file_sha256 != decision_file_sha256:
            errors.append("decision file does not match the approved SHA-256")
        if approval.plan_file_sha256 != plan_file_sha256:
            errors.append("plan file does not match the approved SHA-256")
    if errors:
        raise PartyIdentityBackfillError(errors)
    return approved_by, reason


def _binding_source(plan_digest: str) -> str:
    return f"party_backfill:{plan_digest}"


def _binding_reason(binding: dict[str, Any]) -> str:
    return f"decision={binding['decision_id']};reason_sha256={binding['reason_sha256']}"


def _display_name(subscriber: Subscriber, source: str) -> str:
    if source == "subscriber_full_name":
        value = " ".join(
            part.strip()
            for part in (subscriber.first_name, subscriber.last_name)
            if part and part.strip()
        )
    elif source == "subscriber_display_name":
        value = (subscriber.display_name or "").strip()
    elif source == "company_name":
        value = (subscriber.company_name or "").strip()
    elif source == "legal_name":
        value = (subscriber.legal_name or "").strip()
    else:
        raise PartyIdentityBackfillError(f"unsupported display-name source '{source}'")
    if not value:
        raise PartyIdentityBackfillError(
            f"selected display-name source '{source}' is now blank"
        )
    return value


def _receipt_metadata(plan: PartyBackfillPlan, group: dict[str, Any]) -> dict:
    return {
        _RECEIPT_METADATA_KEY: {
            "contract_version": 1,
            "plan_digest": plan.plan_digest,
            "audit_digest": plan.audit_digest,
            "identity_source_subscriber_id": group["identity_source_subscriber_id"],
            "display_name_source": group["display_name_source"],
            "subscriber_ids": list(group["subscriber_ids"]),
            "decision_ids": list(group["decision_ids"]),
        }
    }


def _lock_subscribers(
    db: Session, subscriber_ids: tuple[UUID, ...]
) -> dict[UUID, Subscriber]:
    if not subscriber_ids:
        return {}
    rows = (
        db.query(Subscriber)
        .filter(Subscriber.id.in_(subscriber_ids))
        .populate_existing()
        .with_for_update()
        .all()
    )
    by_id = {row.id: row for row in rows}
    missing = sorted(set(subscriber_ids) - set(by_id), key=str)
    if missing:
        raise PartyIdentityBackfillError(
            "selected Subscribers disappeared before execution: "
            + ",".join(map(str, missing))
        )
    return by_id


def _lock_planned_party_collisions(
    db: Session, planned_party_ids: tuple[UUID, ...]
) -> None:
    if not planned_party_ids:
        return
    collisions = (
        db.query(Party.id)
        .filter(Party.id.in_(planned_party_ids))
        .with_for_update()
        .all()
    )
    if collisions:
        raise PartyIdentityBackfillError(
            "planned Party UUIDs already exist without this execution receipt: "
            + ",".join(sorted(str(row[0]) for row in collisions))
        )


def _verify_receipt_matches_approval(
    receipt: PartyIdentityBackfillReceipt,
    approval: PartyBackfillExecutionApproval,
    *,
    approved_by: str,
    approval_reason: str,
    approval_file_sha256: str,
) -> None:
    expected = {
        "audit_digest": approval.audit_digest,
        "decision_file_sha256": approval.decision_file_sha256,
        "plan_file_sha256": approval.plan_file_sha256,
        "approval_file_sha256": approval_file_sha256,
        "approved_by_sha256": _text_digest(approved_by),
        "approval_reason_sha256": _text_digest(approval_reason),
    }
    mismatches = [
        field_name
        for field_name, value in expected.items()
        if getattr(receipt, field_name) != value
    ]
    if mismatches:
        raise PartyIdentityBackfillError(
            "existing receipt conflicts with approval fields: " + ",".join(mismatches)
        )
    if party_backfill_plan_digest(receipt.manifest) != receipt.plan_digest:
        raise PartyIdentityBackfillError(
            "existing execution receipt manifest does not match its plan digest"
        )


def _verify_applied_manifest(
    db: Session,
    receipt: PartyIdentityBackfillReceipt,
) -> None:
    manifest = receipt.manifest
    try:
        groups = manifest["groups"]
        bindings = manifest["bindings"]
        planned_party_ids = tuple(UUID(group["planned_party_id"]) for group in groups)
        subscriber_ids = tuple(UUID(item["subscriber_id"]) for item in bindings)
    except (KeyError, TypeError, ValueError) as exc:
        raise PartyIdentityBackfillError(
            "existing execution receipt has an invalid manifest"
        ) from exc
    if len(groups) != receipt.planned_party_count:
        raise PartyIdentityBackfillError(
            "existing execution receipt Party count does not match its manifest"
        )
    if len(bindings) != receipt.binding_count:
        raise PartyIdentityBackfillError(
            "existing execution receipt binding count does not match its manifest"
        )
    parties = {
        row.id: row
        for row in db.query(Party)
        .filter(Party.id.in_(planned_party_ids))
        .with_for_update()
        .all()
    }
    subscribers = _lock_subscribers(db, subscriber_ids)
    errors: list[str] = []
    for group in groups:
        party_id = UUID(group["planned_party_id"])
        party = parties.get(party_id)
        if party is None:
            errors.append(f"Party '{party_id}' from the receipt is missing")
            continue
        if party.party_type != group["party_type"]:
            errors.append(f"Party '{party_id}' type drifted from the receipt")
        provenance = (party.metadata_ or {}).get(_RECEIPT_METADATA_KEY, {})
        expected_provenance = {
            "contract_version": 1,
            "plan_digest": receipt.plan_digest,
            "audit_digest": receipt.audit_digest,
            "identity_source_subscriber_id": group["identity_source_subscriber_id"],
            "display_name_source": group["display_name_source"],
            "subscriber_ids": list(group["subscriber_ids"]),
            "decision_ids": list(group["decision_ids"]),
        }
        if provenance != expected_provenance:
            errors.append(f"Party '{party_id}' backfill provenance drifted")
    source = _binding_source(receipt.plan_digest)
    for binding in bindings:
        subscriber_id = UUID(binding["subscriber_id"])
        subscriber = subscribers[subscriber_id]
        target_id = UUID(binding["planned_party_id"])
        if subscriber.party_id != target_id:
            errors.append(
                f"Subscriber '{subscriber_id}' no longer matches receipt Party "
                f"'{target_id}'"
            )
        if subscriber.party_binding_source != source:
            errors.append(f"Subscriber '{subscriber_id}' binding source drifted")
        if subscriber.party_binding_reason != _binding_reason(binding):
            errors.append(f"Subscriber '{subscriber_id}' binding reason drifted")
    if errors:
        raise PartyIdentityBackfillError(errors)


def execute_party_backfill_plan(
    db: Session,
    *,
    audit: SubscriberIdentityAudit,
    decisions: tuple[PartyIdentityDecision, ...],
    approval: PartyBackfillExecutionApproval,
    decision_file_sha256: str,
    plan_file_sha256: str,
    approval_file_sha256: str,
    executed_at: datetime | None = None,
) -> PartyBackfillExecutionOutcome:
    """Apply one exact approved plan atomically without committing.

    Exact retries use the durable receipt and verify the resulting identity
    links. Any partial state, provenance drift, collision, stale audit, expired
    approval, count overflow, merge, or repoint attempt is refused.
    """

    now = executed_at or datetime.now(UTC)
    approved_by, approval_reason = _validate_approval(
        approval,
        executed_at=now,
        decision_file_sha256=decision_file_sha256,
        plan_file_sha256=plan_file_sha256,
        approval_file_sha256=approval_file_sha256,
    )
    receipt = (
        db.query(PartyIdentityBackfillReceipt)
        .filter(PartyIdentityBackfillReceipt.plan_digest == approval.plan_digest)
        .with_for_update()
        .one_or_none()
    )
    if receipt is not None:
        _verify_receipt_matches_approval(
            receipt,
            approval,
            approved_by=approved_by,
            approval_reason=approval_reason,
            approval_file_sha256=approval_file_sha256,
        )
        _verify_applied_manifest(db, receipt)
        return PartyBackfillExecutionOutcome(
            receipt_id=receipt.id,
            plan_digest=receipt.plan_digest,
            parties_created=0,
            bindings_created=0,
            replayed=True,
        )

    plan = build_party_backfill_plan(audit, decisions, planned_at=now)
    errors: list[str] = []
    if plan.plan_digest != approval.plan_digest:
        errors.append("current plan digest does not match the approved plan")
    if plan.audit_digest != approval.audit_digest:
        errors.append("current audit digest does not match the approved audit")
    if len(plan.groups) > approval.maximum_parties:
        errors.append("planned Party count exceeds the approved maximum")
    if len(plan.bindings) > approval.maximum_bindings:
        errors.append("planned binding count exceeds the approved maximum")
    if len(plan.groups) > MAX_PARTIES_PER_EXECUTION:
        errors.append(
            f"plan exceeds the per-execution Party limit of {MAX_PARTIES_PER_EXECUTION}"
        )
    if len(plan.bindings) > MAX_BINDINGS_PER_EXECUTION:
        errors.append(
            f"plan exceeds the per-execution binding limit of "
            f"{MAX_BINDINGS_PER_EXECUTION}"
        )
    if not plan.groups or not plan.bindings:
        errors.append("plan has no actionable Party and binding writes")
    if errors:
        raise PartyIdentityBackfillError(errors)

    manifest = plan.digest_payload()
    groups = manifest["groups"]
    bindings = manifest["bindings"]
    subscriber_ids = tuple(
        sorted((UUID(item["subscriber_id"]) for item in bindings), key=str)
    )
    planned_party_ids = tuple(
        sorted((UUID(group["planned_party_id"]) for group in groups), key=str)
    )
    with db.begin_nested():
        subscribers = _lock_subscribers(db, subscriber_ids)
        _lock_planned_party_collisions(db, planned_party_ids)
        for group in groups:
            party_id = UUID(group["planned_party_id"])
            source_subscriber = subscribers[
                UUID(group["identity_source_subscriber_id"])
            ]
            party_registry.create_party(
                db,
                party_id=party_id,
                party_type=group["party_type"],
                display_name=_display_name(
                    source_subscriber,
                    group["display_name_source"],
                ),
                data_classification=group["data_classification"],
                metadata=_receipt_metadata(plan, group),
            )
            if group["target_status"] == PartyIdentityStatus.quarantined.value:
                party_registry.quarantine_party(
                    db,
                    party_id=party_id,
                    reason=(
                        "Reviewed identity backfill requires quarantine; "
                        f"plan_digest={plan.plan_digest}"
                    ),
                )
        source = _binding_source(plan.plan_digest)
        for binding in bindings:
            party_registry.bind_subscriber_account(
                db,
                subscriber_id=UUID(binding["subscriber_id"]),
                party_id=UUID(binding["planned_party_id"]),
                source=source,
                reason=_binding_reason(binding),
            )
        receipt = PartyIdentityBackfillReceipt(
            plan_digest=plan.plan_digest,
            audit_digest=plan.audit_digest,
            decision_file_sha256=decision_file_sha256,
            plan_file_sha256=plan_file_sha256,
            approval_file_sha256=approval_file_sha256,
            approved_by_sha256=_text_digest(approved_by),
            approval_reason_sha256=_text_digest(approval_reason),
            approved_at=approval.approved_at,
            expires_at=approval.expires_at,
            applied_at=now,
            planned_party_count=len(groups),
            binding_count=len(bindings),
            manifest=manifest,
        )
        db.add(receipt)
        db.flush()
    return PartyBackfillExecutionOutcome(
        receipt_id=receipt.id,
        plan_digest=plan.plan_digest,
        parties_created=len(groups),
        bindings_created=len(bindings),
        replayed=False,
    )


def execute_party_backfill_transaction(
    db: Session,
    *,
    decisions: tuple[PartyIdentityDecision, ...],
    approval: PartyBackfillExecutionApproval,
    decision_file_sha256: str,
    plan_file_sha256: str,
    approval_file_sha256: str,
) -> PartyBackfillExecutionOutcome:
    """Revalidate and commit one approved backfill through the named owner."""

    try:
        _set_serializable_read_write(db)
        audit = party_identity_audit.build_subscriber_identity_audit(db)
        outcome = execute_party_backfill_plan(
            db,
            audit=audit,
            decisions=decisions,
            approval=approval,
            decision_file_sha256=decision_file_sha256,
            plan_file_sha256=plan_file_sha256,
            approval_file_sha256=approval_file_sha256,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return outcome
