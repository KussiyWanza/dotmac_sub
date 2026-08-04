"""Read-only reconciliation pass over every billing discrepancy class.

Companion to ``docs/audits/BILLING_SOT_AUDIT_2026-07-12.md``. This script does
not write. It has no ``--apply`` flag and never commits: the session is rolled
back in a ``finally`` block. Its job is to size each discrepancy class on real
data so the historical-repair scope can be set before any correction is built.

The audit's "possible impact" caveat is the whole point of this tool. Every
finding in the report is a reachable code path; only a query against real data
says whether it actually fired, and how often, and for how much money.

Classes reported
----------------
  D1  ledger double-swing        F1   reversal posted AND original deactivated
  D2  unbacked dead credit       F1   no Payment and no reconciled legacy mirror
  D3  paid-with-balance          F24  invoice paid yet balance_due > 0
  D4  native orphan payments     F19  succeeded native payment, no allocation, no ledger
  D5  misallocated payments      F3   ledger invoice_id != allocation invoice_id
  D6  succeeded, no paid_at      F15  blinds the enforcement health gate
  D7  reconstructed drift        F4   persisted outputs vs independent expected position
  D8  unfunded credit remainder F4   issued remainder has no reviewed ledger funding
  D9  pending money              F18  pending payment already holding allocation/ledger
  D10 void with live debits      F6   void bypassed the ledger reversal
  D11 opening debits             --   cutover seed cohort, split by deposit sign
  D12 enforcement mismatch       F6/F7 funded+locked / unfunded+served

Two detectors were WRONG on first contact with real data and were narrowed. Both
mistakes were of the same kind — counting a deliberate design decision as damage:

  D2  counted every deactivated ledger row as a silent balance move. Most of them
      duplicate the Payment/Invoice documents the balance is actually derived
      from, so deactivating them removed a double-count. It now requires both no
      native Payment and no per-account mirror/deposit reconciliation. Gross
      figure was NGN 2.2bn; the part that needs adjudication is far smaller.
  D4  counted splynx-imported payments as orphans. They have no local allocation
      or ledger entry BY DESIGN (mirrored, not posted). Gross was 3,117 payments
      / NGN 146M; the real native signal was 1 payment.

Read every future number here the same way: a cluster being real is not the same
as its cause being real.

Examples
--------
  # PostgreSQL replicas are accepted by default; primaries are refused.
  python -m scripts.one_off.billing_alignment_audit --only D2,D7,D12
  python -m scripts.one_off.billing_alignment_audit --out /tmp/align --limit 0
  # Staging primary only, after the host has been explicitly approved:
  python -m scripts.one_off.billing_alignment_audit --only D1,D3 --allow-primary

Production safety
-----------------
The runner sets the PostgreSQL transaction read-only, applies a per-statement
timeout, refuses primaries by default, derives D7/D12 balances with bounded
whole-table aggregates, and resolves D12 thresholds through the canonical batch
owner. In isolated adjudication mode, D7/D12 require an immutable ``--snapshot-at``
and source tables loaded from retained Splynx backups. ``--allow-primary`` is an
explicit exception, not the normal prod path. Free-text memos are used only
inside the detector and are never written to CSV.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import traceback
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    Numeric,
    Uuid,
    and_,
    case,
    column,
    func,
    inspect,
    or_,
    select,
    table,
    text,
)
from sqlalchemy import true as sa_true
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.billing import (
    AccountAdjustment,
    CreditNote,
    CreditNoteStatus,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.prepaid_funding import PrepaidFundingReconstructionBatch
from app.models.subscriber import Subscriber
from app.services.prepaid_funding_reconstruction import LEGACY_FINANCIAL_HANDOFF_AT

ZERO = Decimal("0.00")
LEGACY_LEDGER_CUTOVER = datetime(2026, 3, 15, 23, 59, 59, tzinfo=UTC)
PAYMENT_ACTIVITY_AT = datetime(2026, 6, 16, tzinfo=UTC)
SERVICE_ACTIVITY_AT = datetime(2026, 6, 16, 9, 8, tzinfo=UTC)
INTERNAL_MEMO_EXACT = {"Prepaid opening balance @ cutover"}
INTERNAL_MEMO_PREFIXES = (
    "Correction:",
    "Partial cutover opening balance construction adjustment",
    "Reversal of phantom",
    "Reversal of prepaid opening",
    "Data repair 2026-06-29:",
    "Validated account credit consumed",
    "Payment refund account-credit consumption:",
    "Payment reversal account-credit consumption:",
    "Payment allocation account-credit consumption:",
)
LEGACY_FINANCIAL_REPLAY_AT = LEGACY_FINANCIAL_HANDOFF_AT
LEGACY_FINANCIAL_FINAL_DATE = LEGACY_FINANCIAL_REPLAY_AT.date() - timedelta(days=1)
_REVERSAL_OF = re.compile(
    r"Reversal of ledger entry\s+([0-9a-fA-F-]{36})", re.IGNORECASE
)


@dataclass
class Finding:
    """One discrepancy class: its rows, and the money it represents."""

    code: str
    title: str
    ref: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    amount: Decimal = ZERO
    note: str = ""
    error: str = ""

    @property
    def count(self) -> int:
        return len(self.rows)


# This is the trust-boundary contract for portable audit evidence. The audit
# runs beside the isolated restore on the explicitly approved trusted host;
# only these reviewed, minimal fields may be written outside that boundary.
# A detector that adds a field must update this allowlist deliberately or the
# export fails closed. In particular, external references, invoice numbers,
# memos, JSON, contact data and other free text are not portable evidence.
EVIDENCE_SCHEMAS: dict[str, tuple[tuple[str, ...], ...]] = {
    "D1": (
        (
            "account_id",
            "original_id",
            "reversal_id",
            "entry_type",
            "amount",
            "currency",
            "balance_affecting",
            "overswing",
        ),
    ),
    "D2": (
        (
            "account_id",
            "entry_id",
            "amount",
            "currency",
            "effective_date",
            "created_at",
            "source",
            "cutoff_coverage",
            "cutoff_deposit",
            "mirror_rows",
            "mirror_net",
            "verdict",
        ),
    ),
    "D3": (("account_id", "invoice_id", "total", "balance_due", "currency"),),
    "D4": (("account_id", "payment_id", "amount", "currency", "paid_at"),),
    "D5": (
        (
            "account_id",
            "payment_id",
            "amount",
            "ledger_invoice_id",
            "allocation_invoice_id",
        ),
    ),
    "D6": (("account_id", "payment_id", "amount", "created_at"),),
    "D7": (
        ("account_id", "ledger_credit", "document_position", "gap", "risk"),
        (
            "account_id",
            "expected_position",
            "current_deposit",
            "local_document_position",
            "ledger_credit",
            "deposit_drift",
            "document_drift",
            "ledger_drift",
            "post_legacy_credits",
            "derived_service_charges",
        ),
    ),
    "D8": (
        (
            "account_id",
            "credit_note_id",
            "status",
            "total",
            "applied_total",
            "unapplied",
        ),
    ),
    "D9": (("account_id", "payment_id", "amount", "currency"),),
    "D10": (("account_id", "invoice_id", "live_debit_rows", "live_debit_total"),),
    "D11": (
        ("account_id", "entry_id", "amount", "deposit", "deposit_sign", "verdict"),
    ),
    "D12": (("account_id", "available", "threshold", "locked", "served", "verdict"),),
}


def _money(v: object) -> Decimal:
    if v is None:
        return ZERO
    return Decimal(str(v))


def _account_filter(column, account_ids):
    """Restrict to a set of accounts, or scan the whole table when None.

    A chunked ``IN (...)`` list of a few hundred UUIDs makes the planner scan
    ledger_entries once PER CHUNK and blows the statement timeout. One
    whole-table GROUP BY aggregate over the same rows is a single sequential
    scan (measured on staging: 212k rows, 3.8s, well inside the 10s timeout),
    and it answers for every account at once. Pass None to get that.
    """
    if account_ids is None:
        return sa_true()
    return column.in_(account_ids)


def _chunks(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _before(value: datetime | None, cutoff: datetime) -> bool:
    comparable = _aware(value)
    return comparable is not None and comparable < cutoff


def _add_position(
    positions: dict[tuple[str, str], Decimal],
    account_id: object,
    currency: str | None,
    amount: object,
    *,
    credit: bool,
) -> None:
    key = (str(account_id), currency or "NGN")
    signed = _money(amount) if credit else -_money(amount)
    positions[key] = positions.get(key, ZERO) + signed


def _batch_customer_positions(
    db: Session,
    account_ids: Sequence[Any] | None,
    *,
    currency: str | None,
    use_legacy_mirror: bool = True,
) -> dict[tuple[str, str], Decimal]:
    """Derive canonical customer positions in a fixed number of queries.

    This is the batch equivalent of ``list_customer_financial_events``. It is
    intentionally kept in the audit harness rather than made a second runtime
    balance owner. Tests compare it with the canonical per-account function.
    """
    from app.models.splynx_transaction import SplynxBillingTransaction
    from app.services.invoice_classification import (
        collectible_ar_invoice_filter,
    )

    # account_ids=None means "every account" (one whole-table aggregate). Only an
    # explicitly EMPTY set means "nothing to do".
    if account_ids is not None and not account_ids:
        return {}

    positions: dict[tuple[str, str], Decimal] = {}
    mirror_accounts = (
        {
            str(value)
            for value in db.scalars(
                select(SplynxBillingTransaction.subscriber_id)
                .where(
                    _account_filter(
                        SplynxBillingTransaction.subscriber_id, account_ids
                    ),
                    SplynxBillingTransaction.deleted.is_(False),
                )
                .distinct()
            ).all()
            if value is not None
        }
        if use_legacy_mirror
        else set()
    )

    if mirror_accounts and currency in (None, "NGN"):
        legacy_rows = db.execute(
            select(
                SplynxBillingTransaction.subscriber_id,
                SplynxBillingTransaction.entry_type,
                SplynxBillingTransaction.amount,
            ).where(
                _account_filter(SplynxBillingTransaction.subscriber_id, account_ids),
                SplynxBillingTransaction.deleted.is_(False),
                SplynxBillingTransaction.transaction_date.isnot(None),
            )
        ).all()
        for legacy_row in legacy_rows:
            _add_position(
                positions,
                legacy_row.subscriber_id,
                "NGN",
                legacy_row.amount,
                credit=str(legacy_row.entry_type) == LedgerEntryType.credit.value,
            )

    payment_stmt = select(
        Payment.account_id,
        Payment.currency,
        Payment.amount,
        Payment.refunded_amount,
        Payment.created_at,
    ).where(
        _account_filter(Payment.account_id, account_ids),
        Payment.is_active.is_(True),
        Payment.status.in_(
            [
                PaymentStatus.succeeded,
                PaymentStatus.partially_refunded,
                PaymentStatus.refunded,
            ]
        ),
    )
    if currency is not None:
        payment_stmt = payment_stmt.where(Payment.currency == currency)
    for payment_row in db.execute(payment_stmt).all():
        if str(payment_row.account_id) in mirror_accounts and _before(
            payment_row.created_at, PAYMENT_ACTIVITY_AT
        ):
            continue
        net = _money(payment_row.amount) - _money(payment_row.refunded_amount)
        if net > ZERO:
            _add_position(
                positions,
                payment_row.account_id,
                payment_row.currency,
                net,
                credit=True,
            )

    allocation_stmt = (
        select(
            Invoice.account_id,
            Payment.currency,
            Invoice.currency.label("invoice_currency"),
            PaymentAllocation.amount,
            Payment.created_at,
        )
        .join(Invoice, Invoice.id == PaymentAllocation.invoice_id)
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .where(
            _account_filter(Invoice.account_id, account_ids),
            PaymentAllocation.is_active.is_(True),
            Payment.is_active.is_(True),
            Payment.status.in_(
                [
                    PaymentStatus.succeeded,
                    PaymentStatus.partially_refunded,
                    PaymentStatus.refunded,
                ]
            ),
            or_(Payment.account_id.is_(None), Payment.account_id != Invoice.account_id),
        )
    )
    if currency is not None:
        allocation_stmt = allocation_stmt.where(Payment.currency == currency)
    for allocation_row in db.execute(allocation_stmt).all():
        if str(allocation_row.account_id) in mirror_accounts and _before(
            allocation_row.created_at, PAYMENT_ACTIVITY_AT
        ):
            continue
        _add_position(
            positions,
            allocation_row.account_id,
            allocation_row.currency or allocation_row.invoice_currency,
            allocation_row.amount,
            credit=True,
        )

    invoice_stmt = select(
        Invoice.account_id,
        Invoice.currency,
        Invoice.total,
        Invoice.created_at,
    ).where(
        _account_filter(Invoice.account_id, account_ids),
        Invoice.is_active.is_(True),
        Invoice.status.in_(
            [
                InvoiceStatus.issued,
                InvoiceStatus.partially_paid,
                InvoiceStatus.overdue,
                InvoiceStatus.paid,
            ]
        ),
        Invoice.is_proforma.is_(False),
        collectible_ar_invoice_filter(),
    )
    if currency is not None:
        invoice_stmt = invoice_stmt.where(Invoice.currency == currency)
    for invoice_row in db.execute(invoice_stmt).all():
        if str(invoice_row.account_id) in mirror_accounts and _before(
            invoice_row.created_at, SERVICE_ACTIVITY_AT
        ):
            continue
        _add_position(
            positions,
            invoice_row.account_id,
            invoice_row.currency,
            invoice_row.total,
            credit=False,
        )

    credit_note_stmt = select(
        CreditNote.account_id,
        CreditNote.currency,
        CreditNote.total,
        CreditNote.created_at,
    ).where(
        _account_filter(CreditNote.account_id, account_ids),
        CreditNote.is_active.is_(True),
        CreditNote.status.in_(
            [
                CreditNoteStatus.issued,
                CreditNoteStatus.partially_applied,
                CreditNoteStatus.applied,
            ]
        ),
    )
    if currency is not None:
        credit_note_stmt = credit_note_stmt.where(CreditNote.currency == currency)
    for credit_note_row in db.execute(credit_note_stmt).all():
        if str(credit_note_row.account_id) in mirror_accounts and _before(
            credit_note_row.created_at, SERVICE_ACTIVITY_AT
        ):
            continue
        _add_position(
            positions,
            credit_note_row.account_id,
            credit_note_row.currency,
            credit_note_row.total,
            credit=True,
        )

    ledger_stmt = select(
        LedgerEntry.account_id,
        LedgerEntry.currency,
        LedgerEntry.entry_type,
        LedgerEntry.amount,
        LedgerEntry.memo,
        LedgerEntry.effective_date,
        LedgerEntry.created_at,
    ).where(
        _account_filter(LedgerEntry.account_id, account_ids),
        LedgerEntry.invoice_id.is_(None),
        LedgerEntry.is_active.is_(True),
        or_(
            LedgerEntry.source.in_([LedgerSource.adjustment, LedgerSource.other]),
            and_(
                LedgerEntry.source == LedgerSource.refund,
                LedgerEntry.payment_id.is_(None),
            ),
            and_(
                LedgerEntry.source == LedgerSource.invoice,
                LedgerEntry.entry_type == LedgerEntryType.debit,
            ),
            and_(
                LedgerEntry.source == LedgerSource.payment,
                LedgerEntry.payment_id.is_(None),
            ),
            # CreditNote is the canonical customer-position fact. Funding,
            # application-transfer and void-reversal ledger rows are the
            # operational spendability/evidence projection and must not be
            # counted again here. Keep this in lockstep with
            # customer_financial_ledger.list_customer_financial_events() and
            # customer_financial_balances_by_currency().
        ),
    )
    if currency is not None:
        ledger_stmt = ledger_stmt.where(LedgerEntry.currency == currency)
    for ledger_row in db.execute(ledger_stmt).all():
        memo = str(ledger_row.memo or "")
        if memo in INTERNAL_MEMO_EXACT or memo.startswith(INTERNAL_MEMO_PREFIXES):
            continue
        occurred_at = _aware(ledger_row.effective_date or ledger_row.created_at)
        if (
            str(ledger_row.account_id) in mirror_accounts
            and occurred_at is not None
            and occurred_at <= LEGACY_LEDGER_CUTOVER
        ):
            continue
        _add_position(
            positions,
            ledger_row.account_id,
            ledger_row.currency,
            ledger_row.amount,
            credit=ledger_row.entry_type == LedgerEntryType.credit,
        )

    return positions


@dataclass
class _ReplayService:
    service_id: int
    account_id: str
    subscription_id: str | None
    next_due: date
    cycle_days: int
    charge: Decimal


@dataclass
class ReconstructedPositions:
    """Expected balances plus explicit limits on replay confidence."""

    positions: dict[str, Decimal]
    service_charges: dict[str, Decimal]
    post_legacy_credits: dict[str, Decimal]
    incomplete: dict[str, set[str]]
    service_cycle_gaps: tuple[_ReplayServiceCycleGap, ...]


@dataclass(frozen=True)
class _ReplayServiceCycleGap:
    """One affordable source-derived cycle lacking canonical funding evidence."""

    account_id: str
    subscription_id: str | None
    period_start: date
    period_end: date
    amount: Decimal
    funding_before: Decimal
    currency: str
    reason: str


def _batch_reconstructed_positions(
    db: Session,
    account_ids: Sequence[Any] | None,
    *,
    snapshot_at: datetime,
) -> ReconstructedPositions:
    """Replay the expected NGN position from independent source facts.

    The final Splynx deposit is the opening state. Current Sub deposits,
    invoices, ledger totals and enforcement state are deliberately absent from
    the formula. Post-legacy payments and credit notes are replayed as business
    facts. Service debits are derived from Splynx's final paid-through schedule,
    with applied service extensions shifting the next due date.

    Accounts whose replay needs an unproven manual adjustment, a missing service
    schedule, or an unmodelled plan decision are returned in ``incomplete`` and
    must not become an automated repair worklist.
    """
    from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
    from app.models.event_store import EventStore
    from app.models.service_extension import (
        ServiceExtension,
        ServiceExtensionEntry,
        ServiceExtensionStatus,
    )

    bind = db.get_bind()
    inspector = inspect(bind)
    required_tables = {
        "audit_splynx_final_balances",
        "audit_splynx_final_services",
    }
    missing = sorted(name for name in required_tables if not inspector.has_table(name))
    if missing:
        raise RuntimeError(
            "counterfactual replay requires isolated source tables: "
            + ", ".join(missing)
        )

    snapshot_at = _aware(snapshot_at) or snapshot_at
    if snapshot_at < LEGACY_FINANCIAL_REPLAY_AT:
        raise ValueError("replay snapshot predates the final legacy handoff")
    snapshot_date = snapshot_at.date()
    authority_position_at = _aware(
        db.scalar(
            select(PrepaidFundingReconstructionBatch.position_at).where(
                PrepaidFundingReconstructionBatch.is_authority_cutover.is_(True)
            )
        )
    )

    final_balances = table(
        "audit_splynx_final_balances",
        column("subscriber_id", Uuid(as_uuid=True)),
        column("final_deposit", Numeric(19, 4)),
    )
    final_services = table(
        "audit_splynx_final_services",
        column("splynx_service_id"),
        column("subscriber_id", Uuid(as_uuid=True)),
        column("subscription_id", Uuid(as_uuid=True)),
        column("source_status"),
        column("source_deleted", Boolean),
        column("quantity"),
        column("unit_price", Numeric(19, 4)),
        column("start_date", Date),
        column("last_charge_total", Numeric(19, 4)),
        column("last_period_from", Date),
        column("last_period_to", Date),
        column("noncharge_transaction_rows"),
        column("noncharge_period_rows"),
        column("last_noncharge_transaction_id"),
        column("last_noncharge_type"),
        column("last_noncharge_source"),
        column("last_noncharge_to_invoice", Boolean),
        column("last_noncharge_comment_empty", Boolean),
        column("last_noncharge_category_id"),
        column("last_noncharge_total", Numeric(19, 4)),
        column("last_noncharge_period_from", Date),
        column("last_noncharge_period_to", Date),
    )

    opening_stmt = select(
        final_balances.c.subscriber_id,
        final_balances.c.final_deposit,
    ).where(final_balances.c.subscriber_id.isnot(None))
    if account_ids is not None:
        opening_stmt = opening_stmt.where(
            final_balances.c.subscriber_id.in_(account_ids)
        )
    positions = {
        str(row.subscriber_id): _money(row.final_deposit)
        for row in db.execute(opening_stmt)
    }
    incomplete: dict[str, set[str]] = defaultdict(set)
    service_charges: dict[str, Decimal] = defaultdict(lambda: ZERO)
    post_legacy_credits: dict[str, Decimal] = defaultdict(lambda: ZERO)
    financial_events: dict[str, list[tuple[datetime, Decimal]]] = defaultdict(list)
    native_zero_accounts: set[Any] = set()

    # Accounts born entirely in Sub after the legacy handoff have no Splynx
    # opening row by construction.  Their opening position is provably zero
    # only when the account existed by the requested snapshot, has no Splynx
    # identity or service evidence, and has no native financial fact predating
    # the handoff.  This is the same authority rule used by the runtime reader
    # for post-cutover accounts; it is not a missing-baseline fallback.
    requested_ids = list(account_ids or [])
    if requested_ids:
        source_service_accounts = set(
            db.scalars(
                select(final_services.c.subscriber_id).where(
                    final_services.c.subscriber_id.in_(requested_ids),
                    final_services.c.subscriber_id.isnot(None),
                )
            ).all()
        )
        native_candidates = (
            set(
                db.scalars(
                    select(Subscriber.id).where(
                        Subscriber.id.in_(requested_ids),
                        Subscriber.splynx_customer_id.is_(None),
                        Subscriber.created_at <= snapshot_at,
                    )
                ).all()
            )
            - source_service_accounts
        )
        pre_handoff_accounts: set[Any] = set()
        if native_candidates:
            pre_handoff_accounts.update(
                db.scalars(
                    select(Payment.account_id).where(
                        Payment.account_id.in_(native_candidates),
                        Payment.created_at < LEGACY_FINANCIAL_REPLAY_AT,
                    )
                ).all()
            )
            pre_handoff_accounts.update(
                db.scalars(
                    select(Invoice.account_id).where(
                        Invoice.account_id.in_(native_candidates),
                        Invoice.created_at < LEGACY_FINANCIAL_REPLAY_AT,
                    )
                ).all()
            )
            pre_handoff_accounts.update(
                db.scalars(
                    select(CreditNote.account_id).where(
                        CreditNote.account_id.in_(native_candidates),
                        CreditNote.created_at < LEGACY_FINANCIAL_REPLAY_AT,
                    )
                ).all()
            )
            pre_handoff_accounts.update(
                db.scalars(
                    select(LedgerEntry.account_id).where(
                        LedgerEntry.account_id.in_(native_candidates),
                        LedgerEntry.affects_customer_position.is_(True),
                        LedgerEntry.created_at < LEGACY_FINANCIAL_REPLAY_AT,
                    )
                ).all()
            )
        native_zero_accounts = native_candidates - pre_handoff_accounts
        for native_account_id in native_zero_accounts:
            positions.setdefault(str(native_account_id), ZERO)

    replay_ids = list(positions)
    if not replay_ids:
        return ReconstructedPositions({}, {}, {}, {}, ())

    payment_stmt = select(
        Payment.account_id,
        Payment.amount,
        Payment.refunded_amount,
        Payment.paid_at,
        Payment.created_at,
    ).where(
        Payment.account_id.isnot(None),
        Payment.account_id.in_(replay_ids),
        Payment.is_active.is_(True),
        Payment.status.in_(
            [
                PaymentStatus.succeeded,
                PaymentStatus.partially_refunded,
                PaymentStatus.refunded,
            ]
        ),
        Payment.created_at <= snapshot_at,
        or_(
            func.coalesce(Payment.paid_at, Payment.created_at)
            >= LEGACY_FINANCIAL_REPLAY_AT,
            Payment.created_at >= LEGACY_FINANCIAL_REPLAY_AT,
        ),
        func.coalesce(Payment.paid_at, Payment.created_at) <= snapshot_at,
    )
    for payment_row in db.execute(payment_stmt):
        account_id = str(payment_row.account_id)
        occurred_at = _aware(payment_row.paid_at or payment_row.created_at)
        recorded_at = _aware(payment_row.created_at)
        if occurred_at is None:
            incomplete[account_id].add("payment_without_event_time")
            continue
        if recorded_at is None:
            incomplete[account_id].add("payment_without_recorded_time")
            continue
        if recorded_at >= LEGACY_FINANCIAL_REPLAY_AT:
            occurred_at = max(occurred_at, LEGACY_FINANCIAL_REPLAY_AT)
        net = _money(payment_row.amount) - _money(payment_row.refunded_amount)
        financial_events[account_id].append((occurred_at, net))
        post_legacy_credits[account_id] += net
        if _money(payment_row.refunded_amount) > ZERO:
            incomplete[account_id].add("refund_timeline_requires_source_provenance")

    # Consolidated payments belong to a billing account, but an active
    # allocation is a subscriber-level credit fact.
    allocation_stmt = (
        select(
            Invoice.account_id,
            PaymentAllocation.amount,
            PaymentAllocation.created_at.label("allocation_created_at"),
        )
        .join(Invoice, Invoice.id == PaymentAllocation.invoice_id)
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .where(
            Invoice.account_id.in_(replay_ids),
            PaymentAllocation.is_active.is_(True),
            Payment.account_id.is_(None),
            Payment.is_active.is_(True),
            Payment.status.in_(
                [
                    PaymentStatus.succeeded,
                    PaymentStatus.partially_refunded,
                    PaymentStatus.refunded,
                ]
            ),
            PaymentAllocation.created_at >= LEGACY_FINANCIAL_REPLAY_AT,
            PaymentAllocation.created_at <= snapshot_at,
        )
    )
    for allocation_row in db.execute(allocation_stmt):
        account_id = str(allocation_row.account_id)
        occurred_at = _aware(allocation_row.allocation_created_at)
        if occurred_at is None:
            incomplete[account_id].add("allocation_without_event_time")
            continue
        amount = _money(allocation_row.amount)
        financial_events[account_id].append((occurred_at, amount))
        post_legacy_credits[account_id] += amount

    credit_note_stmt = select(
        CreditNote.account_id,
        CreditNote.total,
        CreditNote.created_at,
    ).where(
        CreditNote.account_id.in_(replay_ids),
        CreditNote.is_active.is_(True),
        CreditNote.status.in_(
            [
                CreditNoteStatus.issued,
                CreditNoteStatus.partially_applied,
                CreditNoteStatus.applied,
            ]
        ),
        CreditNote.created_at >= LEGACY_FINANCIAL_REPLAY_AT,
        CreditNote.created_at <= snapshot_at,
    )
    for credit_note_row in db.execute(credit_note_stmt):
        account_id = str(credit_note_row.account_id)
        occurred_at = _aware(credit_note_row.created_at)
        if occurred_at is None:
            incomplete[account_id].add("credit_note_without_event_time")
            continue
        amount = _money(credit_note_row.total)
        financial_events[account_id].append((occurred_at, amount))
        post_legacy_credits[account_id] += amount

    adjustment_stmt = select(
        LedgerEntry.account_id,
        LedgerEntry.entry_type,
        LedgerEntry.amount,
        LedgerEntry.effective_date,
        LedgerEntry.created_at,
    ).where(
        LedgerEntry.account_id.in_(replay_ids),
        LedgerEntry.is_active.is_(True),
        LedgerEntry.affects_customer_position.is_(True),
        LedgerEntry.invoice_id.is_(None),
        LedgerEntry.source.in_([LedgerSource.adjustment, LedgerSource.other]),
        LedgerEntry.currency == "NGN",
        # ``effective_date`` is the real-world date represented by an entry,
        # while ``created_at`` is when that fact became part of Sub.  A
        # backdated row written after the requested snapshot must not leak into
        # an earlier reconstruction.
        LedgerEntry.created_at <= snapshot_at,
        or_(
            func.coalesce(LedgerEntry.effective_date, LedgerEntry.created_at)
            >= LEGACY_FINANCIAL_REPLAY_AT,
            LedgerEntry.created_at >= LEGACY_FINANCIAL_REPLAY_AT,
        ),
        func.coalesce(LedgerEntry.effective_date, LedgerEntry.created_at)
        <= snapshot_at,
    )
    for adjustment_row in db.execute(adjustment_stmt):
        account_id = str(adjustment_row.account_id)
        occurred_at = _aware(adjustment_row.effective_date or adjustment_row.created_at)
        recorded_at = _aware(adjustment_row.created_at)
        if occurred_at is None:
            incomplete[account_id].add("adjustment_without_event_time")
            continue
        if recorded_at is None:
            incomplete[account_id].add("adjustment_without_recorded_time")
            continue
        if recorded_at >= LEGACY_FINANCIAL_REPLAY_AT:
            occurred_at = max(occurred_at, LEGACY_FINANCIAL_REPLAY_AT)
        amount = _money(adjustment_row.amount)
        signed = (
            amount if adjustment_row.entry_type == LedgerEntryType.credit else -amount
        )
        financial_events[account_id].append((occurred_at, signed))
        if signed > ZERO:
            post_legacy_credits[account_id] += signed

    extension_days: dict[str, int] = defaultdict(int)
    extension_accounts: set[str] = set()
    extension_rows = db.execute(
        select(
            ServiceExtensionEntry.subscription_id,
            ServiceExtensionEntry.subscriber_id,
            ServiceExtensionEntry.previous_next_billing_at,
            ServiceExtensionEntry.new_next_billing_at,
            ServiceExtension.days,
        )
        .join(
            ServiceExtension,
            ServiceExtension.id == ServiceExtensionEntry.extension_id,
        )
        .where(
            ServiceExtensionEntry.subscriber_id.in_(replay_ids),
            ServiceExtension.status == ServiceExtensionStatus.applied,
            ServiceExtension.applied_at >= LEGACY_FINANCIAL_REPLAY_AT,
            ServiceExtension.applied_at <= snapshot_at,
        )
    )
    for extension_row in extension_rows:
        extension_accounts.add(str(extension_row.subscriber_id))
        if extension_row.previous_next_billing_at and extension_row.new_next_billing_at:
            delta = (
                extension_row.new_next_billing_at
                - extension_row.previous_next_billing_at
            )
            days = max(0, int(delta.total_seconds() // 86400))
        else:
            days = max(0, int(extension_row.days or 0))
        extension_days[str(extension_row.subscription_id)] += days

    services: dict[str, list[_ReplayService]] = defaultdict(list)
    service_stmt = select(
        final_services.c.splynx_service_id,
        final_services.c.subscriber_id,
        final_services.c.subscription_id,
        final_services.c.quantity,
        final_services.c.unit_price,
        final_services.c.start_date,
        final_services.c.last_charge_total,
        final_services.c.last_period_from,
        final_services.c.last_period_to,
        final_services.c.noncharge_transaction_rows,
        final_services.c.noncharge_period_rows,
        final_services.c.last_noncharge_type,
        final_services.c.last_noncharge_source,
        final_services.c.last_noncharge_to_invoice,
        final_services.c.last_noncharge_comment_empty,
        final_services.c.last_noncharge_category_id,
        final_services.c.last_noncharge_total,
        final_services.c.last_noncharge_period_from,
        final_services.c.last_noncharge_period_to,
    ).where(
        final_services.c.subscriber_id.in_(replay_ids),
        final_services.c.source_status == "active",
        final_services.c.source_deleted.is_(False),
    )
    for service_row in db.execute(service_stmt):
        account_id = str(service_row.subscriber_id)
        period_from = service_row.last_period_from
        period_to = service_row.last_period_to
        charge_total = service_row.last_charge_total
        if period_from is None or period_to is None:
            # The retained final source contains two auto-generated first-cycle
            # service debits classified under Splynx category 2 rather than the
            # canonical category 1. Accept only the structural shape that proves
            # this is the exact service charge: one row, one period, debit,
            # auto-generated, marked for invoicing, no operator comment, starts
            # with the service, and equals quantity * source unit price. This is
            # source evidence repair, not a general category-2 fallback.
            quantity = int(service_row.quantity or 0)
            noncanonical_total = (
                _money(service_row.last_noncharge_total)
                if service_row.last_noncharge_total is not None
                else None
            )
            source_unit_price = (
                _money(service_row.unit_price)
                if service_row.unit_price is not None
                else None
            )
            reviewed_misclassified_charge = (
                int(service_row.noncharge_transaction_rows or 0) == 1
                and int(service_row.noncharge_period_rows or 0) == 1
                and service_row.last_noncharge_type == "debit"
                and service_row.last_noncharge_source == "auto"
                and service_row.last_noncharge_to_invoice is True
                and service_row.last_noncharge_comment_empty is True
                and int(service_row.last_noncharge_category_id or 0) == 2
                and quantity > 0
                and noncanonical_total is not None
                and noncanonical_total > ZERO
                and source_unit_price is not None
                and noncanonical_total == _money(source_unit_price * Decimal(quantity))
                and service_row.start_date == service_row.last_noncharge_period_from
                and service_row.last_noncharge_period_from is not None
                and service_row.last_noncharge_period_to is not None
            )
            if reviewed_misclassified_charge:
                period_from = service_row.last_noncharge_period_from
                period_to = service_row.last_noncharge_period_to
                charge_total = noncanonical_total
            elif int(service_row.noncharge_transaction_rows or 0) > 0:
                reason = (
                    "source_service_has_noncanonical_period_evidence"
                    if int(service_row.noncharge_period_rows or 0) > 0
                    else "source_service_has_noncanonical_activity"
                )
                incomplete[account_id].add(reason)
                continue
            else:
                incomplete[account_id].add("source_service_without_paid_through_period")
                continue
        if period_from is None or period_to is None:
            # Kept explicit for type narrowing and fail-closed safety if the
            # reviewed source rule above is changed later.
            incomplete[account_id].add("source_service_without_paid_through_period")
            continue
        cycle_days = (period_to - period_from).days + 1
        if cycle_days <= 0 or cycle_days > 366:
            incomplete[account_id].add("invalid_source_service_cycle")
            continue
        if charge_total is None:
            incomplete[account_id].add("source_service_without_charge")
            continue
        charge = _money(charge_total)
        if charge < ZERO:
            incomplete[account_id].add("source_service_negative_charge")
            continue
        next_due = max(
            period_to + timedelta(days=1),
            LEGACY_FINANCIAL_REPLAY_AT.date(),
        )
        if service_row.subscription_id is not None:
            next_due += timedelta(
                days=extension_days.get(str(service_row.subscription_id), 0)
            )
        elif account_id in extension_accounts:
            incomplete[account_id].add("source_service_not_mapped_to_subscription")
        services[account_id].append(
            _ReplayService(
                service_id=int(service_row.splynx_service_id),
                account_id=account_id,
                subscription_id=(
                    str(service_row.subscription_id)
                    if service_row.subscription_id is not None
                    else None
                ),
                next_due=next_due,
                cycle_days=cycle_days,
                charge=charge,
            )
        )

    entitlement_owned_cycles: dict[tuple[str, date], list[Decimal]] = defaultdict(list)
    replay_subscription_ids = {
        service.subscription_id
        for account_services in services.values()
        for service in account_services
        if service.subscription_id is not None
    }
    if authority_position_at is not None and replay_subscription_ids:
        native_entitlement_rows = db.execute(
            select(
                ServiceEntitlement.subscription_id,
                ServiceEntitlement.starts_at,
                ServiceEntitlement.amount_funded,
                ServiceEntitlement.source_invoice_line_id,
                ServiceEntitlement.source_ledger_entry_id,
                InvoiceLine.amount.label("invoice_line_amount"),
                InvoiceLine.is_active.label("invoice_line_active"),
                Invoice.status.label("invoice_status"),
                Invoice.is_active.label("invoice_active"),
                Invoice.is_proforma.label("invoice_proforma"),
                LedgerEntry.amount.label("ledger_amount"),
                LedgerEntry.entry_type.label("ledger_entry_type"),
                LedgerEntry.source.label("ledger_source"),
                LedgerEntry.is_active.label("ledger_active"),
                LedgerEntry.affects_customer_position.label(
                    "ledger_affects_customer_position"
                ),
                AccountAdjustment.origin.label("ledger_adjustment_origin"),
            )
            .outerjoin(
                InvoiceLine,
                InvoiceLine.id == ServiceEntitlement.source_invoice_line_id,
            )
            .outerjoin(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .outerjoin(
                LedgerEntry,
                LedgerEntry.id == ServiceEntitlement.source_ledger_entry_id,
            )
            .outerjoin(
                AccountAdjustment,
                AccountAdjustment.ledger_entry_id == LedgerEntry.id,
            )
            .where(
                ServiceEntitlement.subscription_id.in_(replay_subscription_ids),
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
                ServiceEntitlement.starts_at <= snapshot_at,
                ServiceEntitlement.created_at <= snapshot_at,
            )
        )
        for entitlement_row in native_entitlement_rows:
            period_start = _aware(entitlement_row.starts_at)
            if period_start is None:
                continue
            entitlement_amount: Decimal | None = None
            if entitlement_row.source_invoice_line_id is not None:
                if not (
                    entitlement_row.invoice_line_active
                    and entitlement_row.invoice_active
                    and not entitlement_row.invoice_proforma
                    and entitlement_row.invoice_status == InvoiceStatus.paid
                ):
                    continue
                entitlement_amount = _money(entitlement_row.invoice_line_amount)
            elif entitlement_row.source_ledger_entry_id is not None:
                if not (
                    entitlement_row.ledger_active
                    and entitlement_row.ledger_affects_customer_position
                    and entitlement_row.ledger_entry_type == LedgerEntryType.debit
                    and (
                        entitlement_row.ledger_source == LedgerSource.invoice
                        or (
                            entitlement_row.ledger_source == LedgerSource.adjustment
                            and entitlement_row.ledger_adjustment_origin
                            == "prepaid_service_renewal"
                        )
                    )
                ):
                    continue
                entitlement_amount = _money(entitlement_row.ledger_amount)
            if entitlement_amount is None or abs(
                entitlement_amount - _money(entitlement_row.amount_funded)
            ) > Decimal("0.01"):
                continue
            entitlement_owned_cycles[
                (str(entitlement_row.subscription_id), period_start.date())
            ].append(entitlement_amount)

    # A native service with no source service id is a real post-handoff domain
    # decision. Until its price/schedule event is replayed explicitly, the
    # account is held out instead of trusting its mutable current fields.
    native_service_accounts = db.scalars(
        select(Subscription.subscriber_id).where(
            Subscription.subscriber_id.in_(replay_ids),
            Subscription.splynx_service_id.is_(None),
            Subscription.created_at >= LEGACY_FINANCIAL_REPLAY_AT,
            Subscription.created_at <= snapshot_at,
        )
    ).all()
    for native_account_id in native_service_accounts:
        incomplete[str(native_account_id)].add("native_service_decision_not_replayed")

    # A pre-handoff subscription with no legacy service identifier is not a
    # native service merely because the identifier is NULL. It needs a retained
    # source-service row mapped to the subscription; otherwise the replay has no
    # paid-through schedule and must fail closed. This catches migration gaps
    # without misclassifying genuine post-handoff services above.
    source_subscription_exists = (
        select(final_services.c.splynx_service_id)
        .where(final_services.c.subscription_id == Subscription.id)
        .exists()
    )
    pre_handoff_source_gaps = db.scalars(
        select(Subscription.subscriber_id).where(
            Subscription.subscriber_id.in_(replay_ids),
            Subscription.billing_mode == BillingMode.prepaid,
            Subscription.status.in_(
                [
                    SubscriptionStatus.active,
                    SubscriptionStatus.suspended,
                    SubscriptionStatus.pending,
                    SubscriptionStatus.blocked,
                ]
            ),
            Subscription.splynx_service_id.is_(None),
            Subscription.created_at < LEGACY_FINANCIAL_REPLAY_AT,
            ~source_subscription_exists,
        )
    ).all()
    for source_gap_account_id in pre_handoff_source_gaps:
        incomplete[str(source_gap_account_id)].add(
            "pre_handoff_service_without_source_evidence"
        )

    plan_event_types = {
        "subscription.activated",
        "subscription.canceled",
        "subscription.created",
        "subscription.downgraded",
        "subscription.plan_changed",
        "subscription.upgraded",
    }
    plan_event_accounts = db.execute(
        select(EventStore.account_id, EventStore.subscriber_id).where(
            EventStore.event_type.in_(plan_event_types),
            EventStore.created_at >= LEGACY_FINANCIAL_REPLAY_AT,
            EventStore.created_at <= snapshot_at,
            or_(
                EventStore.account_id.in_(replay_ids),
                EventStore.subscriber_id.in_(replay_ids),
            ),
        )
    )
    for plan_event_row in plan_event_accounts:
        event_account_id = plan_event_row.account_id or plan_event_row.subscriber_id
        if event_account_id is not None:
            incomplete[str(event_account_id)].add("plan_decision_not_replayed")

    service_cycle_gaps: dict[
        tuple[str, str | None, date, str], _ReplayServiceCycleGap
    ] = {}

    def add_service_cycle_gap(
        account_id: str,
        service: _ReplayService,
        reason: str,
    ) -> None:
        key = (account_id, service.subscription_id, service.next_due, reason)
        service_cycle_gaps.setdefault(
            key,
            _ReplayServiceCycleGap(
                account_id=account_id,
                subscription_id=service.subscription_id,
                period_start=service.next_due,
                period_end=service.next_due + timedelta(days=service.cycle_days),
                amount=service.charge,
                funding_before=positions[account_id],
                currency="NGN",
                reason=reason,
            ),
        )

    def attempt_due(account_id: str, through: date) -> None:
        account_services = services.get(account_id, [])
        if not account_services:
            return
        while True:
            due = sorted(
                (
                    service
                    for service in account_services
                    if service.next_due <= through
                ),
                key=lambda service: (service.next_due, service.service_id),
            )
            if not due:
                return
            balance = positions[account_id]
            due_total = sum((service.charge for service in due), ZERO)
            affordable = [service for service in due if service.charge <= balance]
            if len(due) > 1 and affordable and due_total > balance:
                incomplete[account_id].add("multi_service_renewal_order_ambiguous")

            progressed = False
            for service in due:
                if service.charge > positions[account_id]:
                    continue
                due_at = datetime.combine(service.next_due, time.min, tzinfo=UTC)
                if (
                    service.charge > ZERO
                    and authority_position_at is not None
                    and due_at > authority_position_at
                ):
                    if service.subscription_id is None:
                        reason = "due_service_charge_without_subscription_owner"
                        incomplete[account_id].add(reason)
                        add_service_cycle_gap(account_id, service, reason)
                    else:
                        owned_amounts = entitlement_owned_cycles.get(
                            (service.subscription_id, service.next_due), []
                        )
                        if not owned_amounts:
                            reason = "due_service_charge_without_native_entitlement"
                            incomplete[account_id].add(reason)
                            add_service_cycle_gap(account_id, service, reason)
                        elif len(owned_amounts) != 1 or abs(
                            owned_amounts[0] - service.charge
                        ) > Decimal("0.01"):
                            reason = (
                                "due_service_charge_native_entitlement_amount_mismatch"
                            )
                            incomplete[account_id].add(reason)
                            add_service_cycle_gap(account_id, service, reason)
                positions[account_id] -= service.charge
                service_charges[account_id] += service.charge
                service.next_due += timedelta(days=service.cycle_days)
                progressed = True
            if not progressed:
                return

    for account_id in positions:
        for occurred_at, amount in sorted(financial_events.get(account_id, [])):
            attempt_due(account_id, occurred_at.date())
            positions[account_id] += amount
            attempt_due(account_id, occurred_at.date())
        attempt_due(account_id, snapshot_date)

    return ReconstructedPositions(
        positions=positions,
        service_charges=dict(service_charges),
        post_legacy_credits=dict(post_legacy_credits),
        incomplete={key: set(value) for key, value in incomplete.items()},
        service_cycle_gaps=tuple(
            service_cycle_gaps[key] for key in sorted(service_cycle_gaps)
        ),
    )


def _batch_ledger_credit(
    db: Session, account_ids: Sequence[Any] | None, *, currency: str = "NGN"
) -> dict[str, Decimal]:
    # None means every account (one whole-table aggregate); only an explicitly
    # EMPTY set means there is nothing to do.
    if account_ids is not None and not account_ids:
        return {}
    rows = db.execute(
        select(
            LedgerEntry.account_id,
            LedgerEntry.entry_type,
            func.sum(LedgerEntry.amount).label("amount"),
        )
        .where(
            _account_filter(LedgerEntry.account_id, account_ids),
            LedgerEntry.invoice_id.is_(None),
            LedgerEntry.is_active.is_(True),
            LedgerEntry.currency == currency,
        )
        .group_by(LedgerEntry.account_id, LedgerEntry.entry_type)
    ).all()
    totals: dict[str, Decimal] = {}
    for row in rows:
        key = str(row.account_id)
        signed = (
            _money(row.amount)
            if row.entry_type == LedgerEntryType.credit
            else -_money(row.amount)
        )
        totals[key] = totals.get(key, ZERO) + signed
    return totals


# --------------------------------------------------------------------------
# D1 / D2 — ledger reversal integrity (F1)
# --------------------------------------------------------------------------


def d1_double_swings(db: Session) -> Finding:
    """Reversal entry posted AND the original deactivated.

    ``LedgerEntries.reverse`` (billing/ledger.py:112) does both. Every balance
    reader filters ``is_active``, so the original leaving the sum and the
    reversal entering it move the balance by 2x the amount.

    Only unallocated rows (``invoice_id IS NULL``) reach
    ``get_account_credit_balance`` and ``_ledger_event``, so the balance impact
    is real for those; invoice-allocated pairs are reported separately as
    ``balance_affecting=False``.
    """
    f = Finding("D1", "Ledger double-swing (reversal + deactivated original)", "F1")
    reversals = db.scalars(
        select(LedgerEntry).where(
            LedgerEntry.is_active.is_(True),
            LedgerEntry.memo.ilike("Reversal of ledger entry %"),
        )
    ).all()
    for rev in reversals:
        m = _REVERSAL_OF.search(rev.memo or "")
        if not m:
            continue
        original = db.get(LedgerEntry, m.group(1))
        if original is None or original.is_active:
            continue  # append-only and correct: nothing to fix
        balance_affecting = rev.invoice_id is None and original.invoice_id is None
        amt = _money(rev.amount)
        f.rows.append(
            {
                "account_id": str(rev.account_id),
                "original_id": str(original.id),
                "reversal_id": str(rev.id),
                "entry_type": getattr(original.entry_type, "value", ""),
                "amount": f"{amt:.2f}",
                "currency": rev.currency,
                "balance_affecting": balance_affecting,
                "overswing": f"{amt:.2f}" if balance_affecting else "0.00",
            }
        )
        if balance_affecting:
            f.amount += amt
    f.note = "amount = total over-swing on the credit balance (unallocated pairs only)"
    return f


def d2_unbacked_deactivated_credits(db: Session) -> Finding:
    """Deactivated customer CREDITS that no surviving document accounts for.

    The naive form of this check ("every deactivated ledger row is a silent
    balance move") is badly wrong and was rejected against real data: most
    deactivated pre-cutover rows duplicate the Payment/Invoice documents that
    ``customer_financial_ledger`` actually derives the balance from, so
    deactivating them removed a DOUBLE-COUNT rather than destroying money. On
    staging that benign case was 1,759 of 2,229 accounts.

    The authoritative Splynx cutoff ledger, not the mutable local deposit, owns
    the opening position.  Therefore a deactivated local projection dated before
    native payment activity is harmless when that account has cutoff-ledger
    coverage: the reconstruction does not consume the local projection at all.

    The remaining questions are accounts with no mapped cutoff ledger and
    credits dated after native payment activity began.  Those need independent
    settlement/adjustment provenance; current local balance fields are not
    allowed to validate them.
    """
    f = Finding("D2", "Deactivated credit with no backing payment document", "F1")

    from app.models.splynx_transaction import SplynxBillingTransaction

    payment_accounts = (
        select(Payment.account_id)
        .where(
            Payment.account_id.is_not(None),
            Payment.is_active.is_(True),
            Payment.status == PaymentStatus.succeeded,
        )
        .distinct()
        .subquery()
    )
    rows = db.execute(
        select(
            LedgerEntry.id,
            LedgerEntry.account_id,
            LedgerEntry.amount,
            LedgerEntry.currency,
            LedgerEntry.effective_date,
            LedgerEntry.source,
            LedgerEntry.created_at,
        )
        .outerjoin(
            payment_accounts,
            payment_accounts.c.account_id == LedgerEntry.account_id,
        )
        .where(
            LedgerEntry.is_active.is_(False),
            LedgerEntry.invoice_id.is_(None),
            LedgerEntry.entry_type == LedgerEntryType.credit,
            LedgerEntry.amount > ZERO,
            payment_accounts.c.account_id.is_(None),
        )
    ).all()
    account_ids = sorted({row.account_id for row in rows}, key=str)
    cutoff_by_account: dict[str, Decimal] = {}
    cutoff_table_available = inspect(db.get_bind()).has_table(
        "audit_splynx_cutoff_balances"
    )
    if account_ids and cutoff_table_available:
        cutoff_balances = table(
            "audit_splynx_cutoff_balances",
            column("subscriber_id", Uuid(as_uuid=True)),
            column("cutoff_deposit", Numeric(19, 4)),
        )
        cutoff_rows = db.execute(
            select(
                cutoff_balances.c.subscriber_id,
                cutoff_balances.c.cutoff_deposit,
            ).where(cutoff_balances.c.subscriber_id.in_(account_ids))
        ).all()
        cutoff_by_account = {
            str(row.subscriber_id): _money(row.cutoff_deposit) for row in cutoff_rows
        }
    mirror_by_account: dict[str, tuple[int, Decimal]] = {}
    if account_ids and not cutoff_table_available:
        mirror_rows = db.execute(
            select(
                SplynxBillingTransaction.subscriber_id,
                func.count(SplynxBillingTransaction.id).label("row_count"),
                func.sum(
                    case(
                        (
                            SplynxBillingTransaction.entry_type
                            == LedgerEntryType.credit.value,
                            SplynxBillingTransaction.amount,
                        ),
                        else_=-SplynxBillingTransaction.amount,
                    )
                ).label("mirror_net"),
            )
            .where(
                SplynxBillingTransaction.subscriber_id.in_(account_ids),
                SplynxBillingTransaction.deleted.is_(False),
            )
            .group_by(SplynxBillingTransaction.subscriber_id)
        ).all()
        mirror_by_account = {
            str(row.subscriber_id): (int(row.row_count), _money(row.mirror_net))
            for row in mirror_rows
            if row.subscriber_id is not None
        }

    cutoff_covered_rows = 0
    cutoff_covered_accounts: set[str] = set()
    cutoff_covered_amount = ZERO
    unresolved_accounts: set[str] = set()
    for e in rows:
        amt = _money(e.amount)
        account_id = str(e.account_id)
        mirror_count, mirror_net = mirror_by_account.get(account_id, (0, ZERO))
        has_cutoff = (
            account_id in cutoff_by_account
            if cutoff_table_available
            else mirror_count > 0
        )
        occurred_at = e.effective_date or e.created_at
        is_pre_cutoff_projection = _before(occurred_at, PAYMENT_ACTIVITY_AT)
        if has_cutoff and is_pre_cutoff_projection:
            cutoff_covered_rows += 1
            cutoff_covered_accounts.add(account_id)
            cutoff_covered_amount += amt
            continue
        verdict = (
            "post_cutover_credit_without_payment"
            if is_pre_cutoff_projection is False
            else "no_cutoff_mirror"
        )
        unresolved_accounts.add(account_id)
        f.rows.append(
            {
                "account_id": account_id,
                "entry_id": str(e.id),
                "amount": f"{amt:.2f}",
                "currency": e.currency,
                "effective_date": (
                    e.effective_date.isoformat() if e.effective_date else ""
                ),
                "created_at": e.created_at.isoformat(),
                "source": getattr(e.source, "value", str(e.source)),
                "cutoff_coverage": has_cutoff,
                "cutoff_deposit": (
                    f"{cutoff_by_account[account_id]:.2f}"
                    if account_id in cutoff_by_account
                    else ""
                ),
                "mirror_rows": mirror_count,
                "mirror_net": f"{mirror_net:.2f}",
                "verdict": verdict,
            }
        )
        f.amount += amt
    f.note = (
        f"excluded cutoff-covered={cutoff_covered_rows} rows / "
        f"{len(cutoff_covered_accounts)} accounts / "
        f"NGN {cutoff_covered_amount:,.2f}; "
        f"unresolved={len(f.rows)} rows / {len(unresolved_accounts)} accounts. "
        f"coverage_source={'source_cutoff_deposit' if cutoff_table_available else 'transaction_mirror_only'}. "
        "Current subscriber.deposit is deliberately not consulted: it is an "
        "output under audit, not evidence for the cutoff baseline."
    )
    return f


# --------------------------------------------------------------------------
# D3 / D10 — invoice integrity
# --------------------------------------------------------------------------


def d3_paid_with_balance(db: Session) -> Finding:
    """Invoice marked paid yet still carrying balance_due (F24 — fires on prod)."""
    f = Finding("D3", "Invoice paid but balance_due > 0", "F24")
    rows = db.scalars(
        select(Invoice).where(
            Invoice.is_active.is_(True),
            Invoice.status == InvoiceStatus.paid,
            Invoice.balance_due > 0,
        )
    ).all()
    for inv in rows:
        amt = _money(inv.balance_due)
        f.rows.append(
            {
                "account_id": str(inv.account_id),
                "invoice_id": str(inv.id),
                "total": f"{_money(inv.total):.2f}",
                "balance_due": f"{amt:.2f}",
                "currency": inv.currency,
            }
        )
        f.amount += amt
    return f


def d10_void_with_live_debits(db: Session) -> Finding:
    """Void invoices whose debit ledger entries were never reversed.

    ``Invoices.void`` reverses them; the repair services
    (billing_cleanup_remediation, billing_prepaid_overlap_repair) set status and
    balance_due directly and skip the reversal, so the customer keeps being
    charged in the ledger for an invoice that officially never existed.
    """
    f = Finding("D10", "Void invoice with active debit ledger entries", "F6/void")
    rows = db.execute(
        select(
            Invoice.id,
            Invoice.account_id,
            func.sum(LedgerEntry.amount).label("debit_total"),
            func.count(LedgerEntry.id).label("debit_rows"),
        )
        .join(LedgerEntry, LedgerEntry.invoice_id == Invoice.id)
        .where(
            Invoice.status == InvoiceStatus.void,
            Invoice.is_active.is_(True),
            LedgerEntry.is_active.is_(True),
            LedgerEntry.entry_type == LedgerEntryType.debit,
        )
        .group_by(Invoice.id, Invoice.account_id)
    ).all()
    for r in rows:
        amt = _money(r.debit_total)
        f.rows.append(
            {
                "account_id": str(r.account_id),
                "invoice_id": str(r.id),
                "live_debit_rows": r.debit_rows,
                "live_debit_total": f"{amt:.2f}",
            }
        )
        f.amount += amt
    return f


# --------------------------------------------------------------------------
# D4 / D5 / D6 / D9 — payment integrity
# --------------------------------------------------------------------------


def d4_orphan_payments(db: Session) -> Finding:
    """Succeeded payments with neither an allocation nor a ledger entry (F19)."""
    f = Finding("D4", "Succeeded payment with no allocation and no ledger", "F19")
    alloc_exists = (
        select(PaymentAllocation.id)
        .where(
            PaymentAllocation.payment_id == Payment.id,
            PaymentAllocation.is_active.is_(True),
        )
        .exists()
    )
    ledger_exists = (
        select(LedgerEntry.id)
        .where(
            LedgerEntry.payment_id == Payment.id,
            LedgerEntry.is_active.is_(True),
        )
        .exists()
    )
    rows = db.scalars(
        select(Payment).where(
            Payment.is_active.is_(True),
            Payment.status == PaymentStatus.succeeded,
            ~alloc_exists,
            ~ledger_exists,
        )
    ).all()
    imported = 0
    imported_amount = ZERO
    for p in rows:
        amt = _money(p.amount)
        # A Splynx-imported payment legitimately has no local allocation or
        # ledger entry: it was mirrored, not posted. Counting those as orphans
        # inflates this class by ~3,100 rows / ~NGN 146M and is meaningless.
        # Only a NATIVE payment with no allocation and no ledger is the F19 bug.
        if getattr(p, "splynx_payment_id", None) is not None:
            imported += 1
            imported_amount += amt
            continue
        f.rows.append(
            {
                "account_id": str(p.account_id) if p.account_id else "",
                "payment_id": str(p.id),
                "amount": f"{amt:.2f}",
                "currency": p.currency,
                "paid_at": p.paid_at.isoformat() if p.paid_at else "",
            }
        )
        f.amount += amt
    f.note = (
        "native orphans only — cash that settles nothing and is invisible to "
        f"credit. Excluded {imported:,} splynx-imported payments "
        f"(NGN {imported_amount:,.2f}), which are mirrored by design."
    )
    return f


def d5_misallocated_payments(db: Session) -> Finding:
    """Ledger credits pointing at a different invoice than the live allocation (F3).

    The admin payment-edit path (web_billing_payments.py:379) rewrites the
    allocation without touching the ledger, so the two disagree about which
    invoice the money settled.
    """
    f = Finding("D5", "Ledger invoice_id != live allocation invoice_id", "F3")
    rows = db.execute(
        select(
            Payment.id.label("payment_id"),
            Payment.account_id,
            Payment.amount,
            LedgerEntry.invoice_id.label("ledger_invoice"),
            PaymentAllocation.invoice_id.label("alloc_invoice"),
        )
        .join(
            LedgerEntry,
            and_(
                LedgerEntry.payment_id == Payment.id,
                LedgerEntry.is_active.is_(True),
                LedgerEntry.invoice_id.isnot(None),
            ),
        )
        .join(
            PaymentAllocation,
            and_(
                PaymentAllocation.payment_id == Payment.id,
                PaymentAllocation.is_active.is_(True),
            ),
        )
        .where(
            Payment.is_active.is_(True),
            LedgerEntry.invoice_id != PaymentAllocation.invoice_id,
        )
    ).all()
    for r in rows:
        amt = _money(r.amount)
        f.rows.append(
            {
                "account_id": str(r.account_id) if r.account_id else "",
                "payment_id": str(r.payment_id),
                "amount": f"{amt:.2f}",
                "ledger_invoice_id": str(r.ledger_invoice),
                "allocation_invoice_id": str(r.alloc_invoice),
            }
        )
        f.amount += amt
    f.note = "money double-counted: credited to one invoice, allocated to another"
    return f


def d6_succeeded_without_paid_at(db: Session) -> Finding:
    """Succeeded payments with NULL paid_at — blinds the enforcement health gate."""
    f = Finding("D6", "Succeeded payment with NULL paid_at", "F15/F19")
    rows = db.scalars(
        select(Payment).where(
            Payment.is_active.is_(True),
            Payment.status == PaymentStatus.succeeded,
            Payment.paid_at.is_(None),
        )
    ).all()
    for p in rows:
        amt = _money(p.amount)
        f.rows.append(
            {
                "account_id": str(p.account_id) if p.account_id else "",
                "payment_id": str(p.id),
                "amount": f"{amt:.2f}",
                "created_at": p.created_at.isoformat() if p.created_at else "",
            }
        )
        f.amount += amt
    f.note = "recent-settlement volume is counted by paid_at; NULLs are invisible"
    return f


def d9_pending_money(db: Session) -> Finding:
    """Pending payments that already hold an allocation or a ledger credit (F18)."""
    f = Finding("D9", "Pending payment already holding allocation/ledger", "F18")
    alloc_exists = (
        select(PaymentAllocation.id)
        .where(
            PaymentAllocation.payment_id == Payment.id,
            PaymentAllocation.is_active.is_(True),
        )
        .exists()
    )
    ledger_exists = (
        select(LedgerEntry.id)
        .where(
            LedgerEntry.payment_id == Payment.id,
            LedgerEntry.is_active.is_(True),
        )
        .exists()
    )
    rows = db.scalars(
        select(Payment).where(
            Payment.is_active.is_(True),
            Payment.status == PaymentStatus.pending,
            or_(alloc_exists, ledger_exists),
        )
    ).all()
    for p in rows:
        amt = _money(p.amount)
        f.rows.append(
            {
                "account_id": str(p.account_id) if p.account_id else "",
                "payment_id": str(p.id),
                "amount": f"{amt:.2f}",
                "currency": p.currency,
            }
        )
        f.amount += amt
    f.note = (
        "credit moved but invoices never settle: _recalculate counts succeeded only"
    )
    return f


# --------------------------------------------------------------------------
# D7 / D8 — the two-balance split (F4)
# --------------------------------------------------------------------------


def d7_balance_definition_split(
    db: Session,
    limit: int,
    batch_size: int,
    snapshot_at: datetime | None = None,
) -> Finding:
    """Compare persisted billing outputs with an independent expected position.

    On ordinary staging, where the source tables are unavailable, the detector
    retains its old sign-split diagnostic for performance testing only. On the
    isolated adjudication restore it uses the final Splynx position plus the
    provenance-controlled post-legacy replay as the expected state. Current
    deposit, local documents and ledger credit are three comparison outputs.
    """
    source_replay_available = all(
        inspect(db.get_bind()).has_table(name)
        for name in (
            "audit_splynx_final_balances",
            "audit_splynx_final_services",
        )
    )
    f = Finding(
        "D7",
        (
            "Persisted billing outputs differ from reconstructed position"
            if source_replay_available
            else "Ledger credit and document position disagree in sign"
        ),
        "F4",
    )
    account_ids = db.scalars(
        select(Subscriber.id)
        .where(Subscriber.is_active.is_(True))
        .order_by(Subscriber.id)
    ).all()
    if limit:
        account_ids = account_ids[:limit]

    # One whole-table aggregate per source, not a chunked IN(...) list per batch.
    # The chunked form re-scans ledger_entries once per chunk and blows the
    # statement timeout on the FIRST chunk (measured on staging). The aggregate
    # answers for every account in a single sequential scan. When --limit is set
    # we still scan the whole table and filter in Python: correctness is
    # identical, and the scan is what costs, not the row count returned.
    scan_ids = None if not limit else list(account_ids)
    ledger_by_account = _batch_ledger_credit(db, scan_ids)
    document_positions = _batch_customer_positions(
        db,
        scan_ids,
        currency="NGN",
        use_legacy_mirror=not source_replay_available,
    )

    if source_replay_available:
        if snapshot_at is None:
            raise RuntimeError("D7 source replay requires --snapshot-at")
        replay = _batch_reconstructed_positions(db, scan_ids, snapshot_at=snapshot_at)
        post_legacy_credit_total = sum(replay.post_legacy_credits.values(), ZERO)
        service_charge_total = sum(replay.service_charges.values(), ZERO)
        incomplete_reason_counts: dict[str, int] = defaultdict(int)
        for reasons in replay.incomplete.values():
            for reason in reasons:
                incomplete_reason_counts[reason] += 1
        incomplete_reason_summary = ",".join(
            f"{reason}:{count}"
            for reason, count in sorted(incomplete_reason_counts.items())
        )
        deposits = {
            str(account_id): _money(deposit)
            for account_id, deposit in db.execute(
                select(Subscriber.id, Subscriber.deposit).where(
                    Subscriber.id.in_(account_ids)
                )
            )
        }
        incomplete_accounts = set(replay.incomplete)
        missing_baseline = 0
        complete_replay_accounts = 0
        deposit_drift_count = 0
        deposit_drift_amount = ZERO
        deposit_overcredited_count = 0
        deposit_overcredited_amount = ZERO
        deposit_understated_count = 0
        deposit_understated_amount = ZERO
        document_drift_count = 0
        document_drift_amount = ZERO
        ledger_drift_count = 0
        ledger_drift_amount = ZERO
        for batch in _chunks(account_ids, batch_size):
            for aid in batch:
                account_id = str(aid)
                if account_id in incomplete_accounts:
                    continue
                expected = replay.positions.get(account_id)
                if expected is None:
                    missing_baseline += 1
                    continue
                complete_replay_accounts += 1
                current_deposit = deposits.get(account_id, ZERO)
                ledger_credit = ledger_by_account.get(account_id, ZERO)
                document_position = document_positions.get((account_id, "NGN"), ZERO)
                deposit_drift = current_deposit - expected
                ledger_drift = ledger_credit - expected
                document_drift = document_position - expected
                if abs(deposit_drift) > Decimal("0.01"):
                    deposit_drift_count += 1
                    deposit_drift_amount += abs(deposit_drift)
                    if deposit_drift > ZERO:
                        deposit_overcredited_count += 1
                        deposit_overcredited_amount += deposit_drift
                    else:
                        deposit_understated_count += 1
                        deposit_understated_amount += abs(deposit_drift)
                if abs(document_drift) > Decimal("0.01"):
                    document_drift_count += 1
                    document_drift_amount += abs(document_drift)
                if abs(ledger_drift) > Decimal("0.01"):
                    ledger_drift_count += 1
                    ledger_drift_amount += abs(ledger_drift)
                if all(
                    abs(drift) <= Decimal("0.01")
                    for drift in (deposit_drift, ledger_drift, document_drift)
                ):
                    continue
                f.rows.append(
                    {
                        "account_id": account_id,
                        "expected_position": f"{expected:.2f}",
                        "current_deposit": f"{current_deposit:.2f}",
                        "local_document_position": f"{document_position:.2f}",
                        "ledger_credit": f"{ledger_credit:.2f}",
                        "deposit_drift": f"{deposit_drift:.2f}",
                        "document_drift": f"{document_drift:.2f}",
                        "ledger_drift": f"{ledger_drift:.2f}",
                        "post_legacy_credits": (
                            f"{replay.post_legacy_credits.get(account_id, ZERO):.2f}"
                        ),
                        "derived_service_charges": (
                            f"{replay.service_charges.get(account_id, ZERO):.2f}"
                        ),
                    }
                )
        # The headline money is current persisted deposit drift only. Document
        # and ledger gaps are different projections and are reported separately
        # below; adding or taking their maximum would manufacture a bogus repair
        # total from non-equivalent quantities.
        f.amount = deposit_drift_amount
        f.note = (
            "expected = final Splynx position + proven post-legacy credits - "
            "derived funded service renewals; current deposit/documents/ledger "
            "are outputs only; headline amount is current-deposit absolute drift "
            f"only; complete_replay_accounts={complete_replay_accounts}; "
            f"deposit_drift={deposit_drift_count}/NGN {deposit_drift_amount:.2f}; "
            f"deposit_overcredited={deposit_overcredited_count}/"
            f"NGN {deposit_overcredited_amount:.2f}; "
            f"deposit_understated={deposit_understated_count}/"
            f"NGN {deposit_understated_amount:.2f}; "
            f"document_drift={document_drift_count}/NGN {document_drift_amount:.2f}; "
            f"ledger_drift={ledger_drift_count}/NGN {ledger_drift_amount:.2f}; "
            f"post_legacy_credits={len(replay.post_legacy_credits)}/"
            f"NGN {post_legacy_credit_total:.2f}; "
            f"derived_service_charges={len(replay.service_charges)}/"
            f"NGN {service_charge_total:.2f}; "
            f"incomplete_replay_accounts={len(incomplete_accounts)}; "
            f"incomplete_reasons={incomplete_reason_summary or 'none'}; "
            f"missing_source_baseline={missing_baseline}; snapshot={snapshot_at.isoformat()}"
        )
        return f

    for batch in _chunks(account_ids, batch_size):
        for aid in batch:
            ledger_credit = ledger_by_account.get(str(aid), ZERO)
            doc_position = document_positions.get((str(aid), "NGN"), ZERO)
            disagree = (ledger_credit > ZERO and doc_position < ZERO) or (
                ledger_credit < ZERO and doc_position > ZERO
            )
            if not disagree:
                continue
            gap = abs(ledger_credit - doc_position)
            f.rows.append(
                {
                    "account_id": str(aid),
                    "ledger_credit": f"{ledger_credit:.2f}",
                    "document_position": f"{doc_position:.2f}",
                    "gap": f"{gap:.2f}",
                    "risk": (
                        "wrongly_suspended"
                        if ledger_credit > ZERO > doc_position
                        else "free_service"
                    ),
                }
            )
            f.amount += gap
    f.note = (
        "staging/performance fallback only: no final source replay tables; "
        "sign split only; amount = total absolute gap across disagreeing accounts; "
        f"balance derivation batched {batch_size} accounts at a time"
    )
    return f


def d8_unapplied_credit_notes(db: Session) -> Finding:
    """Issued credit-note remainders without reviewed funding evidence.

    The current owner atomically links every new issuance to its exact funding
    entry. An unapplied remainder is therefore normal and must not be reported
    merely because it is still available. Historical issued notes remain a
    finding only while ``funding_ledger_entry_id`` is NULL; reconciliation must
    review or create that exact evidence rather than infer it from memo/amount.
    """
    f = Finding("D8", "Issued credit note has no funding evidence", "F4")
    rows = db.scalars(
        select(CreditNote).where(
            CreditNote.is_active.is_(True),
            CreditNote.funding_ledger_entry_id.is_(None),
            CreditNote.status.in_(
                [CreditNoteStatus.issued, CreditNoteStatus.partially_applied]
            ),
        )
    ).all()
    for cn in rows:
        remainder = _money(cn.total) - _money(getattr(cn, "applied_total", None))
        if remainder <= ZERO:
            continue
        f.rows.append(
            {
                "account_id": str(cn.account_id),
                "credit_note_id": str(cn.id),
                "status": getattr(cn.status, "value", ""),
                "total": f"{_money(cn.total):.2f}",
                "applied_total": f"{_money(getattr(cn, 'applied_total', None)):.2f}",
                "unapplied": f"{remainder:.2f}",
            }
        )
        f.amount += remainder
    f.note = (
        "issued remainder has no reviewed funding link; funded unapplied credit "
        "is normal and excluded"
    )
    return f


# --------------------------------------------------------------------------
# D11 — cutover opening-balance seed cohort
# --------------------------------------------------------------------------


def d11_opening_debits(db: Session) -> Finding:
    """Active opening-balance debits, split by the sign of subscribers.deposit."""
    f = Finding("D11", "Cutover opening-balance debits by deposit sign", "--")
    rows = db.execute(
        select(
            LedgerEntry.account_id,
            LedgerEntry.id,
            LedgerEntry.amount,
            LedgerEntry.memo,
            Subscriber.deposit,
        )
        .join(Subscriber, Subscriber.id == LedgerEntry.account_id)
        .where(
            LedgerEntry.is_active.is_(True),
            LedgerEntry.invoice_id.is_(None),
            LedgerEntry.entry_type == LedgerEntryType.debit,
            LedgerEntry.source == LedgerSource.adjustment,
            LedgerEntry.memo.ilike("%opening%"),
        )
    ).all()
    buckets: dict[str, Decimal] = {"positive": ZERO, "zero": ZERO, "negative": ZERO}
    for r in rows:
        dep = _money(r.deposit)
        bucket = "positive" if dep > ZERO else ("zero" if dep == ZERO else "negative")
        amt = _money(r.amount)
        buckets[bucket] += amt
        f.rows.append(
            {
                "account_id": str(r.account_id),
                "entry_id": str(r.id),
                "amount": f"{amt:.2f}",
                "deposit": f"{dep:.2f}",
                "deposit_sign": bucket,
                "verdict": {
                    "positive": "HOLD - deposit contradicts the seed",
                    "zero": "likely phantom - reversal nets to deposit",
                    "negative": "KEEP - genuine arrears",
                }[bucket],
            }
        )
        f.amount += amt
    f.note = " | ".join(f"{k}={v:.2f}" for k, v in buckets.items())
    return f


# --------------------------------------------------------------------------
# D12 — enforcement mismatch (F6/F7)
# --------------------------------------------------------------------------


def d12_enforcement_mismatch(
    db: Session,
    limit: int,
    batch_size: int,
    snapshot_at: datetime | None = None,
) -> Finding:
    """Suspended-but-funded, and unfunded-but-active, prepaid accounts.

    Both suspension and restoration now consume the canonical prepaid-funding
    decision. This detector reports persisted enforcement drift in either
    direction without reimplementing that decision account-by-account.
    """
    from app.models.catalog import (
        AccessState,
        BillingMode,
        Subscription,
        SubscriptionStatus,
    )
    from app.models.enforcement_lock import EnforcementLock, EnforcementReason
    from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
    from app.services.prepaid_threshold import resolve_prepaid_thresholds

    f = Finding("D12", "Prepaid enforcement state vs funding", "F6/F7")
    # Money locks only. An admin/fraud/customer_hold lock is a deliberate
    # decision, not a billing mistake, and must not read as "wrongly suspended".
    locked_accounts = {
        str(a)
        for a in db.scalars(
            select(EnforcementLock.subscriber_id).where(
                EnforcementLock.is_active.is_(True),
                EnforcementLock.reason.in_(
                    [EnforcementReason.prepaid, EnforcementReason.overdue]
                ),
            )
        ).all()
    }
    # Restrict the detector to accounts that actually have a current prepaid
    # service. A non-prepaid account below the configured prepaid threshold is
    # not an enforcement mismatch.
    # Group by the identifier, not the entity. Subscriber carries a `json`
    # metadata column and Postgres has no equality operator for json. Derive the
    # served projection in this cohort query so D12 does not pay for a second
    # subscription scan.
    prepaid_rows = db.execute(
        select(
            Subscriber.id,
            func.max(
                case(
                    (
                        or_(
                            Subscription.access_state == AccessState.active.value,
                            and_(
                                Subscription.access_state.is_(None),
                                Subscription.status == SubscriptionStatus.active,
                            ),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("is_served"),
        )
        .join(Subscription, Subscription.subscriber_id == Subscriber.id)
        .where(
            Subscriber.is_active.is_(True),
            or_(
                Subscriber.billing_mode == BillingMode.prepaid,
                Subscription.billing_mode == BillingMode.prepaid,
            ),
            Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES),
        )
        .group_by(Subscriber.id)
        .order_by(Subscriber.id)
    ).all()
    if limit:
        prepaid_rows = prepaid_rows[:limit]
    prepaid_ids = [row.id for row in prepaid_rows]
    served_accounts = {str(row.id) for row in prepaid_rows if bool(row.is_served)}

    source_replay_available = all(
        inspect(db.get_bind()).has_table(name)
        for name in (
            "audit_splynx_final_balances",
            "audit_splynx_final_services",
        )
    )
    incomplete_accounts: set[str] = set()
    by_account: dict[str, list[Decimal]]
    if source_replay_available:
        if snapshot_at is None:
            raise RuntimeError("D12 source replay requires --snapshot-at")
        replay = _batch_reconstructed_positions(
            db,
            None if not limit else list(prepaid_ids),
            snapshot_at=snapshot_at,
        )
        by_account = {
            account_id: [balance] for account_id, balance in replay.positions.items()
        }
        incomplete_accounts = set(replay.incomplete)
    else:
        # Staging/performance fallback. currency=None because enforcement takes
        # the MINIMUM across currencies.
        positions = _batch_customer_positions(
            db, None if not limit else list(prepaid_ids), currency=None
        )
        by_account = {}
        for (account_id, _currency), balance in positions.items():
            by_account.setdefault(account_id, []).append(balance)
    thresholds = resolve_prepaid_thresholds(
        db,
        prepaid_ids,
        now=snapshot_at if source_replay_available else None,
    )

    missing_baseline = 0
    suspended_but_funded = 0
    suspended_but_funded_gap = ZERO
    unfunded_but_active = 0
    unfunded_but_active_gap = ZERO
    unfunded_and_served = 0
    unfunded_and_served_gap = ZERO
    for batch in _chunks(prepaid_ids, batch_size):
        for account_id in batch:
            account_key = str(account_id)
            if account_key in incomplete_accounts:
                continue
            balances = by_account.get(account_key, [])
            if source_replay_available and not balances:
                missing_baseline += 1
                continue
            available = min(balances) if balances else ZERO
            threshold = thresholds.get(account_key, ZERO)
            locked = account_key in locked_accounts
            funded = available >= threshold
            if locked and funded:
                verdict = "suspended_but_funded"
                suspended_but_funded += 1
                suspended_but_funded_gap += abs(available - threshold)
            elif not locked and not funded:
                served = account_key in served_accounts
                verdict = (
                    "unfunded_and_served" if served else "unfunded_without_money_lock"
                )
                unfunded_but_active += 1
                unfunded_but_active_gap += abs(available - threshold)
                if served:
                    unfunded_and_served += 1
                    unfunded_and_served_gap += abs(available - threshold)
            else:
                continue
            f.rows.append(
                {
                    "account_id": account_key,
                    "available": f"{available:.2f}",
                    "threshold": f"{threshold:.2f}",
                    "locked": locked,
                    "served": account_key in served_accounts,
                    "verdict": verdict,
                }
            )
            f.amount += abs(available - threshold)
    f.note = (
        "prepaid cohort only; suspended_but_funded = wrongly cut off; "
        "unfunded_without_money_lock is lock drift; only its served subset is "
        "a current free-service signal; "
        + (
            "funding is reconstructed from the final Splynx position and "
            "proven post-legacy facts; "
            if source_replay_available
            else "staging/performance fallback uses current document balance; "
        )
        + "canonical threshold derivation is batched; "
        + f"suspended_but_funded={suspended_but_funded}/"
        + f"NGN {suspended_but_funded_gap:.2f}; "
        + f"unfunded_but_active={unfunded_but_active}/"
        + f"NGN {unfunded_but_active_gap:.2f}; "
        + f"unfunded_and_served={unfunded_and_served}/"
        + f"NGN {unfunded_and_served_gap:.2f}; "
        + f"incomplete_replay_accounts={len(incomplete_accounts)}; "
        + f"missing_source_baseline={missing_baseline}; "
        f"output iteration chunked {batch_size} accounts at a time"
    )
    return f


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def _write_csv(out_dir: Path, finding: Finding) -> Path | None:
    if not finding.rows:
        return None
    allowed_schemas = EVIDENCE_SCHEMAS.get(finding.code)
    if allowed_schemas is None:
        raise ValueError(
            f"no portable-evidence schema is registered for {finding.code}"
        )
    fields = tuple(finding.rows[0])
    if fields not in allowed_schemas:
        raise ValueError(
            f"{finding.code} evidence fields are not allowlisted: {fields!r}"
        )
    for row in finding.rows:
        if tuple(row) != fields:
            raise ValueError(f"{finding.code} evidence rows do not share one schema")

    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if out_dir.is_symlink() or not out_dir.is_dir():
        raise ValueError(f"refusing unsafe evidence directory: {out_dir}")
    path = out_dir / f"{finding.code}.csv"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(finding.rows)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    return path


def _configure_read_only_session(
    db: Session, *, statement_timeout_ms: int, allow_primary: bool
) -> None:
    """Apply database-side safety rails before the first audit query."""
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    db.execute(text("SET TRANSACTION READ ONLY"))
    db.execute(text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"))
    is_replica = bool(db.scalar(text("SELECT pg_is_in_recovery()")))
    if not is_replica and not allow_primary:
        raise RuntimeError(
            "Refusing to run on a PostgreSQL primary. Use a read replica, or "
            "pass --allow-primary only after explicit production-host approval."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default="billing_alignment", help="directory for the per-class CSVs"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="cap accounts scanned in the per-account classes (D7, D12); 0 = all",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=250,
        help="accounts per batch for D7/D12 balance derivation (default: 250)",
    )
    parser.add_argument(
        "--statement-timeout-ms",
        type=int,
        default=10000,
        help="PostgreSQL statement timeout for each audit query (default: 10000)",
    )
    parser.add_argument(
        "--allow-primary",
        action="store_true",
        help="allow PostgreSQL primary execution after explicit host approval",
    )
    parser.add_argument(
        "--snapshot-at",
        type=datetime.fromisoformat,
        default=None,
        help=(
            "immutable dataset timestamp for source-based D7/D12 replay, "
            "ISO-8601 with offset"
        ),
    )
    parser.add_argument(
        "--only", default="", help="comma-separated class codes, e.g. D1,D3,D12"
    )
    args = parser.parse_args()
    if args.batch_size <= 0:
        parser.error("--batch-size must be greater than zero")
    if args.statement_timeout_ms <= 0:
        parser.error("--statement-timeout-ms must be greater than zero")

    wanted = {c.strip().upper() for c in args.only.split(",") if c.strip()}
    out_dir = Path(args.out)

    checks: list[tuple[str, Callable[[Session], Finding]]] = [
        ("D1", d1_double_swings),
        ("D2", d2_unbacked_deactivated_credits),
        ("D3", d3_paid_with_balance),
        ("D4", d4_orphan_payments),
        ("D5", d5_misallocated_payments),
        ("D6", d6_succeeded_without_paid_at),
        (
            "D7",
            lambda db: d7_balance_definition_split(
                db,
                args.limit,
                args.batch_size,
                snapshot_at=args.snapshot_at,
            ),
        ),
        ("D8", d8_unapplied_credit_notes),
        ("D9", d9_pending_money),
        ("D10", d10_void_with_live_debits),
        ("D11", d11_opening_debits),
        (
            "D12",
            lambda db: d12_enforcement_mismatch(
                db,
                args.limit,
                args.batch_size,
                snapshot_at=args.snapshot_at,
            ),
        ),
    ]

    db = SessionLocal()
    findings: list[Finding] = []
    try:
        _configure_read_only_session(
            db,
            statement_timeout_ms=args.statement_timeout_ms,
            allow_primary=args.allow_primary,
        )
        for code, fn in checks:
            if wanted and code not in wanted:
                continue
            try:
                # A PostgreSQL statement timeout aborts the current savepoint.
                # Containing each class lets the runner roll that one class
                # back and continue without dropping the outer read-only
                # transaction or its safety settings.
                with db.begin_nested():
                    findings.append(fn(db))
            except Exception as exc:  # noqa: BLE001 - report, never abort the pass
                broken = Finding(code, f"FAILED: {exc}", "--")
                broken.error = traceback.format_exc(limit=3)
                findings.append(broken)
    finally:
        # Read-only by construction. Nothing above writes, and this guarantees it.
        db.rollback()
        db.close()

    width = 78
    print("=" * width)
    print("BILLING ALIGNMENT — READ-ONLY RECONCILIATION PASS")
    print("no writes, no commits; see docs/audits/BILLING_SOT_AUDIT_2026-07-12.md")
    print("=" * width)
    print(f"{'':<5}{'ROWS':>8}{'AMOUNT (NGN)':>18}  CLASS")
    print("-" * width)
    for f in findings:
        if f.error:
            print(f"{f.code:<5}{'ERR':>8}{'-':>18}  {f.title}")
            continue
        print(f"{f.code:<5}{f.count:>8}{f.amount:>18,.2f}  {f.title}  [{f.ref}]")
    print("-" * width)

    for f in findings:
        if f.error:
            print(f"\n{f.code} FAILED:\n{f.error}")
            continue
        if not f.rows:
            continue
        path = _write_csv(out_dir, f)
        print(f"\n{f.code}  {f.title}")
        if f.note:
            print(f"      note: {f.note}")
        if path:
            print(f"      csv:  {path}")

    total = sum((f.amount for f in findings if not f.error), ZERO)
    print(f"\ntotal money touched by at least one discrepancy class: NGN {total:,.2f}")
    print("(classes overlap — this is an upper bound, not a sum of distinct money)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
