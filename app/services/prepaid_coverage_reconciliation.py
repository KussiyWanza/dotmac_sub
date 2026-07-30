"""Exact-evidence repair owner for historical prepaid coverage gaps.

The preview classifies every collectible prepaid subscription from structural
evidence. Confirmation locks the affected accounts, subscriptions, and source
rows; recomputes the fingerprint; creates only missing exact entitlements; and
persists immutable run/item evidence. It never posts money, edits a balance,
or treats ``next_billing_at`` as proof of service.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import NoReturn
from uuid import UUID

from sqlalchemy import String, cast, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.models.billing import (
    AccountAdjustment,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, Subscription
from app.models.prepaid_coverage import (
    PrepaidCoverageReconciliationItem,
    PrepaidCoverageReconciliationRun,
)
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionEntry,
    ServiceExtensionStatus,
)
from app.models.subscriber import Subscriber
from app.services.billing.adjustments import AccountAdjustmentOrigin
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.billing_statuses import BILLABLE_SUBSCRIBER_STATUSES
from app.services.common import round_money, to_decimal
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.prepaid_service_coverage import (
    PrepaidCoverageSource,
    resolve_prepaid_service_coverage,
)
from app.services.service_entitlements import (
    ensure_prepaid_entitlement_for_paid_invoice_line,
    ensure_prepaid_entitlement_for_wallet_debit,
)

_OWNER = "financial.prepaid_service_coverage_reconciliation"
_CONCERN = "exact prepaid coverage evidence reconciliation"
_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_CONCERN,
    name="reconcile_prepaid_service_coverage",
)
_ADJUSTMENT_ORIGIN = AccountAdjustmentOrigin.prepaid_service_renewal
_ORIGIN_REF_PATTERN = re.compile(
    r"^(?P<subscription>[0-9a-fA-F-]{36}):"
    r"(?P<starts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})):"
    r"(?P<ends>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))$"
)


class CoverageReconciliationDecision(StrEnum):
    entitlement_created = "entitlement_created"
    already_covered = "already_covered"
    no_repair_required = "no_repair_required"
    quarantined = "quarantined"


class CoverageReconciliationSource(StrEnum):
    service_entitlement = "service_entitlement"
    service_extension = "service_extension"
    invoice_line = "invoice_line"
    account_adjustment = "account_adjustment"
    none = "none"


class CoverageReconciliationReason(StrEnum):
    funded_entitlement = "funded_entitlement"
    explicit_service_extension = "explicit_service_extension"
    exact_paid_invoice_line = "exact_paid_invoice_line"
    exact_renewal_adjustment = "exact_renewal_adjustment"
    due_without_coverage = "due_without_coverage"
    future_anchor_without_exact_evidence = "future_anchor_without_exact_evidence"
    duplicate_current_entitlements = "duplicate_current_entitlements"
    duplicate_current_extensions = "duplicate_current_extensions"
    overlapping_coverage_sources = "overlapping_coverage_sources"
    conflicting_financial_sources = "conflicting_financial_sources"
    ambiguous_paid_invoice_lines = "ambiguous_paid_invoice_lines"
    ambiguous_renewal_adjustments = "ambiguous_renewal_adjustments"
    malformed_paid_invoice_period = "malformed_paid_invoice_period"
    malformed_renewal_origin = "malformed_renewal_origin"
    source_entitlement_conflict = "source_entitlement_conflict"
    inactive_account_with_collectible_subscription = (
        "inactive_account_with_collectible_subscription"
    )


class PrepaidCoverageReconciliationError(DomainError):
    """Stable fail-closed reconciliation error."""


@dataclass(frozen=True, slots=True)
class PrepaidCoverageReconciliationPreviewItem:
    subscription_id: UUID
    account_id: UUID
    decision: CoverageReconciliationDecision
    reason: CoverageReconciliationReason
    source: CoverageReconciliationSource
    source_id: UUID | None
    starts_at: datetime | None
    ends_at: datetime | None
    amount: Decimal | None
    currency: str | None
    evidence_fingerprint: str

    @property
    def blocks_enforcement(self) -> bool:
        return self.decision in {
            CoverageReconciliationDecision.entitlement_created,
            CoverageReconciliationDecision.quarantined,
        }


@dataclass(frozen=True, slots=True)
class PrepaidCoverageReconciliationPreview:
    as_of: datetime
    subscription_ids: tuple[UUID, ...]
    items: tuple[PrepaidCoverageReconciliationPreviewItem, ...]
    fingerprint: str

    @property
    def repairable_count(self) -> int:
        return sum(
            item.decision == CoverageReconciliationDecision.entitlement_created
            for item in self.items
        )

    @property
    def quarantined_count(self) -> int:
        return sum(
            item.decision == CoverageReconciliationDecision.quarantined
            for item in self.items
        )

    @property
    def blocker_count(self) -> int:
        return sum(item.blocks_enforcement for item in self.items)


@dataclass(frozen=True, slots=True)
class PrepaidCoverageEnforcementBlocker:
    """Exact financial evidence that makes suspension unsafe."""

    subscription_id: UUID
    reason: CoverageReconciliationReason


@dataclass(frozen=True, slots=True)
class ReconcilePrepaidCoverageCommand:
    context: CommandContext
    as_of: datetime
    preview_fingerprint: str
    subscription_ids: tuple[UUID, ...] | None = None


@dataclass(frozen=True, slots=True)
class PrepaidCoverageReconciliationResult:
    run_id: UUID
    preview_fingerprint: str
    entitlement_created_count: int
    already_covered_count: int
    no_repair_required_count: int
    quarantined_count: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class _InvoiceEvidence:
    invoice: Invoice
    line: InvoiceLine
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class _AdjustmentEvidence:
    adjustment: AccountAdjustment
    ledger_entry: LedgerEntry
    subscription_id: UUID
    starts_at: datetime
    ends_at: datetime


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise PrepaidCoverageReconciliationError(
        code=f"{_OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: (
            str(value)
            if isinstance(value, UUID)
            else (
                value.isoformat()
                if isinstance(value, datetime)
                else f"{value:.2f}"
                if isinstance(value, Decimal)
                else value.value
                if isinstance(value, StrEnum)
                else value
            )
        ),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _item(
    *,
    subscription: Subscription,
    decision: CoverageReconciliationDecision,
    reason: CoverageReconciliationReason,
    source: CoverageReconciliationSource = CoverageReconciliationSource.none,
    source_id: UUID | None = None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    amount: Decimal | None = None,
    currency: str | None = None,
) -> PrepaidCoverageReconciliationPreviewItem:
    payload = {
        "subscription_id": subscription.id,
        "account_id": subscription.subscriber_id,
        "decision": decision,
        "reason": reason,
        "source": source,
        "source_id": source_id,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "amount": amount,
        "currency": currency,
    }
    return PrepaidCoverageReconciliationPreviewItem(
        subscription_id=subscription.id,
        account_id=subscription.subscriber_id,
        decision=decision,
        reason=reason,
        source=source,
        source_id=source_id,
        starts_at=starts_at,
        ends_at=ends_at,
        amount=amount,
        currency=currency,
        evidence_fingerprint=_hash(payload),
    )


def _parse_adjustment_origin(
    value: str | None,
) -> tuple[UUID, datetime, datetime] | None:
    match = _ORIGIN_REF_PATTERN.fullmatch(value or "")
    if match is None:
        return None
    try:
        subscription_id = UUID(match.group("subscription"))
        starts_at = _utc(
            datetime.fromisoformat(match.group("starts").replace("Z", "+00:00"))
        )
        ends_at = _utc(
            datetime.fromisoformat(match.group("ends").replace("Z", "+00:00"))
        )
    except ValueError:
        return None
    if ends_at <= starts_at:
        return None
    return subscription_id, starts_at, ends_at


def _subscriptions(
    db: Session,
    subscription_ids: tuple[UUID, ...] | None,
) -> list[Subscription]:
    statement = select(Subscription).where(
        Subscription.billing_mode == BillingMode.prepaid,
        Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES),
    )
    if subscription_ids is not None:
        normalized_ids = tuple(sorted(set(subscription_ids), key=str))
        if not normalized_ids:
            return []
        statement = statement.where(Subscription.id.in_(normalized_ids))
    rows = list(db.scalars(statement.order_by(Subscription.id)).all())
    if subscription_ids is not None:
        found = {row.id for row in rows}
        missing = sorted(set(subscription_ids) - found, key=str)
        if missing:
            _error(
                "subscription_not_found",
                "A selected collectible prepaid subscription was not found.",
                subscription_ids=[str(value) for value in missing],
            )
    return rows


def _current_entitlements(
    db: Session,
    subscription_ids: tuple[UUID, ...],
    as_of: datetime,
) -> dict[UUID, list[ServiceEntitlement]]:
    grouped: dict[UUID, list[ServiceEntitlement]] = defaultdict(list)
    if not subscription_ids:
        return grouped
    rows = db.scalars(
        select(ServiceEntitlement)
        .where(
            ServiceEntitlement.subscription_id.in_(subscription_ids),
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
            ServiceEntitlement.starts_at <= as_of,
            ServiceEntitlement.ends_at > as_of,
        )
        .order_by(ServiceEntitlement.subscription_id, ServiceEntitlement.id)
    ).all()
    for row in rows:
        grouped[row.subscription_id].append(row)
    return grouped


def _current_extensions(
    db: Session,
    subscription_ids: tuple[UUID, ...],
    as_of: datetime,
) -> dict[UUID, list[ServiceExtensionEntry]]:
    grouped: dict[UUID, list[ServiceExtensionEntry]] = defaultdict(list)
    if not subscription_ids:
        return grouped
    rows = db.scalars(
        select(ServiceExtensionEntry)
        .join(
            ServiceExtension,
            ServiceExtension.id == ServiceExtensionEntry.extension_id,
        )
        .where(
            ServiceExtensionEntry.subscription_id.in_(subscription_ids),
            ServiceExtension.status == ServiceExtensionStatus.applied,
            ServiceExtensionEntry.grant_starts_at.isnot(None),
            ServiceExtensionEntry.grant_starts_at <= as_of,
            ServiceExtensionEntry.grant_ends_at.isnot(None),
            ServiceExtensionEntry.grant_ends_at > as_of,
        )
        .order_by(ServiceExtensionEntry.subscription_id, ServiceExtensionEntry.id)
    ).all()
    for row in rows:
        grouped[row.subscription_id].append(row)
    return grouped


def _paid_invoice_evidence(
    db: Session,
    subscription_ids: tuple[UUID, ...],
    as_of: datetime,
) -> tuple[dict[UUID, list[_InvoiceEvidence]], set[UUID]]:
    grouped: dict[UUID, list[_InvoiceEvidence]] = defaultdict(list)
    malformed: set[UUID] = set()
    if not subscription_ids:
        return grouped, malformed
    rows = db.execute(
        select(InvoiceLine, Invoice)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(
            InvoiceLine.subscription_id.in_(subscription_ids),
            InvoiceLine.is_active.is_(True),
            InvoiceLine.amount > Decimal("0.00"),
            Invoice.is_active.is_(True),
            Invoice.status == InvoiceStatus.paid,
            Invoice.balance_due <= Decimal("0.00"),
        )
        .order_by(InvoiceLine.subscription_id, Invoice.created_at, InvoiceLine.id)
    ).all()
    for line, invoice in rows:
        subscription_id = line.subscription_id
        if subscription_id is None:
            continue
        starts_at = invoice.billing_period_start
        ends_at = invoice.billing_period_end
        if starts_at is None or ends_at is None or _utc(ends_at) <= _utc(starts_at):
            malformed.add(subscription_id)
            continue
        normalized_start = _utc(starts_at)
        normalized_end = _utc(ends_at)
        if normalized_start <= as_of < normalized_end:
            grouped[subscription_id].append(
                _InvoiceEvidence(
                    invoice=invoice,
                    line=line,
                    starts_at=normalized_start,
                    ends_at=normalized_end,
                )
            )
    for subscription_id, candidates in tuple(grouped.items()):
        base_candidates = [
            value
            for value in candidates
            if (value.line.metadata_ or {}).get("kind") == "base_subscription"
        ]
        if base_candidates:
            grouped[subscription_id] = base_candidates
    return grouped, malformed


def _adjustment_evidence(
    db: Session,
    subscriptions: list[Subscription],
    as_of: datetime,
) -> tuple[dict[UUID, list[_AdjustmentEvidence]], set[UUID]]:
    grouped: dict[UUID, list[_AdjustmentEvidence]] = defaultdict(list)
    malformed_accounts: set[UUID] = set()
    account_ids = {subscription.subscriber_id for subscription in subscriptions}
    subscription_ids = {subscription.id for subscription in subscriptions}
    if not account_ids:
        return grouped, malformed_accounts
    rows = db.execute(
        select(AccountAdjustment, LedgerEntry)
        .join(LedgerEntry, LedgerEntry.id == AccountAdjustment.ledger_entry_id)
        .where(
            AccountAdjustment.account_id.in_(account_ids),
            AccountAdjustment.origin == _ADJUSTMENT_ORIGIN,
            AccountAdjustment.reversed_at.is_(None),
            AccountAdjustment.category == LedgerCategory.internet_service,
            LedgerEntry.is_active.is_(True),
            LedgerEntry.entry_type == LedgerEntryType.debit,
        )
        .order_by(AccountAdjustment.account_id, AccountAdjustment.created_at)
    ).all()
    for adjustment, ledger_entry in rows:
        parsed = _parse_adjustment_origin(adjustment.origin_ref)
        if parsed is None:
            malformed_accounts.add(adjustment.account_id)
            continue
        subscription_id, starts_at, ends_at = parsed
        if subscription_id not in subscription_ids:
            continue
        if (
            adjustment.account_id != ledger_entry.account_id
            or round_money(adjustment.amount) != round_money(ledger_entry.amount)
            or adjustment.currency != ledger_entry.currency
        ):
            malformed_accounts.add(adjustment.account_id)
            continue
        if starts_at <= as_of < ends_at:
            grouped[subscription_id].append(
                _AdjustmentEvidence(
                    adjustment=adjustment,
                    ledger_entry=ledger_entry,
                    subscription_id=subscription_id,
                    starts_at=starts_at,
                    ends_at=ends_at,
                )
            )
    return grouped, malformed_accounts


def resolve_prepaid_coverage_enforcement_blockers(
    db: Session,
    subscriptions: Sequence[Subscription],
    *,
    as_of: datetime,
) -> tuple[PrepaidCoverageEnforcementBlocker, ...]:
    """Return uncovered services whose financial evidence requires reconciliation.

    This is the enforcement-safe, batched query surface of the reconciliation
    owner. The caller remains responsible for canonical coverage resolution;
    this query only identifies exact or malformed financial source evidence
    that makes an uncovered classification unsafe for adverse action.
    """
    candidates = list(subscriptions)
    subscription_ids = tuple(subscription.id for subscription in candidates)
    if not subscription_ids:
        return ()

    observed_at = _utc(as_of)
    account_ids = {subscription.subscriber_id for subscription in candidates}
    invoice_sources = (
        select(
            literal("invoice").label("source_kind"),
            cast(InvoiceLine.subscription_id, String).label("subscription_id"),
            cast(Invoice.account_id, String).label("account_id"),
            literal(None).label("origin_ref"),
            Invoice.billing_period_start.label("starts_at"),
            Invoice.billing_period_end.label("ends_at"),
            InvoiceLine.amount.label("source_amount"),
            Invoice.currency.label("source_currency"),
            literal(None).label("ledger_account_id"),
            literal(None).label("ledger_amount"),
            literal(None).label("ledger_currency"),
        )
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(
            InvoiceLine.subscription_id.in_(subscription_ids),
            InvoiceLine.is_active.is_(True),
            InvoiceLine.amount > Decimal("0.00"),
            Invoice.is_active.is_(True),
            Invoice.status == InvoiceStatus.paid,
            Invoice.balance_due <= Decimal("0.00"),
        )
    )
    adjustment_sources = (
        select(
            literal("adjustment").label("source_kind"),
            literal(None).label("subscription_id"),
            cast(AccountAdjustment.account_id, String).label("account_id"),
            AccountAdjustment.origin_ref.label("origin_ref"),
            literal(None).label("starts_at"),
            literal(None).label("ends_at"),
            AccountAdjustment.amount.label("source_amount"),
            AccountAdjustment.currency.label("source_currency"),
            cast(LedgerEntry.account_id, String).label("ledger_account_id"),
            LedgerEntry.amount.label("ledger_amount"),
            LedgerEntry.currency.label("ledger_currency"),
        )
        .join(
            LedgerEntry,
            LedgerEntry.id == AccountAdjustment.ledger_entry_id,
        )
        .where(
            AccountAdjustment.account_id.in_(account_ids),
            AccountAdjustment.origin == _ADJUSTMENT_ORIGIN,
            AccountAdjustment.reversed_at.is_(None),
            AccountAdjustment.category == LedgerCategory.internet_service,
            LedgerEntry.is_active.is_(True),
            LedgerEntry.entry_type == LedgerEntryType.debit,
        )
    )
    rows = db.execute(union_all(invoice_sources, adjustment_sources)).all()

    candidate_ids = set(subscription_ids)
    invoice_counts: dict[UUID, int] = defaultdict(int)
    adjustment_counts: dict[UUID, int] = defaultdict(int)
    malformed_invoice_subscriptions: set[UUID] = set()
    malformed_adjustment_accounts: set[UUID] = set()
    for row in rows:
        if row.source_kind == "invoice":
            subscription_id = UUID(row.subscription_id)
            starts_at = row.starts_at
            ends_at = row.ends_at
            if starts_at is None or ends_at is None or _utc(ends_at) <= _utc(starts_at):
                malformed_invoice_subscriptions.add(subscription_id)
            elif _utc(starts_at) <= observed_at < _utc(ends_at):
                invoice_counts[subscription_id] += 1
            continue

        account_id = UUID(row.account_id)
        parsed = _parse_adjustment_origin(row.origin_ref)
        if parsed is None:
            malformed_adjustment_accounts.add(account_id)
            continue
        subscription_id, starts_at, ends_at = parsed
        if subscription_id not in candidate_ids:
            continue
        if (
            row.account_id != row.ledger_account_id
            or round_money(to_decimal(row.source_amount))
            != round_money(to_decimal(row.ledger_amount))
            or row.source_currency != row.ledger_currency
        ):
            malformed_adjustment_accounts.add(account_id)
            continue
        if starts_at <= observed_at < ends_at:
            adjustment_counts[subscription_id] += 1

    blockers: list[PrepaidCoverageEnforcementBlocker] = []
    for subscription in candidates:
        invoice_count = invoice_counts[subscription.id]
        adjustment_count = adjustment_counts[subscription.id]
        if subscription.id in malformed_invoice_subscriptions:
            reason = CoverageReconciliationReason.malformed_paid_invoice_period
        elif subscription.subscriber_id in malformed_adjustment_accounts:
            reason = CoverageReconciliationReason.malformed_renewal_origin
        elif invoice_count and adjustment_count:
            reason = CoverageReconciliationReason.conflicting_financial_sources
        elif invoice_count > 1:
            reason = CoverageReconciliationReason.ambiguous_paid_invoice_lines
        elif adjustment_count > 1:
            reason = CoverageReconciliationReason.ambiguous_renewal_adjustments
        elif invoice_count:
            reason = CoverageReconciliationReason.exact_paid_invoice_line
        elif adjustment_count:
            reason = CoverageReconciliationReason.exact_renewal_adjustment
        else:
            continue
        blockers.append(
            PrepaidCoverageEnforcementBlocker(
                subscription_id=subscription.id,
                reason=reason,
            )
        )

    return tuple(sorted(blockers, key=lambda item: str(item.subscription_id)))


def preview_prepaid_coverage_reconciliation(
    db: Session,
    *,
    as_of: datetime | None = None,
    subscription_ids: tuple[UUID, ...] | None = None,
) -> PrepaidCoverageReconciliationPreview:
    """Classify a selected or complete cohort without changing state."""
    observed_at = _utc(as_of or datetime.now(UTC))
    subscriptions = _subscriptions(db, subscription_ids)
    ids = tuple(subscription.id for subscription in subscriptions)
    account_ids = {subscription.subscriber_id for subscription in subscriptions}
    accounts = {
        account.id: account
        for account in db.scalars(
            select(Subscriber).where(Subscriber.id.in_(account_ids))
        ).all()
    }
    coverage = resolve_prepaid_service_coverage(
        db,
        subscriptions,
        as_of=observed_at,
    )
    current_entitlements = _current_entitlements(db, ids, observed_at)
    current_extensions = _current_extensions(db, ids, observed_at)
    invoice_evidence, malformed_invoice_subscriptions = _paid_invoice_evidence(
        db, ids, observed_at
    )
    adjustment_evidence, malformed_adjustment_accounts = _adjustment_evidence(
        db, subscriptions, observed_at
    )
    invoice_source_ids = {
        candidate.line.id
        for values in invoice_evidence.values()
        for candidate in values
    }
    ledger_source_ids = {
        candidate.ledger_entry.id
        for values in adjustment_evidence.values()
        for candidate in values
    }
    source_entitlements: dict[UUID, list[ServiceEntitlement]] = defaultdict(list)
    if invoice_source_ids or ledger_source_ids:
        source_filters = []
        if invoice_source_ids:
            source_filters.append(
                ServiceEntitlement.source_invoice_line_id.in_(invoice_source_ids)
            )
        if ledger_source_ids:
            source_filters.append(
                ServiceEntitlement.source_ledger_entry_id.in_(ledger_source_ids)
            )
        rows = db.scalars(select(ServiceEntitlement).where(or_(*source_filters))).all()
        for entitlement in rows:
            source_id = (
                entitlement.source_invoice_line_id or entitlement.source_ledger_entry_id
            )
            if source_id is not None:
                source_entitlements[source_id].append(entitlement)

    items: list[PrepaidCoverageReconciliationPreviewItem] = []
    for subscription in subscriptions:
        account = accounts.get(subscription.subscriber_id)
        if (
            account is None
            or not account.is_active
            or account.status not in BILLABLE_SUBSCRIBER_STATUSES
        ):
            items.append(
                _item(
                    subscription=subscription,
                    decision=CoverageReconciliationDecision.quarantined,
                    reason=(
                        CoverageReconciliationReason.inactive_account_with_collectible_subscription
                    ),
                )
            )
            continue
        entitlements = current_entitlements.get(subscription.id, [])
        extensions = current_extensions.get(subscription.id, [])
        decision = coverage[subscription.id]
        if len(entitlements) > 1:
            items.append(
                _item(
                    subscription=subscription,
                    decision=CoverageReconciliationDecision.quarantined,
                    reason=CoverageReconciliationReason.duplicate_current_entitlements,
                )
            )
            continue
        if len(extensions) > 1:
            items.append(
                _item(
                    subscription=subscription,
                    decision=CoverageReconciliationDecision.quarantined,
                    reason=CoverageReconciliationReason.duplicate_current_extensions,
                )
            )
            continue
        if entitlements and extensions:
            items.append(
                _item(
                    subscription=subscription,
                    decision=CoverageReconciliationDecision.quarantined,
                    reason=CoverageReconciliationReason.overlapping_coverage_sources,
                )
            )
            continue
        if decision.covered and decision.evidence is not None:
            source = (
                CoverageReconciliationSource.service_entitlement
                if decision.evidence.source == PrepaidCoverageSource.funded_entitlement
                else CoverageReconciliationSource.service_extension
            )
            reason = (
                CoverageReconciliationReason.funded_entitlement
                if source == CoverageReconciliationSource.service_entitlement
                else CoverageReconciliationReason.explicit_service_extension
            )
            items.append(
                _item(
                    subscription=subscription,
                    decision=CoverageReconciliationDecision.already_covered,
                    reason=reason,
                    source=source,
                    source_id=decision.evidence.source_id,
                    starts_at=_utc(decision.evidence.starts_at),
                    ends_at=_utc(decision.evidence.ends_at),
                )
            )
            continue

        invoices = invoice_evidence.get(subscription.id, [])
        adjustments = adjustment_evidence.get(subscription.id, [])
        if len(invoices) > 1:
            items.append(
                _item(
                    subscription=subscription,
                    decision=CoverageReconciliationDecision.quarantined,
                    reason=CoverageReconciliationReason.ambiguous_paid_invoice_lines,
                )
            )
            continue
        if len(adjustments) > 1:
            items.append(
                _item(
                    subscription=subscription,
                    decision=CoverageReconciliationDecision.quarantined,
                    reason=CoverageReconciliationReason.ambiguous_renewal_adjustments,
                )
            )
            continue
        if invoices and adjustments:
            items.append(
                _item(
                    subscription=subscription,
                    decision=CoverageReconciliationDecision.quarantined,
                    reason=CoverageReconciliationReason.conflicting_financial_sources,
                )
            )
            continue
        if invoices:
            invoice_candidate = invoices[0]
            if source_entitlements.get(invoice_candidate.line.id):
                items.append(
                    _item(
                        subscription=subscription,
                        decision=CoverageReconciliationDecision.quarantined,
                        reason=CoverageReconciliationReason.source_entitlement_conflict,
                        source=CoverageReconciliationSource.invoice_line,
                        source_id=invoice_candidate.line.id,
                    )
                )
                continue
            items.append(
                _item(
                    subscription=subscription,
                    decision=CoverageReconciliationDecision.entitlement_created,
                    reason=CoverageReconciliationReason.exact_paid_invoice_line,
                    source=CoverageReconciliationSource.invoice_line,
                    source_id=invoice_candidate.line.id,
                    starts_at=invoice_candidate.starts_at,
                    ends_at=invoice_candidate.ends_at,
                    amount=round_money(to_decimal(invoice_candidate.line.amount)),
                    currency=invoice_candidate.invoice.currency or "NGN",
                )
            )
            continue
        if adjustments:
            adjustment_candidate = adjustments[0]
            if source_entitlements.get(adjustment_candidate.ledger_entry.id):
                items.append(
                    _item(
                        subscription=subscription,
                        decision=CoverageReconciliationDecision.quarantined,
                        reason=CoverageReconciliationReason.source_entitlement_conflict,
                        source=CoverageReconciliationSource.account_adjustment,
                        source_id=adjustment_candidate.adjustment.id,
                    )
                )
                continue
            items.append(
                _item(
                    subscription=subscription,
                    decision=CoverageReconciliationDecision.entitlement_created,
                    reason=CoverageReconciliationReason.exact_renewal_adjustment,
                    source=CoverageReconciliationSource.account_adjustment,
                    source_id=adjustment_candidate.adjustment.id,
                    starts_at=adjustment_candidate.starts_at,
                    ends_at=adjustment_candidate.ends_at,
                    amount=round_money(
                        to_decimal(adjustment_candidate.adjustment.amount)
                    ),
                    currency=adjustment_candidate.adjustment.currency,
                )
            )
            continue
        if subscription.id in malformed_invoice_subscriptions:
            reason = CoverageReconciliationReason.malformed_paid_invoice_period
        elif subscription.subscriber_id in malformed_adjustment_accounts:
            reason = CoverageReconciliationReason.malformed_renewal_origin
        elif (
            subscription.next_billing_at is not None
            and _utc(subscription.next_billing_at) > observed_at
        ):
            reason = CoverageReconciliationReason.future_anchor_without_exact_evidence
        else:
            items.append(
                _item(
                    subscription=subscription,
                    decision=CoverageReconciliationDecision.no_repair_required,
                    reason=CoverageReconciliationReason.due_without_coverage,
                )
            )
            continue
        items.append(
            _item(
                subscription=subscription,
                decision=CoverageReconciliationDecision.quarantined,
                reason=reason,
            )
        )

    ordered = tuple(sorted(items, key=lambda value: str(value.subscription_id)))
    fingerprint = _hash(
        {
            "owner": _OWNER,
            "as_of": observed_at,
            "subscription_ids": ids,
            "items": [item.evidence_fingerprint for item in ordered],
        }
    )
    return PrepaidCoverageReconciliationPreview(
        as_of=observed_at,
        subscription_ids=ids,
        items=ordered,
        fingerprint=fingerprint,
    )


def _result(
    run: PrepaidCoverageReconciliationRun, *, replayed: bool
) -> PrepaidCoverageReconciliationResult:
    return PrepaidCoverageReconciliationResult(
        run_id=run.id,
        preview_fingerprint=run.preview_fingerprint,
        entitlement_created_count=run.entitlement_created_count,
        already_covered_count=run.already_covered_count,
        no_repair_required_count=run.no_repair_required_count,
        quarantined_count=run.quarantined_count,
        replayed=replayed,
    )


def _idempotent_result(
    db: Session,
    *,
    key: str,
    preview_fingerprint: str,
) -> PrepaidCoverageReconciliationResult | None:
    existing = db.scalar(
        select(PrepaidCoverageReconciliationRun).where(
            PrepaidCoverageReconciliationRun.idempotency_key == key
        )
    )
    if existing is None:
        return None
    if existing.preview_fingerprint != preview_fingerprint:
        _error(
            "idempotency_conflict",
            "The idempotency key belongs to different reconciliation evidence.",
        )
    return _result(existing, replayed=True)


def _reconcile(
    db: Session,
    command: ReconcilePrepaidCoverageCommand,
) -> PrepaidCoverageReconciliationResult:
    key = (command.context.idempotency_key or "").strip()
    if not key:
        _error("missing_idempotency_key", "An idempotency key is required.")
    if len(command.context.reason.strip()) < 16:
        _error(
            "invalid_reason",
            "The reconciliation reason must explain the reviewed evidence.",
        )
    replay = _idempotent_result(
        db,
        key=key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if replay is not None:
        return replay

    selected = _subscriptions(db, command.subscription_ids)
    account_ids = sorted({row.subscriber_id for row in selected}, key=str)
    if account_ids:
        list(
            db.scalars(
                select(Subscriber)
                .where(Subscriber.id.in_(account_ids))
                .order_by(Subscriber.id)
                .with_for_update()
            ).all()
        )
        # The first idempotency lookup intentionally precedes the lock for the
        # cheap replay path. Recheck after the account lock so two concurrent
        # confirmations with the same key converge on the committed run rather
        # than letting the waiter re-preview the newly-created entitlement as
        # stale evidence.
        replay = _idempotent_result(
            db,
            key=key,
            preview_fingerprint=command.preview_fingerprint,
        )
        if replay is not None:
            return replay
    if selected:
        list(
            db.scalars(
                select(Subscription)
                .where(Subscription.id.in_([row.id for row in selected]))
                .order_by(Subscription.id)
                .with_for_update()
            ).all()
        )
    preview = preview_prepaid_coverage_reconciliation(
        db,
        as_of=command.as_of,
        subscription_ids=command.subscription_ids,
    )
    source_items = [
        item
        for item in preview.items
        if item.decision == CoverageReconciliationDecision.entitlement_created
        and item.source_id is not None
    ]
    invoice_line_ids = sorted(
        {
            item.source_id
            for item in source_items
            if item.source == CoverageReconciliationSource.invoice_line
            and item.source_id is not None
        },
        key=str,
    )
    adjustment_ids = sorted(
        {
            item.source_id
            for item in source_items
            if item.source == CoverageReconciliationSource.account_adjustment
            and item.source_id is not None
        },
        key=str,
    )
    if invoice_line_ids:
        lines = list(
            db.scalars(
                select(InvoiceLine)
                .where(InvoiceLine.id.in_(invoice_line_ids))
                .order_by(InvoiceLine.id)
                .with_for_update()
            ).all()
        )
        invoice_ids = sorted({line.invoice_id for line in lines}, key=str)
        list(
            db.scalars(
                select(Invoice)
                .where(Invoice.id.in_(invoice_ids))
                .order_by(Invoice.id)
                .with_for_update()
            ).all()
        )
    if adjustment_ids:
        adjustments = list(
            db.scalars(
                select(AccountAdjustment)
                .where(AccountAdjustment.id.in_(adjustment_ids))
                .order_by(AccountAdjustment.id)
                .with_for_update()
            ).all()
        )
        ledger_ids = sorted({row.ledger_entry_id for row in adjustments}, key=str)
        list(
            db.scalars(
                select(LedgerEntry)
                .where(LedgerEntry.id.in_(ledger_ids))
                .order_by(LedgerEntry.id)
                .with_for_update()
            ).all()
        )
    current = preview_prepaid_coverage_reconciliation(
        db,
        as_of=command.as_of,
        subscription_ids=command.subscription_ids,
    )
    if current.fingerprint != command.preview_fingerprint:
        _error(
            "stale_preview",
            "Coverage evidence changed after preview; preview again.",
            expected_fingerprint=command.preview_fingerprint,
            current_fingerprint=current.fingerprint,
        )

    run = PrepaidCoverageReconciliationRun(
        idempotency_key=key,
        preview_fingerprint=current.fingerprint,
        as_of=current.as_of,
        requested_subscription_count=len(current.items),
        entitlement_created_count=current.repairable_count,
        already_covered_count=sum(
            item.decision == CoverageReconciliationDecision.already_covered
            for item in current.items
        ),
        no_repair_required_count=sum(
            item.decision == CoverageReconciliationDecision.no_repair_required
            for item in current.items
        ),
        quarantined_count=current.quarantined_count,
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
        actor=command.context.actor,
        reason=command.context.reason.strip(),
    )
    db.add(run)
    db.flush()

    for item in current.items:
        entitlement: ServiceEntitlement | None = None
        if item.decision == CoverageReconciliationDecision.entitlement_created:
            assert item.source_id is not None
            assert item.starts_at is not None
            assert item.ends_at is not None
            if item.source == CoverageReconciliationSource.invoice_line:
                line = db.get(InvoiceLine, item.source_id)
                invoice = db.get(Invoice, line.invoice_id) if line is not None else None
                if line is None or invoice is None:
                    _error("source_changed", "Paid invoice evidence disappeared.")
                entitlement = ensure_prepaid_entitlement_for_paid_invoice_line(
                    db,
                    invoice=invoice,
                    line=line,
                    reconciliation_fingerprint=item.evidence_fingerprint,
                )
            elif item.source == CoverageReconciliationSource.account_adjustment:
                adjustment = db.get(AccountAdjustment, item.source_id)
                subscription = db.get(Subscription, item.subscription_id)
                if adjustment is None or subscription is None:
                    _error("source_changed", "Renewal evidence disappeared.")
                entitlement = ensure_prepaid_entitlement_for_wallet_debit(
                    db,
                    subscription=subscription,
                    ledger_entry=adjustment.ledger_entry,
                    starts_at=item.starts_at,
                    ends_at=item.ends_at,
                )
                if entitlement is not None:
                    metadata = dict(entitlement.metadata_ or {})
                    metadata.update(
                        {
                            "reconciled_by": _OWNER,
                            "reconciliation_fingerprint": item.evidence_fingerprint,
                        }
                    )
                    entitlement.metadata_ = metadata
            if entitlement is None:
                _error(
                    "incomplete_repair",
                    "Exact evidence did not produce a service entitlement.",
                    subscription_id=str(item.subscription_id),
                )
            if (
                entitlement.subscription_id != item.subscription_id
                or entitlement.account_id != item.account_id
                or _utc(entitlement.starts_at) != item.starts_at
                or _utc(entitlement.ends_at) != item.ends_at
            ):
                _error(
                    "incomplete_repair",
                    "Created entitlement differs from reviewed evidence.",
                    subscription_id=str(item.subscription_id),
                )
        db.add(
            PrepaidCoverageReconciliationItem(
                run_id=run.id,
                subscription_id=item.subscription_id,
                account_id=item.account_id,
                decision=item.decision.value,
                reason_code=item.reason.value,
                source_type=item.source.value,
                source_entitlement_id=(
                    item.source_id
                    if item.source == CoverageReconciliationSource.service_entitlement
                    else None
                ),
                source_service_extension_entry_id=(
                    item.source_id
                    if item.source == CoverageReconciliationSource.service_extension
                    else None
                ),
                source_invoice_line_id=(
                    item.source_id
                    if item.source == CoverageReconciliationSource.invoice_line
                    else None
                ),
                source_account_adjustment_id=(
                    item.source_id
                    if item.source == CoverageReconciliationSource.account_adjustment
                    else None
                ),
                starts_at=item.starts_at,
                ends_at=item.ends_at,
                amount=item.amount,
                currency=item.currency,
                evidence_fingerprint=item.evidence_fingerprint,
                entitlement_id=entitlement.id if entitlement is not None else None,
            )
        )
    db.flush()
    emit_event(
        db,
        EventType.prepaid_coverage_reconciled,
        {
            "schema_version": 1,
            "run_id": str(run.id),
            "preview_fingerprint": run.preview_fingerprint,
            "requested_subscription_count": run.requested_subscription_count,
            "entitlement_created_count": run.entitlement_created_count,
            "already_covered_count": run.already_covered_count,
            "no_repair_required_count": run.no_repair_required_count,
            "quarantined_count": run.quarantined_count,
        },
        actor=command.context.actor,
    )
    return _result(run, replayed=False)


def reconcile_prepaid_service_coverage(
    db: Session,
    command: ReconcilePrepaidCoverageCommand,
) -> PrepaidCoverageReconciliationResult:
    """Execute one fingerprint-bound reconciliation transaction."""
    return execute_owner_command(
        db,
        definition=_COMMAND,
        context=command.context,
        operation=lambda: _reconcile(db, command),
    )


__all__ = [
    "CoverageReconciliationDecision",
    "CoverageReconciliationReason",
    "CoverageReconciliationSource",
    "PrepaidCoverageReconciliationError",
    "PrepaidCoverageReconciliationPreview",
    "PrepaidCoverageReconciliationPreviewItem",
    "PrepaidCoverageReconciliationResult",
    "ReconcilePrepaidCoverageCommand",
    "preview_prepaid_coverage_reconciliation",
    "reconcile_prepaid_service_coverage",
]
