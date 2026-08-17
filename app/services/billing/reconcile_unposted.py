"""Reconcile cutover payments that posted as credit but never settled debt.

Background
----------
During the legacy-BSS -> local-ledger cutover (from 2026-06-13) a cohort of
``succeeded`` gateway payments landed without their balance being applied to the
customer's open invoices. Two distinct failure modes produced the same visible
symptom (*"paid but still walled-garden"*):

1. **Reseller-consolidated payments** lost ``billing_account_id`` in the webhook
   ingest path (see ``providers.PaymentProviderEvents.ingest``), so the surplus
   never incremented ``BillingAccount.balance`` and never settled invoices.
2. **Native subscriber payments** that were captured *before* the period's
   invoice was issued: the top-up auto-allocated to nothing and parked as
   unallocated account credit, then the new invoice issued afterwards and the
   credit just sat there while the debt aged the subscriber into suspension.

The durable fix is to take each account's *available balance* (unallocated
payment credit) and apply it to the account's open invoices — settling the debt
exactly as if the money had been posted at invoice time — then let the existing
``restore_account_services`` chain recompute status and lift enforcement.

This module is an orchestration adapter. It projects candidate work, then sends
each exact payment/invoice transfer through ``PaymentAllocations`` preview and
fingerprint-bound confirmation. It never constructs allocation or ledger rows.
It is invoked by the one-off CLI
``scripts/one_off/reconcile_unposted_payments.py`` (dry-run first) and is safe
to re-run through a stable idempotency key per (payment, invoice).
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.billing import (
    CreditNoteApplication,
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, Subscription
from app.schemas.billing import (
    PaymentAllocationConfirm,
    PaymentAllocationPreviewRequest,
)
from app.services.account_lifecycle import (
    BillingAnchorProjectionCommand,
    BillingAnchorProjectionSource,
    stage_subscription_billing_anchor,
)
from app.services.billing._common import (
    _recalculate_invoice_totals as recalculate_invoice_totals,
)
from app.services.billing._common import (
    get_account_credit_balance,
    lock_account,
)
from app.services.billing.invoices import InvoiceIssuanceInput, Invoices
from app.services.billing.payments import PaymentAllocations
from app.services.common import coerce_uuid, round_money, to_decimal
from app.services.notification_suppression import suppress_notifications
from app.services.service_entitlements import (
    ensure_prepaid_entitlements_for_paid_invoice,
)

logger = logging.getLogger(__name__)

_OPEN_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass
class SettleResult:
    """Outcome of settling one account's open invoices from its credit."""

    account_id: str
    available_credit: Decimal = Decimal("0.00")
    applied: Decimal = Decimal("0.00")
    invoices_settled: list[str] = field(default_factory=list)
    invoices_touched: list[str] = field(default_factory=list)
    # Credit that exists but is not backed by an allocatable succeeded payment
    # (e.g. it came from an adjustment/credit-note rather than a payment). We do
    # NOT fabricate a backing payment to spend it — that is exactly the kind of
    # re-derivation that produced the phantom-invoice divergence — so it is left
    # untouched and surfaced here for a human to look at.
    unbacked_credit: Decimal = Decimal("0.00")

    @property
    def changed(self) -> bool:
        return self.applied > 0


def _open_invoices(db: Session, account_id: str) -> list[Invoice]:
    """Account's open invoices, oldest-due first (matches _auto_allocate order)."""
    return (
        db.query(Invoice)
        .filter(Invoice.account_id == coerce_uuid(account_id))
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.status.in_(_OPEN_STATUSES))
        .filter(Invoice.balance_due > 0)
        .order_by(Invoice.due_at.asc().nulls_last(), Invoice.created_at.asc())
        .all()
    )


def _allocatable_payments(
    db: Session, account_id: str
) -> list[tuple[Payment, Decimal]]:
    """Return owner-evidenced allocatable payment credit, oldest first."""
    rows = (
        db.query(Payment)
        .filter(Payment.account_id == coerce_uuid(account_id))
        .filter(Payment.is_active.is_(True))
        .filter(Payment.status == PaymentStatus.succeeded)
        # Imported Splynx payments are historical evidence, not current wallet
        # backing. Reusing them to settle new local invoices over-allocates old
        # receipts and destroys the migrated deposit trail.
        .filter(Payment.splynx_payment_id.is_(None))
        .order_by(Payment.paid_at.asc().nulls_last(), Payment.created_at.asc())
        .all()
    )
    account_remaining_by_currency: dict[str, Decimal] = {}
    out: list[tuple[Payment, Decimal]] = []
    for payment in rows:
        currency = payment.currency or "NGN"
        if currency not in account_remaining_by_currency:
            account_remaining_by_currency[currency] = max(
                Decimal("0.00"),
                round_money(
                    get_account_credit_balance(db, str(account_id), currency=currency)
                ),
            )
        room = min(
            PaymentAllocations.available_amount(db, str(payment.id)),
            account_remaining_by_currency[currency],
        )
        if room > 0:
            out.append((payment, room))
            account_remaining_by_currency[currency] = round_money(
                account_remaining_by_currency[currency] - room
            )
    return out


def _allocation_key(payment: Payment, invoice: Invoice) -> str:
    return f"reconcile-unposted-{payment.id}-{invoice.id}"


def _confirm_allocation(
    db: Session,
    *,
    payment: Payment,
    invoice: Invoice,
    amount: Decimal,
) -> Decimal:
    """Preview and confirm one exact transfer through the payment owner."""
    request = PaymentAllocationPreviewRequest(
        payment_id=payment.id,
        invoice_id=invoice.id,
        amount=amount,
    )
    preview = PaymentAllocations.preview(db, request)
    result = PaymentAllocations.stage_confirm(
        db,
        PaymentAllocationConfirm(
            **request.model_dump(),
            preview_fingerprint=preview.fingerprint,
            idempotency_key=_allocation_key(payment, invoice),
        ),
    )
    if result.allocation.ledger_entry_id is None:
        raise RuntimeError("Allocation owner returned no invoice ledger evidence")
    if result.allocation.consumption_ledger_entry_id is None:
        raise RuntimeError("Allocation owner returned no credit-consumption evidence")
    return round_money(to_decimal(result.allocation.amount))


def settle_open_invoices_from_credit(db: Session, account_id: str) -> SettleResult:
    """Apply an account's available (unallocated) credit to its open invoices.

    The credit is consumed from owner-evidenced succeeded payments, oldest
    first, and applied to the oldest open invoices. Every transfer uses the
    payment-allocation owner's preview and confirmation, which links the exact
    invoice credit and unallocated-credit consumption debit.

    Does NOT commit — the caller owns the transaction boundary (so the one-off
    can commit per account and roll back a single bad account without losing the
    batch). Returns a :class:`SettleResult`.
    """
    # Compatibility adapter for one-off/repair callers. The named account-credit
    # owner now makes the allocation decision and composes PaymentAllocations.
    from app.services.billing.account_credit import AccountCreditApplications

    owner_result = AccountCreditApplications.apply(db, str(account_id))
    return SettleResult(
        account_id=owner_result.account_id,
        available_credit=owner_result.available_credit,
        applied=owner_result.applied,
        invoices_settled=owner_result.invoices_settled,
        invoices_touched=owner_result.invoices_touched,
        unbacked_credit=owner_result.unbacked_credit,
    )


def settle_single_invoice_from_credit(
    db: Session, invoice: Invoice, *, only_if_full: bool = False
) -> Decimal:
    """Apply an account's payment-backed credit to ONE invoice. Returns applied.

    Targeted, migrated-data-safe counterpart to
    :func:`settle_open_invoices_from_credit`: it only ever touches ``invoice``,
    so it cannot destroy credit on unrelated historical invoices (the reason the
    account-wide settle is disabled by default). Same accounting — a
    ``PaymentAllocation`` per applied naira plus one offsetting unallocated
    debit ledger entry through the payment owner, so
    ``get_account_credit_balance`` drops by exactly the amount and money is
    never double-counted. Does NOT commit.

    ``only_if_full=True`` makes it all-or-nothing: if the payment-backed credit
    cannot fully cover the invoice remaining, nothing is applied (returns 0).
    Used by the prepaid draft-until-funded path so a renewal is either fully
    funded from the deposit or left entirely as a draft.
    """
    if only_if_full:
        from app.services.billing.account_credit import AccountCreditApplications

        funding = AccountCreditApplications.preview_invoice_funding(db, invoice)
        if not funding.fully_funded:
            return Decimal("0.00")
        return AccountCreditApplications.apply_invoice_fully(
            db,
            invoice,
            preview_fingerprint=funding.fingerprint,
        ).applied

    account_id = str(invoice.account_id)
    currency = invoice.currency or "NGN"

    # Serialize the read-modify-write of the credit pool for this account.
    lock_account(db, account_id)

    invoice_remaining = _project_invoice_remaining(db, invoice)
    if invoice_remaining <= 0:
        return Decimal("0.00")

    credit = get_account_credit_balance(db, account_id, currency=currency)
    payments = [
        (payment, room)
        for payment, room in _allocatable_payments(db, account_id)
        if (payment.currency or "NGN") == currency
    ]
    payment_backed = round_money(
        sum((room for _payment, room in payments), Decimal("0.00"))
    )
    # Spend only credit that real succeeded payments can back (mirrors the
    # account-wide settle's unbacked-credit guard).
    spendable = min(max(credit, Decimal("0.00")), payment_backed)
    if spendable <= 0:
        return Decimal("0.00")
    if only_if_full and spendable < invoice_remaining:
        return Decimal("0.00")

    remaining = min(spendable, invoice_remaining)
    applied_total = Decimal("0.00")
    for payment, room in payments:
        if remaining <= 0:
            break
        amount = min(remaining, room)
        if amount <= 0:
            continue
        applied = _confirm_allocation(
            db,
            payment=payment,
            invoice=invoice,
            amount=amount,
        )
        applied_total = round_money(applied_total + applied)
        remaining = round_money(remaining - applied)
    if applied_total <= 0:
        return Decimal("0.00")

    db.flush()
    recalculate_invoice_totals(db, invoice)
    if invoice.status == InvoiceStatus.paid:
        _advance_prepaid_billing_from_entitlements(db, invoice)
    return applied_total


def _advance_prepaid_billing_from_entitlements(db: Session, invoice: Invoice) -> None:
    """Keep prepaid scheduling aligned with the exact paid-invoice evidence."""
    ensure_prepaid_entitlements_for_paid_invoice(db, invoice)
    entitlements = (
        db.query(ServiceEntitlement)
        .filter(ServiceEntitlement.source_invoice_id == invoice.id)
        .filter(ServiceEntitlement.status == ServiceEntitlementStatus.active)
        .all()
    )
    for entitlement in entitlements:
        subscription = db.get(Subscription, entitlement.subscription_id)
        entitlement_end = _as_utc(entitlement.ends_at)
        current_next_billing = (
            _as_utc(subscription.next_billing_at) if subscription is not None else None
        )
        if subscription is not None and (
            entitlement_end is not None
            and (current_next_billing is None or current_next_billing < entitlement_end)
        ):
            stage_subscription_billing_anchor(
                db,
                subscription,
                BillingAnchorProjectionCommand(
                    subscription_id=subscription.id,
                    expected_previous=subscription.next_billing_at,
                    target=entitlement_end,
                    source=BillingAnchorProjectionSource.prepaid_coverage,
                    evidence_ref=f"entitlement:{entitlement.id}",
                ),
            )


def _prepaid_draft_invoices(db: Session, account_id: str) -> list[Invoice]:
    """Draft prepaid renewal invoices for one account, oldest first."""
    return (
        db.query(Invoice)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .join(Subscription, Subscription.id == InvoiceLine.subscription_id)
        .filter(Invoice.account_id == coerce_uuid(account_id))
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.status == InvoiceStatus.draft)
        .filter(Invoice.balance_due > 0)
        .filter(InvoiceLine.is_active.is_(True))
        .filter(Subscription.billing_mode == BillingMode.prepaid)
        .distinct()
        .order_by(
            Invoice.billing_period_start.asc().nulls_last(),
            Invoice.created_at.asc(),
            Invoice.id.asc(),
        )
        .all()
    )


def settle_prepaid_draft_invoices_from_credit(
    db: Session, account_id: str, *, run_at: datetime | None = None
) -> SettleResult:
    """Issue and settle fully funded prepaid draft renewals for one account.

    Draft-until-funded renewals deliberately do not become AR when the wallet is
    short. A later top-up must therefore revisit those drafts; otherwise the
    billing runner has already advanced ``next_billing_at`` and the period is
    never recognized. This settles oldest-first and remains all-or-nothing per
    invoice: insufficient credit leaves the draft untouched.
    """
    result = SettleResult(account_id=str(account_id))
    now = run_at or datetime.now(UTC)

    for invoice in _prepaid_draft_invoices(db, str(account_id)):
        Invoices.issue_draft_system(
            db,
            str(invoice.id),
            issuance=InvoiceIssuanceInput(
                issued_at=now,
                due_at=now,
                due_date_basis=InvoiceDueDateBasis.prepaid_service_period,
                due_date_basis_ref=f"invoice:{invoice.id}:confirmed-credit",
                due_date_policy_version="prepaid-draft-settlement-v1",
                reason="reconcile_prepaid_draft_from_confirmed_credit",
            ),
            apply_available_credit=False,
        )

        applied = settle_single_invoice_from_credit(db, invoice, only_if_full=True)
        db.flush()
        recalculate_invoice_totals(db, invoice)
        if invoice.status == InvoiceStatus.paid:
            result.applied = round_money(result.applied + applied)
            result.invoices_touched.append(str(invoice.id))
            result.invoices_settled.append(str(invoice.id))
            continue

        # Underfunded or raced by another consumer: restore the non-AR state.
        Invoices.return_unfunded_prepaid_to_draft_system(
            db,
            str(invoice.id),
            reason="reconcile_prepaid_draft_underfunded",
        )
        break

    return result


def project_settlement(db: Session, account_id: str) -> SettleResult:
    """Read-only projection of :func:`settle_open_invoices_from_credit`.

    Computes how much credit *would* be applied and which invoices *would*
    settle, writing nothing. Used by the one-off CLI's dry-run so a dry-run is
    guaranteed side-effect free (never relies on a write-then-rollback).
    """
    result = SettleResult(account_id=str(account_id))
    invoices = _open_invoices(db, str(account_id))
    if not invoices:
        return result

    currencies = sorted({invoice.currency or "NGN" for invoice in invoices})
    credit_by_currency = {
        currency: get_account_credit_balance(db, str(account_id), currency=currency)
        for currency in currencies
    }
    result.available_credit = round_money(
        sum(
            (
                credit
                for credit in credit_by_currency.values()
                if credit > Decimal("0.00")
            ),
            Decimal("0.00"),
        )
    )
    if result.available_credit <= 0:
        return result

    payments = _allocatable_payments(db, str(account_id))
    payment_backed_by_currency: dict[str, Decimal] = {}
    for payment, room in payments:
        currency = payment.currency or "NGN"
        payment_backed_by_currency[currency] = round_money(
            payment_backed_by_currency.get(currency, Decimal("0.00")) + room
        )
    payment_backed = round_money(
        sum((room for _payment, room in payments), Decimal("0.00"))
    )
    remaining_by_currency = {
        currency: min(
            max(credit_by_currency.get(currency, Decimal("0.00")), Decimal("0.00")),
            payment_backed_by_currency.get(currency, Decimal("0.00")),
        )
        for currency in currencies
    }
    result.unbacked_credit = round_money(
        sum(
            (
                max(
                    credit_by_currency.get(currency, Decimal("0.00"))
                    - payment_backed_by_currency.get(currency, Decimal("0.00")),
                    Decimal("0.00"),
                )
                for currency in currencies
            ),
            Decimal("0.00"),
        )
    )
    _ = payment_backed
    for invoice in invoices:
        currency = invoice.currency or "NGN"
        remaining = remaining_by_currency.get(currency, Decimal("0.00"))
        if remaining <= 0:
            continue
        invoice_remaining = _project_invoice_remaining(db, invoice)
        if invoice_remaining <= 0:
            continue
        apply_here = min(remaining, invoice_remaining)
        if apply_here <= 0:
            continue
        result.applied = round_money(result.applied + apply_here)
        remaining = round_money(remaining - apply_here)
        remaining_by_currency[currency] = remaining
        result.invoices_touched.append(str(invoice.id))
        if apply_here >= invoice_remaining:
            result.invoices_settled.append(str(invoice.id))
    return result


def _project_invoice_remaining(db: Session, invoice: Invoice) -> Decimal:
    allocated = (
        db.query(func.coalesce(func.sum(PaymentAllocation.amount), 0))
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .filter(PaymentAllocation.invoice_id == invoice.id)
        .filter(PaymentAllocation.is_active.is_(True))
        .filter(Payment.is_active.is_(True))
        .filter(Payment.status == PaymentStatus.succeeded)
        .scalar()
    )
    credited = (
        db.query(func.coalesce(func.sum(CreditNoteApplication.amount), 0))
        .filter(CreditNoteApplication.invoice_id == invoice.id)
        .scalar()
    )
    computed_remaining = round_money(
        to_decimal(invoice.total) - to_decimal(allocated) - to_decimal(credited)
    )
    return max(
        Decimal("0.00"), min(to_decimal(invoice.balance_due), computed_remaining)
    )


@dataclass
class ReconcileResult:
    """Outcome of reconciling one subscriber (money + queued service restore)."""

    account_id: str
    settle: SettleResult
    subscriptions_restored: int = 0
    subscription_ids: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def changed(self) -> bool:
        return self.settle.changed or self.subscriptions_restored > 0


def _account_subscription_ids(db: Session, account_id: str) -> list[str]:
    from app.models.catalog import Subscription

    rows = (
        db.query(Subscription.id)
        .filter(Subscription.subscriber_id == coerce_uuid(account_id))
        .all()
    )
    return [str(r[0]) for r in rows]


def reconcile_subscriber(
    db: Session, account_id: str, *, dry_run: bool = True
) -> ReconcileResult:
    """Settle one account's debt from available balance and restore its service.

    Dry-run returns a read-only projection. Apply mode mutates and **commits**
    on success (so the caller's batch survives a single bad account) or rolls
    back and records the error. RADIUS refresh + CoA are handled once, at the
    batch level, by :func:`reconcile_cohort` after the money pass.
    """
    if dry_run:
        return ReconcileResult(
            account_id=str(account_id),
            settle=project_settlement(db, str(account_id)),
            subscription_ids=_account_subscription_ids(db, str(account_id)),
        )

    from app.services import collections as collections_service

    result = ReconcileResult(
        account_id=str(account_id),
        settle=SettleResult(account_id=str(account_id)),
    )
    try:
        result.settle = settle_open_invoices_from_credit(db, str(account_id))
        # Restore service for each invoice we fully settled. restore_account_services
        # is idempotent (it only acts on still-suspended subscriptions / open dunning
        # cases), so it is safe to call once per settled invoice.
        seen: set = set()
        for invoice_id in result.settle.invoices_settled:
            restored = collections_service.restore_account_services(
                db, str(account_id), invoice_id=invoice_id
            )
            result.subscriptions_restored += restored
            seen.add(invoice_id)
        # Even when nothing was newly settled, run a no-invoice restore so an
        # account that already cleared its debt (credit fully covers it) but is
        # still suspended gets re-evaluated.
        if (
            not result.settle.invoices_settled
            and not collections_service.has_overdue_balance(db, str(account_id))
        ):
            result.subscriptions_restored += (
                collections_service.restore_account_services(db, str(account_id))
            )
        result.subscription_ids = _account_subscription_ids(db, str(account_id))
        db.commit()
    except Exception as exc:  # noqa: BLE001 — isolate one bad account from the batch
        db.rollback()
        result.error = str(exc)
        logger.exception("Cutover reconcile failed for account %s", account_id)
    return result


def find_cohort_account_ids(
    db: Session, *, since: datetime | None = None, limit: int | None = None
) -> list[str]:
    """Accounts that hold unallocated credit AND still carry open invoice debt —
    money sitting while debt ages ('paid but unsettled').

    Keyed on credit + open-debt state, NOT on when a payment landed (the credit
    can predate any given date) nor on ``billing_account_id IS NULL`` (normal for
    native subscribers). ``since``, when given, additionally scopes candidates to
    accounts with a succeeded payment on/after that date — useful to target a
    specific window, but it UNDER-counts the true cohort, so the default (None)
    is the full set. The settler only ever applies payment-backed credit, so
    deposit-seed / credit-note balances surface as ``unbacked_credit`` rather
    than being settled.
    """
    # Necessary condition: an open invoice with a positive balance. This bounds
    # the candidate set without depending on when any payment arrived.
    candidate_ids = [
        str(r[0])
        for r in (
            db.query(Invoice.account_id)
            .filter(Invoice.account_id.isnot(None))
            .filter(Invoice.is_active.is_(True))
            .filter(Invoice.status.in_(_OPEN_STATUSES))
            .filter(Invoice.balance_due > 0)
            .distinct()
            .all()
        )
    ]
    if since is not None:
        paid_since = {
            str(r[0])
            for r in (
                db.query(Payment.account_id)
                .filter(Payment.account_id.isnot(None))
                .filter(Payment.is_active.is_(True))
                .filter(Payment.status == PaymentStatus.succeeded)
                .filter(Payment.created_at >= since)
                .distinct()
                .all()
            )
        }
        candidate_ids = [a for a in candidate_ids if a in paid_since]

    out: list[str] = []
    for account_id in candidate_ids:
        if get_account_credit_balance(db, account_id) <= 0:
            continue
        out.append(account_id)
        if limit is not None and len(out) >= limit:
            break
    return out


def reconcile_cohort(
    db: Session,
    *,
    since: datetime | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    refresh_radius: bool = True,
    send_coa: bool = True,
    extra_subscription_ids: list[str] | None = None,
    notify: bool = False,
) -> dict:
    """Drive the full cutover reconcile: settle the cohort's debt from balance,
    then (apply only) rebuild RADIUS once and CoA-kick the affected sessions.

    ``extra_subscription_ids`` lets an operator force the RADIUS refresh + CoA
    onto specific reported logins that have no remaining debt but a stale
    walled-garden tag (the 'paid + active but still walled' cohort).

    ``notify`` is False by default: this is bookkeeping catch-up over old,
    already-arrived funds across a mostly-churned cohort, so customer
    notifications ("Payment received" / "Service resumed") are suppressed — they
    would be factually wrong and a bulk burst would damage sender reputation.
    Set notify=True only for a live, customer-initiated reconcile.
    """
    account_ids = find_cohort_account_ids(db, since=since, limit=limit)

    suppress_ctx = nullcontext() if (notify or dry_run) else suppress_notifications()
    with suppress_ctx:
        results = [
            reconcile_subscriber(db, aid, dry_run=dry_run) for aid in account_ids
        ]

    total_applied = round_money(
        sum((r.settle.applied for r in results), Decimal("0.00"))
    )
    changed = [r for r in results if r.changed]
    errors = [r for r in results if r.error]

    # Subscriptions to kick: every subscription under a changed account, plus any
    # explicitly-supplied reported logins.
    coa_subscription_ids: set = set(extra_subscription_ids or [])
    for r in changed:
        coa_subscription_ids.update(r.subscription_ids)

    summary = {
        "since": since.isoformat() if since is not None else None,
        "candidates": len(account_ids),
        "accounts_changed": len(changed),
        "total_applied": str(total_applied),
        "invoices_settled": sum(len(r.settle.invoices_settled) for r in results),
        "subscriptions_restored": sum(r.subscriptions_restored for r in results),
        "errors": len(errors),
        "unbacked_credit_accounts": sum(
            1 for r in results if r.settle.unbacked_credit > 0
        ),
        "dry_run": dry_run,
        "radius_refreshed": False,
        "sessions_kicked": 0,
        "results": results,
    }

    if dry_run:
        return summary

    if refresh_radius:
        # Whole-table, idempotent rebuild of radcheck/radreply from current
        # subscription/subscriber status. This is what lifts the stale
        # walled-garden tag for the 'paid + active but still walled' accounts,
        # not just the ones whose debt we just settled.
        from app.services.radius_population import populate

        populate(dry_run=False)
        summary["radius_refreshed"] = True

    if send_coa and coa_subscription_ids:
        from app.services.enforcement import disconnect_subscription_sessions

        kicked = 0
        for subscription_id in coa_subscription_ids:
            try:
                kicked += disconnect_subscription_sessions(
                    db, subscription_id, reason="cutover payment reconcile"
                )
            except Exception:
                logger.warning(
                    "Cutover reconcile: CoA kick failed for subscription %s",
                    subscription_id,
                    exc_info=True,
                )
        summary["sessions_kicked"] = kicked

    return summary
