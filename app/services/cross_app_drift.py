"""Cross-app drift detector — runner, check registry, and checks.

Read-only. Each check answers one yes/no business rule and yields ``Finding``s;
the runner persists them by a stable fingerprint so the same drift is one row
tracked across runs (new / recurring / worsened / resolved / waived). It never
heals — every finding names the reconciler that should.

Scope note: dotmac_sub has no read path to ERP, so the first identity check
covers the CRM↔sub axis (the I-1 duplicate). The sub↔ERP leg and the money /
reseller / asset checks need an ERP read endpoint (or a dedicated ops service);
adding a check is just a new class in ``DEFAULT_CHECKS``.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from urllib.parse import urlencode

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models.billing import (
    CreditNoteApplication,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.models.catalog import (
    AccessCredential,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.cross_app_drift import (
    EVENT_CREATED,
    EVENT_RECURRING,
    EVENT_REOPENED,
    EVENT_RESOLVED,
    EVENT_WORSENED,
    RUN_COMPLETED,
    RUN_RUNNING,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    SEVERITY_ORDER,
    STATUS_OPEN,
    STATUS_RESOLVED,
    STATUS_WAIVED,
    CrossAppDriftFinding,
    CrossAppDriftFindingEvent,
    CrossAppDriftRun,
    CrossAppDriftWaiver,
)
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.services.common import round_money, to_decimal

logger = logging.getLogger(__name__)

# A subscriber holding any of these is legitimately entitled to service.
_SERVICEABLE_STATUSES = {SubscriptionStatus.active, SubscriptionStatus.pending}
# ...and any of these means the subscriber should NOT be able to use service.
_NON_SERVICEABLE_STATUSES = {
    SubscriptionStatus.suspended,
    SubscriptionStatus.blocked,
    SubscriptionStatus.stopped,
    SubscriptionStatus.disabled,
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
    SubscriptionStatus.archived,
}
# Grace so normal async enforcement lag (suspend -> CoA kick) isn't flagged.
_ENFORCEMENT_GRACE = timedelta(minutes=15)
_MONEY_TOLERANCE = Decimal("0.01")
_POSTED_INVOICE_STATUSES = {
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.paid,
    InvoiceStatus.overdue,
    InvoiceStatus.written_off,
}
_OPEN_INVOICE_STATUSES = {
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
}
_RECONCILIATION_HOLD_INVOICE_STATUSES = {
    InvoiceStatus.draft,
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
}
_RECONCILIATION_HOLD_REVIEW_WINDOW = timedelta(hours=48)


def _money(value) -> Decimal:  # noqa: ANN001
    return round_money(to_decimal(value))


def _money_differs(left, right) -> bool:  # noqa: ANN001
    return abs(_money(left) - _money(right)) > _MONEY_TOLERANCE


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _parse_metadata_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _metadata_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


@dataclass
class Finding:
    """One disagreement a check found this run."""

    check_name: str
    entity_type: str
    canonical_entity_id: str
    mismatch_type: str
    severity: str
    details: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        raw = "|".join(
            (
                self.check_name,
                self.entity_type,
                self.canonical_entity_id,
                self.mismatch_type,
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DriftCheck(Protocol):
    name: str

    def run(self, db: Session) -> Iterable[Finding]: ...


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


class IdentityCardinalityCheck:
    """CRM person ↔ sub subscriber cardinality (the axis sub can see today).

    Flags a CRM person mapped to more than one ACTIVE sub subscriber — the I-1
    duplicate that fragments billing / AR / the NCC subscriber count downstream.
    The advisory lock now prevents *new* ones; this proves none slipped and
    surfaces any that predate the lock. (The sub↔ERP leg needs an ERP read path.)
    """

    name = "identity_cardinality"

    def run(self, db: Session) -> Iterable[Finding]:
        rows = db.execute(
            select(
                Subscriber.metadata_["crm_person_id"].as_string(),
                Subscriber.id,
            ).where(Subscriber.is_active.is_(True))
        ).all()
        by_person: dict[str, list[str]] = {}
        for person_id, sub_id in rows:
            if not person_id:
                continue
            by_person.setdefault(str(person_id), []).append(str(sub_id))

        for person_id, sub_ids in by_person.items():
            if len(sub_ids) <= 1:
                continue
            yield Finding(
                check_name=self.name,
                entity_type="crm_person",
                canonical_entity_id=person_id,
                mismatch_type="duplicate_sub_subscriber",
                severity=SEVERITY_HIGH,
                evidence={
                    "crm_person_id": person_id,
                    "sub_subscriber_ids": sorted(sub_ids),
                    "sub_subscriber_count": len(sub_ids),
                },
                details={
                    "suggested_owner": "sub CRM customer create-path (dedup)",
                    "suggested_action": (
                        "Merge the duplicate subscribers for this CRM person and "
                        "reconcile their invoices/payments; then add the unique key."
                    ),
                },
            )


class ServiceEnforcementCheck:
    """Billing/subscription status ↔ RADIUS access state, self-contained in sub.

    Three distinct mismatch fingerprints so ownership + remediation stay obvious:

    * ``suspended_but_online`` (critical) — a subscriber with no serviceable
      subscription still has a live RADIUS session. Live unauthorized service /
      revenue leak; a grace window absorbs normal enforcement lag.
    * ``active_but_blocked`` (high) — a subscriber walled-gardened at the BNG
      (``status='blocked'``) while its subscriptions are all active. Paid but cut
      off — the account_status reconciler's cohort.
    * ``throttle_profile_mismatch`` (medium) — an active credential points at a
      missing/inactive RADIUS profile, so the intended profile (a throttle
      included) silently won't apply. Config drift, no immediate money impact.
    * ``sellable_capped_offer_cannot_enforce`` (high) — an offer in a family
      defined by its cap is on sale with no active ``reduce_speed`` rule. Sold
      on a limit that will never fire.
    """

    name = "service_enforcement"

    def run(self, db: Session) -> Iterable[Finding]:
        now = datetime.now(UTC)
        yield from self._suspended_but_online(db, now)
        yield from self._active_but_blocked(db)
        yield from self._throttle_profile_mismatch(db)
        yield from self._sellable_capped_offer_cannot_enforce(db)

    def _suspended_but_online(self, db: Session, now: datetime) -> Iterable[Finding]:
        grace_cutoff = now - _ENFORCEMENT_GRACE
        # Online subscribers (fresh RADIUS session) + their live-session counts.
        from app.services.topology.health_classifier import _fresh

        session_rows = db.execute(
            select(RadiusActiveSession.subscriber_id, func.count())
            .where(_fresh(now))
            .group_by(RadiusActiveSession.subscriber_id)
        ).all()
        session_counts = {sid: int(cnt) for sid, cnt in session_rows if sid}
        if not session_counts:
            return

        sub_rows = db.execute(
            select(
                Subscription.subscriber_id,
                Subscription.status,
                Subscription.updated_at,
            ).where(Subscription.subscriber_id.in_(list(session_counts)))
        ).all()
        by_subscriber: dict = defaultdict(list)
        for subscriber_id, status, updated_at in sub_rows:
            by_subscriber[subscriber_id].append((status, updated_at))

        for subscriber_id, items in by_subscriber.items():
            statuses = [status for status, _ in items]
            # Legitimately entitled if ANY subscription is serviceable.
            if any(status in _SERVICEABLE_STATUSES for status in statuses):
                continue
            if not any(status in _NON_SERVICEABLE_STATUSES for status in statuses):
                continue
            changed = [u for _, u in items if u is not None]
            latest_change = max(changed) if changed else None
            if latest_change is not None and latest_change.tzinfo is None:
                latest_change = latest_change.replace(tzinfo=UTC)  # SQLite naive
            # Grace: skip if the status changed within the enforcement window.
            if latest_change is not None and latest_change > grace_cutoff:
                continue
            yield Finding(
                check_name=self.name,
                entity_type="subscriber",
                canonical_entity_id=str(subscriber_id),
                mismatch_type="suspended_but_online",
                severity=SEVERITY_CRITICAL,
                evidence={
                    "billing_status": sorted({s.value for s in statuses}),
                    "radius_authorized": True,
                    "active_sessions": session_counts[subscriber_id],
                    "last_status_change": (
                        latest_change.isoformat() if latest_change else None
                    ),
                },
                details={
                    "suggested_owner": (
                        "enforcement reconciler "
                        "(app.tasks.radius.run_enforcement_reconciler)"
                    ),
                    "suggested_action": (
                        "CoA-kick the live session(s) and verify radcheck carries "
                        "Auth-Type := Reject for this subscriber."
                    ),
                },
            )

    def _active_but_blocked(self, db: Session) -> Iterable[Finding]:
        from app.services.account_status_reconcile import (
            find_account_projection_drift_ids,
        )

        for account_id in find_account_projection_drift_ids(db):
            yield Finding(
                check_name=self.name,
                entity_type="subscriber",
                canonical_entity_id=str(account_id),
                mismatch_type="active_but_blocked",
                severity=SEVERITY_HIGH,
                evidence={
                    "billing_status": "active",
                    "subscriber_status": "blocked",
                    "radius_authorized": False,
                    "note": "active service with a blocking account projection",
                },
                details={
                    "suggested_owner": (
                        "access-control reconciler "
                        "(app.tasks.radius.run_enforcement_reconciler)"
                    ),
                    "suggested_action": (
                        "Re-derive the subscriber status from its subscriptions "
                        "and refresh RADIUS so the walled-garden tag drops."
                    ),
                },
            )

    def _sellable_capped_offer_cannot_enforce(self, db: Session) -> Iterable[Finding]:
        """Offers sold on a cap that nothing enforces.

        Two guards prevent this going forward — the sale-time check in
        ``web_catalog_offers`` and the rule-lifecycle guard in ``fup`` — but
        neither can see drift that predates them, arrives by import, or is
        written straight to the database. Production reached 63 subscribers on
        five such offers with nobody alerted, because an offer with no rules is
        indistinguishable at a glance from a family that legitimately has none.

        Emitted per offer rather than per subscriber: the defect is one missing
        rule, and the remediation is one rule.
        """
        from app.models.catalog import CatalogOffer
        from app.services.fup import (
            FUP_REQUIRED_FAMILIES,
            active_speed_reduction_rule_ids,
        )

        offers = db.scalars(
            select(CatalogOffer).where(
                CatalogOffer.is_active.is_(True),
                CatalogOffer.available_for_services.is_(True),
                CatalogOffer.plan_family.in_(FUP_REQUIRED_FAMILIES),
            )
        ).all()
        for offer in offers:
            if active_speed_reduction_rule_ids(db, offer.id):
                continue
            affected = db.scalar(
                select(func.count(Subscription.id)).where(
                    Subscription.offer_id == offer.id,
                    Subscription.status == SubscriptionStatus.active,
                )
            )
            yield Finding(
                check_name=self.name,
                entity_type="catalog_offer",
                canonical_entity_id=str(offer.id),
                mismatch_type="sellable_capped_offer_cannot_enforce",
                severity=SEVERITY_HIGH,
                evidence={
                    "offer_name": offer.name,
                    "plan_family": offer.plan_family,
                    "active_subscriptions": int(affected or 0),
                    "expected": "an active reduce_speed FUP rule",
                    "observed": "no active reduce_speed rule",
                    "remediation": (
                        "add the rule on the offer's FUP screen, or withdraw "
                        "the offer from sale"
                    ),
                },
            )

    def _throttle_profile_mismatch(self, db: Session) -> Iterable[Finding]:
        active_profile_ids = set(
            db.scalars(
                select(RadiusProfile.id).where(RadiusProfile.is_active.is_(True))
            ).all()
        )
        rows = db.execute(
            select(
                AccessCredential.subscriber_id,
                AccessCredential.username,
                AccessCredential.radius_profile_id,
            ).where(
                AccessCredential.is_active.is_(True),
                AccessCredential.radius_profile_id.isnot(None),
            )
        ).all()
        for subscriber_id, username, profile_id in rows:
            if profile_id in active_profile_ids:
                continue
            yield Finding(
                check_name=self.name,
                entity_type="subscriber",
                canonical_entity_id=str(subscriber_id),
                mismatch_type="throttle_profile_mismatch",
                severity=SEVERITY_MEDIUM,
                evidence={
                    "credential_username": username,
                    "radius_profile_id": str(profile_id),
                    "radius_profile": "missing_or_inactive",
                    "expected_profile": "an active RadiusProfile",
                },
                details={
                    "suggested_owner": "billing / enforcement config",
                    "suggested_action": (
                        "Point the credential at an active RadiusProfile (or clear "
                        "the stale reference); as-is any intended profile — a "
                        "throttle included — silently won't apply."
                    ),
                },
            )


# Bound the detector's working set so no single run can OOM the app container.
# Source scans stream instead of materializing every row; reconciliation resolves
# stale findings in id batches; high-cardinality legacy noise is aggregated.
_SCAN_YIELD = 1000
_RESOLVE_BATCH = 1000
_AGG_SAMPLE_LIMIT = 20


def _id_batches(ids: list, size: int) -> Iterable[list]:
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


class _LegacyAggregator:
    """Roll a high-cardinality legacy archive-completeness class into ONE finding.

    Historical Splynx-import noise (e.g. ~105k invoices with empty line totals)
    has no per-invoice action — emitting one finding per row both spams the queue
    and OOMs detection. Instead we accumulate a count, the total absolute delta,
    and a bounded sample, and emit a single aggregated finding for the class.
    """

    def __init__(self) -> None:
        self.count = 0
        self.total_abs_delta = Decimal("0.00")
        self.samples: list[dict] = []

    def add(self, invoice_id: object, invoice_number: object, delta: Decimal) -> None:
        self.count += 1
        self.total_abs_delta += abs(_money(delta))
        if len(self.samples) < _AGG_SAMPLE_LIMIT:
            self.samples.append(
                {
                    "invoice_id": str(invoice_id),
                    "invoice_number": invoice_number,
                    "delta": str(_money(delta)),
                }
            )

    def finding(self, check_name: str, mismatch_type: str, action: str) -> Finding:
        return Finding(
            check_name=check_name,
            entity_type="invoice_class",
            canonical_entity_id=f"legacy_splynx_import:{mismatch_type}",
            mismatch_type=mismatch_type,
            severity=SEVERITY_LOW,
            evidence={
                "invoice_source": "legacy_splynx_import",
                "aggregated": True,
                "count": self.count,
                "total_abs_delta": str(_money(self.total_abs_delta)),
                "sample_invoices": self.samples,
                "sample_limit": _AGG_SAMPLE_LIMIT,
            },
            details={
                "suggested_owner": "finance migration review",
                "suggested_action": action,
            },
        )


class MoneySelfConsistencyCheck:
    """Sub-internal invoice/payment invariants before ERP reads exist.

    These checks compare denormalized billing columns to the same local source
    rows the billing services use to maintain them. They do not decide ERP truth;
    they catch sub-side money drift before it propagates cross-app.
    """

    name = "money_self_consistency"

    def run(self, db: Session) -> Iterable[Finding]:
        yield from self._invoice_header_mismatch(db)
        yield from self._invoice_balance_mismatch(db)
        yield from self._payment_overallocated(db)
        yield from self._negative_invoice_balance(db)

    def _invoice_header_mismatch(self, db: Session) -> Iterable[Finding]:
        stmt = (
            select(
                Invoice.id,
                Invoice.invoice_number,
                Invoice.status,
                Invoice.subtotal,
                Invoice.tax_total,
                Invoice.total,
                Invoice.splynx_invoice_id,
            )
            .where(Invoice.is_active.is_(True))
            .where(Invoice.status.in_(_POSTED_INVOICE_STATUSES))
        )
        legacy = _LegacyAggregator()
        for (
            invoice_id,
            invoice_number,
            status,
            subtotal,
            tax_total,
            total,
            splynx_invoice_id,
        ) in db.execute(stmt.execution_options(yield_per=_SCAN_YIELD)):
            expected = _money(subtotal) + _money(tax_total)
            actual = _money(total)
            if not _money_differs(actual, expected):
                continue
            is_legacy_splynx_import = splynx_invoice_id is not None
            legacy_missing_line_totals = (
                is_legacy_splynx_import
                and expected == Decimal("0.00")
                and actual > Decimal("0.00")
            )
            # Archive-completeness noise (imported total preserved, line totals
            # empty) has no per-invoice action — roll it up, don't emit one row
            # each. Native + other-legacy header drift stays per-invoice.
            if legacy_missing_line_totals:
                legacy.add(invoice_id, invoice_number, actual - expected)
                continue
            source = "legacy_splynx_import" if is_legacy_splynx_import else "native"
            severity = SEVERITY_MEDIUM if is_legacy_splynx_import else SEVERITY_HIGH
            suggested_owner = (
                "finance migration review"
                if is_legacy_splynx_import
                else "billing invoice recalculation"
            )
            if is_legacy_splynx_import:
                suggested_action = (
                    "Review the historical Splynx import header mapping; imported "
                    "invoice totals were preserved, but subtotal/tax_total may be "
                    "missing or net/gross split differently. Do not recalculate "
                    "paid historical invoices without finance sign-off."
                )
            else:
                suggested_action = (
                    "Recalculate this invoice from active lines and verify "
                    "subtotal + tax_total equals total before ERP sync."
                )
            yield Finding(
                check_name=self.name,
                entity_type="invoice",
                canonical_entity_id=str(invoice_id),
                mismatch_type="invoice_total_mismatch",
                severity=severity,
                evidence={
                    "invoice_number": invoice_number,
                    "subtotal": str(_money(subtotal)),
                    "tax_total": str(_money(tax_total)),
                    "expected_total": str(expected),
                    "actual_total": str(actual),
                    "delta": str(_money(actual - expected)),
                    "status": status.value if status else None,
                    "invoice_source": source,
                    "splynx_invoice_id": splynx_invoice_id,
                },
                details={
                    "suggested_owner": suggested_owner,
                    "suggested_action": suggested_action,
                },
            )
        if legacy.count:
            yield legacy.finding(
                self.name,
                "legacy_invoice_line_totals_missing",
                "Historical Splynx invoices whose imported totals were preserved "
                "but line subtotal/tax fields are empty. Archive-data completeness, "
                "not a current invoice arithmetic bug — no per-invoice action.",
            )

    def _invoice_balance_mismatch(self, db: Session) -> Iterable[Finding]:
        allocation_rows = db.execute(
            select(
                PaymentAllocation.invoice_id,
                func.coalesce(func.sum(PaymentAllocation.amount), Decimal("0.00")),
            )
            .join(Payment, Payment.id == PaymentAllocation.payment_id)
            .where(PaymentAllocation.is_active.is_(True))
            .where(Payment.is_active.is_(True))
            .where(Payment.status == PaymentStatus.succeeded)
            .group_by(PaymentAllocation.invoice_id)
        ).all()
        allocated_by_invoice = {
            invoice_id: round_money(to_decimal(amount))
            for invoice_id, amount in allocation_rows
        }
        credit_rows = db.execute(
            select(
                CreditNoteApplication.invoice_id,
                func.coalesce(func.sum(CreditNoteApplication.amount), Decimal("0.00")),
            ).group_by(CreditNoteApplication.invoice_id)
        ).all()
        credits_by_invoice = {
            invoice_id: round_money(to_decimal(amount))
            for invoice_id, amount in credit_rows
        }

        stmt = (
            select(
                Invoice.id,
                Invoice.invoice_number,
                Invoice.status,
                Invoice.total,
                Invoice.balance_due,
                Invoice.splynx_invoice_id,
            )
            .where(Invoice.is_active.is_(True))
            .where(Invoice.status != InvoiceStatus.draft)
        )
        legacy = _LegacyAggregator()
        for (
            invoice_id,
            invoice_number,
            status,
            total,
            balance_due,
            splynx_invoice_id,
        ) in db.execute(stmt.execution_options(yield_per=_SCAN_YIELD)):
            if status in (InvoiceStatus.void, InvoiceStatus.written_off):
                expected_balance = Decimal("0.00")
            else:
                expected_balance = max(
                    Decimal("0.00"),
                    _money(
                        _money(total)
                        - allocated_by_invoice.get(invoice_id, Decimal("0.00"))
                        - credits_by_invoice.get(invoice_id, Decimal("0.00"))
                    ),
                )
            actual_balance = _money(balance_due)
            if not _money_differs(actual_balance, expected_balance):
                continue
            is_legacy_splynx_import = splynx_invoice_id is not None
            legacy_paid_reconstruction_gap = (
                is_legacy_splynx_import
                and status == InvoiceStatus.paid
                and actual_balance == Decimal("0.00")
            )
            # Archive allocation-completeness (imported paid/zero-balance, but
            # local allocations don't reconstruct settlement) has no per-invoice
            # action — roll it up. Native + other-legacy balance drift stays
            # per-invoice (it can indicate a live receivable error).
            if legacy_paid_reconstruction_gap:
                legacy.add(
                    invoice_id, invoice_number, actual_balance - expected_balance
                )
                continue
            source = "legacy_splynx_import" if is_legacy_splynx_import else "native"
            severity = SEVERITY_MEDIUM if is_legacy_splynx_import else SEVERITY_HIGH
            suggested_owner = (
                "finance migration review"
                if is_legacy_splynx_import
                else "billing invoice recalculation"
            )
            if is_legacy_splynx_import:
                suggested_action = (
                    "Review the historical Splynx settlement import for this "
                    "invoice; imported balance_due/status may reflect Splynx-paid "
                    "state even when local allocations do not reconstruct it. Do "
                    "not reopen or recalculate paid historical invoices without "
                    "finance sign-off."
                )
            else:
                suggested_action = (
                    "Run the invoice recalculation path for this invoice and "
                    "review active allocations/credit applications."
                )
            yield Finding(
                check_name=self.name,
                entity_type="invoice",
                canonical_entity_id=str(invoice_id),
                mismatch_type="invoice_balance_mismatch",
                severity=severity,
                evidence={
                    "invoice_number": invoice_number,
                    "status": status.value if status else None,
                    "total": str(_money(total)),
                    "succeeded_allocations": str(
                        allocated_by_invoice.get(invoice_id, Decimal("0.00"))
                    ),
                    "credit_applications": str(
                        credits_by_invoice.get(invoice_id, Decimal("0.00"))
                    ),
                    "expected_balance_due": str(expected_balance),
                    "actual_balance_due": str(actual_balance),
                    "delta": str(_money(actual_balance - expected_balance)),
                    "invoice_source": source,
                    "splynx_invoice_id": splynx_invoice_id,
                },
                details={
                    "suggested_owner": suggested_owner,
                    "suggested_action": suggested_action,
                },
            )
        if legacy.count:
            yield legacy.finding(
                self.name,
                "legacy_paid_invoice_allocation_gap",
                "Historical Splynx invoices imported as paid with zero balance "
                "whose local allocation rows do not reconstruct the settlement. "
                "Archive allocation completeness, not current receivable drift — "
                "no per-invoice action.",
            )

    def _payment_overallocated(self, db: Session) -> Iterable[Finding]:
        rows = db.execute(
            select(
                Payment.id,
                Payment.amount,
                Payment.currency,
                Payment.status,
                Payment.external_id,
                Payment.splynx_payment_id,
                Payment.provider_id,
                Payment.payment_channel_id,
                Payment.payment_method_id,
                func.coalesce(func.sum(PaymentAllocation.amount), Decimal("0.00")),
            )
            .join(PaymentAllocation, PaymentAllocation.payment_id == Payment.id)
            .where(Payment.is_active.is_(True))
            .where(PaymentAllocation.is_active.is_(True))
            .group_by(
                Payment.id,
                Payment.amount,
                Payment.currency,
                Payment.status,
                Payment.external_id,
                Payment.splynx_payment_id,
                Payment.provider_id,
                Payment.payment_channel_id,
                Payment.payment_method_id,
            )
        ).all()
        for (
            payment_id,
            amount,
            currency,
            status,
            external_id,
            splynx_payment_id,
            provider_id,
            payment_channel_id,
            payment_method_id,
            allocated,
        ) in rows:
            payment_amount = _money(amount)
            allocated_amount = _money(allocated)
            if allocated_amount - payment_amount <= _MONEY_TOLERANCE:
                continue
            is_legacy_splynx_import = (
                splynx_payment_id is not None
                and external_id is None
                and provider_id is None
                and payment_channel_id is None
                and payment_method_id is None
            )
            severity = SEVERITY_MEDIUM if is_legacy_splynx_import else SEVERITY_CRITICAL
            source = "legacy_splynx_import" if is_legacy_splynx_import else "native"
            suggested_owner = (
                "finance migration review"
                if is_legacy_splynx_import
                else "billing payment allocation"
            )
            suggested_action = (
                "Review the historical Splynx import mapping for this payment; "
                "do not mutate current allocations until finance confirms the "
                "legacy receipt-to-invoice treatment."
                if is_legacy_splynx_import
                else (
                    "Review active payment allocations; total active "
                    "allocations must not exceed the payment amount."
                )
            )
            yield Finding(
                check_name=self.name,
                entity_type="payment",
                canonical_entity_id=str(payment_id),
                mismatch_type="payment_over_allocated",
                severity=severity,
                evidence={
                    "payment_amount": str(payment_amount),
                    "allocated_amount": str(allocated_amount),
                    "over_by": str(_money(allocated_amount - payment_amount)),
                    "currency": currency,
                    "payment_status": status.value if status else None,
                    "payment_source": source,
                    "splynx_payment_id": splynx_payment_id,
                },
                details={
                    "suggested_owner": suggested_owner,
                    "suggested_action": suggested_action,
                },
            )

    def _negative_invoice_balance(self, db: Session) -> Iterable[Finding]:
        rows = db.execute(
            select(
                Invoice.id,
                Invoice.invoice_number,
                Invoice.status,
                Invoice.balance_due,
                Invoice.total,
            )
            .where(Invoice.is_active.is_(True))
            .where(Invoice.status.in_(_OPEN_INVOICE_STATUSES))
            .where(Invoice.balance_due < -_MONEY_TOLERANCE)
        ).all()
        for invoice_id, invoice_number, status, balance_due, total in rows:
            balance = _money(balance_due)
            if balance >= -_MONEY_TOLERANCE:
                continue
            yield Finding(
                check_name=self.name,
                entity_type="invoice",
                canonical_entity_id=str(invoice_id),
                mismatch_type="negative_invoice_balance",
                severity=SEVERITY_HIGH,
                evidence={
                    "invoice_number": invoice_number,
                    "status": status.value if status else None,
                    "balance_due": str(balance),
                    "total": str(_money(total)),
                },
                details={
                    "suggested_owner": "billing payments / credits",
                    "suggested_action": (
                        "Recalculate the invoice and inspect allocations/credits; "
                        "an open invoice must not carry negative balance_due."
                    ),
                },
            )


class BillingReconciliationHoldCheck:
    """Open invoices parked for manual reconciliation must stay visible.

    The prepaid-overlap repair intentionally creates holds for invoices with
    partial payments or allocations because auto-deciding them would be unsafe.
    This check turns those holds into durable drift findings until billing
    either clears the hold or closes the invoice.
    """

    name = "billing_reconciliation_hold"

    def run(self, db: Session) -> Iterable[Finding]:
        now = datetime.now(UTC)
        rows = db.execute(
            select(
                Invoice.id,
                Invoice.account_id,
                Invoice.invoice_number,
                Invoice.status,
                Invoice.total,
                Invoice.balance_due,
                Invoice.metadata_,
                Invoice.created_at,
                Invoice.updated_at,
            )
            .where(Invoice.is_active.is_(True))
            .where(Invoice.status.in_(_RECONCILIATION_HOLD_INVOICE_STATUSES))
        ).all()
        for (
            invoice_id,
            account_id,
            invoice_number,
            status,
            total,
            balance_due,
            metadata,
            created_at,
            updated_at,
        ) in rows:
            metadata = metadata or {}
            if not _metadata_truthy(metadata.get("reconciliation_hold")):
                continue

            repair = metadata.get("prepaid_overlap_repair")
            repair = repair if isinstance(repair, dict) else {}
            reason = (
                metadata.get("reconciliation_hold_reason")
                or repair.get("reason")
                or "reconciliation_hold"
            )
            held_since = (
                _parse_metadata_datetime(repair.get("detected_at"))
                or _parse_metadata_datetime(metadata.get("reconciliation_hold_at"))
                or _as_utc(updated_at)
                or _as_utc(created_at)
            )
            hold_age_hours = None
            if held_since is not None:
                elapsed = max(timedelta(), now - held_since)
                hold_age_hours = round(elapsed.total_seconds() / 3600, 2)
            sla_hours = int(_RECONCILIATION_HOLD_REVIEW_WINDOW.total_seconds() // 3600)
            sla_breached = (
                held_since is not None
                and now - held_since > _RECONCILIATION_HOLD_REVIEW_WINDOW
            )
            repair_evidence = {
                key: str(repair[key])
                for key in (
                    "detected_at",
                    "valid_paid_invoice_id",
                    "valid_paid_invoice_number",
                    "paid_through",
                    "reason",
                )
                if key in repair
            }

            yield Finding(
                check_name=self.name,
                entity_type="invoice",
                canonical_entity_id=str(invoice_id),
                mismatch_type="reconciliation_hold_pending_review",
                severity=SEVERITY_HIGH if sla_breached else SEVERITY_MEDIUM,
                evidence={
                    "invoice_id": str(invoice_id),
                    "invoice_number": invoice_number,
                    "account_id": str(account_id),
                    "status": status.value if status else None,
                    "total": str(_money(total)),
                    "balance_due": str(_money(balance_due)),
                    "reconciliation_hold_reason": str(reason),
                    "held_since": held_since.isoformat() if held_since else None,
                    "hold_age_hours": hold_age_hours,
                    "hold_review_sla_hours": sla_hours,
                    "hold_sla_breached": sla_breached,
                    "prepaid_overlap_repair": repair_evidence,
                },
                details={
                    "suggested_owner": "billing manual review",
                    "suggested_action": (
                        "Review the held invoice: void or credit it if it is a "
                        "prepaid-overlap duplicate; otherwise clear the hold so "
                        "normal billing can resume."
                    ),
                    "hold_review_sla_hours": sla_hours,
                    "hold_sla_breached": sla_breached,
                },
            )


DEFAULT_CHECKS: list[DriftCheck] = [
    IdentityCardinalityCheck(),
    ServiceEnforcementCheck(),
    MoneySelfConsistencyCheck(),
    BillingReconciliationHoldCheck(),
]


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def _active_waiver_fingerprints(db: Session, now: datetime) -> set[str]:
    rows = db.scalars(
        select(CrossAppDriftWaiver.fingerprint).where(
            CrossAppDriftWaiver.is_active.is_(True),
            or_(
                CrossAppDriftWaiver.expires_at.is_(None),
                CrossAppDriftWaiver.expires_at > now,
            ),
        )
    ).all()
    return set(rows)


def _log_event(
    db: Session,
    finding: CrossAppDriftFinding,
    run: CrossAppDriftRun,
    event_type: str,
    snapshot: dict | None,
) -> None:
    db.add(
        CrossAppDriftFindingEvent(
            finding_id=finding.id,
            run_id=run.id,
            event_type=event_type,
            at=datetime.now(UTC),
            snapshot=snapshot,
        )
    )


def run_detection(
    db: Session, *, checks: list[DriftCheck] | None = None
) -> CrossAppDriftRun:
    """Execute every check, reconcile findings by fingerprint, persist, commit.

    Detect-only. Returns the completed ``CrossAppDriftRun`` with counts.
    """
    checks = checks if checks is not None else DEFAULT_CHECKS
    now = datetime.now(UTC)
    run = CrossAppDriftRun(status=RUN_RUNNING, started_at=now)
    db.add(run)
    db.flush()

    current: dict[str, Finding] = {}
    for check in checks:
        try:
            for produced in check.run(db):
                current[produced.fingerprint] = produced
        except Exception:
            logger.exception(
                "cross_app_drift check failed: %s", getattr(check, "name", check)
            )
    run.checks_run = len(checks)

    waived = _active_waiver_fingerprints(db, now)

    # Upsert only the emitted findings, one point-lookup each. `current` is
    # bounded (high-cardinality legacy classes are aggregated to one finding),
    # so we never materialize the whole findings table — the detector's memory
    # scales with emitted + open findings, not with all-time resolved history.
    new_count = 0
    for fp, found in current.items():
        finding = db.scalar(
            select(CrossAppDriftFinding).where(CrossAppDriftFinding.fingerprint == fp)
        )
        if finding is None:
            finding = CrossAppDriftFinding(
                fingerprint=fp,
                check_name=found.check_name,
                entity_type=found.entity_type,
                canonical_entity_id=found.canonical_entity_id,
                mismatch_type=found.mismatch_type,
                severity=found.severity,
                status=STATUS_WAIVED if fp in waived else STATUS_OPEN,
                occurrences=1,
                first_seen_at=now,
                last_seen_at=now,
                first_run_id=run.id,
                last_run_id=run.id,
                details=found.details,
                evidence=found.evidence,
            )
            db.add(finding)
            db.flush()
            _log_event(db, finding, run, EVENT_CREATED, found.details)
            new_count += 1
            continue

        prev_status = finding.status
        prev_severity = finding.severity
        finding.occurrences += 1
        finding.last_seen_at = now
        finding.last_run_id = run.id
        finding.severity = found.severity
        finding.details = found.details
        finding.evidence = found.evidence
        finding.resolved_at = None

        if fp in waived:
            finding.status = STATUS_WAIVED
        elif prev_status in (STATUS_RESOLVED, STATUS_WAIVED):
            finding.status = STATUS_OPEN
            _log_event(db, finding, run, EVENT_REOPENED, found.details)

        if SEVERITY_ORDER.get(found.severity, 0) > SEVERITY_ORDER.get(prev_severity, 0):
            _log_event(db, finding, run, EVENT_WORSENED, found.details)
        else:
            _log_event(db, finding, run, EVENT_RECURRING, found.details)

    # Resolve open/waived findings whose fingerprint was not re-emitted. Only
    # non-terminal findings can transition, so we scan just those (never the
    # resolved history), collect their ids, and resolve in id batches so a large
    # backlog (e.g. the one-time legacy-noise cleanup) cannot OOM the run.
    current_fps = set(current.keys())
    stale_ids = [
        fid
        for fid, fp in db.execute(
            select(CrossAppDriftFinding.id, CrossAppDriftFinding.fingerprint).where(
                CrossAppDriftFinding.status.in_((STATUS_OPEN, STATUS_WAIVED))
            )
        )
        if fp not in current_fps
    ]
    resolved_count = 0
    for batch in _id_batches(stale_ids, _RESOLVE_BATCH):
        db.execute(
            update(CrossAppDriftFinding)
            .where(CrossAppDriftFinding.id.in_(batch))
            .values(status=STATUS_RESOLVED, resolved_at=now, last_run_id=run.id)
            .execution_options(synchronize_session=False),
        )
        for fid in batch:
            db.add(
                CrossAppDriftFindingEvent(
                    finding_id=fid,
                    run_id=run.id,
                    event_type=EVENT_RESOLVED,
                    at=now,
                    snapshot=None,
                )
            )
        db.flush()
        resolved_count += len(batch)

    db.flush()  # persist status changes so the open-count reflects this run
    open_count = db.scalar(
        select(func.count())
        .select_from(CrossAppDriftFinding)
        .where(CrossAppDriftFinding.status == STATUS_OPEN)
    )
    run.findings_open = int(open_count or 0)
    run.findings_new = new_count
    run.findings_resolved = resolved_count
    run.status = RUN_COMPLETED
    run.finished_at = datetime.now(UTC)
    db.commit()
    return run


def open_findings_by_severity(db: Session) -> dict[str, int]:
    """Current open-finding counts per severity — for alerting/metrics."""
    rows = db.execute(
        select(CrossAppDriftFinding.severity, func.count())
        .where(CrossAppDriftFinding.status == STATUS_OPEN)
        .group_by(CrossAppDriftFinding.severity)
    ).all()
    return {severity: int(count) for severity, count in rows}


# Material drift (these severities) is mirrored into the admin alert console so
# it pages and shows up in the operational surfaces; medium/low stay findings.
_ALERTING_SEVERITIES = (SEVERITY_CRITICAL, SEVERITY_HIGH)
_DRIFT_ALERT_PREFIX = "drift:"
_DRIFT_ALERT_CATEGORY = "cross_app_drift"
_DRIFT_ALERT_DETAIL_LIMIT = 500

# Ageing SLA: how long an open finding may sit before it needs action. Paged
# severities get a clock; medium/low are tracked on the dashboard, never paged.
_SLA_WINDOWS = {
    SEVERITY_CRITICAL: timedelta(hours=24),  # same day
    SEVERITY_HIGH: timedelta(days=2),  # 1–2 business days
}


def sla_status(finding: CrossAppDriftFinding, now: datetime | None = None) -> dict:
    """Ageing state for a finding: whether it pages, its due time, and whether
    it has breached (open past its window). Medium/low are tracked, not paged."""
    now = now or datetime.now(UTC)
    window = _SLA_WINDOWS.get(finding.severity)
    if window is None:
        return {"paged": False, "due_at": None, "breached": False}
    first = finding.first_seen_at
    if first is not None and first.tzinfo is None:
        first = first.replace(tzinfo=UTC)
    due = first + window if first is not None else None
    breached = bool(due and finding.status == STATUS_OPEN and now > due)
    return {
        "paged": True,
        "due_at": due.isoformat() if due else None,
        "breached": breached,
    }


def sync_drift_alerts(db: Session) -> dict[str, int | str]:
    """Mirror open critical/high findings into the admin alert console and
    resolve alerts whose finding cleared or dropped below material.

    Reuses the same fingerprint-lifecycle sink as infrastructure alerts, so
    drift findings page and render in the existing operational surfaces
    (notification menu, dashboard summary) — the alert path + read view. The
    coarse alert severity (info/warning/critical) carries the precise drift
    severity in ``details``. Detect-only; commits its own alert writes.
    """
    from app.models.admin_alert import AlertSeverity
    from app.services.admin_alerts import (
        AlertFinding,
        resolve_missing_alerts,
        sync_alert,
    )

    findings = db.scalars(
        select(CrossAppDriftFinding).where(
            CrossAppDriftFinding.status == STATUS_OPEN,
            CrossAppDriftFinding.severity.in_(_ALERTING_SEVERITIES),
        )
    ).all()

    now = datetime.now(UTC)
    active: set[str] = set()
    opened_or_escalated = 0
    if len(findings) > _DRIFT_ALERT_DETAIL_LIMIT:
        groups: dict[tuple[str, str, str, str], list[CrossAppDriftFinding]] = (
            defaultdict(list)
        )
        for finding in findings:
            groups[
                (
                    finding.severity,
                    finding.check_name,
                    finding.mismatch_type,
                    finding.entity_type,
                )
            ].append(finding)

        for (severity, check_name, mismatch_type, entity_type), items in groups.items():
            alert_fp = (
                f"{_DRIFT_ALERT_PREFIX}summary:{severity}:{check_name}:"
                f"{mismatch_type}:{entity_type}"
            )
            active.add(alert_fp)
            sample = items[:5]
            breached = any(sla_status(finding, now)["breached"] for finding in items)
            alert_severity = (
                AlertSeverity.critical
                if (severity == SEVERITY_CRITICAL or breached)
                else AlertSeverity.warning
            )
            alert = AlertFinding(
                fingerprint=alert_fp,
                category=_DRIFT_ALERT_CATEGORY,
                source=check_name,
                severity=alert_severity,
                title=f"{len(items)} {mismatch_type} drift finding(s)"[:180],
                summary=(
                    "Material cross-app drift exceeded per-finding alert volume; "
                    "review the grouped findings in /admin/drift."
                )[:255],
                details={
                    "check": check_name,
                    "mismatch_type": mismatch_type,
                    "entity_type": entity_type,
                    "drift_severity": severity,
                    "finding_count": len(items),
                    "alert_mode": "grouped",
                    "per_finding_alert_limit": _DRIFT_ALERT_DETAIL_LIMIT,
                    "sample_finding_ids": [str(finding.id) for finding in sample],
                    "sample_canonical_entity_ids": [
                        finding.canonical_entity_id for finding in sample
                    ],
                    "sample_evidence": [finding.evidence for finding in sample],
                },
                target_url="/admin/drift?"
                + urlencode(
                    {
                        "status": STATUS_OPEN,
                        "check": check_name,
                        "entity_type": entity_type,
                    }
                ),
            )
            if sync_alert(db, alert) in ("opened", "escalated"):
                opened_or_escalated += 1

        resolved = resolve_missing_alerts(
            db, managed_prefix=_DRIFT_ALERT_PREFIX, active_fingerprints=active
        )
        db.commit()
        return {
            "alerted": len(active),
            "opened_or_escalated": opened_or_escalated,
            "resolved": resolved,
            "mode": "grouped",
        }

    for finding in findings:
        alert_fp = f"{_DRIFT_ALERT_PREFIX}{finding.fingerprint}"
        active.add(alert_fp)
        remediation = finding.details or {}
        sla = sla_status(finding, now)
        # sync_alert only re-notifies on open/severity-escalation — never on a
        # plain recurring run. An SLA breach forces the alert to critical so it
        # re-pages (escalation), which is exactly "alert again when overdue".
        alert_severity = (
            AlertSeverity.critical
            if (finding.severity == SEVERITY_CRITICAL or sla["breached"])
            else AlertSeverity.warning
        )
        alert = AlertFinding(
            fingerprint=alert_fp,
            category=_DRIFT_ALERT_CATEGORY,
            source=finding.check_name,
            severity=alert_severity,
            title=f"{finding.mismatch_type}: {finding.entity_type} "
            f"{finding.canonical_entity_id}"[:180],
            summary=(remediation.get("suggested_action") or finding.mismatch_type)[
                :255
            ],
            details={
                "check": finding.check_name,
                "mismatch_type": finding.mismatch_type,
                "entity_type": finding.entity_type,
                "canonical_entity_id": finding.canonical_entity_id,
                "drift_severity": finding.severity,
                "occurrences": finding.occurrences,
                "first_seen_at": finding.first_seen_at,
                "sla": sla,
                "evidence": finding.evidence,
                **remediation,
            },
            target_url="/admin/drift?"
            + urlencode(
                {
                    "status": STATUS_OPEN,
                    "check": finding.check_name,
                    "entity_type": finding.entity_type,
                }
            ),
        )
        if sync_alert(db, alert) in ("opened", "escalated"):
            opened_or_escalated += 1

    resolved = resolve_missing_alerts(
        db, managed_prefix=_DRIFT_ALERT_PREFIX, active_fingerprints=active
    )
    db.commit()
    return {
        "alerted": len(active),
        "opened_or_escalated": opened_or_escalated,
        "resolved": resolved,
        "mode": "per_finding",
    }


def _finding_row(finding: CrossAppDriftFinding, now: datetime) -> dict:
    remediation = finding.details or {}
    return {
        "id": str(finding.id),
        "fingerprint": finding.fingerprint,
        "check": finding.check_name,
        "entity_type": finding.entity_type,
        "canonical_entity_id": finding.canonical_entity_id,
        "mismatch_type": finding.mismatch_type,
        "severity": finding.severity,
        "status": finding.status,
        "occurrences": finding.occurrences,
        "first_seen_at": (
            finding.first_seen_at.isoformat() if finding.first_seen_at else None
        ),
        "last_seen_at": (
            finding.last_seen_at.isoformat() if finding.last_seen_at else None
        ),
        "last_run_id": str(finding.last_run_id) if finding.last_run_id else None,
        "suggested_owner": remediation.get("suggested_owner"),
        "suggested_action": remediation.get("suggested_action"),
        "evidence": finding.evidence or {},
        "sla": sla_status(finding, now),
    }


def open_findings_report(db: Session) -> list[dict]:
    """Read view: open findings (worst first) with owner, action, evidence, SLA.

    Feeds a dashboard / read endpoint without exposing the ORM. Includes
    medium/low too (which don't page) so nothing is invisible.
    """
    now = datetime.now(UTC)
    findings = list(
        db.scalars(
            select(CrossAppDriftFinding).where(
                CrossAppDriftFinding.status == STATUS_OPEN
            )
        ).all()
    )
    findings.sort(
        key=lambda f: (
            -SEVERITY_ORDER.get(f.severity, 0),
            f.check_name,
            f.canonical_entity_id,
        )
    )
    return [_finding_row(f, now) for f in findings]


def drift_findings_context(
    db: Session,
    *,
    status: str | None = "open",
    severity: str | None = None,
    check: str | None = None,
    entity_type: str | None = None,
    owner: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    """Filterable admin-table view: what's broken, how bad, who owns it, what to
    do, and the evidence — with SLA ageing and recurrence. ``status`` accepts a
    lifecycle value or ``"all"``; ``owner`` is a substring match on the
    suggested owner. Read-only.
    """
    now = datetime.now(UTC)
    query = select(CrossAppDriftFinding)
    if status and status != "all":
        query = query.where(CrossAppDriftFinding.status == status)
    if severity:
        query = query.where(CrossAppDriftFinding.severity == severity)
    if check:
        query = query.where(CrossAppDriftFinding.check_name == check)
    if entity_type:
        query = query.where(CrossAppDriftFinding.entity_type == entity_type)
    findings = list(db.scalars(query).all())
    if owner:
        needle = owner.lower()
        findings = [
            f
            for f in findings
            if needle in str((f.details or {}).get("suggested_owner") or "").lower()
        ]
    findings.sort(
        key=lambda f: (
            0 if f.status == STATUS_OPEN else 1,
            -SEVERITY_ORDER.get(f.severity, 0),
            -f.occurrences,
        )
    )
    rows = [_finding_row(f, now) for f in findings]
    total = len(rows)
    start = (page - 1) * per_page
    page_rows = rows[start : start + per_page]
    total_pages = max(1, (total + per_page - 1) // per_page)

    # Filter option lists + top-level counts are global so the summary cards
    # don't change meaning when the table is filtered.
    all_findings = list(db.scalars(select(CrossAppDriftFinding)).all())
    return {
        "findings": page_rows,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "has_previous_page": page > 1,
        "has_next_page": page < total_pages,
        "status": status or "",
        "severity": severity or "",
        "check": check or "",
        "entity_type": entity_type or "",
        "owner": owner or "",
        "checks": sorted({f.check_name for f in all_findings}),
        "entity_types": sorted({f.entity_type for f in all_findings}),
        "severities": [
            SEVERITY_CRITICAL,
            SEVERITY_HIGH,
            SEVERITY_MEDIUM,
            SEVERITY_LOW,
        ],
        "statuses": [STATUS_OPEN, STATUS_WAIVED, STATUS_RESOLVED, "all"],
        "open_by_severity": open_findings_by_severity(db),
        "breached_count": sum(
            1
            for f in all_findings
            if f.status == STATUS_OPEN and sla_status(f, now)["breached"]
        ),
    }
