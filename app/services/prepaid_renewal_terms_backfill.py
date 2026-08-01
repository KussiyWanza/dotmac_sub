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
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
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

_POLICY_VERSION = "prepaid-renewal-terms-backfill-v1"
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
    no_evidence = "no_evidence"


@dataclass(frozen=True, slots=True)
class RenewalTermsEvidenceItem:
    """Exact-evidence verdict for one blocked prepaid subscription."""

    subscription_id: UUID
    account_id: UUID
    decision: RenewalTermsDecision
    contracted_amount: Decimal | None
    distinct_paid_amounts: tuple[Decimal, ...]
    paid_line_count: int


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
    rows = db.scalars(
        select(Subscription).where(
            Subscription.status == SubscriptionStatus.active,
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


def _paid_base_line_amounts(db: Session, subscription_id: UUID) -> list[Decimal]:
    lines = db.execute(
        select(InvoiceLine)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(
            InvoiceLine.subscription_id == subscription_id,
            InvoiceLine.is_active.is_(True),
            Invoice.status == InvoiceStatus.paid,
        )
    ).scalars()
    amounts: list[Decimal] = []
    for line in lines:
        if (line.metadata_ or {}).get("kind") != "base_subscription":
            continue
        value = Decimal(str(line.unit_price))
        if value > Decimal("0.00"):
            amounts.append(value)
    return amounts


def _classify(db: Session, subscription: Subscription) -> RenewalTermsEvidenceItem:
    amounts = _paid_base_line_amounts(db, subscription.id)
    distinct = tuple(sorted({a.quantize(Decimal("0.01")) for a in amounts}))
    if not distinct:
        decision = RenewalTermsDecision.no_evidence
        contracted: Decimal | None = None
    elif len(distinct) == 1:
        decision = RenewalTermsDecision.repairable
        contracted = distinct[0]
    else:
        decision = RenewalTermsDecision.ambiguous_amounts
        contracted = None
    return RenewalTermsEvidenceItem(
        subscription_id=subscription.id,
        account_id=subscription.subscriber_id,
        decision=decision,
        contracted_amount=contracted,
        distinct_paid_amounts=distinct,
        paid_line_count=len(amounts),
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
                "distinct": [str(a) for a in item.distinct_paid_amounts],
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
    as_of = now or datetime.now(UTC)
    items = tuple(_classify(db, sub) for sub in _blocked_subscriptions(db))
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
