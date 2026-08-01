"""Prepaid renewal-terms backfill (ADR 0007 stage 3, migration owner).

Prepaid enforcement fails closed with ``renewal_terms_unresolved`` when an
active prepaid subscription carries no frozen contracted amount
(``Subscription.unit_price`` NULL or <= 0). The contracted amount is never
inferred from the mutable catalog (ADR 0007 Phase 1): this owner restores it
only from the subscription's own exact evidence — the base-subscription lines
of its PAID invoices. A subscription whose paid evidence is absent or
self-contradictory becomes an owned, SLA-bound finance work item and stays
fail-closed.

TRANSITIONAL: retire at the ADR 0007 Phase 1 cutover, when
``billing.contracts`` becomes authoritative and ``Subscription.unit_price``
stops being the renewal-charge authority.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import BillingMode, Subscription
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)

OWNER = "financial.prepaid_renewal_terms_backfill"

_CAPTURE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="prepaid renewal-terms evidence backfill",
    name="capture_prepaid_renewal_terms_backfill",
)

_POLICY_VERSION = "prepaid-renewal-terms-backfill-v2"
_FINDING_PREFIX = "prepaid-renewal-terms:evidence:"
#: Finance review window recorded on each unresolved-evidence work item.
_EVIDENCE_SLA_HOURS = 72


class PrepaidRenewalTermsBackfillError(DomainError):
    """Fail-closed renewal-terms backfill error."""


def _error(code: str, message: str) -> PrepaidRenewalTermsBackfillError:
    return PrepaidRenewalTermsBackfillError(code=code, message=message)


class RenewalTermsDecision(StrEnum):
    repairable = "repairable"
    ambiguous_amounts = "ambiguous_amounts"
    insufficient_cycle_evidence = "insufficient_cycle_evidence"
    no_evidence = "no_evidence"


#: A billing period is accepted as one explicit full monthly cycle when its
#: length falls inside this window.
_FULL_CYCLE_MIN_DAYS = 25
_FULL_CYCLE_MAX_DAYS = 35
_PRORATION_MARKERS = ("proration", "prorated", "pro_rata")


@dataclass(frozen=True, slots=True)
class PaidLineEvidence:
    """One paid base-subscription invoice line, fully identity-bound.

    The v2 fingerprint covers every field, so any evidence change — even one
    that leaves the classified amount identical — invalidates a reviewed
    preview.
    """

    invoice_id: UUID
    invoice_line_id: UUID
    unit_price: Decimal
    quantity: Decimal
    amount: Decimal
    currency: str
    period_start: datetime | None
    period_end: datetime | None
    proration_marker: str | None
    full_cycle: bool
    compatible: bool
    incompatibility: str | None

    def as_payload(self) -> dict[str, str | None]:
        return {
            "invoice_id": str(self.invoice_id),
            "invoice_line_id": str(self.invoice_line_id),
            "unit_price": str(self.unit_price),
            "quantity": str(self.quantity),
            "amount": str(self.amount),
            "currency": self.currency,
            "period_start": (
                self.period_start.isoformat() if self.period_start else None
            ),
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "proration_marker": self.proration_marker,
            "full_cycle": str(self.full_cycle),
            "compatible": str(self.compatible),
            "incompatibility": self.incompatibility,
        }


@dataclass(frozen=True, slots=True)
class RenewalTermsEvidenceItem:
    """Exact-evidence verdict for one blocked prepaid subscription."""

    subscription_id: UUID
    account_id: UUID
    decision: RenewalTermsDecision
    contracted_amount: Decimal | None
    distinct_paid_amounts: tuple[Decimal, ...]
    paid_line_count: int
    evidence: tuple[PaidLineEvidence, ...] = ()
    insufficiency_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RenewalTermsBackfillPreview:
    as_of: datetime
    items: tuple[RenewalTermsEvidenceItem, ...]
    fingerprint: str

    @property
    def repairable_count(self) -> int:
        return sum(
            1 for i in self.items if i.decision is RenewalTermsDecision.repairable
        )

    @property
    def unresolved_count(self) -> int:
        return len(self.items) - self.repairable_count


@dataclass(frozen=True, slots=True)
class CaptureRenewalTermsBackfillCommand:
    preview_fingerprint: str
    as_of: datetime


@dataclass(frozen=True, slots=True)
class RenewalTermsBackfillResult:
    repaired_count: int
    work_item_count: int
    fingerprint: str


def _blocked_subscriptions(db: Session) -> list[Subscription]:
    # The threshold owner evaluates every COLLECTIBLE status, not just
    # active: a suspended prepaid subscription with no frozen contracted
    # amount still blocks its account (including funded restoration), so the
    # backfill must see it too.
    from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES

    rows = db.scalars(
        select(Subscription).where(
            Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES),
            Subscription.billing_mode == BillingMode.prepaid,
        )
    ).all()
    return sorted(
        (
            sub
            for sub in rows
            if sub.unit_price is None or sub.unit_price <= Decimal("0.00")
        ),
        key=lambda sub: str(sub.id),
    )


def _paid_base_line_evidence(
    db: Session, subscription: Subscription, *, enforcement_currency: str
) -> tuple[PaidLineEvidence, ...]:
    rows = db.execute(
        select(InvoiceLine, Invoice)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(
            InvoiceLine.subscription_id == subscription.id,
            InvoiceLine.is_active.is_(True),
            Invoice.status == InvoiceStatus.paid,
        )
        .order_by(Invoice.id, InvoiceLine.id)
    ).all()
    evidence: list[PaidLineEvidence] = []
    for line, invoice in rows:
        metadata = line.metadata_ or {}
        if metadata.get("kind") != "base_subscription":
            continue
        unit_price = Decimal(str(line.unit_price)).quantize(Decimal("0.01"))
        if unit_price <= Decimal("0.00"):
            continue
        quantity = Decimal(str(line.quantity))
        amount = Decimal(str(line.amount)).quantize(Decimal("0.01"))
        proration_marker = next(
            (marker for marker in _PRORATION_MARKERS if metadata.get(marker)),
            None,
        )
        period_start = invoice.billing_period_start
        period_end = invoice.billing_period_end
        full_cycle = False
        if period_start is not None and period_end is not None:
            length_days = (period_end - period_start).days
            full_cycle = _FULL_CYCLE_MIN_DAYS <= length_days <= _FULL_CYCLE_MAX_DAYS
        incompatibility: str | None = None
        if (invoice.currency or "").upper() != enforcement_currency.upper():
            incompatibility = "currency_mismatch"
        elif proration_marker is not None:
            incompatibility = "prorated"
        elif quantity != Decimal("1"):
            incompatibility = "quantity_not_one"
        elif amount != (unit_price * quantity).quantize(Decimal("0.01")):
            incompatibility = "amount_mismatch"
        evidence.append(
            PaidLineEvidence(
                invoice_id=invoice.id,
                invoice_line_id=line.id,
                unit_price=unit_price,
                quantity=quantity,
                amount=amount,
                currency=(invoice.currency or ""),
                period_start=period_start,
                period_end=period_end,
                proration_marker=proration_marker,
                full_cycle=full_cycle,
                compatible=incompatibility is None,
                incompatibility=incompatibility,
            )
        )
    return tuple(evidence)


def _classify(
    db: Session, subscription: Subscription, *, enforcement_currency: str
) -> RenewalTermsEvidenceItem:
    evidence = _paid_base_line_evidence(
        db, subscription, enforcement_currency=enforcement_currency
    )
    cycle = subscription.billing_cycle.value if subscription.billing_cycle else None
    compatible = [e for e in evidence if e.compatible]
    proven = [e for e in compatible if e.full_cycle]
    distinct_all = tuple(sorted({e.unit_price for e in evidence}))
    distinct_compatible = tuple(sorted({e.unit_price for e in compatible}))
    reasons: list[str] = []
    contracted: Decimal | None = None

    if cycle not in (None, "monthly"):
        decision = RenewalTermsDecision.insufficient_cycle_evidence
        reasons.append(f"cadence_incompatible:{cycle}")
    elif not evidence:
        decision = RenewalTermsDecision.no_evidence
    elif len(distinct_compatible) > 1:
        decision = RenewalTermsDecision.ambiguous_amounts
        reasons.append("conflicting_compatible_amounts")
    elif not compatible:
        decision = RenewalTermsDecision.insufficient_cycle_evidence
        reasons.extend(
            sorted({e.incompatibility for e in evidence if e.incompatibility})
        )
    elif proven or len(compatible) >= 2:
        # One explicitly proven full-cycle line, or repeated compatible
        # lines, establishes the contracted monthly amount.
        decision = RenewalTermsDecision.repairable
        contracted = distinct_compatible[0]
    else:
        # A lone line with no explicit full-cycle proof is not enough: it
        # may be a prorated or partial first invoice.
        decision = RenewalTermsDecision.insufficient_cycle_evidence
        reasons.append("lone_line_without_full_cycle_proof")

    return RenewalTermsEvidenceItem(
        subscription_id=subscription.id,
        account_id=subscription.subscriber_id,
        decision=decision,
        contracted_amount=contracted,
        distinct_paid_amounts=distinct_all,
        paid_line_count=len(evidence),
        evidence=evidence,
        insufficiency_reasons=tuple(reasons),
    )


def _fingerprint(items: tuple[RenewalTermsEvidenceItem, ...]) -> str:
    payload = {
        "policy_version": _POLICY_VERSION,
        "items": [
            {
                "subscription_id": str(item.subscription_id),
                "decision": item.decision.value,
                "amount": (
                    str(item.contracted_amount)
                    if item.contracted_amount is not None
                    else None
                ),
                "reasons": list(item.insufficiency_reasons),
                "evidence": [e.as_payload() for e in item.evidence],
            }
            for item in items
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def preview_prepaid_renewal_terms_backfill(
    db: Session, *, now: datetime | None = None
) -> RenewalTermsBackfillPreview:
    """Classify every blocked prepaid subscription against its paid evidence."""
    from app.services.prepaid_currency import resolve_prepaid_enforcement_currency

    as_of = now or datetime.now(UTC)
    currency = resolve_prepaid_enforcement_currency(db)
    items = tuple(
        _classify(db, sub, enforcement_currency=currency)
        for sub in _blocked_subscriptions(db)
    )
    return RenewalTermsBackfillPreview(
        as_of=as_of, items=items, fingerprint=_fingerprint(items)
    )


def _sync_evidence_work_items(
    db: Session,
    unresolved: tuple[RenewalTermsEvidenceItem, ...],
    *,
    now: datetime,
) -> None:
    from app.models.network_monitoring import AlertSeverity
    from app.services.observability import Finding, record_finding, resolve_findings

    for item in unresolved:
        record_finding(
            db,
            Finding(
                fingerprint=f"{_FINDING_PREFIX}{item.subscription_id}",
                domain="prepaid_enforcement",
                source="prepaid_renewal_terms_backfill",
                severity=AlertSeverity.warning,
                title="Prepaid renewal terms need finance review",
                summary=(
                    "Active prepaid subscription with no frozen contracted "
                    "amount; paid-invoice evidence is missing or conflicting. "
                    "Record the price via a reviewed staff correction — never "
                    "inferred from the catalog."
                ),
                details={
                    "owner": "finance-billing",
                    "account_id": str(item.account_id),
                    "subscription_id": str(item.subscription_id),
                    "decision": item.decision.value,
                    "insufficiency_reasons": list(item.insufficiency_reasons),
                    "distinct_paid_amounts": [
                        str(a) for a in item.distinct_paid_amounts
                    ],
                    "sla_due_at": (
                        now + timedelta(hours=_EVIDENCE_SLA_HOURS)
                    ).isoformat(),
                },
            ),
        )
    resolve_findings(
        db,
        managed_prefix=_FINDING_PREFIX,
        active_fingerprints={
            f"{_FINDING_PREFIX}{item.subscription_id}" for item in unresolved
        },
    )


def capture_prepaid_renewal_terms_backfill(
    db: Session,
    command: CaptureRenewalTermsBackfillCommand,
    *,
    context: CommandContext,
) -> RenewalTermsBackfillResult:
    """Apply the fingerprint-bound backfill through the owner boundary."""
    return execute_owner_command(
        db,
        definition=_CAPTURE_COMMAND,
        context=context,
        operation=lambda: _capture(db, command=command, context=context),
    )


def _capture(
    db: Session,
    *,
    command: CaptureRenewalTermsBackfillCommand,
    context: CommandContext,
) -> RenewalTermsBackfillResult:
    if not context.idempotency_key:
        raise _error(
            "missing_idempotency_key",
            "Capturing a renewal-terms backfill requires a business idempotency key.",
        )
    preview = preview_prepaid_renewal_terms_backfill(db, now=command.as_of)
    if preview.fingerprint != command.preview_fingerprint:
        raise _error(
            "stale_preview",
            "Evidence changed since the reviewed preview; re-run the preview "
            "and review the new fingerprint.",
        )
    repaired = 0
    unresolved: list[RenewalTermsEvidenceItem] = []
    for item in preview.items:
        if item.decision is RenewalTermsDecision.repairable:
            subscription = db.execute(
                select(Subscription)
                .where(Subscription.id == item.subscription_id)
                .with_for_update()
            ).scalar_one_or_none()
            if subscription is None:
                continue
            if (
                subscription.unit_price is not None
                and subscription.unit_price > Decimal("0.00")
            ):
                continue
            subscription.unit_price = item.contracted_amount
            repaired += 1
            from app.services.events import EventType, emit_event

            emit_event(
                db,
                EventType.prepaid_renewal_terms_backfilled,
                {
                    "schema_version": 1,
                    "account_id": str(item.account_id),
                    "subscription_id": str(item.subscription_id),
                    "contracted_amount": str(item.contracted_amount),
                    "paid_line_count": item.paid_line_count,
                    "preview_fingerprint": preview.fingerprint,
                },
                subscriber_id=item.account_id,
                account_id=item.account_id,
                subscription_id=item.subscription_id,
            )
            logger.info(
                "prepaid_renewal_terms_backfilled: subscription=%s amount=%s "
                "paid_lines=%d",
                item.subscription_id,
                item.contracted_amount,
                item.paid_line_count,
            )
        else:
            unresolved.append(item)
    _sync_evidence_work_items(db, tuple(unresolved), now=preview.as_of)
    return RenewalTermsBackfillResult(
        repaired_count=repaired,
        work_item_count=len(unresolved),
        fingerprint=preview.fingerprint,
    )


_CORRECT_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="prepaid renewal-terms evidence backfill",
    name="correct_prepaid_renewal_terms",
)


class RenewalTermsCorrectionAction(StrEnum):
    apply_reviewed_term = "apply_reviewed_term"
    restore_fail_closed = "restore_fail_closed"


@dataclass(frozen=True, slots=True)
class CorrectRenewalTermsCommand:
    """Finance-reviewed supersession of a previously restored amount."""

    subscription_id: UUID
    action: RenewalTermsCorrectionAction
    review_reference: str
    reviewed_amount: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RenewalTermsCorrectionResult:
    subscription_id: UUID
    action: RenewalTermsCorrectionAction
    previous_amount: Decimal | None
    new_amount: Decimal | None
    replayed: bool


def correct_prepaid_renewal_terms(
    db: Session,
    command: CorrectRenewalTermsCommand,
    *,
    context: CommandContext,
) -> RenewalTermsCorrectionResult:
    """Apply a finance-reviewed term or restore the fail-closed state.

    The only sanctioned way to supersede a backfilled amount — never direct
    SQL. ``restore_fail_closed`` re-opens the finance work item so the
    account cannot be silently parked without an owner.
    """
    return execute_owner_command(
        db,
        definition=_CORRECT_COMMAND,
        context=context,
        operation=lambda: _correct(db, command=command),
    )


def _correct(
    db: Session, *, command: CorrectRenewalTermsCommand
) -> RenewalTermsCorrectionResult:
    from app.services.events import EventType, emit_event

    if not command.review_reference.strip():
        raise _error(
            "missing_review_reference",
            "A correction requires the finance review reference.",
        )
    subscription = db.execute(
        select(Subscription)
        .where(Subscription.id == command.subscription_id)
        .with_for_update()
    ).scalar_one_or_none()
    if subscription is None:
        raise _error("subscription_not_found", "Subscription was not found.")
    previous = (
        Decimal(str(subscription.unit_price))
        if subscription.unit_price is not None
        else None
    )
    if command.action is RenewalTermsCorrectionAction.apply_reviewed_term:
        if command.reviewed_amount is None or command.reviewed_amount <= Decimal(
            "0.00"
        ):
            raise _error(
                "invalid_reviewed_amount",
                "apply_reviewed_term requires a positive reviewed amount.",
            )
        new_amount: Decimal | None = command.reviewed_amount.quantize(Decimal("0.01"))
        if previous == new_amount:
            return RenewalTermsCorrectionResult(
                subscription_id=subscription.id,
                action=command.action,
                previous_amount=previous,
                new_amount=new_amount,
                replayed=True,
            )
        subscription.unit_price = new_amount
        from app.services.observability import resolve_findings

        resolve_findings(
            db,
            managed_prefix=f"{_FINDING_PREFIX}{subscription.id}",
            active_fingerprints=set(),
        )
    else:
        new_amount = None
        if previous is None:
            return RenewalTermsCorrectionResult(
                subscription_id=subscription.id,
                action=command.action,
                previous_amount=previous,
                new_amount=None,
                replayed=True,
            )
        subscription.unit_price = None
        _sync_correction_work_item(
            db, subscription, review_reference=command.review_reference
        )
    emit_event(
        db,
        EventType.prepaid_renewal_terms_corrected,
        {
            "schema_version": 1,
            "subscription_id": str(subscription.id),
            "account_id": str(subscription.subscriber_id),
            "action": command.action.value,
            "previous_amount": str(previous) if previous is not None else None,
            "new_amount": str(new_amount) if new_amount is not None else None,
            "review_reference": command.review_reference,
        },
        subscriber_id=subscription.subscriber_id,
        account_id=subscription.subscriber_id,
        subscription_id=subscription.id,
    )
    logger.info(
        "prepaid_renewal_terms_corrected: subscription=%s action=%s "
        "previous=%s new=%s reference=%s",
        subscription.id,
        command.action.value,
        previous,
        new_amount,
        command.review_reference,
    )
    return RenewalTermsCorrectionResult(
        subscription_id=subscription.id,
        action=command.action,
        previous_amount=previous,
        new_amount=new_amount,
        replayed=False,
    )


def _sync_correction_work_item(
    db: Session, subscription: Subscription, *, review_reference: str
) -> None:
    from app.models.network_monitoring import AlertSeverity
    from app.services.observability import Finding, record_finding

    record_finding(
        db,
        Finding(
            fingerprint=f"{_FINDING_PREFIX}{subscription.id}",
            domain="prepaid_enforcement",
            source="prepaid_renewal_terms_backfill",
            severity=AlertSeverity.warning,
            title="Prepaid renewal terms need finance review",
            summary=(
                "A previously restored contracted amount was reverted to the "
                "fail-closed state after finance review. Record the correct "
                "price via a reviewed correction."
            ),
            details={
                "owner": "finance-billing",
                "account_id": str(subscription.subscriber_id),
                "subscription_id": str(subscription.id),
                "decision": "correction_fail_closed",
                "review_reference": review_reference,
                "sla_due_at": (
                    datetime.now(UTC) + timedelta(hours=_EVIDENCE_SLA_HOURS)
                ).isoformat(),
            },
        ),
    )


@dataclass(frozen=True, slots=True)
class RenewalTermsAuditItem:
    """v2 re-audit of one previously restored subscription."""

    subscription_id: UUID
    account_id: UUID
    current_unit_price: Decimal | None
    v2_decision: RenewalTermsDecision
    v2_amount: Decimal | None
    amount_confirmed: bool
    insufficiency_reasons: tuple[str, ...]


def audit_restored_renewal_terms(
    db: Session, *, now: datetime | None = None
) -> tuple[RenewalTermsAuditItem, ...]:
    """Reclassify every backfilled subscription under the v2 proof contract.

    Restored subscriptions leave the blocked cohort (their unit_price is
    set), so the ordinary preview never re-examines them. This read-only
    audit replays the v2 evidence rules against the same paid lines; any
    item that is not confirmed needs an owner-led correction.
    """
    from app.models.event_store import EventStore
    from app.services.events import EventType
    from app.services.prepaid_currency import resolve_prepaid_enforcement_currency

    currency = resolve_prepaid_enforcement_currency(db)
    events = db.execute(
        select(EventStore).where(
            EventStore.event_type == EventType.prepaid_renewal_terms_backfilled.value
        )
    ).scalars()
    subscription_ids: dict[UUID, None] = {}
    for event in events:
        payload = event.payload or {}
        raw = payload.get("subscription_id")
        if raw:
            subscription_ids.setdefault(UUID(str(raw)), None)
    items: list[RenewalTermsAuditItem] = []
    for subscription_id in subscription_ids:
        subscription = db.get(Subscription, subscription_id)
        if subscription is None:
            continue
        verdict = _classify(db, subscription, enforcement_currency=currency)
        current = (
            Decimal(str(subscription.unit_price))
            if subscription.unit_price is not None
            else None
        )
        items.append(
            RenewalTermsAuditItem(
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
                current_unit_price=current,
                v2_decision=verdict.decision,
                v2_amount=verdict.contracted_amount,
                amount_confirmed=(
                    verdict.decision is RenewalTermsDecision.repairable
                    and verdict.contracted_amount == current
                ),
                insufficiency_reasons=verdict.insufficiency_reasons,
            )
        )
    return tuple(sorted(items, key=lambda i: str(i.subscription_id)))
