import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import (
    AccessCredential,
    BillingMode,
    DunningAction,
    PolicyDunningStep,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.collections import (
    DunningActionLog,
    DunningCase,
    DunningCaseStatus,
    FinancialAccessAction,
    FinancialAccessConsequence,
    FinancialAccessConsequenceEvidence,
    FinancialAccessEvidenceOperation,
    FinancialAccessOrigin,
)
from app.models.domain_settings import SettingDomain
from app.models.enforcement_lock import (
    AccessRestrictionMode,
    EnforcementLock,
    EnforcementReason,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.audit import AuditEventCreate
from app.schemas.collections import (
    BillingEnforcementRunRequest,
    BillingEnforcementRunResponse,
    DunningActionLogCreate,
    DunningActionLogUpdate,
    DunningCaseCreate,
    DunningCaseUpdate,
    DunningRunRequest,
    DunningRunResponse,
)
from app.services import enforcement_window, settings_spec
from app.services.access_resolution import (
    PrepaidFundingDecision,
    resolve_prepaid_available_balance,
    resolve_prepaid_funding,
)
from app.services.audit import AuditEvents
from app.services.billing._common import resolve_invoice_settlement_amounts
from app.services.billing.invoices import Invoices
from app.services.billing_prepaid_overlap_repair import (
    apply_prepaid_overlap_hold,
    invoice_paid_prepaid_overlap,
)
from app.services.billing_profile import resolve_billing_profile
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.collections.grace_policy import (
    resolve_grace_decision,
    resolve_policy_set_for_account,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    round_money,
    to_decimal,
    validate_enum,
)
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.invoice_classification import collectible_ar_invoice_filter
from app.services.invoice_collectibility import collection_due_date_eligible_filter
from app.services.payment_arrangements import (
    active_arrangement_shield_reason,
    bulk_active_arrangement_shield_reasons,
)
from app.services.prepaid_enforcement_state import clear_prepaid_enforcement_timers
from app.services.response import ListResponseMixin
from app.services.walled_garden_policy import resolve_walled_garden_decision

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FinancialAccessCredentialChange:
    credential_id: UUID
    profile_before_id: UUID | None
    profile_after_id: UUID | None


@dataclass(frozen=True)
class FinancialAccessConsequencePreview:
    account_id: UUID
    action: FinancialAccessAction
    requested_reason: EnforcementReason | None
    origin: FinancialAccessOrigin
    dunning_case_id: UUID | None
    eligible: bool
    outcome: str
    target_subscription_ids: tuple[UUID, ...]
    target_lock_ids: tuple[UUID, ...]
    target_case_ids: tuple[UUID, ...]
    credential_changes: tuple[FinancialAccessCredentialChange, ...]
    decision_inputs: dict
    fingerprint: str


@dataclass(frozen=True)
class FinancialAccessConsequenceResult:
    consequence: FinancialAccessConsequence
    preview: FinancialAccessConsequencePreview
    subscriptions_changed: int
    idempotent_replay: bool = False
    # One typed restoration outcome per subscription the payment touched.
    # `subscriptions_changed` alone cannot tell an operator why a settled
    # customer is still offline; these can.
    restoration_outcomes: tuple[dict[str, object], ...] = ()

    @property
    def settled_but_access_blocked(self) -> tuple[dict[str, object], ...]:
        return tuple(
            item
            for item in self.restoration_outcomes
            if item.get("financially_settled_but_access_blocked")
        )


@dataclass(frozen=True, slots=True)
class FinancialAccessRestorationParticipantCommand:
    """Typed flush-only restoration request from another deciding owner."""

    account_id: UUID
    origin: FinancialAccessOrigin
    idempotency_key: str
    invoice_id: UUID | None
    resolved_by: str


class FinancialAccessRestorationParticipantError(DomainError):
    """Stable rejection raised to an owning coordinator."""


class DunningStaffAction(StrEnum):
    """Staff case actions that require an exact impact preview."""

    pause = "pause"
    resume = "resume"
    close = "close"


@dataclass(frozen=True, slots=True)
class DunningStaffCaseImpact:
    case_id: UUID
    exists: bool
    account_id: UUID | None
    current_status: DunningCaseStatus | None
    resulting_status: DunningCaseStatus | None
    eligible: bool
    reason: str | None
    unpaid_invoice_count: int
    unpaid_receivables: tuple[tuple[str, Decimal], ...]
    current_step: int | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class DunningStaffActionPreview:
    action: DunningStaffAction
    requested_case_ids: tuple[UUID, ...]
    impacts: tuple[DunningStaffCaseImpact, ...]
    fingerprint: str

    @property
    def eligible_case_ids(self) -> tuple[UUID, ...]:
        return tuple(impact.case_id for impact in self.impacts if impact.eligible)

    @property
    def selected_count(self) -> int:
        return len(self.requested_case_ids)

    @property
    def eligible_count(self) -> int:
        return len(self.eligible_case_ids)

    @property
    def skipped_count(self) -> int:
        return self.selected_count - self.eligible_count


@dataclass(frozen=True, slots=True)
class DunningAccountRunCommand:
    """One independently committable dunning decision root."""

    account_id: UUID
    invoice_ids: tuple[UUID, ...]
    run_at: datetime
    dry_run: bool


@dataclass(frozen=True, slots=True)
class DunningAccountRunResult:
    cases_created: int = 0
    actions_created: int = 0
    skipped: int = 0
    has_effective_overdue: bool = False


def _financial_access_fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _overdue_receivable_snapshot(db: Session, account_id: UUID) -> list[dict]:
    """Exact collectible overdue receivables used by access policy.

    The stored ``balance_due`` remains a materialized query accelerator, but
    the decision evidence records the receivable recomputed from the invoice
    total and canonical payment/credit application facts.
    """
    now = datetime.now(UTC)
    invoices = (
        db.query(Invoice)
        .filter(Invoice.account_id == account_id)
        .filter(Invoice.is_active.is_(True))
        .filter(collectible_ar_invoice_filter())
        .filter(collection_due_date_eligible_filter())
        .filter(
            or_(
                Invoice.status == InvoiceStatus.overdue,
                and_(
                    Invoice.status.in_(
                        [InvoiceStatus.issued, InvoiceStatus.partially_paid]
                    ),
                    Invoice.due_at.is_not(None),
                    Invoice.due_at <= now,
                ),
            )
        )
        .order_by(Invoice.due_at.asc(), Invoice.id.asc())
        .all()
    )
    result: list[dict] = []
    for invoice in invoices:
        if (invoice.metadata_ or {}).get("reconciliation_hold"):
            continue
        settlement = resolve_invoice_settlement_amounts(db, invoice.id)
        receivable = max(
            Decimal("0.00"),
            round_money(
                to_decimal(invoice.total)
                - settlement.payments_applied
                - settlement.credits_applied
            ),
        )
        if receivable <= 0:
            continue
        result.append(
            {
                "invoice_id": str(invoice.id),
                "currency": invoice.currency,
                "receivable": f"{receivable:.2f}",
                "payments_applied": f"{settlement.payments_applied:.2f}",
                "credits_applied": f"{settlement.credits_applied:.2f}",
            }
        )
    return result


def _resolve_positive_int_setting(
    db: Session,
    domain: SettingDomain,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = settings_spec.resolve_value(db, domain, key)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _suspension_notification_dedupe_hours(db: Session) -> int:
    return _resolve_positive_int_setting(
        db,
        SettingDomain.collections,
        "suspension_notification_dedupe_hours",
        24,
        minimum=1,
        maximum=168,
    )


def _resolve_prepaid_available_balance(db: Session, account_id: str) -> Decimal:
    """Authoritative prepaid available balance from customer financial events.

    Enforcement uses the same canonical credits and debits that statements show:
    real payments, real service charges, real credit notes/refunds, real approved
    adjustments, and legacy mirrored transactions. Internal repair artifacts are
    excluded before this balance is calculated.
    """
    return resolve_prepaid_available_balance(db, account_id)


def get_available_balance(db: Session, account_id: str) -> Decimal:
    """Return the available account balance visible to customer billing flows."""
    return _resolve_prepaid_available_balance(db, account_id)


def has_overdue_balance(db: Session, account_id: str) -> bool:
    """Return whether canonical settlement facts leave overdue receivable."""
    return bool(_overdue_receivable_snapshot(db, coerce_uuid(account_id)))


def overdue_receivable_snapshot(db: Session, account_id: str) -> list[dict]:
    """Public exact overdue-receivable evidence for owner-side recomputation.

    Callers that must PROVE zero overdue receivable — not merely observe a
    boolean — record this snapshot as their decision evidence. Exact
    arithmetic: a sub-naira residue is a real receivable and appears here.
    """
    return _overdue_receivable_snapshot(db, coerce_uuid(account_id))


def _effective_billing_mode_for_account(
    db: Session, account: Subscriber
) -> BillingMode | None:
    """Resolve billing mode through the canonical billing profile."""
    profile = resolve_billing_profile(db, account)
    if not profile.is_valid:
        logger.warning(
            "Invalid billing profile for account %s: reason=%s account=%s "
            "subscription_modes=%s",
            account.id,
            profile.invalid_reason,
            profile.account_mode.value if profile.account_mode else None,
            sorted(mode.value for mode in profile.subscription_modes),
        )
        return None
    if profile.account_subscription_mismatch:
        logger.info(
            "Resolved billing mode for account %s from collectible subscriptions: "
            "account=%s effective=%s",
            account.id,
            profile.account_mode.value if profile.account_mode else None,
            profile.effective_mode.value if profile.effective_mode else None,
        )
    return profile.effective_mode


def _resolve_policy_set_for_account(db: Session, account_id: str):
    """Resolve the dunning policy for an account, most specific override first:

    1. account override   (subscriber.policy_set_id)
    2. reseller override   (reseller.policy_set_id)
    3. offer / offer_version policy_set_id
    4. general default by billing mode (collections setting)
    """
    account = cast(Subscriber | None, db.get(Subscriber, coerce_uuid(account_id)))
    if account is None:
        return None
    return resolve_policy_set_for_account(db, account)


def _resolve_dunning_steps(db: Session, policy_set_id: str):
    return (
        db.query(PolicyDunningStep)
        .filter(PolicyDunningStep.policy_set_id == policy_set_id)
        .order_by(PolicyDunningStep.day_offset.asc())
        .all()
    )


def _resolve_overdue_days(
    invoice: Invoice,
    run_at: datetime,
    account: Subscriber | None = None,
    db: Session | None = None,
    policy_set_id: UUID | None = None,
) -> int:
    """Calculate days overdue, accounting for account grace period.

    Args:
        invoice: The invoice to check
        run_at: The reference datetime for calculating overdue
        account: The subscriber account (optional, for grace period)

    Returns:
        Number of days overdue (after grace period), minimum 0
    """
    if not invoice.due_at:
        return 0
    if account is None or db is None:
        return max((run_at.date() - invoice.due_at.date()).days, 0)
    return resolve_grace_decision(
        db,
        account,
        starts_at=invoice.due_at,
        as_of=run_at,
        policy_set_id=policy_set_id,
    ).elapsed_days_after_grace


def _create_action_log(
    db: Session,
    case: DunningCase,
    action: DunningAction,
    step_day: int | None,
    invoice_id: str | None,
    outcome: str | None = None,
    notes: str | None = None,
    access_consequence: FinancialAccessConsequence | None = None,
):
    log = DunningActionLog(
        case_id=case.id,
        invoice_id=invoice_id,
        step_day=step_day,
        action=action,
        outcome=outcome,
        notes=notes,
        access_consequence_id=(
            access_consequence.id if access_consequence is not None else None
        ),
    )
    db.add(log)
    return log


def _refresh_account_status(db: Session, account_id) -> None:
    """Keep subscriber status aligned with dunning case state."""
    from app.services.account_lifecycle import compute_account_status

    try:
        compute_account_status(db, str(account_id))
    except ValueError:
        logger.warning(
            "Cannot recompute account status for %s: account not found", account_id
        )


def _account_has_dedicated_bundle(db: Session, account_id) -> bool:
    """True if any of the account's subscriptions is in a dedicated bundle.

    Dedicated internet (``plan_family='dedicated'``) is contract/SLA-managed and
    must never be auto-suspended. Because a bundle shares one subscriber-level
    RADIUS identity, a single dedicated member makes the whole account hands-off.
    """
    from app.models.catalog import SubscriptionBundle

    return (
        db.scalar(
            select(SubscriptionBundle.id)
            .join(Subscription, Subscription.bundle_id == SubscriptionBundle.id)
            .where(
                Subscription.subscriber_id == coerce_uuid(account_id),
                SubscriptionBundle.is_dedicated.is_(True),
            )
            .limit(1)
        )
        is not None
    )


def preview_financial_access_consequence(
    db: Session,
    account_id: str,
    *,
    action: FinancialAccessAction,
    reason: EnforcementReason,
    origin: FinancialAccessOrigin,
    dunning_case_id: UUID | None = None,
    overdue_days: int | None = None,
) -> FinancialAccessConsequencePreview:
    """Resolve a financial access consequence without mutating service state."""
    account_uuid = coerce_uuid(account_id)
    account = db.get(Subscriber, account_uuid)
    target_subscriptions: tuple[UUID, ...] = ()
    credential_changes: tuple[FinancialAccessCredentialChange, ...] = ()
    eligible = True
    outcome = f"{action.value}_ready"
    receivables: list[dict] = []
    prepaid_funding: dict | None = None
    prepaid_target_ids: set[UUID] | None = None
    grace_decision: dict | None = None
    access_decision: dict | None = None
    shield_reason: str | None = None
    health_reasons: list[str] = []
    profile_payload: dict = {"valid": False, "automation_safe": False}
    dedicated_bundle = False
    window_decision = enforcement_window.resolve_enforcement_window_decision(db)

    if account is None:
        eligible = False
        outcome = "account_not_found"
    elif account.status == SubscriberStatus.canceled:
        eligible = False
        outcome = "account_canceled"
    else:
        dedicated_bundle = _account_has_dedicated_bundle(db, account.id)
        profile = resolve_billing_profile(db, account)
        profile_payload = {
            "valid": profile.is_valid,
            "automation_safe": profile.automation_safe,
            "effective_mode": (
                profile.effective_mode.value if profile.effective_mode else None
            ),
            "source": profile.source,
            "invalid_reason": profile.invalid_reason,
        }
        if dedicated_bundle:
            eligible = False
            outcome = "dedicated_bundle"
        elif not profile.automation_safe:
            eligible = False
            outcome = "billing_profile_invalid"
        elif reason == EnforcementReason.overdue:
            receivables = _overdue_receivable_snapshot(db, account.id)
            due_at = (
                db.query(func.min(Invoice.due_at))
                .filter(
                    Invoice.id.in_(
                        [coerce_uuid(item["invoice_id"]) for item in receivables]
                    )
                )
                .scalar()
                if receivables
                else None
            )
            case = db.get(DunningCase, dunning_case_id) if dunning_case_id else None
            grace_decision = resolve_grace_decision(
                db,
                account,
                starts_at=due_at,
                policy_set_id=case.policy_set_id if case else None,
            ).as_dict()
            grace_decision.pop("as_of", None)
            if profile.effective_mode != BillingMode.postpaid:
                eligible = False
                outcome = "billing_profile_invalid"
            elif not receivables:
                eligible = False
                outcome = "balance_cleared"
        elif reason == EnforcementReason.prepaid:
            if profile.effective_mode != BillingMode.prepaid:
                eligible = False
                outcome = "billing_profile_invalid"
            else:
                funding = resolve_prepaid_funding(db, account)
                prepaid_funding = {
                    "currency": funding.currency,
                    "available_balance": f"{funding.available_balance:.2f}",
                    "required_balance": f"{funding.required_balance:.2f}",
                    "funded": funding.funded,
                    "covered_subscription_ids": [
                        str(value) for value in funding.covered_subscription_ids
                    ],
                    "actionable_uncovered_subscription_ids": [
                        str(value)
                        for value in funding.actionable_uncovered_subscription_ids
                    ],
                    "unresolved_projection_subscription_ids": [
                        str(value)
                        for value in funding.unresolved_projection_subscription_ids
                    ],
                    "unresolved_renewal_subscription_ids": [
                        str(value)
                        for value in funding.unresolved_renewal_subscription_ids
                    ],
                }
                prepaid_target_ids = set(funding.actionable_uncovered_subscription_ids)
                grace_decision = resolve_grace_decision(
                    db,
                    account,
                    starts_at=account.prepaid_low_balance_at,
                ).as_dict()
                grace_decision.pop("as_of", None)
                if funding.unresolved_projection_subscription_ids:
                    eligible = False
                    outcome = "prepaid_coverage_unresolved"
                elif funding.unresolved_renewal_subscription_ids:
                    eligible = False
                    outcome = "prepaid_renewal_terms_unresolved"
                elif funding.funded:
                    eligible = False
                    outcome = "prepaid_balance_available"

        if eligible:
            shield_reason = _dunning_shield_reason(db, account.id)
            if shield_reason:
                eligible = False
                outcome = "shielded"
        if eligible and overdue_days is not None:
            minimum_age_skip = _minimum_enforcement_age_skip_reason(
                db, account, overdue_days
            )
            if minimum_age_skip:
                eligible = False
                outcome = minimum_age_skip
        if (
            eligible
            and reason == EnforcementReason.overdue
            and action
            in {
                FinancialAccessAction.suspend,
                FinancialAccessAction.reject,
                FinancialAccessAction.throttle,
            }
            and window_decision.should_defer
        ):
            eligible = False
            outcome = "outside_enforcement_window"
        from app.services.billing_enforcement_guards import billing_enforcement_health

        # Health is operator-visible evidence, not a global customer-lifecycle
        # switch. Current account facts decide the consequence.
        health_reasons = list(billing_enforcement_health(db).reasons)

        if eligible and action == FinancialAccessAction.throttle:
            throttle_profile_id = settings_spec.resolve_value(
                db, SettingDomain.collections, "throttle_radius_profile_id"
            )
            throttle_profile = (
                db.get(RadiusProfile, coerce_uuid(throttle_profile_id))
                if throttle_profile_id
                else None
            )
            if throttle_profile is None or not throttle_profile.is_active:
                eligible = False
                outcome = "throttle_failed"
            else:
                credentials = (
                    db.query(AccessCredential)
                    .filter(AccessCredential.subscriber_id == account.id)
                    .filter(AccessCredential.is_active.is_(True))
                    .order_by(AccessCredential.id.asc())
                    .all()
                )
                credential_changes = tuple(
                    FinancialAccessCredentialChange(
                        credential_id=credential.id,
                        profile_before_id=credential.radius_profile_id,
                        profile_after_id=throttle_profile.id,
                    )
                    for credential in credentials
                    if credential.radius_profile_id != throttle_profile.id
                )
                if not credential_changes:
                    eligible = False
                    outcome = (
                        "already_throttled"
                        if credentials
                        else "no_credentials_to_throttle"
                    )
        elif eligible:
            subscriptions = (
                db.query(Subscription)
                .filter(Subscription.subscriber_id == account.id)
                .filter(Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES))
                .order_by(Subscription.id.asc())
                .all()
            )
            active_reason_locks = {
                row[0]
                for row in (
                    db.query(EnforcementLock.subscription_id)
                    .filter(EnforcementLock.subscriber_id == account.id)
                    .filter(EnforcementLock.reason == reason)
                    .filter(EnforcementLock.is_active.is_(True))
                    .all()
                )
            }
            target_subscriptions = tuple(
                subscription.id
                for subscription in subscriptions
                if subscription.id not in active_reason_locks
                and (
                    reason != EnforcementReason.prepaid
                    or prepaid_target_ids is None
                    or subscription.id in prepaid_target_ids
                )
            )
            if not target_subscriptions:
                eligible = False
                outcome = (
                    "already_rejected"
                    if action == FinancialAccessAction.reject and active_reason_locks
                    else (
                        "already_suspended"
                        if active_reason_locks
                        else "no_eligible_subscriptions"
                    )
                )

        if action in {
            FinancialAccessAction.suspend,
            FinancialAccessAction.reject,
        }:
            requested_mode = (
                AccessRestrictionMode.captive
                if action == FinancialAccessAction.suspend
                else AccessRestrictionMode.hard_reject
            )
            access_decision = resolve_walled_garden_decision(
                db,
                account,
                requested_mode=requested_mode,
            ).as_dict()

    inputs = {
        "account_status": (
            account.status.value if account is not None and account.status else None
        ),
        "profile": profile_payload,
        "overdue_receivables": receivables,
        "prepaid_funding": prepaid_funding,
        "grace_decision": grace_decision,
        "access_decision": access_decision,
        "shield_reason": shield_reason,
        "billing_health_reasons": health_reasons,
        "dedicated_bundle": dedicated_bundle,
        "inside_enforcement_window": window_decision.inside_window,
        "enforcement_window_block_reason": window_decision.block_reason,
        "overdue_days": overdue_days,
        "target_subscription_ids": [str(value) for value in target_subscriptions],
        "credential_changes": [
            {
                "credential_id": str(change.credential_id),
                "profile_before_id": (
                    str(change.profile_before_id) if change.profile_before_id else None
                ),
                "profile_after_id": (
                    str(change.profile_after_id) if change.profile_after_id else None
                ),
            }
            for change in credential_changes
        ],
    }
    fingerprint_payload = {
        "account_id": str(account_uuid),
        "action": action.value,
        "requested_reason": reason.value,
        "origin": origin.value,
        "dunning_case_id": str(dunning_case_id) if dunning_case_id else None,
        "eligible": eligible,
        "outcome": outcome,
        "inputs": inputs,
    }
    return FinancialAccessConsequencePreview(
        account_id=account_uuid,
        action=action,
        requested_reason=reason,
        origin=origin,
        dunning_case_id=dunning_case_id,
        eligible=eligible,
        outcome=outcome,
        target_subscription_ids=target_subscriptions,
        target_lock_ids=(),
        target_case_ids=(),
        credential_changes=credential_changes,
        decision_inputs=inputs,
        fingerprint=_financial_access_fingerprint(fingerprint_payload),
    )


def _financial_access_replay(
    db: Session,
    *,
    idempotency_key: str,
    account_id: UUID,
    action: FinancialAccessAction,
    requested_reason: EnforcementReason | None,
    preview_fingerprint: str,
) -> FinancialAccessConsequenceResult | None:
    consequence = db.scalar(
        select(FinancialAccessConsequence).where(
            FinancialAccessConsequence.idempotency_key == idempotency_key
        )
    )
    if consequence is None:
        return None
    if (
        consequence.account_id != account_id
        or consequence.action != action
        or consequence.requested_reason != requested_reason
        or consequence.preview_fingerprint != preview_fingerprint
    ):
        raise HTTPException(
            status_code=409,
            detail="Idempotency key was used for a different access consequence",
        )
    return FinancialAccessConsequenceResult(
        consequence=consequence,
        preview=FinancialAccessConsequencePreview(
            account_id=consequence.account_id,
            action=consequence.action,
            requested_reason=consequence.requested_reason,
            origin=consequence.origin,
            dunning_case_id=consequence.dunning_case_id,
            eligible=consequence.eligible,
            outcome=consequence.outcome,
            target_subscription_ids=tuple(
                coerce_uuid(value)
                for value in consequence.decision_inputs.get(
                    "target_subscription_ids", []
                )
            ),
            target_lock_ids=(),
            target_case_ids=(),
            credential_changes=(),
            decision_inputs=consequence.decision_inputs,
            fingerprint=consequence.preview_fingerprint,
        ),
        subscriptions_changed=int(consequence.result.get("subscriptions_changed", 0)),
        idempotent_replay=True,
    )


def confirm_financial_access_consequence(
    db: Session,
    account_id: str,
    *,
    action: FinancialAccessAction,
    reason: EnforcementReason,
    origin: FinancialAccessOrigin,
    preview_fingerprint: str,
    idempotency_key: str,
    source: str,
    dunning_case_id: UUID | None = None,
    overdue_days: int | None = None,
    commit: bool = False,
) -> FinancialAccessConsequenceResult:
    """Lock, recompute, confirm, and evidence one financial access action."""
    key = idempotency_key.strip()
    if not key or len(key) > 120:
        raise HTTPException(status_code=400, detail="Invalid idempotency key")
    account_uuid = coerce_uuid(account_id)
    replay = _financial_access_replay(
        db,
        idempotency_key=key,
        account_id=account_uuid,
        action=action,
        requested_reason=reason,
        preview_fingerprint=preview_fingerprint,
    )
    if replay:
        return replay
    account = db.execute(
        select(Subscriber).where(Subscriber.id == account_uuid).with_for_update()
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    preview = preview_financial_access_consequence(
        db,
        account_id,
        action=action,
        reason=reason,
        origin=origin,
        dunning_case_id=dunning_case_id,
        overdue_days=overdue_days,
    )
    if preview.fingerprint != preview_fingerprint:
        raise HTTPException(
            status_code=409,
            detail="Financial access state changed after preview; preview again",
        )

    lock_results: list[EnforcementLock] = []
    credential_results: list[FinancialAccessCredentialChange] = []
    subscriptions_changed = 0
    if preview.eligible and action in {
        FinancialAccessAction.suspend,
        FinancialAccessAction.reject,
    }:
        from app.services.account_lifecycle import suspend_subscription

        access_mode = AccessRestrictionMode(
            preview.decision_inputs["access_decision"]["effective_mode"]
        )

        for subscription_id in preview.target_subscription_ids:
            subscription = db.get(Subscription, subscription_id)
            before = subscription.status if subscription is not None else None
            lock = suspend_subscription(
                db,
                str(subscription_id),
                reason=reason,
                source=source,
                access_mode=access_mode,
            )
            lock_results.append(lock)
            if before not in {
                SubscriptionStatus.suspended,
                SubscriptionStatus.blocked,
                SubscriptionStatus.stopped,
            }:
                subscriptions_changed += 1
    elif preview.eligible and action == FinancialAccessAction.throttle:
        credential_ids = [change.credential_id for change in preview.credential_changes]
        credentials = {
            credential.id: credential
            for credential in (
                db.query(AccessCredential)
                .filter(AccessCredential.id.in_(credential_ids))
                .with_for_update()
                .all()
            )
        }
        for change in preview.credential_changes:
            credential = credentials.get(change.credential_id)
            if credential is None:
                raise HTTPException(
                    status_code=409,
                    detail="Access credential changed after preview; preview again",
                )
            if (
                credential.radius_profile_id != change.profile_before_id
                or change.profile_after_id is None
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Access credential profile changed after preview",
                )
            if credential.radius_profile_id is not None:
                credential.pre_throttle_radius_profile_id = credential.radius_profile_id
            credential.radius_profile_id = change.profile_after_id
            credential_results.append(change)
        db.flush()
        emit_event(
            db,
            EventType.subscriber_throttled,
            {
                "account_id": str(account.id),
                "credentials_throttled": len(credential_results),
                "throttle_profile_id": (
                    str(credential_results[0].profile_after_id)
                    if credential_results
                    else None
                ),
            },
            account_id=account.id,
        )

    outcome = preview.outcome
    if preview.eligible:
        if action == FinancialAccessAction.suspend:
            outcome = "suspended"
        elif action == FinancialAccessAction.reject:
            outcome = "rejected"
        elif action == FinancialAccessAction.throttle:
            outcome = "throttled"

    consequence = FinancialAccessConsequence(
        account_id=account.id,
        dunning_case_id=dunning_case_id,
        action=action,
        requested_reason=reason,
        access_mode=(
            AccessRestrictionMode(
                preview.decision_inputs["access_decision"]["effective_mode"]
            )
            if preview.decision_inputs.get("access_decision")
            else None
        ),
        origin=origin,
        eligible=preview.eligible,
        outcome=outcome,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=key,
        decision_inputs=preview.decision_inputs,
        result={
            "subscriptions_changed": subscriptions_changed,
            "enforcement_lock_ids": [str(lock.id) for lock in lock_results],
            "access_mode": (
                preview.decision_inputs["access_decision"]["effective_mode"]
                if preview.decision_inputs.get("access_decision")
                else None
            ),
            "access_credential_ids": [
                str(change.credential_id) for change in credential_results
            ],
        },
    )
    db.add(consequence)
    db.flush()
    evidence: list[FinancialAccessConsequenceEvidence] = [
        FinancialAccessConsequenceEvidence(
            consequence_id=consequence.id,
            enforcement_lock_id=lock.id,
            operation=FinancialAccessEvidenceOperation.lock_created,
        )
        for lock in lock_results
    ]
    evidence.extend(
        FinancialAccessConsequenceEvidence(
            consequence_id=consequence.id,
            access_credential_id=change.credential_id,
            operation=FinancialAccessEvidenceOperation.credential_throttled,
            profile_before_id=change.profile_before_id,
            profile_after_id=change.profile_after_id,
        )
        for change in credential_results
    )
    db.add_all(evidence)
    db.flush()
    consequence.evidence = evidence
    AuditEvents.stage(
        db,
        AuditEventCreate(
            action="confirm_financial_access_consequence",
            entity_type="subscriber",
            entity_id=str(account.id),
            metadata_={
                "consequence_id": str(consequence.id),
                "action": action.value,
                "requested_reason": reason.value,
                "origin": origin.value,
                "outcome": outcome,
                "preview_fingerprint": preview.fingerprint,
                "enforcement_lock_ids": [str(lock.id) for lock in lock_results],
                "access_credential_ids": [
                    str(change.credential_id) for change in credential_results
                ],
            },
        ),
    )
    if subscriptions_changed:
        emit_event(
            db,
            EventType.subscriber_suspended,
            {
                "account_id": str(account.id),
                "subscriber_id": str(account.id),
                "suspended_subscriptions": subscriptions_changed,
                "access_consequence_id": str(consequence.id),
            },
            account_id=account.id,
            subscriber_id=account.id,
        )
    if commit:
        db.commit()
        db.refresh(consequence)
    return FinancialAccessConsequenceResult(
        consequence=consequence,
        preview=preview,
        subscriptions_changed=subscriptions_changed,
    )


def _suspend_account(
    db: Session,
    account_id: str,
    reason: EnforcementReason = EnforcementReason.overdue,
    source: str = "dunning",
) -> bool:
    """Compatibility adapter through the canonical consequence owner."""
    origin = (
        FinancialAccessOrigin.prepaid_enforcement
        if reason == EnforcementReason.prepaid
        else FinancialAccessOrigin.dunning
    )
    preview = preview_financial_access_consequence(
        db,
        account_id,
        action=FinancialAccessAction.suspend,
        reason=reason,
        origin=origin,
    )
    result = confirm_financial_access_consequence(
        db,
        account_id,
        action=FinancialAccessAction.suspend,
        reason=reason,
        origin=origin,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=(
            f"financial-suspend:{account_id}:{reason.value}:{preview.fingerprint[:24]}"
        ),
        source=source,
    )
    return bool(result.consequence.result.get("enforcement_lock_ids"))


def preview_financial_access_restoration(
    db: Session,
    account_id: str,
    *,
    origin: FinancialAccessOrigin = FinancialAccessOrigin.financial_reconciliation,
) -> FinancialAccessConsequencePreview:
    """Preview exact financial locks/cases/throttles eligible for release."""
    account_uuid = coerce_uuid(account_id)
    account = db.get(Subscriber, account_uuid)
    target_locks: list[EnforcementLock] = []
    target_cases: list[DunningCase] = []
    credential_changes: list[FinancialAccessCredentialChange] = []
    receivables: list[dict] = []
    prepaid_funding: dict | None = None
    legacy_throttle_ids: list[str] = []
    clear_prepaid_timers = False
    eligible = True
    outcome = "restore_ready"
    profile_payload: dict = {"valid": False, "automation_safe": False}

    if account is None:
        eligible = False
        outcome = "account_not_found"
    elif account.status == SubscriberStatus.canceled:
        eligible = False
        outcome = "account_canceled"
    else:
        receivables = _overdue_receivable_snapshot(db, account.id)
        profile = resolve_billing_profile(db, account)
        profile_payload = {
            "valid": profile.is_valid,
            "automation_safe": profile.automation_safe,
            "effective_mode": (
                profile.effective_mode.value if profile.effective_mode else None
            ),
            "source": profile.source,
            "invalid_reason": profile.invalid_reason,
        }
        active_locks = (
            db.query(EnforcementLock)
            .filter(EnforcementLock.subscriber_id == account.id)
            .filter(EnforcementLock.is_active.is_(True))
            .filter(
                EnforcementLock.reason.in_(
                    [EnforcementReason.overdue, EnforcementReason.prepaid]
                )
            )
            .order_by(EnforcementLock.created_at.asc(), EnforcementLock.id.asc())
            .all()
        )
        if not receivables:
            target_locks.extend(
                lock
                for lock in active_locks
                if lock.reason == EnforcementReason.overdue
            )
            target_cases = (
                db.query(DunningCase)
                .filter(DunningCase.account_id == account.id)
                .filter(DunningCase.status == DunningCaseStatus.open)
                .order_by(DunningCase.started_at.asc(), DunningCase.id.asc())
                .all()
            )

            throttle_profile_id = settings_spec.resolve_value(
                db, SettingDomain.collections, "throttle_radius_profile_id"
            )
            if throttle_profile_id:
                credentials = (
                    db.query(AccessCredential)
                    .filter(AccessCredential.subscriber_id == account.id)
                    .filter(
                        AccessCredential.radius_profile_id
                        == coerce_uuid(throttle_profile_id)
                    )
                    .filter(AccessCredential.is_active.is_(True))
                    .order_by(AccessCredential.id.asc())
                    .all()
                )
                for credential in credentials:
                    if credential.pre_throttle_radius_profile_id is None:
                        legacy_throttle_ids.append(str(credential.id))
                        continue
                    credential_changes.append(
                        FinancialAccessCredentialChange(
                            credential_id=credential.id,
                            profile_before_id=credential.radius_profile_id,
                            profile_after_id=(
                                credential.pre_throttle_radius_profile_id
                            ),
                        )
                    )

        if not profile.has_collectible_subscriptions:
            target_locks.extend(
                lock
                for lock in active_locks
                if lock.reason == EnforcementReason.prepaid
            )
            clear_prepaid_timers = True
        elif profile.automation_safe and profile.effective_mode == BillingMode.prepaid:
            funding = resolve_prepaid_funding(db, account)
            prepaid_funding = {
                "currency": funding.currency,
                "available_balance": f"{funding.available_balance:.2f}",
                "required_balance": f"{funding.required_balance:.2f}",
                "funded": funding.funded,
                "covered_subscription_ids": [
                    str(value) for value in funding.covered_subscription_ids
                ],
                "actionable_uncovered_subscription_ids": [
                    str(value)
                    for value in funding.actionable_uncovered_subscription_ids
                ],
                "unresolved_projection_subscription_ids": [
                    str(value)
                    for value in funding.unresolved_projection_subscription_ids
                ],
                "unresolved_renewal_subscription_ids": [
                    str(value) for value in funding.unresolved_renewal_subscription_ids
                ],
            }
            restorable_prepaid_ids = (
                {
                    lock.subscription_id
                    for lock in active_locks
                    if lock.reason == EnforcementReason.prepaid
                }
                if funding.funded
                else set(funding.covered_subscription_ids)
            )
            target_locks.extend(
                lock
                for lock in active_locks
                if lock.reason == EnforcementReason.prepaid
                and lock.subscription_id in restorable_prepaid_ids
            )
            if funding.funded:
                clear_prepaid_timers = True
        elif profile.automation_safe and profile.effective_mode != BillingMode.prepaid:
            target_locks.extend(
                lock
                for lock in active_locks
                if lock.reason == EnforcementReason.prepaid
            )
            clear_prepaid_timers = True

        if not (
            target_locks
            or target_cases
            or credential_changes
            or (
                clear_prepaid_timers
                and (
                    account.prepaid_low_balance_at is not None
                    or account.prepaid_deactivation_at is not None
                )
            )
        ):
            outcome = "no_change"

    target_subscription_ids = tuple(
        sorted({lock.subscription_id for lock in target_locks}, key=str)
    )
    inputs = {
        "account_status": (
            account.status.value if account is not None and account.status else None
        ),
        "profile": profile_payload,
        "overdue_receivables": receivables,
        "prepaid_funding": prepaid_funding,
        "target_subscription_ids": [str(value) for value in target_subscription_ids],
        "target_lock_ids": [str(lock.id) for lock in target_locks],
        "target_case_ids": [str(case.id) for case in target_cases],
        "credential_changes": [
            {
                "credential_id": str(change.credential_id),
                "profile_before_id": (
                    str(change.profile_before_id) if change.profile_before_id else None
                ),
                "profile_after_id": (
                    str(change.profile_after_id) if change.profile_after_id else None
                ),
            }
            for change in credential_changes
        ],
        "legacy_throttle_credential_ids": legacy_throttle_ids,
        "clear_prepaid_timers": clear_prepaid_timers,
        "prepaid_low_balance_at": (
            account.prepaid_low_balance_at.isoformat()
            if account is not None and account.prepaid_low_balance_at
            else None
        ),
        "prepaid_deactivation_at": (
            account.prepaid_deactivation_at.isoformat()
            if account is not None and account.prepaid_deactivation_at
            else None
        ),
    }
    fingerprint_payload = {
        "account_id": str(account_uuid),
        "action": FinancialAccessAction.restore.value,
        "origin": origin.value,
        "eligible": eligible,
        "outcome": outcome,
        "inputs": inputs,
    }
    return FinancialAccessConsequencePreview(
        account_id=account_uuid,
        action=FinancialAccessAction.restore,
        requested_reason=None,
        origin=origin,
        dunning_case_id=None,
        eligible=eligible,
        outcome=outcome,
        target_subscription_ids=target_subscription_ids,
        target_lock_ids=tuple(lock.id for lock in target_locks),
        target_case_ids=tuple(case.id for case in target_cases),
        credential_changes=tuple(credential_changes),
        decision_inputs=inputs,
        fingerprint=_financial_access_fingerprint(fingerprint_payload),
    )


def confirm_financial_access_restoration(
    db: Session,
    account_id: str,
    *,
    preview_fingerprint: str,
    idempotency_key: str,
    origin: FinancialAccessOrigin = FinancialAccessOrigin.financial_reconciliation,
    invoice_id: str | None = None,
    resolved_by: str | None = None,
    overdue_trigger: str = "payment",
    commit: bool = False,
) -> FinancialAccessConsequenceResult:
    """Confirm exact eligible financial lock, throttle, and case releases."""
    key = idempotency_key.strip()
    if not key or len(key) > 120:
        raise HTTPException(status_code=400, detail="Invalid idempotency key")
    account_uuid = coerce_uuid(account_id)
    replay = _financial_access_replay(
        db,
        idempotency_key=key,
        account_id=account_uuid,
        action=FinancialAccessAction.restore,
        requested_reason=None,
        preview_fingerprint=preview_fingerprint,
    )
    if replay:
        return replay
    account = db.execute(
        select(Subscriber).where(Subscriber.id == account_uuid).with_for_update()
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    preview = preview_financial_access_restoration(db, account_id, origin=origin)
    if preview.fingerprint != preview_fingerprint:
        raise HTTPException(
            status_code=409,
            detail="Financial access state changed after preview; preview again",
        )

    from app.services.account_lifecycle import (
        SUSPENDED_EQUIVALENT,
        resolve_stale_lock_without_restoration,
        restore_subscription_detailed,
    )
    from app.services.settled_access_blocked import (
        clear_financially_settled_but_access_blocked,
    )

    resolved_lock_ids: list[UUID] = []
    restored_subscriptions = 0
    restoration_outcomes: list[dict[str, object]] = []
    for lock_id in preview.target_lock_ids:
        lock = db.get(EnforcementLock, lock_id)
        if lock is None or not lock.is_active:
            raise HTTPException(
                status_code=409,
                detail="Financial enforcement lock changed after preview",
            )
        trigger = (
            "top_up" if lock.reason == EnforcementReason.prepaid else overdue_trigger
        )
        subscription = db.get(Subscription, lock.subscription_id)
        if subscription is None:
            raise HTTPException(
                status_code=409,
                detail="Financial enforcement subscription no longer exists",
            )
        if subscription.status in SUSPENDED_EQUIVALENT:
            outcome = restore_subscription_detailed(
                db,
                str(lock.subscription_id),
                trigger=trigger,
                resolved_by=resolved_by or f"financial_access:{account.id}",
                reason=lock.reason,
            )
            # `subscriptions_changed` is a count of state transitions and keeps
            # its legacy meaning; the worklist entry is only cleared once the
            # customer genuinely has service again, so an account still held by
            # a lifecycle override stays on the queue.
            restored = outcome.subscription_reactivated
            restoration_outcomes.append(outcome.to_dict())
            if outcome.access_restored:
                clear_financially_settled_but_access_blocked(
                    db, str(lock.subscription_id)
                )
        else:
            resolved = resolve_stale_lock_without_restoration(
                db,
                str(lock.subscription_id),
                trigger=trigger,
                resolved_by=resolved_by or f"financial_access:{account.id}",
                reason=lock.reason,
                notes="Stale financial lock reconciled without service activation",
            )
            restored = False
            if resolved == 0:
                raise HTTPException(
                    status_code=409,
                    detail="Financial enforcement lock was not resolved by its owner",
                )
        db.flush()
        if lock.is_active:
            raise HTTPException(
                status_code=409,
                detail="Financial enforcement lock was not resolved by its owner",
            )
        resolved_lock_ids.append(lock.id)
        if restored:
            restored_subscriptions += 1

    resolved_case_ids: list[UUID] = []
    now = datetime.now(UTC)
    for case_id in preview.target_case_ids:
        case = db.get(DunningCase, case_id)
        if case is None or case.status != DunningCaseStatus.open:
            raise HTTPException(
                status_code=409, detail="Dunning case changed after preview"
            )
        case.status = DunningCaseStatus.resolved
        case.resolved_at = now
        _create_action_log(
            db,
            case,
            DunningAction.notify,
            case.current_step,
            invoice_id,
            outcome="resolved",
            notes=(
                "Resolved after financial access reconciliation"
                if overdue_trigger == "payment"
                else "Resolved with no overdue receivable"
            ),
        )
        emit_event(
            db,
            EventType.dunning_resolved,
            {
                "case_id": str(case.id),
                "account_id": str(case.account_id),
                "reason": (
                    "financial_reconciliation"
                    if overdue_trigger == "payment"
                    else "no_overdue_invoices"
                ),
            },
            account_id=case.account_id,
        )
        resolved_case_ids.append(case.id)

    restored_credentials: list[FinancialAccessCredentialChange] = []
    credential_ids = [change.credential_id for change in preview.credential_changes]
    credentials = {
        credential.id: credential
        for credential in (
            db.query(AccessCredential)
            .filter(AccessCredential.id.in_(credential_ids))
            .with_for_update()
            .all()
            if credential_ids
            else []
        )
    }
    for change in preview.credential_changes:
        credential = credentials.get(change.credential_id)
        if (
            credential is None
            or credential.radius_profile_id != change.profile_before_id
            or credential.pre_throttle_radius_profile_id != change.profile_after_id
        ):
            raise HTTPException(
                status_code=409,
                detail="Throttled credential changed after preview",
            )
        credential.radius_profile_id = change.profile_after_id
        credential.pre_throttle_radius_profile_id = None
        restored_credentials.append(change)

    if preview.decision_inputs.get("clear_prepaid_timers"):
        _clear_prepaid_dunning_flags(db, account_id)
    db.flush()
    if resolved_case_ids:
        _refresh_account_status(db, account.id)
    if restored_credentials:
        emit_event(
            db,
            EventType.subscriber_unthrottled,
            {
                "account_id": str(account.id),
                "credentials_restored": len(restored_credentials),
            },
            account_id=account.id,
        )

    has_effect = bool(resolved_lock_ids or resolved_case_ids or restored_credentials)
    consequence_outcome = (
        "restored"
        if restored_subscriptions
        else ("reconciled" if has_effect else preview.outcome)
    )
    consequence = FinancialAccessConsequence(
        account_id=account.id,
        action=FinancialAccessAction.restore,
        requested_reason=None,
        origin=origin,
        eligible=preview.eligible,
        outcome=consequence_outcome,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=key,
        decision_inputs=preview.decision_inputs,
        result={
            "subscriptions_changed": restored_subscriptions,
            "enforcement_lock_ids": [str(value) for value in resolved_lock_ids],
            "dunning_case_ids": [str(value) for value in resolved_case_ids],
            "access_credential_ids": [
                str(change.credential_id) for change in restored_credentials
            ],
        },
    )
    db.add(consequence)
    db.flush()
    evidence = [
        FinancialAccessConsequenceEvidence(
            consequence_id=consequence.id,
            enforcement_lock_id=lock_id,
            operation=FinancialAccessEvidenceOperation.lock_resolved,
        )
        for lock_id in resolved_lock_ids
    ]
    evidence.extend(
        FinancialAccessConsequenceEvidence(
            consequence_id=consequence.id,
            dunning_case_id=case_id,
            operation=FinancialAccessEvidenceOperation.dunning_case_resolved,
        )
        for case_id in resolved_case_ids
    )
    evidence.extend(
        FinancialAccessConsequenceEvidence(
            consequence_id=consequence.id,
            access_credential_id=change.credential_id,
            operation=FinancialAccessEvidenceOperation.credential_restored,
            profile_before_id=change.profile_before_id,
            profile_after_id=change.profile_after_id,
        )
        for change in restored_credentials
    )
    db.add_all(evidence)
    db.flush()
    consequence.evidence = evidence
    AuditEvents.stage(
        db,
        AuditEventCreate(
            action="confirm_financial_access_restoration",
            entity_type="subscriber",
            entity_id=str(account.id),
            metadata_={
                "consequence_id": str(consequence.id),
                "origin": origin.value,
                "outcome": consequence_outcome,
                "preview_fingerprint": preview.fingerprint,
                "resolved_lock_ids": [str(value) for value in resolved_lock_ids],
                "resolved_case_ids": [str(value) for value in resolved_case_ids],
                "restored_credential_ids": [
                    str(change.credential_id) for change in restored_credentials
                ],
                "legacy_throttle_credential_ids": preview.decision_inputs.get(
                    "legacy_throttle_credential_ids", []
                ),
            },
        ),
    )
    if restored_subscriptions:
        emit_event(
            db,
            EventType.subscriber_reactivated,
            {
                "account_id": str(account.id),
                "subscriber_id": str(account.id),
                "restored_subscriptions": restored_subscriptions,
                "access_consequence_id": str(consequence.id),
            },
            account_id=account.id,
            subscriber_id=account.id,
        )
    if commit:
        db.commit()
        db.refresh(consequence)
    return FinancialAccessConsequenceResult(
        consequence=consequence,
        preview=preview,
        subscriptions_changed=restored_subscriptions,
        restoration_outcomes=tuple(restoration_outcomes),
    )


def confirm_financial_access_restoration_for_owner(
    db: Session,
    command: FinancialAccessRestorationParticipantCommand,
) -> FinancialAccessConsequenceResult:
    """Apply one typed, flush-only restoration participant request."""

    preview = preview_financial_access_restoration(
        db,
        str(command.account_id),
        origin=command.origin,
    )
    try:
        return confirm_financial_access_restoration(
            db,
            str(command.account_id),
            preview_fingerprint=preview.fingerprint,
            idempotency_key=command.idempotency_key,
            origin=command.origin,
            invoice_id=str(command.invoice_id) if command.invoice_id else None,
            resolved_by=command.resolved_by,
        )
    except HTTPException as exc:
        raise FinancialAccessRestorationParticipantError(
            code="financial.dunning.restoration_participant_rejected",
            message="Financial access restoration evidence was rejected.",
            details={
                "account_id": str(command.account_id),
                "reason": str(exc.detail),
            },
        ) from exc


def _get_account_email(db: Session, account_id: str) -> str | None:
    """Get the billing email for an account."""
    account = cast(Subscriber | None, db.get(Subscriber, coerce_uuid(account_id)))
    if not account:
        return None
    return str(account.email) if account.email else None


def _throttle_account(db: Session, account_id: str) -> tuple[bool, int]:
    """Apply throttle RADIUS profile to account's access credentials.

    Throttling reduces bandwidth for the subscriber without fully suspending
    service. This requires a 'throttle' RADIUS profile to be configured.

    Args:
        db: Database session
        account_id: The account to throttle

    Returns:
        Tuple of (success: bool, credentials_throttled: int)
    """
    # Get throttle profile ID from settings
    throttle_profile_id = settings_spec.resolve_value(
        db, SettingDomain.collections, "throttle_radius_profile_id"
    )
    if not throttle_profile_id:
        logger.warning(
            f"Cannot throttle account {account_id}: throttle_radius_profile_id not configured"
        )
        return False, 0

    # Verify the throttle profile exists
    throttle_profile = db.get(RadiusProfile, throttle_profile_id)
    if not throttle_profile or not throttle_profile.is_active:
        logger.warning(
            f"Cannot throttle account {account_id}: throttle profile {throttle_profile_id} not found or inactive"
        )
        return False, 0

    # Get all active access credentials for the account
    credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == coerce_uuid(account_id))
        .filter(AccessCredential.is_active.is_(True))
        .all()
    )

    if not credentials:
        logger.info(f"No active credentials to throttle for account {account_id}")
        return True, 0

    throttled_count = 0
    for cred in credentials:
        # PERSIST the profile we are about to replace. The throttle is a temporary
        # override, so the value it overrides has to survive it — previously this
        # was only written to a log line, which meant a customer who paid could
        # never get their speed back.
        #
        # Only capture on the FIRST throttle: re-throttling an already-throttled
        # credential must not overwrite the real profile with the throttle profile.
        if cred.radius_profile_id and str(cred.radius_profile_id) != str(
            throttle_profile_id
        ):
            cred.pre_throttle_radius_profile_id = cred.radius_profile_id
        cred.radius_profile_id = throttle_profile.id
        throttled_count += 1

    # Emit throttle event
    emit_event(
        db,
        EventType.subscriber_throttled,
        {
            "account_id": str(account_id),
            "credentials_throttled": throttled_count,
            "throttle_profile_id": str(throttle_profile_id),
        },
        account_id=coerce_uuid(account_id),
    )

    logger.info(f"Throttled {throttled_count} credentials for account {account_id}")
    return True, throttled_count


def _restore_throttle(db: Session, account_id: str) -> int:
    """Remove throttle and restore original RADIUS profiles.

    When a throttled account makes payment, restore their original
    bandwidth by removing the throttle profile.

    Args:
        db: Database session
        account_id: The account to restore

    Returns:
        Number of credentials restored
    """
    throttle_profile_id = settings_spec.resolve_value(
        db, SettingDomain.collections, "throttle_radius_profile_id"
    )
    if not throttle_profile_id:
        return 0

    # Get credentials with throttle profile
    credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == coerce_uuid(account_id))
        .filter(AccessCredential.radius_profile_id == coerce_uuid(throttle_profile_id))
        .filter(AccessCredential.is_active.is_(True))
        .all()
    )

    if not credentials:
        return 0

    restored_count = 0
    for cred in credentials:
        if cred.pre_throttle_radius_profile_id is not None:
            # Give back exactly what we took. This preserves credential-level
            # overrides and provides exact restoration evidence.
            cred.radius_profile_id = cred.pre_throttle_radius_profile_id
            cred.pre_throttle_radius_profile_id = None
            restored_count += 1
            continue

        logger.warning(
            "Legacy throttled credential %s has no exact pre-throttle profile; "
            "leaving it unchanged for reviewed reconciliation",
            cred.id,
        )

    if restored_count:
        logger.info(
            f"Restored {restored_count} throttled credentials for account {account_id}"
        )
        # The throttle emits ``subscriber_throttled``, which enqueues a RADIUS
        # refresh. The un-throttle emitted nothing, so the customer's speed came
        # back only on the next scheduled sweep — the throttle landed in seconds
        # and the release took up to 15 minutes. Emit the mirror event.
        emit_event(
            db,
            EventType.subscriber_unthrottled,
            {
                "account_id": str(account_id),
                "credentials_restored": restored_count,
            },
            account_id=coerce_uuid(account_id),
        )

    return restored_count


def _create_throttle_notification(
    db: Session, account_id: str, days_overdue: int
) -> None:
    """Create email notification that account has been throttled."""
    from app.models.notification import NotificationChannel
    from app.schemas.notification import NotificationCreate
    from app.services.notification import notifications as notifications_svc

    email = _get_account_email(db, account_id)
    if not email:
        logger.warning(
            f"Cannot create throttle notification for account {account_id}: no email found"
        )
        return

    notifications_svc.queue_customer_notification(
        db,
        NotificationCreate(
            subscriber_id=coerce_uuid(account_id),
            channel=NotificationChannel.email,
            event_type="account_throttled",
            category="billing",
            recipient=email,
            subject="Service Speed Reduced - Payment Overdue",
            body=f"Your internet speed has been reduced due to payment being {days_overdue} days overdue. "
            "Please make a payment to restore full speed.",
        ),
    )
    logger.info(f"Created throttle notification for account {account_id}")


def _create_suspension_warning_notification(
    db: Session,
    account_id: str,
    days_overdue: int,
    note: str | None = None,
    invoice_id: str | None = None,
) -> None:
    """Emit a suspension warning event so notification policy owns delivery."""
    invoice = db.get(Invoice, coerce_uuid(invoice_id)) if invoice_id else None
    emit_event(
        db,
        EventType.subscription_suspension_warning,
        {
            "invoice_id": str(invoice.id) if invoice else (invoice_id or ""),
            "invoice_number": invoice.invoice_number if invoice else "",
            "amount": str(invoice.balance_due or invoice.total or 0)
            if invoice
            else "0.00",
            "days_overdue": str(days_overdue),
            "grace_hours": "0",
            "reason": "dunning",
            "note": note or "",
        },
        account_id=coerce_uuid(account_id),
    )
    logger.info("Emitted suspension warning event for account %s", account_id)


def _create_suspension_notification(db: Session, account_id: str) -> None:
    """Create email notification that account has been suspended."""
    from app.models.notification import Notification, NotificationChannel
    from app.schemas.notification import NotificationCreate
    from app.services.notification import notifications as notifications_svc

    email = _get_account_email(db, account_id)
    if not email:
        logger.warning(
            f"Cannot create suspension notification for account {account_id}: no email found"
        )
        return

    # Idempotency check: don't create duplicate suspension notification within the
    # configured suppression window.
    recent_threshold = datetime.now(UTC) - timedelta(
        hours=_suspension_notification_dedupe_hours(db)
    )
    existing = (
        db.query(Notification)
        .filter(Notification.recipient == email)
        .filter(Notification.subject == "Account Suspended")
        .filter(Notification.created_at > recent_threshold)
        .filter(Notification.is_active.is_(True))
        .first()
    )
    if existing:
        logger.debug(
            f"Skipping suspension notification for account {account_id}: recent notification exists"
        )
        return

    notifications_svc.queue_customer_notification(
        db,
        NotificationCreate(
            subscriber_id=coerce_uuid(account_id),
            channel=NotificationChannel.email,
            event_type="account_suspended",
            category="billing",
            recipient=email,
            subject="Account Suspended",
            body="Your account has been suspended due to non-payment. Please make a payment to restore service.",
        ),
    )
    logger.info(f"Created suspension notification for account {account_id}")


def _dunning_shield_reason(db: Session, account_id) -> str | None:
    """Return why dunning enforcement should be skipped, or None.

    Mirrors the event-driven overdue path (``EnforcementHandler.
    _suspension_shield_reason``) so the two enforcement systems agree: a
    customer with an admin-approved payment arrangement or a bank-transfer
    proof under review must NOT be dunned/suspended. The scheduled dunning
    runner previously ignored this shield entirely.
    """
    from app.models.payment_proof import PaymentProof, PaymentProofStatus

    arrangement_reason = active_arrangement_shield_reason(db, account_id)
    if arrangement_reason:
        return arrangement_reason
    proof_id = (
        db.query(PaymentProof.id)
        .filter(PaymentProof.account_id == account_id)
        .filter(PaymentProof.status == PaymentProofStatus.submitted)
        .limit(1)
        .scalar()
    )
    if proof_id:
        return f"payment proof {proof_id} pending review"
    from app.services.service_extensions import extension_shield_reason

    return extension_shield_reason(db, account_id)


def _bulk_dunning_shield_reasons(
    db: Session, account_ids: list[UUID] | set[UUID]
) -> dict[UUID, str]:
    """Return account shield reasons for a dunning cohort in bulk."""
    if not account_ids:
        return {}
    ids = {coerce_uuid(str(account_id)) for account_id in account_ids}
    from app.models.payment_proof import PaymentProof, PaymentProofStatus

    reasons = bulk_active_arrangement_shield_reasons(db, ids)

    proof_rows = (
        db.query(PaymentProof.account_id, PaymentProof.id)
        .filter(PaymentProof.account_id.in_(ids))
        .filter(PaymentProof.status == PaymentProofStatus.submitted)
        .all()
    )
    for account_id, proof_id in proof_rows:
        reasons.setdefault(account_id, f"payment proof {proof_id} pending review")

    from app.services.service_extensions import bulk_extension_shield_reasons

    for account_id, reason in bulk_extension_shield_reasons(db, ids).items():
        reasons.setdefault(account_id, reason)
    return reasons


# Dunning actions that enforce against the account (and so must re-check the
# live balance + shield right before acting, not trust the run's snapshot).
_ENFORCING_ACTIONS = frozenset(
    {DunningAction.suspend, DunningAction.reject, DunningAction.throttle}
)
_NON_ADVANCING_DUNNING_OUTCOMES = frozenset(
    {
        "balance_cleared",
        "shielded",
        "prepaid_balance_available",
        "billing_profile_invalid",
        "notice_grace_active",
        "enforcement_health_blocked",
    }
)


def _account_has_prepaid_service(db: Session, account: Subscriber) -> bool:
    profile = resolve_billing_profile(db, account)
    return profile.is_valid and profile.effective_mode == BillingMode.prepaid


def _prepaid_balance_gate_skip_reason(db: Session, account: Subscriber) -> str | None:
    """Return why prepaid enforcement should not cut service, or None.

    Prepaid service cuts are guarded by local available balance, not by prepaid
    invoice rows. Ledger credit that covers the account prevents suspension
    even if a legacy prepaid invoice row is still technically past due.
    """
    if not _account_has_prepaid_service(db, account):
        return None

    funding = resolve_prepaid_funding(db, account)
    if funding.funded:
        logger.info(
            "Dunning enforcement skipped for prepaid account %s: "
            "available balance %s >= threshold %s",
            account.id,
            funding.available_balance,
            funding.required_balance,
        )
        return "prepaid_balance_available"
    return None


def _minimum_enforcement_age_skip_reason(
    db: Session, account: Subscriber, overdue_days: int
) -> str | None:
    """Block service-affecting action until the notice runway has elapsed."""
    if _effective_billing_mode_for_account(db, account) == BillingMode.prepaid:
        return None
    value = settings_spec.resolve_value(
        db,
        SettingDomain.collections,
        "billing_enforcement_min_enforcing_day_offset",
    )
    try:
        minimum_days = int(str(value if value is not None else 3))
    except (TypeError, ValueError):
        minimum_days = 3
    if minimum_days <= 0:
        return None
    if overdue_days < minimum_days:
        logger.info(
            "Dunning enforcement skipped for account %s: overdue_days %s < "
            "minimum enforcing day %s",
            account.id,
            overdue_days,
            minimum_days,
        )
        return "notice_grace_active"
    return None


def _execute_dunning_action_with_evidence(
    db: Session,
    case: DunningCase,
    action: DunningAction,
    day_offset: int,
    note: str | None,
    overdue_days: int | None = None,
    invoice_id: str | None = None,
) -> tuple[str, FinancialAccessConsequence | None]:
    """Execute one dunning action through its consequence owner."""
    account_id = str(case.account_id)

    if action == DunningAction.notify:
        if _dunning_shield_reason(db, case.account_id):
            return "shielded", None
        _create_suspension_warning_notification(
            db, account_id, day_offset, note, invoice_id=invoice_id
        )
        return "notification_sent", None

    if action not in _ENFORCING_ACTIONS:
        return "unknown_action", None
    financial_action = {
        DunningAction.suspend: FinancialAccessAction.suspend,
        DunningAction.reject: FinancialAccessAction.reject,
        DunningAction.throttle: FinancialAccessAction.throttle,
    }[action]
    effective_overdue_days = day_offset if overdue_days is None else overdue_days
    preview = preview_financial_access_consequence(
        db,
        account_id,
        action=financial_action,
        reason=EnforcementReason.overdue,
        origin=FinancialAccessOrigin.dunning,
        dunning_case_id=case.id,
        overdue_days=effective_overdue_days,
    )
    if not preview.decision_inputs.get("inside_enforcement_window", True):
        logger.info(
            "enforcement_window_deferred",
            extra={
                "event": "enforcement_window_deferred",
                "path": "dunning",
                "action": action.value,
                "account_id": account_id,
                "deferred": preview.outcome == "outside_enforcement_window",
                "block_reason": preview.decision_inputs.get(
                    "enforcement_window_block_reason"
                ),
                "timezone": enforcement_window.resolve_timezone_name(db),
            },
        )
    result = confirm_financial_access_consequence(
        db,
        account_id,
        action=financial_action,
        reason=EnforcementReason.overdue,
        origin=FinancialAccessOrigin.dunning,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=(
            f"dunning:{case.id}:{action.value}:{day_offset}:{preview.fingerprint[:20]}"
        ),
        source=f"dunning_case:{case.id}",
        dunning_case_id=case.id,
        overdue_days=effective_overdue_days,
    )
    outcome = result.consequence.outcome
    if outcome in {"suspended", "rejected"}:
        _create_suspension_notification(db, account_id)
    elif outcome == "throttled":
        _create_throttle_notification(db, account_id, day_offset)
    return outcome, result.consequence


def _execute_dunning_action(
    db: Session,
    case: DunningCase,
    action: DunningAction,
    day_offset: int,
    note: str | None,
    overdue_days: int | None = None,
    invoice_id: str | None = None,
) -> str:
    """Compatibility adapter returning only the owner-confirmed outcome."""
    outcome, _ = _execute_dunning_action_with_evidence(
        db,
        case,
        action,
        day_offset,
        note,
        overdue_days=overdue_days,
        invoice_id=invoice_id,
    )
    return outcome


def _dunning_staff_case_impact(
    db: Session,
    *,
    case_id: UUID,
    case: DunningCase | None,
    action: DunningStaffAction,
) -> DunningStaffCaseImpact:
    if case is None:
        return DunningStaffCaseImpact(
            case_id=case_id,
            exists=False,
            account_id=None,
            current_status=None,
            resulting_status=None,
            eligible=False,
            reason="Dunning case was not found.",
            unpaid_invoice_count=0,
            unpaid_receivables=(),
            current_step=None,
            updated_at=None,
        )

    eligible = False
    reason: str | None = None
    resulting_status: DunningCaseStatus | None = None
    receivables: list[dict] = []
    if action is DunningStaffAction.pause:
        eligible = case.status is DunningCaseStatus.open
        resulting_status = DunningCaseStatus.paused if eligible else None
        if not eligible:
            reason = (
                f"Only an open case can be paused; this case is {case.status.value}."
            )
    elif action is DunningStaffAction.resume:
        eligible = case.status is DunningCaseStatus.paused
        resulting_status = DunningCaseStatus.open if eligible else None
        if not eligible:
            reason = (
                f"Only a paused case can be resumed; this case is {case.status.value}."
            )
    else:
        if case.status not in {DunningCaseStatus.open, DunningCaseStatus.paused}:
            reason = (
                "Only an open or paused case can be closed; "
                f"this case is {case.status.value}."
            )
        else:
            receivables = _overdue_receivable_snapshot(db, case.account_id)
            if receivables:
                reason = (
                    "The account still has collectible overdue receivables. "
                    "Settle or explicitly resolve them before closing the case."
                )
            else:
                eligible = True
                resulting_status = DunningCaseStatus.closed

    grouped: dict[str, Decimal] = {}
    for receivable in receivables:
        currency = str(receivable["currency"] or "NGN").upper()
        grouped[currency] = grouped.get(currency, Decimal("0.00")) + Decimal(
            str(receivable["receivable"])
        )
    return DunningStaffCaseImpact(
        case_id=case.id,
        exists=True,
        account_id=case.account_id,
        current_status=case.status,
        resulting_status=resulting_status,
        eligible=eligible,
        reason=reason,
        unpaid_invoice_count=len(receivables),
        unpaid_receivables=tuple(sorted(grouped.items())),
        current_step=case.current_step,
        updated_at=case.updated_at,
    )


def _dunning_staff_preview_fingerprint(
    *,
    action: DunningStaffAction,
    impacts: tuple[DunningStaffCaseImpact, ...],
) -> str:
    payload = {
        "action": action.value,
        "impacts": [
            {
                "case_id": str(impact.case_id),
                "exists": impact.exists,
                "account_id": str(impact.account_id) if impact.account_id else None,
                "current_status": (
                    impact.current_status.value if impact.current_status else None
                ),
                "resulting_status": (
                    impact.resulting_status.value if impact.resulting_status else None
                ),
                "eligible": impact.eligible,
                "reason": impact.reason,
                "unpaid_invoice_count": impact.unpaid_invoice_count,
                "unpaid_receivables": [
                    [currency, str(amount)]
                    for currency, amount in impact.unpaid_receivables
                ],
                "current_step": impact.current_step,
                "updated_at": (
                    impact.updated_at.isoformat() if impact.updated_at else None
                ),
            }
            for impact in impacts
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def preview_dunning_staff_action(
    db: Session,
    *,
    case_ids: tuple[UUID, ...],
    action: DunningStaffAction,
    lock: bool = False,
) -> DunningStaffActionPreview:
    """Resolve exact per-case eligibility and impact for one staff action."""

    normalized_ids = tuple(sorted(set(case_ids), key=str))
    if not normalized_ids:
        return DunningStaffActionPreview(
            action=action,
            requested_case_ids=(),
            impacts=(),
            fingerprint=_dunning_staff_preview_fingerprint(
                action=action,
                impacts=(),
            ),
        )
    statement = (
        select(DunningCase)
        .where(DunningCase.id.in_(normalized_ids))
        .order_by(DunningCase.id.asc())
    )
    if lock:
        statement = statement.with_for_update()
    cases = {case.id: case for case in db.scalars(statement).all()}
    if lock:
        account_ids = sorted(
            {case.account_id for case in cases.values()},
            key=str,
        )
        if account_ids:
            db.scalars(
                select(Subscriber)
                .where(Subscriber.id.in_(account_ids))
                .order_by(Subscriber.id.asc())
                .with_for_update()
            ).all()
    impacts = tuple(
        _dunning_staff_case_impact(
            db,
            case_id=case_id,
            case=cases.get(case_id),
            action=action,
        )
        for case_id in normalized_ids
    )
    return DunningStaffActionPreview(
        action=action,
        requested_case_ids=normalized_ids,
        impacts=impacts,
        fingerprint=_dunning_staff_preview_fingerprint(
            action=action,
            impacts=impacts,
        ),
    )


def stage_dunning_staff_case_action(
    db: Session,
    *,
    case_id: UUID,
    action: DunningStaffAction,
    actor: str,
) -> tuple[DunningCase, DunningActionLog]:
    """Stage one previously locked and confirmed case transition."""

    case = cast(DunningCase | None, db.get(DunningCase, case_id))
    if case is None:
        raise RuntimeError("Confirmed dunning case disappeared while locked")
    if action is DunningStaffAction.pause:
        case.status = DunningCaseStatus.paused
        outcome = "paused"
        note = "Case paused by staff"
        emit_event(
            db,
            EventType.dunning_paused,
            {
                "case_id": str(case.id),
                "account_id": str(case.account_id),
                "reason": note,
            },
            actor=actor,
            account_id=case.account_id,
        )
    elif action is DunningStaffAction.resume:
        case.status = DunningCaseStatus.open
        outcome = "resumed"
        note = "Case resumed by staff"
    else:
        case.status = DunningCaseStatus.closed
        case.resolved_at = datetime.now(UTC)
        outcome = "closed"
        note = "Case closed manually by staff"
    action_log = _create_action_log(
        db,
        case,
        DunningAction.notify,
        case.current_step,
        None,
        outcome=outcome,
        notes=note,
    )
    db.flush()
    _refresh_account_status(db, case.account_id)
    return case, action_log


class DunningCases(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: DunningCaseCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.collections, "default_dunning_case_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, DunningCaseStatus, "status"
                )
        case = DunningCase(**data)
        db.add(case)
        db.flush()
        _refresh_account_status(db, case.account_id)
        db.commit()
        db.refresh(case)
        return case

    @staticmethod
    def get(db: Session, case_id: str):
        case = db.get(DunningCase, case_id)
        if not case:
            raise HTTPException(status_code=404, detail="Dunning case not found")
        return case

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(DunningCase)
        if account_id:
            query = query.filter(DunningCase.account_id == account_id)
        if status:
            query = query.filter(
                DunningCase.status == validate_enum(status, DunningCaseStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": DunningCase.created_at, "status": DunningCase.status},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, case_id: str, payload: DunningCaseUpdate):
        case = db.get(DunningCase, case_id)
        if not case:
            raise HTTPException(status_code=404, detail="Dunning case not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(case, key, value)
        db.flush()
        _refresh_account_status(db, case.account_id)
        db.commit()
        db.refresh(case)
        return case

    @staticmethod
    def delete(db: Session, case_id: str):
        case = cast(DunningCase | None, db.get(DunningCase, case_id))
        if not case:
            raise HTTPException(status_code=404, detail="Dunning case not found")
        db.delete(case)
        db.commit()

    @staticmethod
    def add_note(db: Session, case_id: str, note: str) -> DunningCase:
        """Add a note to a dunning case."""
        case = cast(DunningCase | None, db.get(DunningCase, case_id))
        if not case:
            raise HTTPException(status_code=404, detail="Dunning case not found")
        case.notes = (case.notes + "\n" + note) if case.notes else note
        _create_action_log(
            db,
            case,
            DunningAction.notify,
            case.current_step,
            None,
            outcome="note_added",
            notes=note,
        )
        db.commit()
        db.refresh(case)
        return case

    @staticmethod
    def get_status_counts(db: Session) -> dict:
        """Get counts of dunning cases by status.

        Returns:
            Dict with keys: 'open', 'paused', 'resolved', 'closed'
            Each value is the count of cases in that status.
        """
        counts = (
            db.query(DunningCase.status, func.count(DunningCase.id))
            .group_by(DunningCase.status)
            .all()
        )
        result = {"open": 0, "paused": 0, "resolved": 0, "closed": 0}
        for status, count in counts:
            if status == DunningCaseStatus.open:
                result["open"] = count
            elif status == DunningCaseStatus.paused:
                result["paused"] = count
            elif status == DunningCaseStatus.resolved:
                result["resolved"] = count
            elif status == DunningCaseStatus.closed:
                result["closed"] = count
        return result


class DunningActionLogs(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: DunningActionLogCreate):
        action = DunningActionLog(**payload.model_dump())
        db.add(action)
        db.commit()
        db.refresh(action)
        return action

    @staticmethod
    def get(db: Session, action_id: str):
        action = db.get(DunningActionLog, action_id)
        if not action:
            raise HTTPException(status_code=404, detail="Dunning action not found")
        return action

    @staticmethod
    def list(
        db: Session,
        case_id: str | None,
        invoice_id: str | None,
        payment_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(DunningActionLog)
        if case_id:
            query = query.filter(DunningActionLog.case_id == case_id)
        if invoice_id:
            query = query.filter(DunningActionLog.invoice_id == invoice_id)
        if payment_id:
            query = query.filter(DunningActionLog.payment_id == payment_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "executed_at": DunningActionLog.executed_at,
                "action": DunningActionLog.action,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, action_id: str, payload: DunningActionLogUpdate):
        action = db.get(DunningActionLog, action_id)
        if not action:
            raise HTTPException(status_code=404, detail="Dunning action not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(action, key, value)
        db.commit()
        db.refresh(action)
        return action

    @staticmethod
    def delete(db: Session, action_id: str):
        action = db.get(DunningActionLog, action_id)
        if not action:
            raise HTTPException(status_code=404, detail="Dunning action not found")
        db.delete(action)
        db.commit()


def _run_dunning_account(
    db: Session,
    command: DunningAccountRunCommand,
) -> DunningAccountRunResult:
    """Stage one account's complete dunning consequence without committing."""

    account = db.scalar(
        select(Subscriber).where(Subscriber.id == command.account_id).with_for_update()
    )
    if account is None:
        return DunningAccountRunResult(skipped=1)
    account_invoices = list(
        db.scalars(
            select(Invoice)
            .where(
                Invoice.id.in_(command.invoice_ids),
                Invoice.account_id == command.account_id,
                Invoice.balance_due > 0,
                Invoice.due_at.is_not(None),
                Invoice.due_at <= command.run_at,
                collection_due_date_eligible_filter(),
                Invoice.is_active.is_(True),
                collectible_ar_invoice_filter(),
                Invoice.status.in_(
                    [
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    ]
                ),
            )
            .order_by(Invoice.due_at.asc(), Invoice.id.asc())
            .with_for_update()
        ).all()
    )
    eligible_invoices: list[Invoice] = []
    for invoice in account_invoices:
        overlap_hold = (
            invoice_paid_prepaid_overlap(db, invoice) is not None
            if command.dry_run
            else apply_prepaid_overlap_hold(db, invoice)
        )
        if (invoice.metadata_ or {}).get("reconciliation_hold") or overlap_hold:
            continue
        eligible_invoices.append(invoice)
        if not command.dry_run:
            Invoices.mark_overdue_system(
                db,
                str(invoice.id),
                as_of=command.run_at,
                reason="dunning_candidate_resolution",
            )
    if not eligible_invoices:
        return DunningAccountRunResult(skipped=1)

    profile = resolve_billing_profile(db, account)
    if not profile.automation_safe or profile.effective_mode != BillingMode.postpaid:
        return DunningAccountRunResult(skipped=1, has_effective_overdue=True)
    has_collectible_postpaid_service = db.scalar(
        select(Subscription.id)
        .where(
            Subscription.subscriber_id == command.account_id,
            Subscription.billing_mode == BillingMode.postpaid,
            Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES),
        )
        .limit(1)
    )
    if has_collectible_postpaid_service is None:
        return DunningAccountRunResult(skipped=1, has_effective_overdue=True)
    if _dunning_shield_reason(db, command.account_id):
        return DunningAccountRunResult(skipped=1, has_effective_overdue=True)

    policy_set_id = _resolve_policy_set_for_account(db, str(command.account_id))
    if not policy_set_id:
        return DunningAccountRunResult(skipped=1, has_effective_overdue=True)
    steps = _resolve_dunning_steps(db, str(policy_set_id))
    if not steps:
        return DunningAccountRunResult(skipped=1, has_effective_overdue=True)

    max_days = max(
        _resolve_overdue_days(
            invoice,
            command.run_at,
            account,
            db,
            policy_set_id=policy_set_id,
        )
        for invoice in eligible_invoices
    )
    if max_days <= 0:
        return DunningAccountRunResult(skipped=1, has_effective_overdue=True)

    case = db.scalar(
        select(DunningCase)
        .where(
            DunningCase.account_id == command.account_id,
            DunningCase.status.in_([DunningCaseStatus.open, DunningCaseStatus.paused]),
        )
        .order_by(DunningCase.started_at.desc())
        .limit(1)
    )
    cases_created = 0
    if case is None:
        case = DunningCase(
            account_id=command.account_id,
            policy_set_id=policy_set_id,
            status=DunningCaseStatus.open,
            started_at=command.run_at,
        )
        if not command.dry_run:
            db.add(case)
            db.flush()
            _refresh_account_status(db, command.account_id)
            emit_event(
                db,
                EventType.dunning_started,
                {
                    "case_id": str(case.id),
                    "account_id": str(command.account_id),
                    "policy_set_id": str(policy_set_id),
                    "max_days_overdue": max_days,
                },
                account_id=command.account_id,
            )
        cases_created = 1
    elif not command.dry_run:
        case.policy_set_id = policy_set_id

    if case.status == DunningCaseStatus.paused:
        logger.debug(
            "Skipping dunning steps for paused case %s (account %s)",
            case.id,
            command.account_id,
        )
        return DunningAccountRunResult(
            cases_created=cases_created,
            skipped=1,
            has_effective_overdue=True,
        )

    step = None
    for candidate in steps:
        if candidate.day_offset <= max_days:
            step = candidate
    if step is None or (
        case.current_step is not None and step.day_offset <= case.current_step
    ):
        return DunningAccountRunResult(
            cases_created=cases_created,
            has_effective_overdue=True,
        )

    oldest_invoice = min(
        eligible_invoices,
        key=lambda invoice: invoice.due_at or command.run_at,
    )
    if command.dry_run and step.action in _ENFORCING_ACTIONS:
        preview_financial_access_consequence(
            db,
            str(command.account_id),
            action={
                DunningAction.suspend: FinancialAccessAction.suspend,
                DunningAction.reject: FinancialAccessAction.reject,
                DunningAction.throttle: FinancialAccessAction.throttle,
            }[step.action],
            reason=EnforcementReason.overdue,
            origin=FinancialAccessOrigin.dunning,
            dunning_case_id=case.id,
            overdue_days=max_days,
        )
    if not command.dry_run:
        outcome, access_consequence = _execute_dunning_action_with_evidence(
            db,
            case,
            step.action,
            step.day_offset,
            step.note,
            overdue_days=max_days,
            invoice_id=str(oldest_invoice.id),
        )
        _create_action_log(
            db,
            case,
            step.action,
            step.day_offset,
            str(oldest_invoice.id),
            outcome=outcome,
            notes=step.note,
            access_consequence=access_consequence,
        )
        if outcome not in _NON_ADVANCING_DUNNING_OUTCOMES:
            case.current_step = step.day_offset
        emit_event(
            db,
            EventType.dunning_action_executed,
            {
                "case_id": str(case.id),
                "account_id": str(command.account_id),
                "action": step.action.value,
                "day_offset": step.day_offset,
                "overdue_days": max_days,
                "outcome": outcome,
                "invoice_id": str(oldest_invoice.id),
            },
            account_id=command.account_id,
        )
    return DunningAccountRunResult(
        cases_created=cases_created,
        actions_created=1,
        has_effective_overdue=True,
    )


def _record_dunning_account_failure(
    db: Session,
    *,
    account_id: UUID,
    run_at: datetime,
    phase: str,
    error: Exception,
) -> None:
    """Persist bounded failure evidence after the account transaction rolls back."""

    try:
        AuditEvents.stage(
            db,
            AuditEventCreate(
                action="dunning_account_failed",
                entity_type="subscriber",
                entity_id=str(account_id),
                is_success=False,
                metadata_={
                    "owner": "financial.dunning",
                    "phase": phase,
                    "run_at": run_at.isoformat(),
                    "error_type": type(error).__name__,
                },
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "dunning_account_failure_evidence_failed",
            extra={
                "event": "dunning_account_failure_evidence_failed",
                "account_id": str(account_id),
                "phase": phase,
            },
        )


class DunningWorkflow(ListResponseMixin):
    @staticmethod
    def run(db: Session, payload: DunningRunRequest) -> DunningRunResponse:
        run_at = payload.run_at or datetime.now(UTC)
        rows = db.execute(
            select(Invoice.id, Invoice.account_id)
            .where(
                Invoice.balance_due > 0,
                Invoice.due_at.is_not(None),
                Invoice.due_at <= run_at,
                collection_due_date_eligible_filter(),
                Invoice.is_active.is_(True),
                collectible_ar_invoice_filter(),
                Invoice.status.in_(
                    [
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    ]
                ),
            )
            .order_by(Invoice.account_id.asc(), Invoice.due_at.asc())
        ).all()
        overdue_accounts: dict[UUID, list[UUID]] = {}
        for invoice_id, raw_account_id in rows:
            account_id = coerce_uuid(str(raw_account_id))
            overdue_accounts.setdefault(account_id, []).append(invoice_id)

        # End the cohort read transaction before any account consequence starts.
        # Each following iteration owns an independent commit or rollback.
        db.rollback()
        cases_created = 0
        actions_created = 0
        skipped = 0
        errors = 0
        effective_overdue_accounts: set[UUID] = set()
        for account_id, invoice_ids in overdue_accounts.items():
            try:
                result = _run_dunning_account(
                    db,
                    DunningAccountRunCommand(
                        account_id=account_id,
                        invoice_ids=tuple(invoice_ids),
                        run_at=run_at,
                        dry_run=payload.dry_run,
                    ),
                )
                cases_created += result.cases_created
                actions_created += result.actions_created
                skipped += result.skipped
                if result.has_effective_overdue:
                    effective_overdue_accounts.add(account_id)
                if payload.dry_run:
                    db.rollback()
                else:
                    db.commit()
            except Exception as exc:
                db.rollback()
                errors += 1
                skipped += 1
                # A failed decision is unknown, never proof that debt cleared.
                effective_overdue_accounts.add(account_id)
                logger.exception(
                    "dunning_account_failed",
                    extra={
                        "event": "dunning_account_failed",
                        "account_id": str(account_id),
                    },
                )
                if not payload.dry_run:
                    _record_dunning_account_failure(
                        db,
                        account_id=account_id,
                        run_at=run_at,
                        phase="decision_and_consequence",
                        error=exc,
                    )

        if not payload.dry_run:
            clean_case_query = select(DunningCase.account_id).where(
                DunningCase.status == DunningCaseStatus.open
            )
            if effective_overdue_accounts:
                clean_case_query = clean_case_query.where(
                    DunningCase.account_id.notin_(tuple(effective_overdue_accounts))
                )
            clean_account_ids = sorted(
                {
                    coerce_uuid(str(value))
                    for value in db.scalars(clean_case_query).all()
                },
                key=str,
            )
            db.rollback()
            for account_id in clean_account_ids:
                try:
                    restore_account_services(
                        db,
                        str(account_id),
                        origin=FinancialAccessOrigin.financial_reconciliation,
                        resolved_by=f"dunning_reconcile:{account_id}",
                        overdue_trigger="collections_resolution",
                    )
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    errors += 1
                    logger.exception(
                        "billing_enforcement_access_restore_failed",
                        extra={
                            "event": "billing_enforcement_access_restore_failed",
                            "account_id": str(account_id),
                        },
                    )
                    _record_dunning_account_failure(
                        db,
                        account_id=account_id,
                        run_at=run_at,
                        phase="clean_account_restoration",
                        error=exc,
                    )
        return DunningRunResponse(
            run_at=run_at,
            accounts_scanned=len(overdue_accounts),
            cases_created=cases_created,
            actions_created=actions_created,
            skipped=skipped,
            errors=errors,
        )

    @staticmethod
    def resolve_cases_for_account(
        db: Session,
        account_id: str,
        invoice_id: str | None = None,
        commit: bool = True,
    ) -> int:
        cases = (
            db.query(DunningCase)
            .filter(DunningCase.account_id == account_id)
            # Only auto-resolve OPEN cases on payment; a paused case is an
            # operator hold and must be released by a human, not by a payment.
            .filter(DunningCase.status == DunningCaseStatus.open)
            .all()
        )
        if not cases:
            return 0
        now = datetime.now(UTC)
        for case in cases:
            case.status = DunningCaseStatus.resolved
            case.resolved_at = now
            _create_action_log(
                db,
                case,
                DunningAction.notify,
                case.current_step,
                invoice_id,
                outcome="resolved",
                notes="Resolved after payment",
            )
        db.flush()
        _refresh_account_status(db, account_id)
        if commit:
            db.commit()
        return len(cases)


class BillingEnforcementReconciler:
    """Single billing enforcement writer.

    Invoice generation creates AR for every production billing mode. Service
    enforcement converges here: invoice due dates + policy decide
    notify/throttle/suspend/restore through the dunning case and account
    lifecycle machinery, while prepaid enforcing actions are gated by local
    ledger available balance.
    """

    @staticmethod
    def _settle_due_credit_before_dunning(
        db: Session, run_at: datetime
    ) -> dict[str, int | str]:
        """Apply payment-backed credit to due invoices before escalation."""
        from app.services.billing.reconcile_unposted import (
            settle_open_invoices_from_credit,
        )

        account_ids = [
            str(row[0])
            for row in (
                db.query(Invoice.account_id)
                .filter(Invoice.is_active.is_(True))
                .filter(Invoice.balance_due > 0)
                .filter(
                    Invoice.status.in_(
                        [
                            InvoiceStatus.issued,
                            InvoiceStatus.partially_paid,
                            InvoiceStatus.overdue,
                        ]
                    )
                )
                .filter(
                    or_(
                        Invoice.status == InvoiceStatus.overdue,
                        and_(
                            Invoice.due_at.is_not(None),
                            Invoice.due_at <= run_at,
                        ),
                    )
                )
                .filter(collection_due_date_eligible_filter())
                .distinct()
                .all()
            )
        ]
        stats: dict[str, int | str] = {
            "credit_accounts_scanned": len(account_ids),
            "credit_accounts_settled": 0,
            "credit_invoices_touched": 0,
            "credit_settlement_errors": 0,
            "credit_applied": "0.00",
        }
        total_applied = Decimal("0.00")
        for account_id in account_ids:
            try:
                result = settle_open_invoices_from_credit(db, account_id)
                db.commit()
                if result.changed:
                    total_applied += result.applied
                    stats["credit_accounts_settled"] = (
                        int(stats["credit_accounts_settled"]) + 1
                    )
                    stats["credit_invoices_touched"] = int(
                        stats["credit_invoices_touched"]
                    ) + len(result.invoices_touched)
            except Exception:
                db.rollback()
                stats["credit_settlement_errors"] = (
                    int(stats["credit_settlement_errors"]) + 1
                )
                logger.exception(
                    "billing_enforcement_credit_settlement_failed",
                    extra={
                        "event": "billing_enforcement_credit_settlement_failed",
                        "account_id": account_id,
                    },
                )
                continue

            if not result.changed:
                continue
            try:
                # Payment allocation is the financial owner and has already
                # committed. Access restoration is a separate idempotent
                # reconciliation consequence; its failure must not undo cash
                # allocation, and it needs no participant savepoint because it
                # receives its own root transaction here.
                if not has_overdue_balance(db, account_id):
                    from app.services.account_lifecycle import compute_account_status

                    invoice_id = (
                        result.invoices_settled[0]
                        if result.invoices_settled
                        else (
                            result.invoices_touched[0]
                            if result.invoices_touched
                            else None
                        )
                    )
                    restore_account_services(db, account_id, invoice_id=invoice_id)
                    compute_account_status(db, account_id)
                db.commit()
            except Exception as exc:
                db.rollback()
                logger.exception(
                    "billing_enforcement_credit_restore_failed",
                    extra={
                        "event": "billing_enforcement_credit_restore_failed",
                        "account_id": account_id,
                    },
                )
                _record_dunning_account_failure(
                    db,
                    account_id=coerce_uuid(account_id),
                    run_at=run_at,
                    phase="credit_settlement_restoration",
                    error=exc,
                )
        stats["credit_applied"] = str(total_applied)
        return stats

    @staticmethod
    def run(
        db: Session, payload: BillingEnforcementRunRequest
    ) -> BillingEnforcementRunResponse:
        run_at = payload.run_at or datetime.now(UTC)
        credit_stats: dict[str, int | str] = {
            "credit_accounts_scanned": 0,
            "credit_accounts_settled": 0,
            "credit_invoices_touched": 0,
            "credit_settlement_errors": 0,
            "credit_applied": "0.00",
        }
        if not payload.dry_run:
            credit_stats = (
                BillingEnforcementReconciler._settle_due_credit_before_dunning(
                    db, run_at
                )
            )
        dunning = DunningWorkflow.run(
            db,
            DunningRunRequest(run_at=run_at, dry_run=payload.dry_run),
        )
        return BillingEnforcementRunResponse(
            run_at=dunning.run_at,
            accounts_scanned=dunning.accounts_scanned,
            cases_created=dunning.cases_created,
            actions_created=dunning.actions_created,
            skipped=dunning.skipped,
            dunning_accounts_scanned=dunning.accounts_scanned,
            dunning_cases_created=dunning.cases_created,
            dunning_actions_created=dunning.actions_created,
            dunning_skipped=dunning.skipped,
            dunning_errors=dunning.errors,
            credit_accounts_scanned=int(credit_stats["credit_accounts_scanned"]),
            credit_accounts_settled=int(credit_stats["credit_accounts_settled"]),
            credit_invoices_touched=int(credit_stats["credit_invoices_touched"]),
            credit_settlement_errors=int(credit_stats["credit_settlement_errors"]),
            credit_applied=str(credit_stats["credit_applied"]),
        )


def _clear_prepaid_dunning_flags(db: Session, account_id: str) -> None:
    """Clear the prepaid low-balance / scheduled-deactivation timestamps.

    A payment or top-up that restores the account makes these stale; clearing
    them here — instead of waiting for the next collections sweep — stops a
    just-paid customer from being deactivated on a pending timer. The sweep
    re-sets them if the account is still below its minimum balance.
    """
    clear_prepaid_enforcement_timers(db, account_id)


def _restore_prepaid_if_funded(
    db: Session,
    account: Subscriber,
    funding: PrepaidFundingDecision,
    *,
    resolved_by: str,
) -> int:
    """Resolve prepaid locks backed by funding or exact current coverage."""
    if not funding.funded and not funding.covered_subscription_ids:
        logger.info(
            "Prepaid restore skipped for account %s: no funded or covered service "
            "(available %s, required %s)",
            account.id,
            funding.available_balance,
            funding.required_balance,
        )
        return 0
    return restore_account_services(
        db,
        str(account.id),
        origin=FinancialAccessOrigin.prepaid_enforcement,
        resolved_by=resolved_by,
    )


def restore_account_services_detailed(
    db: Session,
    account_id: str,
    invoice_id: str | None = None,
    *,
    origin: FinancialAccessOrigin = FinancialAccessOrigin.financial_reconciliation,
    idempotency_key: str | None = None,
    resolved_by: str | None = None,
    overdue_trigger: str = "payment",
) -> FinancialAccessConsequenceResult | None:
    """Reconcile financial locks and return the full typed consequence.

    ``overdue`` and ``prepaid`` are independent enforcement reasons and use
    independent, named gates.  Invoice debt must be cleared before an overdue
    lock/case is resolved.  Prepaid access must meet the same available-balance
    threshold used by the suspension sweep before a prepaid lock or timer is
    cleared.  No caller can turn the mere existence of a payment into access.

    ``None`` means the account does not exist. Every other outcome — including
    "money settled, access still blocked" — is carried in
    ``restoration_outcomes`` so payment results, staff surfaces, alerts, and
    metrics can say exactly what happened instead of reporting a bare count.
    """
    preview = preview_financial_access_restoration(db, account_id, origin=origin)
    if preview.outcome == "account_not_found":
        logger.warning("Cannot restore account %s: account not found", account_id)
        return None
    return confirm_financial_access_restoration(
        db,
        account_id,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=(
            idempotency_key
            or f"financial-restore:{account_id}:{origin.value}:"
            f"{preview.fingerprint[:24]}"
        ),
        origin=origin,
        invoice_id=invoice_id,
        resolved_by=resolved_by or f"financial_access:{account_id}",
        overdue_trigger=overdue_trigger,
    )


def restore_account_services(
    db: Session,
    account_id: str,
    invoice_id: str | None = None,
    *,
    origin: FinancialAccessOrigin = FinancialAccessOrigin.financial_reconciliation,
    idempotency_key: str | None = None,
    resolved_by: str | None = None,
    overdue_trigger: str = "payment",
) -> int:
    """Count-only facade over :func:`restore_account_services_detailed`."""
    result = restore_account_services_detailed(
        db,
        account_id,
        invoice_id,
        origin=origin,
        idempotency_key=idempotency_key,
        resolved_by=resolved_by,
        overdue_trigger=overdue_trigger,
    )
    if result is None:
        return 0
    return result.subscriptions_changed


dunning_cases = DunningCases()
dunning_action_logs = DunningActionLogs()
dunning_workflow = DunningWorkflow()
billing_enforcement_reconciler = BillingEnforcementReconciler()
