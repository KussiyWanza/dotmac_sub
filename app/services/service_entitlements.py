from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    AccountAdjustment,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerSource,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, Subscription
from app.services.common import round_money, to_decimal

logger = logging.getLogger(__name__)


def ensure_prepaid_entitlements_for_paid_invoice(
    db: Session, invoice: Invoice
) -> list[ServiceEntitlement]:
    """Create prepaid service entitlements for a paid prepaid invoice.

    This is intentionally idempotent on ``source_invoice_line_id`` so webhook,
    billing-run, and later draft-settlement retries cannot double-grant service.
    """

    if (
        not invoice.is_active
        or invoice.status != InvoiceStatus.paid
        or to_decimal(invoice.balance_due) > 0
    ):
        return []

    created: list[ServiceEntitlement] = []
    lines = (
        db.query(InvoiceLine)
        .join(Subscription, Subscription.id == InvoiceLine.subscription_id)
        .filter(InvoiceLine.invoice_id == invoice.id)
        .filter(InvoiceLine.is_active.is_(True))
        .filter(Subscription.billing_mode == BillingMode.prepaid)
        .all()
    )
    lines = _base_subscription_lines(lines)
    for line in lines:
        existing = db.scalar(
            select(ServiceEntitlement).where(
                ServiceEntitlement.source_invoice_line_id == line.id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
        )
        entitlement = ensure_prepaid_entitlement_for_paid_invoice_line(
            db,
            invoice=invoice,
            line=line,
        )
        if entitlement is not None and existing is None:
            created.append(entitlement)
    if created:
        db.flush()
    return created


def project_paid_invoice_billing_anchors(
    db: Session,
    invoice: Invoice,
) -> tuple[Subscription, ...]:
    """Project next billing strictly from this paid invoice's entitlements."""

    entitlements = list(
        db.scalars(
            select(ServiceEntitlement)
            .where(
                ServiceEntitlement.source_invoice_id == invoice.id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
            .order_by(
                ServiceEntitlement.subscription_id,
                ServiceEntitlement.ends_at.desc(),
            )
        ).all()
    )
    ends_by_subscription: dict[object, datetime] = {}
    for entitlement in entitlements:
        current = ends_by_subscription.get(entitlement.subscription_id)
        if current is None or entitlement.ends_at > current:
            ends_by_subscription[entitlement.subscription_id] = entitlement.ends_at
    projected: list[Subscription] = []
    for subscription_id, entitlement_end in ends_by_subscription.items():
        subscription = db.get(Subscription, subscription_id)
        if subscription is None or subscription.subscriber_id != invoice.account_id:
            continue
        subscription.next_billing_at = entitlement_end
        projected.append(subscription)
    if projected:
        db.flush()
    return tuple(projected)


def ensure_prepaid_entitlement_for_paid_invoice_line(
    db: Session,
    *,
    invoice: Invoice,
    line: InvoiceLine,
    reconciliation_fingerprint: str | None = None,
) -> ServiceEntitlement | None:
    """Stage one exact paid-line entitlement without completing a transaction."""
    if (
        not invoice.is_active
        or invoice.status != InvoiceStatus.paid
        or to_decimal(invoice.balance_due) > 0
        or not line.is_active
        or line.invoice_id != invoice.id
        or line.subscription_id is None
    ):
        return None
    subscription = db.get(Subscription, line.subscription_id)
    if (
        subscription is None
        or subscription.subscriber_id != invoice.account_id
        or subscription.billing_mode != BillingMode.prepaid
    ):
        return None
    starts_at, ends_at = _line_period(invoice, line)
    if starts_at is None or ends_at is None or ends_at <= starts_at:
        return None
    existing = db.scalar(
        select(ServiceEntitlement).where(
            ServiceEntitlement.source_invoice_line_id == line.id,
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
        )
    )
    if existing is not None:
        return existing
    metadata: dict[str, str] = {"source": "paid_prepaid_invoice"}
    if reconciliation_fingerprint:
        metadata.update(
            {
                "reconciled_by": "financial.prepaid_service_coverage_reconciliation",
                "reconciliation_fingerprint": reconciliation_fingerprint,
            }
        )
    entitlement = ServiceEntitlement(
        account_id=invoice.account_id,
        subscription_id=line.subscription_id,
        source_invoice_id=invoice.id,
        source_invoice_line_id=line.id,
        starts_at=starts_at,
        ends_at=ends_at,
        amount_funded=round_money(to_decimal(line.amount)),
        currency=invoice.currency or "NGN",
        status=ServiceEntitlementStatus.active,
        metadata_=metadata,
    )
    db.add(entitlement)
    db.flush()
    return entitlement


def revoke_prepaid_entitlements_for_unpaid_invoice(
    db: Session, invoice: Invoice
) -> list[ServiceEntitlement]:
    """Revoke the entitlements an invoice funded, once it is no longer paid.

    The mirror of ``ensure_prepaid_entitlements_for_paid_invoice``. Entitlements
    were granted when a prepaid invoice was paid and then **never revoked** —
    ``ServiceEntitlementStatus.void``/``reversed`` were written nowhere in the
    codebase. So a refund or chargeback reopened the invoice (``paid`` →
    ``issued``/``overdue``) while the entitlement it had funded stayed ``active``,
    and ``resolve_prepaid_funding`` counts active entitlements as paid coverage:
    the customer kept the service they had just been refunded for, free, for the
    whole period.

    Called from the one place every payment effect funnels through, so refunds,
    reversals and allocation changes are all covered by construction.

    ``reversed`` (not ``void``) is the right terminal state: the entitlement was
    real and was earned back, which is different from one that should never have
    existed.
    """
    if invoice.status == InvoiceStatus.paid:
        return []

    entitlements = (
        db.query(ServiceEntitlement)
        .filter(ServiceEntitlement.source_invoice_id == invoice.id)
        .filter(ServiceEntitlement.status == ServiceEntitlementStatus.active)
        .all()
    )
    if not entitlements:
        return []

    for entitlement in entitlements:
        entitlement.status = ServiceEntitlementStatus.reversed
        metadata = dict(entitlement.metadata_ or {})
        metadata["revoked_reason"] = "source_invoice_no_longer_paid"
        metadata["revoked_invoice_status"] = (
            invoice.status.value if invoice.status else None
        )
        entitlement.metadata_ = metadata
    db.flush()
    logger.info(
        "Revoked %d prepaid entitlement(s) for invoice %s (status=%s)",
        len(entitlements),
        invoice.id,
        invoice.status.value if invoice.status else None,
    )
    return entitlements


def ensure_prepaid_entitlement_for_wallet_debit(
    db: Session,
    *,
    subscription: Subscription,
    ledger_entry: LedgerEntry,
    starts_at: datetime,
    ends_at: datetime,
) -> ServiceEntitlement | None:
    """Create prepaid service entitlement for a direct wallet-funded renewal."""

    adjustment_owned = False
    if ledger_entry.source == LedgerSource.adjustment:
        adjustment_owned = (
            db.query(AccountAdjustment.id)
            .filter(AccountAdjustment.ledger_entry_id == ledger_entry.id)
            .filter(AccountAdjustment.origin == "prepaid_service_renewal")
            .first()
            is not None
        )
    if (
        ledger_entry.source != LedgerSource.invoice and not adjustment_owned
    ) or not ledger_entry.is_active:
        return None
    metadata = ledger_entry.memo or ""
    existing = (
        db.query(ServiceEntitlement)
        .filter(ServiceEntitlement.source_ledger_entry_id == ledger_entry.id)
        .filter(ServiceEntitlement.status == ServiceEntitlementStatus.active)
        .first()
    )
    if existing is not None:
        return existing
    entitlement = ServiceEntitlement(
        account_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        source_ledger_entry_id=ledger_entry.id,
        starts_at=starts_at,
        ends_at=ends_at,
        amount_funded=round_money(to_decimal(ledger_entry.amount)),
        currency=ledger_entry.currency or "NGN",
        status=ServiceEntitlementStatus.active,
        metadata_={
            "source": (
                "scheduled_prepaid_service_renewal"
                if adjustment_owned
                else "wallet_prepaid_renewal"
            ),
            "source_ledger_entry_id": str(ledger_entry.id),
            "memo": metadata,
        },
    )
    db.add(entitlement)
    db.flush()
    return entitlement


def prepaid_entitlement_coverage_end(
    db: Session,
    *,
    subscription_id: object,
    account_id: object,
    period_start: datetime,
    period_end: datetime,
) -> datetime | None:
    """Return entitlement paid-through coverage from the proposed period start."""

    row = (
        db.query(ServiceEntitlement.ends_at)
        .filter(ServiceEntitlement.account_id == account_id)
        .filter(ServiceEntitlement.subscription_id == subscription_id)
        .filter(ServiceEntitlement.status == ServiceEntitlementStatus.active)
        .filter(ServiceEntitlement.starts_at <= period_start)
        .filter(ServiceEntitlement.ends_at > period_start)
        .order_by(ServiceEntitlement.ends_at.desc())
        .first()
    )
    return row[0] if row is not None else None


def current_prepaid_entitlement_end(
    db: Session,
    *,
    subscription_id: object,
    account_id: object,
    now: datetime,
) -> datetime | None:
    """Return the current funded entitlement end, if now is inside its interval."""

    row = (
        db.query(ServiceEntitlement.ends_at)
        .filter(ServiceEntitlement.account_id == account_id)
        .filter(ServiceEntitlement.subscription_id == subscription_id)
        .filter(ServiceEntitlement.status == ServiceEntitlementStatus.active)
        .filter(ServiceEntitlement.starts_at <= now)
        .filter(ServiceEntitlement.ends_at > now)
        .order_by(ServiceEntitlement.ends_at.desc())
        .first()
    )
    return row[0] if row is not None else None


def _base_subscription_lines(lines: list[InvoiceLine]) -> list[InvoiceLine]:
    base_lines = [
        line
        for line in lines
        if (line.metadata_ or {}).get("kind") == "base_subscription"
    ]
    if base_lines:
        return base_lines
    billable_lines = [
        line for line in lines if round_money(to_decimal(line.amount)) > 0
    ]
    if len(billable_lines) == 1:
        return billable_lines
    if len(lines) == 1:
        return lines
    return []


def _line_period(
    invoice: Invoice, line: InvoiceLine
) -> tuple[datetime | None, datetime | None]:
    metadata = line.metadata_ or {}
    starts_at = _coerce_datetime(metadata.get("billing_period_start"))
    ends_at = _coerce_datetime(metadata.get("billing_period_end"))
    if starts_at is not None and ends_at is not None:
        return starts_at, ends_at
    return invoice.billing_period_start, invoice.billing_period_end


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
