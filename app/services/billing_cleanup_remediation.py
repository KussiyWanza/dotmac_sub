"""Gated remediation for billing cleanup audit CSVs."""

from __future__ import annotations

import csv
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.billing import (
    CreditNoteApplication,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    PaymentAllocation,
)
from app.models.catalog import (
    AccessCredential,
    BillingMode,
    CatalogOffer,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Subscriber
from app.services.account_lifecycle import (
    SUSPENDED_EQUIVALENT,
    BillingAnchorProjectionCommand,
    BillingAnchorProjectionSource,
    compute_account_status,
    get_active_locks,
    reactivation_blocked_by_active_login,
    resolve_locks_for_trigger,
    restore_subscription,
    stage_subscription_billing_anchor,
)
from app.services.billing._common import _recalculate_invoice_totals
from app.services.billing.invoices import Invoices
from app.services.billing_profile import (
    BillingProfileReason,
    plan_billing_mode_transition,
    resolve_billing_profile,
)
from app.services.billing_statuses import BILLABLE_SUBSCRIBER_STATUSES
from app.services.collections import has_overdue_balance
from app.services.common import coerce_uuid
from app.services.radius import _external_password_row

logger = logging.getLogger(__name__)

_RESOLVED_BY = "billing_cleanup_remediation"
_LOCK_NOTES = "Cleared stale overdue lock from billing cleanup audit"
ANCHOR_REPAIR_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.paid,
    InvoiceStatus.overdue,
)
LINE_REPAIR_INVOICE_STATUSES = (
    InvoiceStatus.draft,
    InvoiceStatus.issued,
    InvoiceStatus.overdue,
)
PREPAID_AR_REPAIR_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.overdue,
)


def load_cleanup_csv(path: str) -> list[dict[str, str]]:
    with open(path, newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _same_datetime(left: datetime | None, right: datetime | None) -> bool:
    return _as_utc(left) == _as_utc(right)


def _refuse(action: str, row: dict[str, str], reason: str) -> dict[str, Any]:
    return {
        "action": action,
        "decision": "refuse",
        "reason": reason,
        "row": row,
    }


def _invoice_financial_activity(db: Session, invoice_id) -> dict[str, int]:
    return {
        "active_allocations": db.query(PaymentAllocation.id)
        .filter(PaymentAllocation.invoice_id == invoice_id)
        .filter(PaymentAllocation.is_active.is_(True))
        .count(),
        "ledger_entries": db.query(LedgerEntry.id)
        .filter(LedgerEntry.invoice_id == invoice_id)
        .filter(LedgerEntry.is_active.is_(True))
        .count(),
        "credit_note_applications": db.query(CreditNoteApplication.id)
        .filter(CreditNoteApplication.invoice_id == invoice_id)
        .count(),
    }


def _has_invoice_financial_activity(db: Session, invoice_id) -> bool:
    return any(_invoice_financial_activity(db, invoice_id).values())


def _metadata_with_cleanup_marker(
    metadata: dict | None, marker: str, payload: dict[str, Any]
) -> dict[str, Any]:
    updated = dict(metadata or {})
    updated[marker] = {
        **payload,
        "at": datetime.now(UTC).isoformat(),
        "source": "billing_cleanup_remediation",
    }
    return updated


def plan_stale_overdue_lock_row(db: Session, row: dict[str, str]) -> dict[str, Any]:
    action = "resolve_stale_overdue_lock"
    lock_id = (row.get("lock_id") or "").strip()
    if not lock_id:
        return _refuse(action, row, "missing_lock_id")
    try:
        lock = db.get(EnforcementLock, coerce_uuid(lock_id))
    except Exception:
        return _refuse(action, row, "bad_lock_id")
    if lock is None:
        return _refuse(action, row, "lock_missing")
    if not lock.is_active:
        return _refuse(action, row, "lock_already_inactive")
    if lock.reason != EnforcementReason.overdue:
        return _refuse(action, row, "lock_not_overdue")
    if row.get("account_id") and str(lock.subscriber_id) != row["account_id"]:
        return _refuse(action, row, "account_id_changed")
    if (
        row.get("subscription_id")
        and str(lock.subscription_id) != row["subscription_id"]
    ):
        return _refuse(action, row, "subscription_id_changed")
    if row.get("source") and lock.source != row["source"]:
        return _refuse(action, row, "lock_source_changed")
    if has_overdue_balance(db, str(lock.subscriber_id)):
        return _refuse(action, row, "account_has_collectible_overdue_ar")

    subscription = db.get(Subscription, lock.subscription_id)
    if subscription is None:
        return _refuse(action, row, "subscription_missing")
    other_locks = sorted(
        {
            active.reason.value
            for active in get_active_locks(db, subscription_id=str(subscription.id))
            if active.id != lock.id and active.reason != EnforcementReason.overdue
        }
    )
    would_restore = subscription.status in SUSPENDED_EQUIVALENT and not other_locks
    if would_restore and reactivation_blocked_by_active_login(db, subscription):
        return _refuse(action, row, "reactivation_blocked_by_active_login")

    return {
        "action": action,
        "decision": "apply",
        "lock_id": str(lock.id),
        "subscriber_id": str(lock.subscriber_id),
        "subscription_id": str(lock.subscription_id),
        "would_restore": would_restore,
        "other_active_locks": other_locks,
        "before": {
            "lock_is_active": lock.is_active,
            "lock_reason": lock.reason.value,
            "lock_source": lock.source,
            "subscription_status": subscription.status.value,
        },
    }


def plan_anchor_row(db: Session, row: dict[str, str]) -> dict[str, Any]:
    action = "advance_prepaid_next_billing_at"
    subscription_id = (row.get("subscription_id") or "").strip()
    paid_through = _parse_datetime(row.get("paid_through"))
    if not subscription_id:
        return _refuse(action, row, "missing_subscription_id")
    if paid_through is None:
        return _refuse(action, row, "bad_paid_through")
    try:
        subscription = db.get(Subscription, coerce_uuid(subscription_id))
    except Exception:
        return _refuse(action, row, "bad_subscription_id")
    if subscription is None:
        return _refuse(action, row, "subscription_missing")
    if subscription.billing_mode != BillingMode.prepaid:
        return _refuse(action, row, "subscription_not_prepaid")
    if row.get("account_id") and str(subscription.subscriber_id) != row["account_id"]:
        return _refuse(action, row, "account_id_changed")
    current = _as_utc(subscription.next_billing_at)
    target = _as_utc(paid_through)
    if target is None:
        return _refuse(action, row, "bad_paid_through")
    if current is not None and current >= target:
        return {
            "action": action,
            "decision": "skip",
            "reason": "already_at_or_after_paid_through",
            "subscription_id": str(subscription.id),
            "before": {"next_billing_at": _iso(subscription.next_billing_at)},
        }
    reviewed_current = _parse_datetime(row.get("current_next_billing_at"))
    if reviewed_current is not None and current != _as_utc(reviewed_current):
        return _refuse(action, row, "next_billing_at_changed_since_audit")

    return {
        "action": action,
        "decision": "apply",
        "subscription_id": str(subscription.id),
        "subscriber_id": str(subscription.subscriber_id),
        "target_next_billing_at": target.isoformat(),
        "before": {"next_billing_at": _iso(subscription.next_billing_at)},
    }


def plan_account_mode_row(db: Session, row: dict[str, str]) -> dict[str, Any]:
    action = "align_account_billing_mode"
    if row.get("issue") != "subscription_vs_account":
        return _refuse(action, row, "not_subscription_vs_account")
    subscriber_id = (row.get("subscriber_id") or "").strip()
    subscription_mode = (row.get("subscription_mode") or "").strip()
    if subscription_mode not in {mode.value for mode in BillingMode}:
        return _refuse(action, row, "bad_subscription_mode")
    if not subscriber_id:
        return _refuse(action, row, "missing_subscriber_id")
    try:
        account = db.get(Subscriber, coerce_uuid(subscriber_id))
    except Exception:
        return _refuse(action, row, "bad_subscriber_id")
    if account is None:
        return _refuse(action, row, "subscriber_missing")

    target_mode = BillingMode(subscription_mode)
    decision = plan_billing_mode_transition(
        resolve_billing_profile(db, account),
        target_mode,
    )
    if decision.reason is BillingProfileReason.ALREADY_ALIGNED:
        return {
            "action": action,
            "decision": "skip",
            "reason": "already_aligned",
            "subscriber_id": str(account.id),
            "before": {"account_billing_mode": account.billing_mode.value},
        }
    if not decision.allowed or decision.requires_subscription_alignment:
        reason = decision.reason or BillingProfileReason.BILLING_MODE_UNRESOLVED
        return _refuse(action, row, reason.value)
    if row.get("account_mode") and account.billing_mode.value != row["account_mode"]:
        return _refuse(action, row, "account_mode_changed_since_audit")
    return {
        "action": action,
        "decision": "apply",
        "subscriber_id": str(account.id),
        "target_billing_mode": target_mode.value,
        "before": {"account_billing_mode": account.billing_mode.value},
    }


def discover_invoice_anchor_rows(
    db: Session, *, run_at: datetime | None = None
) -> list[dict[str, str]]:
    """Find subscriptions whose anchor is behind active invoice-line coverage."""
    effective_run_at = run_at or datetime.now(UTC)
    rows = (
        db.query(Subscription, Subscriber, CatalogOffer, Invoice, InvoiceLine)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .outerjoin(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
        .join(InvoiceLine, InvoiceLine.subscription_id == Subscription.id)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(Subscriber.status.in_(BILLABLE_SUBSCRIBER_STATUSES))
        .filter(Subscription.next_billing_at.isnot(None))
        .filter(Subscription.next_billing_at <= effective_run_at)
        .filter(InvoiceLine.is_active.is_(True))
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.is_proforma.is_(False))
        .filter(Invoice.status.in_(ANCHOR_REPAIR_STATUSES))
        .filter(Invoice.billing_period_end.isnot(None))
        .filter(Invoice.billing_period_end > Subscription.next_billing_at)
        .order_by(
            Subscription.id.asc(),
            Invoice.billing_period_end.desc(),
            Invoice.created_at.desc(),
            Invoice.id.desc(),
        )
        .all()
    )

    latest_by_subscription: dict[str, dict[str, str]] = {}
    for subscription, account, offer, invoice, _line in rows:
        subscription_id = str(subscription.id)
        if subscription_id in latest_by_subscription:
            continue
        account_name = getattr(account, "display_name", "") or ""
        latest_by_subscription[subscription_id] = {
            "issue": "invoice_anchor_behind_paid_through",
            "account_id": str(account.id),
            "account_name": account_name,
            "account_status": account.status.value,
            "subscription_id": subscription_id,
            "subscription_status": subscription.status.value,
            "subscription_mode": subscription.billing_mode.value,
            "offer_id": str(subscription.offer_id),
            "offer_name": offer.name if offer else "",
            "current_next_billing_at": _iso(subscription.next_billing_at),
            "paid_through": _iso(invoice.billing_period_end),
            "latest_invoice_id": str(invoice.id),
            "latest_invoice_number": invoice.invoice_number or "",
            "latest_invoice_status": invoice.status.value,
            "latest_invoice_period_start": _iso(invoice.billing_period_start),
            "latest_invoice_period_end": _iso(invoice.billing_period_end),
        }
    return list(latest_by_subscription.values())


def plan_invoice_anchor_row(db: Session, row: dict[str, str]) -> dict[str, Any]:
    action = "advance_invoice_next_billing_at"
    if row.get("issue") not in {
        "",
        None,
        "invoice_anchor_behind_paid_through",
    }:
        return _refuse(action, row, "not_invoice_anchor_behind_paid_through")
    subscription_id = (row.get("subscription_id") or "").strip()
    target = _parse_datetime(row.get("paid_through"))
    reviewed_current = _parse_datetime(row.get("current_next_billing_at"))
    if not subscription_id:
        return _refuse(action, row, "missing_subscription_id")
    if target is None:
        return _refuse(action, row, "bad_paid_through")
    target_utc = _as_utc(target)
    if target_utc is None:
        return _refuse(action, row, "bad_paid_through")
    try:
        subscription = db.get(Subscription, coerce_uuid(subscription_id))
    except Exception:
        return _refuse(action, row, "bad_subscription_id")
    if subscription is None:
        return _refuse(action, row, "subscription_missing")
    if row.get("account_id") and str(subscription.subscriber_id) != row["account_id"]:
        return _refuse(action, row, "account_id_changed")
    if (
        row.get("subscription_mode")
        and subscription.billing_mode.value != row["subscription_mode"]
    ):
        return _refuse(action, row, "subscription_mode_changed")
    if (
        row.get("subscription_status")
        and subscription.status.value != row["subscription_status"]
    ):
        return _refuse(action, row, "subscription_status_changed")
    current = _as_utc(subscription.next_billing_at)
    if reviewed_current is not None and not _same_datetime(current, reviewed_current):
        return _refuse(action, row, "next_billing_at_changed_since_audit")
    if current is not None and current >= target_utc:
        return {
            "action": action,
            "decision": "skip",
            "reason": "already_at_or_after_paid_through",
            "subscription_id": str(subscription.id),
            "before": {"next_billing_at": _iso(subscription.next_billing_at)},
        }

    invoice_id = (row.get("latest_invoice_id") or "").strip()
    if invoice_id:
        try:
            invoice = db.get(Invoice, coerce_uuid(invoice_id))
        except Exception:
            return _refuse(action, row, "bad_latest_invoice_id")
        if invoice is None:
            return _refuse(action, row, "latest_invoice_missing")
        if (
            not invoice.is_active
            or invoice.is_proforma
            or invoice.status not in ANCHOR_REPAIR_STATUSES
            or not _same_datetime(invoice.billing_period_end, target)
        ):
            return _refuse(action, row, "latest_invoice_changed_since_audit")
        active_line = (
            db.query(InvoiceLine.id)
            .filter(InvoiceLine.invoice_id == invoice.id)
            .filter(InvoiceLine.subscription_id == subscription.id)
            .filter(InvoiceLine.is_active.is_(True))
            .first()
        )
        if active_line is None:
            return _refuse(action, row, "latest_invoice_line_missing")

    return {
        "action": action,
        "decision": "apply",
        "subscription_id": str(subscription.id),
        "subscriber_id": str(subscription.subscriber_id),
        "target_next_billing_at": target_utc.isoformat(),
        "latest_invoice_id": invoice_id,
        "before": {"next_billing_at": _iso(subscription.next_billing_at)},
    }


def plan_disabled_service_line_row(db: Session, row: dict[str, str]) -> dict[str, Any]:
    action = "deactivate_disabled_service_line"
    if row.get("finding_type") != "disabled_service":
        return _refuse(action, row, "not_disabled_service")
    if row.get("proposed_disposition") not in {
        "credit_or_void_required",
        "deactivate_line",
    }:
        return _refuse(action, row, "not_reviewed_for_line_deactivation")
    line_id = (row.get("invoice_line_id") or "").strip()
    if not line_id:
        return _refuse(action, row, "missing_invoice_line_id")
    try:
        line = db.get(InvoiceLine, coerce_uuid(line_id))
    except Exception:
        return _refuse(action, row, "bad_invoice_line_id")
    if line is None or not line.is_active:
        return _refuse(action, row, "line_missing_or_inactive")
    invoice = db.get(Invoice, line.invoice_id)
    if invoice is None or not invoice.is_active:
        return _refuse(action, row, "invoice_missing_or_inactive")
    subscription = db.get(Subscription, line.subscription_id)
    if subscription is None:
        return _refuse(action, row, "subscription_missing")
    if row.get("invoice_id") and str(invoice.id) != row["invoice_id"]:
        return _refuse(action, row, "invoice_id_changed")
    if row.get("subscription_id") and str(subscription.id) != row["subscription_id"]:
        return _refuse(action, row, "subscription_id_changed")
    if row.get("invoice_status") and invoice.status.value != row["invoice_status"]:
        return _refuse(action, row, "invoice_status_changed")
    if subscription.status not in {
        SubscriptionStatus.canceled,
        SubscriptionStatus.expired,
        SubscriptionStatus.disabled,
    }:
        return _refuse(action, row, "subscription_not_terminal")
    ended_at = _as_utc(subscription.canceled_at or subscription.end_at)
    period_start = _as_utc(invoice.billing_period_start)
    if ended_at is None or period_start is None or period_start <= ended_at:
        return _refuse(action, row, "line_no_longer_bills_after_end")
    if invoice.status not in LINE_REPAIR_INVOICE_STATUSES:
        return _refuse(action, row, "invoice_status_not_safe_for_line_deactivation")
    activity = _invoice_financial_activity(db, invoice.id)
    if any(activity.values()):
        return {
            **_refuse(action, row, "invoice_has_financial_activity"),
            "activity": activity,
        }
    return {
        "action": action,
        "decision": "apply",
        "invoice_line_id": str(line.id),
        "invoice_id": str(invoice.id),
        "subscription_id": str(subscription.id),
        "before": {
            "invoice_status": invoice.status.value,
            "invoice_total": str(invoice.total or 0),
            "invoice_balance_due": str(invoice.balance_due or 0),
            "line_amount": str(line.amount or 0),
        },
    }


def _plan_duplicate_group(
    db: Session, group_key: str, rows: list[dict[str, str]]
) -> list[dict[str, Any]]:
    action = "deactivate_duplicate_period_line"
    if len(rows) < 2:
        return [_refuse(action, rows[0], "duplicate_group_has_single_row")]
    loaded: list[tuple[dict[str, str], InvoiceLine, Invoice]] = []
    for row in rows:
        if row.get("finding_type") != "duplicate_period":
            return [_refuse(action, row, "not_duplicate_period")]
        if row.get("proposed_disposition") not in {
            "duplicate_review",
            "deactivate_duplicate_line",
        }:
            return [_refuse(action, row, "not_reviewed_for_duplicate_deactivation")]
        line_id = (row.get("invoice_line_id") or "").strip()
        try:
            line = db.get(InvoiceLine, coerce_uuid(line_id))
        except Exception:
            return [_refuse(action, row, "bad_invoice_line_id")]
        if line is None or not line.is_active:
            return [_refuse(action, row, "line_missing_or_inactive")]
        invoice = db.get(Invoice, line.invoice_id)
        if invoice is None or not invoice.is_active:
            return [_refuse(action, row, "invoice_missing_or_inactive")]
        if row.get("invoice_status") and invoice.status.value != row["invoice_status"]:
            return [_refuse(action, row, "invoice_status_changed")]
        if invoice.status not in LINE_REPAIR_INVOICE_STATUSES:
            return [
                _refuse(action, row, "invoice_status_not_safe_for_line_deactivation")
            ]
        activity = _invoice_financial_activity(db, invoice.id)
        if any(activity.values()):
            item = _refuse(action, row, "invoice_has_financial_activity")
            item["activity"] = activity
            return [item]
        current_key = "|".join(
            [
                str(line.subscription_id),
                _iso(invoice.billing_period_start),
                _iso(invoice.billing_period_end),
                line.description or "",
            ]
        )
        if current_key != group_key:
            return [_refuse(action, row, "duplicate_group_changed")]
        loaded.append((row, line, invoice))

    loaded.sort(key=lambda item: (item[1].created_at, str(item[1].id)))
    keep = loaded[0][1]
    items: list[dict[str, Any]] = []
    for row, line, invoice in loaded[1:]:
        items.append(
            {
                "action": action,
                "decision": "apply",
                "duplicate_group_key": group_key,
                "kept_invoice_line_id": str(keep.id),
                "invoice_line_id": str(line.id),
                "invoice_id": str(invoice.id),
                "subscription_id": str(line.subscription_id),
                "before": {
                    "invoice_status": invoice.status.value,
                    "invoice_total": str(invoice.total or 0),
                    "invoice_balance_due": str(invoice.balance_due or 0),
                    "line_amount": str(line.amount or 0),
                },
            }
        )
    return items


def plan_orphan_addon_row(db: Session, row: dict[str, str]) -> dict[str, Any]:
    action = "end_orphan_recurring_addon"
    add_on_row_id = (row.get("subscription_add_on_id") or "").strip()
    if not add_on_row_id:
        return _refuse(action, row, "missing_subscription_add_on_id")
    try:
        sub_addon = db.get(SubscriptionAddOn, coerce_uuid(add_on_row_id))
    except Exception:
        return _refuse(action, row, "bad_subscription_add_on_id")
    if sub_addon is None:
        return _refuse(action, row, "subscription_add_on_missing")
    subscription = db.get(Subscription, sub_addon.subscription_id)
    if subscription is None:
        return _refuse(action, row, "subscription_missing")
    if row.get("subscription_id") and str(subscription.id) != row["subscription_id"]:
        return _refuse(action, row, "subscription_id_changed")
    if subscription.status not in {
        SubscriptionStatus.canceled,
        SubscriptionStatus.expired,
        SubscriptionStatus.disabled,
    }:
        return _refuse(action, row, "parent_subscription_not_terminal")
    target = _as_utc(subscription.canceled_at or subscription.end_at)
    if target is None:
        return _refuse(action, row, "parent_end_missing")
    current_end = _as_utc(sub_addon.end_at)
    if current_end is not None and current_end <= target:
        return {
            "action": action,
            "decision": "skip",
            "reason": "already_ended_at_or_before_parent_end",
            "subscription_add_on_id": str(sub_addon.id),
        }
    reviewed_current = _parse_datetime(row.get("current_end_at"))
    if reviewed_current is not None and not _same_datetime(
        current_end, reviewed_current
    ):
        return _refuse(action, row, "addon_end_changed_since_audit")
    return {
        "action": action,
        "decision": "apply",
        "subscription_add_on_id": str(sub_addon.id),
        "subscription_id": str(subscription.id),
        "target_end_at": target.isoformat(),
        "before": {"end_at": _iso(sub_addon.end_at)},
    }


def plan_missing_radius_row(db: Session, row: dict[str, str]) -> dict[str, Any]:
    action = "sync_missing_radius_subscription"
    subscription_id = (row.get("subscription_id") or "").strip()
    if not subscription_id:
        return _refuse(action, row, "missing_subscription_id")
    try:
        subscription = db.get(Subscription, coerce_uuid(subscription_id))
    except Exception:
        return _refuse(action, row, "bad_subscription_id")
    if subscription is None:
        return _refuse(action, row, "subscription_missing")
    if subscription.status != SubscriptionStatus.active:
        return _refuse(action, row, "subscription_not_active")
    login = (subscription.login or "").strip()
    if not login:
        return _refuse(action, row, "subscription_login_missing")
    if row.get("login") and row["login"] != login:
        return _refuse(action, row, "login_changed_since_audit")
    credential = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscription.subscriber_id)
        .filter(AccessCredential.username == login)
        .filter(AccessCredential.is_active.is_(True))
        .first()
    )
    usable = (
        credential is not None
        and _external_password_row(
            credential,
            default_attribute="Cleartext-Password",
            default_op=":=",
        )
        is not None
    )
    if not usable:
        return _refuse(action, row, "credential_unusable_requires_password_reset")
    assert credential is not None
    return {
        "action": action,
        "decision": "apply",
        "subscription_id": str(subscription.id),
        "subscriber_id": str(subscription.subscriber_id),
        "login": login,
        "credential_id": str(credential.id),
    }


def plan_prepaid_collectible_ar_row(db: Session, row: dict[str, str]) -> dict[str, Any]:
    action = "retire_unfunded_prepaid_ar"
    invoice_id = (row.get("invoice_id") or "").strip()
    if not invoice_id:
        return _refuse(action, row, "missing_invoice_id")
    try:
        invoice = db.get(Invoice, coerce_uuid(invoice_id))
    except Exception:
        return _refuse(action, row, "bad_invoice_id")
    if invoice is None or not invoice.is_active:
        return _refuse(action, row, "invoice_missing_or_inactive")
    if row.get("invoice_status") and invoice.status.value != row["invoice_status"]:
        return _refuse(action, row, "invoice_status_changed")
    if invoice.status == InvoiceStatus.partially_paid:
        return _refuse(action, row, "partially_paid_requires_manual_review")
    if invoice.status not in PREPAID_AR_REPAIR_STATUSES:
        return _refuse(action, row, "invoice_status_not_repairable_prepaid_ar")
    if _has_invoice_financial_activity(db, invoice.id):
        return _refuse(action, row, "invoice_has_financial_activity")
    account = db.get(Subscriber, invoice.account_id)
    has_prepaid_subscription = (
        db.query(Subscription.id)
        .filter(Subscription.subscriber_id == invoice.account_id)
        .filter(Subscription.billing_mode == BillingMode.prepaid)
        .filter(
            Subscription.status.in_(
                [
                    SubscriptionStatus.active,
                    SubscriptionStatus.pending,
                    SubscriptionStatus.suspended,
                    SubscriptionStatus.blocked,
                ]
            )
        )
        .first()
        is not None
    )
    if not (
        account is not None
        and (account.billing_mode == BillingMode.prepaid or has_prepaid_subscription)
    ):
        return _refuse(action, row, "account_no_longer_prepaid")
    return {
        "action": action,
        "decision": "apply",
        "invoice_id": str(invoice.id),
        "subscriber_id": str(invoice.account_id),
        "before": {
            "invoice_status": invoice.status.value,
            "balance_due": str(invoice.balance_due or 0),
        },
    }


def plan_prepaid_overlap_row(db: Session, row: dict[str, str]) -> dict[str, Any]:
    action = "void_safe_prepaid_overlap_invoice"
    if row.get("action") != "void_unpaid_invoice":
        return _refuse(action, row, "overlap_requires_manual_review")
    invoice_id = (row.get("bad_invoice_id") or "").strip()
    if not invoice_id:
        return _refuse(action, row, "missing_bad_invoice_id")
    try:
        invoice = db.get(Invoice, coerce_uuid(invoice_id))
    except Exception:
        return _refuse(action, row, "bad_invoice_id")
    if invoice is None or not invoice.is_active:
        return _refuse(action, row, "invoice_missing_or_inactive")
    if (
        row.get("bad_invoice_status")
        and invoice.status.value != row["bad_invoice_status"]
    ):
        return _refuse(action, row, "invoice_status_changed")
    if _has_invoice_financial_activity(db, invoice.id):
        return _refuse(action, row, "invoice_has_financial_activity")
    if invoice.status == InvoiceStatus.paid:
        return _refuse(action, row, "paid_invoice_not_voidable")
    return {
        "action": action,
        "decision": "apply",
        "invoice_id": str(invoice.id),
        "subscriber_id": str(invoice.account_id),
        "valid_paid_invoice_id": row.get("valid_paid_invoice_id") or "",
        "paid_through": row.get("corrected_next_billing_at") or "",
        "before": {
            "invoice_status": invoice.status.value,
            "balance_due": str(invoice.balance_due or 0),
        },
    }


def plan_cleanup_remediation(
    db: Session,
    *,
    stale_lock_rows: list[dict[str, str]] | None = None,
    anchor_rows: list[dict[str, str]] | None = None,
    mode_rows: list[dict[str, str]] | None = None,
    invoice_anchor_rows: list[dict[str, str]] | None = None,
    prepaid_ar_rows: list[dict[str, str]] | None = None,
    prepaid_overlap_rows: list[dict[str, str]] | None = None,
    disabled_line_rows: list[dict[str, str]] | None = None,
    duplicate_line_rows: list[dict[str, str]] | None = None,
    orphan_addon_rows: list[dict[str, str]] | None = None,
    missing_radius_rows: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    items.extend(plan_stale_overdue_lock_row(db, row) for row in stale_lock_rows or [])
    items.extend(plan_anchor_row(db, row) for row in anchor_rows or [])
    items.extend(plan_account_mode_row(db, row) for row in mode_rows or [])
    items.extend(plan_invoice_anchor_row(db, row) for row in invoice_anchor_rows or [])
    items.extend(
        plan_prepaid_collectible_ar_row(db, row) for row in prepaid_ar_rows or []
    )
    items.extend(
        plan_prepaid_overlap_row(db, row) for row in prepaid_overlap_rows or []
    )
    items.extend(
        plan_disabled_service_line_row(db, row) for row in disabled_line_rows or []
    )
    items.extend(plan_orphan_addon_row(db, row) for row in orphan_addon_rows or [])
    items.extend(plan_missing_radius_row(db, row) for row in missing_radius_rows or [])

    duplicate_groups: dict[str, list[dict[str, str]]] = {}
    for row in duplicate_line_rows or []:
        group_key = (row.get("duplicate_group_key") or "").strip()
        if not group_key:
            items.append(
                _refuse(
                    "deactivate_duplicate_period_line",
                    row,
                    "missing_duplicate_group_key",
                )
            )
            continue
        duplicate_groups.setdefault(group_key, []).append(row)
    for group_key, rows in duplicate_groups.items():
        items.extend(_plan_duplicate_group(db, group_key, rows))

    from collections import Counter

    by_decision = Counter(item["decision"] for item in items)
    by_action = Counter(item["action"] for item in items if item["decision"] == "apply")
    return {
        "items": items,
        "counts": {
            "apply": by_decision.get("apply", 0),
            "skip": by_decision.get("skip", 0),
            "refuse": by_decision.get("refuse", 0),
            "by_action": dict(by_action),
        },
    }


def apply_cleanup_remediation(
    db: Session, plan: dict[str, Any], *, dry_run: bool = True
) -> dict[str, Any]:
    applied: list[dict[str, Any]] = []
    errors = 0
    for item in plan["items"]:
        if item["decision"] != "apply":
            continue
        if dry_run:
            applied.append({**item, "applied": False})
            continue
        try:
            after = _execute_item(db, item)
            db.commit()
            applied.append({**item, "applied": True, "after": after})
        except Exception:
            db.rollback()
            errors += 1
            logger.exception("billing cleanup remediation failed: %s", item)
            applied.append({**item, "applied": False, "error": True})
    return {
        "dry_run": dry_run,
        "applied": applied,
        "errors": errors,
        "applied_count": sum(1 for item in applied if item.get("applied")),
    }


def _execute_item(db: Session, item: dict[str, Any]) -> dict[str, Any]:
    if item["action"] == "resolve_stale_overdue_lock":
        subscription = db.get(Subscription, coerce_uuid(item["subscription_id"]))
        if subscription is None:
            raise ValueError(f"subscription missing: {item['subscription_id']}")
        if item["before"]["subscription_status"] in {
            status.value for status in SUSPENDED_EQUIVALENT
        }:
            restored = restore_subscription(
                db,
                item["subscription_id"],
                trigger="admin",
                resolved_by=_RESOLVED_BY,
                reason=EnforcementReason.overdue,
                notes=_LOCK_NOTES,
            )
        else:
            resolve_locks_for_trigger(
                db,
                subscription,
                trigger="admin",
                resolved_by=_RESOLVED_BY,
                reason=EnforcementReason.overdue,
                notes=_LOCK_NOTES,
            )
            compute_account_status(db, item["subscriber_id"])
            restored = False
        db.flush()
        lock = db.get(EnforcementLock, coerce_uuid(item["lock_id"]))
        subscription = db.get(Subscription, coerce_uuid(item["subscription_id"]))
        return {
            "lock_is_active": bool(lock and lock.is_active),
            "subscription_status": subscription.status.value if subscription else "",
            "restored": restored,
        }
    if item["action"] == "advance_prepaid_next_billing_at":
        subscription = db.get(Subscription, coerce_uuid(item["subscription_id"]))
        if subscription is None:
            raise ValueError(f"subscription missing: {item['subscription_id']}")
        target = _parse_datetime(item["target_next_billing_at"])
        if target is None:
            raise ValueError(f"bad target_next_billing_at: {item['subscription_id']}")
        stage_subscription_billing_anchor(
            db,
            subscription,
            BillingAnchorProjectionCommand(
                subscription_id=subscription.id,
                expected_previous=subscription.next_billing_at,
                target=target,
                source=BillingAnchorProjectionSource.reviewed_reconciliation,
                evidence_ref=f"billing-cleanup:{item['action']}:{subscription.id}",
            ),
        )
        return {"next_billing_at": _iso(subscription.next_billing_at)}
    if item["action"] == "align_account_billing_mode":
        account = db.get(Subscriber, coerce_uuid(item["subscriber_id"]))
        if account is None:
            raise ValueError(f"subscriber missing: {item['subscriber_id']}")
        target_mode = BillingMode(item["target_billing_mode"])
        decision = plan_billing_mode_transition(
            resolve_billing_profile(db, account),
            target_mode,
        )
        if not decision.allowed or decision.requires_subscription_alignment:
            reason = decision.reason or BillingProfileReason.BILLING_MODE_UNRESOLVED
            raise ValueError(f"billing mode transition rejected: {reason.value}")
        account.billing_mode = target_mode
        db.flush()
        return {"account_billing_mode": account.billing_mode.value}
    if item["action"] == "advance_invoice_next_billing_at":
        subscription = db.get(Subscription, coerce_uuid(item["subscription_id"]))
        if subscription is None:
            raise ValueError(f"subscription missing: {item['subscription_id']}")
        target = _parse_datetime(item["target_next_billing_at"])
        if target is None:
            raise ValueError(f"bad target_next_billing_at: {item['subscription_id']}")
        stage_subscription_billing_anchor(
            db,
            subscription,
            BillingAnchorProjectionCommand(
                subscription_id=subscription.id,
                expected_previous=subscription.next_billing_at,
                target=target,
                source=BillingAnchorProjectionSource.reviewed_reconciliation,
                evidence_ref=f"billing-cleanup:{item['action']}:{subscription.id}",
            ),
        )
        return {"next_billing_at": _iso(subscription.next_billing_at)}
    if item["action"] in {
        "deactivate_disabled_service_line",
        "deactivate_duplicate_period_line",
    }:
        line = db.get(InvoiceLine, coerce_uuid(item["invoice_line_id"]))
        if line is None:
            raise ValueError(f"invoice line missing: {item['invoice_line_id']}")
        invoice = db.get(Invoice, line.invoice_id)
        if invoice is None:
            raise ValueError(f"invoice missing for line: {item['invoice_line_id']}")
        marker = (
            "disabled_service_line_cleanup"
            if item["action"] == "deactivate_disabled_service_line"
            else "duplicate_period_line_cleanup"
        )
        line.is_active = False
        line.metadata_ = _metadata_with_cleanup_marker(
            line.metadata_,
            marker,
            {
                "action": item["action"],
                "invoice_line_id": str(line.id),
                "kept_invoice_line_id": item.get("kept_invoice_line_id", ""),
            },
        )
        invoice.metadata_ = _metadata_with_cleanup_marker(
            invoice.metadata_,
            marker,
            {"action": item["action"], "invoice_line_id": str(line.id)},
        )
        db.flush()
        _recalculate_invoice_totals(db, invoice)
        active_lines = (
            db.query(InvoiceLine.id)
            .filter(InvoiceLine.invoice_id == invoice.id)
            .filter(InvoiceLine.is_active.is_(True))
            .count()
        )
        if active_lines == 0:
            Invoices.void_system(
                db,
                str(invoice.id),
                reason=f"Invoice void after {marker}",
                idempotency_key=f"billing-cleanup-empty-invoice-{invoice.id}",
                commit=False,
            )
        db.flush()
        return {
            "invoice_status": invoice.status.value,
            "invoice_subtotal": str(invoice.subtotal or 0),
            "invoice_total": str(invoice.total or 0),
            "invoice_balance_due": str(invoice.balance_due or 0),
            "line_is_active": line.is_active,
            "active_lines": active_lines,
        }
    if item["action"] == "end_orphan_recurring_addon":
        sub_addon = db.get(
            SubscriptionAddOn, coerce_uuid(item["subscription_add_on_id"])
        )
        if sub_addon is None:
            raise ValueError(
                f"subscription add-on missing: {item['subscription_add_on_id']}"
            )
        target = _parse_datetime(item["target_end_at"])
        if target is None:
            raise ValueError(f"bad target_end_at: {item['subscription_add_on_id']}")
        sub_addon.end_at = target
        db.flush()
        return {"end_at": _iso(sub_addon.end_at)}
    if item["action"] == "sync_missing_radius_subscription":
        from app.services.radius import reconcile_subscription_connectivity

        return reconcile_subscription_connectivity(
            db, item["subscription_id"]
        ).as_dict()
    if item["action"] == "retire_unfunded_prepaid_ar":
        invoice = db.get(Invoice, coerce_uuid(item["invoice_id"]))
        if invoice is None:
            raise ValueError(f"invoice missing: {item['invoice_id']}")
        Invoices.return_unfunded_prepaid_to_draft_system(
            db,
            str(invoice.id),
            reason="billing_cleanup_retire_unfunded_prepaid_ar",
        )
        invoice.metadata_ = _metadata_with_cleanup_marker(
            invoice.metadata_,
            "prepaid_phantom_ar_cleanup",
            {"action": "draft_unfunded", "invoice_id": str(invoice.id)},
        )
        db.flush()
        return {
            "invoice_status": invoice.status.value,
            "balance_due": str(invoice.balance_due or 0),
            "due_at": _iso(invoice.due_at),
        }
    if item["action"] == "void_safe_prepaid_overlap_invoice":
        invoice = db.get(Invoice, coerce_uuid(item["invoice_id"]))
        if invoice is None:
            raise ValueError(f"invoice missing: {item['invoice_id']}")
        Invoices.void_system(
            db,
            str(invoice.id),
            reason="Voided by prepaid overlap cleanup",
            idempotency_key=f"billing-cleanup-overlap-void-{invoice.id}",
            commit=False,
        )
        invoice.metadata_ = _metadata_with_cleanup_marker(
            invoice.metadata_,
            "prepaid_overlap_cleanup",
            {
                "action": "void_unpaid_invoice",
                "invoice_id": str(invoice.id),
                "valid_paid_invoice_id": item.get("valid_paid_invoice_id", ""),
                "paid_through": item.get("paid_through", ""),
            },
        )
        db.flush()
        return {
            "invoice_status": invoice.status.value,
            "balance_due": str(invoice.balance_due or 0),
            "due_at": _iso(invoice.due_at),
        }
    raise ValueError(f"unknown cleanup action {item['action']}")
