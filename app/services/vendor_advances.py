"""Canonical owner for money advanced to a vendor before work is invoiced.

Nothing modelled an advance: no mobilisation payment, no drawdown against an
approved quote. Vendors doing buildout routinely need one before trenching
starts, and without it the only path was an off-system payment with no
project-anchored record.

Boundary (``docs/BACKOFFICE_INTEGRATION_BOUNDARY.md``): Sub decides *whether*
to advance and how much; the payables provider owns the payment and any netting
against the vendor's later invoice. Sub never computes settlement, never marks
itself paid, and never adjusts an invoice total — a `settled` advance is an
observation of the provider, not a Sub decision.

**The amount is entered, not derived.** Staff approval is the control: an
approver sees the amount, the quote total, and what the project has already
committed, and decides. Sub applies exactly two limits:

* A hard bound at the approved quote total. This is arithmetic, not policy —
  you cannot advance more than the work is agreed to be worth, and the answer
  to cost escalation is a variation, not an over-advance. It counts advances
  already committed, so the bound cannot be evaded by splitting a request.
* An optional percentage guard rail from ``projects.vendor_advance_max_percent``.
  It defaults to 0, meaning no percentage policy at all. Where an operator sets
  one it stops an obviously out-of-policy request before it reaches an approver;
  it does not replace the approver's judgement.

An invented cap would be worse than none: it forces the workarounds this domain
exists to eliminate — splitting advances across requests, or paying off-system
with no project-anchored record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteStatus,
)
from app.models.vendor_supply import VendorAdvance, VendorAdvanceStatus
from app.services.common import coerce_uuid
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.settings_spec import resolve_value

# An advance funds work about to start or under way. Advancing against verified
# work would be paying for something already complete and invoiceable.
_ADVANCEABLE_PROJECT_STATUSES = frozenset(
    {
        InstallationProjectStatus.approved.value,
        InstallationProjectStatus.in_progress.value,
    }
)
# Advances that still consume the ceiling. A rejected or canceled request
# reserves nothing.
_COMMITTED_STATUSES = frozenset(
    {
        VendorAdvanceStatus.requested.value,
        VendorAdvanceStatus.approved.value,
        VendorAdvanceStatus.settled.value,
    }
)
# 0 means "no percentage policy" — the operator has not set one, so the
# approver alone decides. There is deliberately no code default percentage:
# a number nobody chose is policy by accident.
NO_PERCENT_CAP = Decimal("0")


class VendorAdvanceError(ValueError):
    def __init__(self, code: str, message: str, *, kind: str = "invalid") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


def _error(code: str, message: str, *, kind: str = "invalid"):
    return VendorAdvanceError(code, message, kind=kind)


def _now() -> datetime:
    return datetime.now(UTC)


def _money(value) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError) as exc:
        raise _error("invalid_amount", "Advance amount is not a valid number.") from exc


@dataclass(frozen=True, slots=True)
class RequestVendorAdvance:
    project_id: UUID
    vendor_id: UUID
    amount: object
    requested_by_person_id: UUID | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AdvanceRequestEligibility:
    """Owner-supplied advance decision facts for commands and UI projections."""

    allowed: bool
    reason: str | None
    quote_id: UUID | None = None
    currency: str | None = None
    quote_total: Decimal | None = None
    max_percent: Decimal = NO_PERCENT_CAP
    ceiling: Decimal | None = None
    committed: Decimal = Decimal("0.00")
    remaining: Decimal = Decimal("0.00")


def request_eligibility(
    db: Session,
    *,
    project_id: UUID | str,
    vendor_id: UUID | str,
) -> AdvanceRequestEligibility:
    project = db.get(InstallationProject, coerce_uuid(project_id))
    if project is None or not project.is_active:
        return AdvanceRequestEligibility(
            allowed=False,
            reason="Installation project not found.",
        )
    if str(project.assigned_vendor_id or "") != str(vendor_id):
        return AdvanceRequestEligibility(
            allowed=False,
            reason="An advance is available only to the assigned vendor.",
        )
    if project.status not in _ADVANCEABLE_PROJECT_STATUSES:
        return AdvanceRequestEligibility(
            allowed=False,
            reason="An advance is available once work is approved and before verification.",
        )
    quote = (
        db.get(ProjectQuote, project.approved_quote_id)
        if project.approved_quote_id
        else None
    )
    if quote is None or quote.status != ProjectQuoteStatus.approved.value:
        return AdvanceRequestEligibility(
            allowed=False,
            reason="An advance requires an approved quote to draw against.",
        )
    max_percent = configured_max_percent(db)
    ceiling = advance_ceiling(quote, max_percent)
    already = committed_total(db, project.id)
    remaining = max(ceiling - already, Decimal("0.00"))
    if remaining <= Decimal("0.00"):
        return AdvanceRequestEligibility(
            allowed=False,
            reason="The available advance allowance has already been committed.",
            quote_id=quote.id,
            currency=quote.currency,
            quote_total=_money(quote.total),
            max_percent=max_percent,
            ceiling=ceiling,
            committed=already,
            remaining=remaining,
        )
    return AdvanceRequestEligibility(
        allowed=True,
        reason=None,
        quote_id=quote.id,
        currency=quote.currency,
        quote_total=_money(quote.total),
        max_percent=max_percent,
        ceiling=ceiling,
        committed=already,
        remaining=remaining,
    )


def review_eligibility(status: str) -> tuple[bool, str | None]:
    if status != VendorAdvanceStatus.requested.value:
        return False, "Only a requested advance can be reviewed."
    return True, None


def _advance(
    db: Session, advance_id: UUID | str, *, for_update: bool = False
) -> VendorAdvance:
    query = db.query(VendorAdvance).filter(VendorAdvance.id == coerce_uuid(advance_id))
    if for_update:
        query = query.with_for_update(of=VendorAdvance)
    row = query.one_or_none()
    if row is None or not row.is_active:
        raise _error("advance_not_found", "Advance not found.", kind="not_found")
    return row


def committed_total(db: Session, project_id: UUID | str) -> Decimal:
    """What this project has already committed in advances."""
    rows = db.scalars(
        select(VendorAdvance)
        .where(VendorAdvance.project_id == coerce_uuid(project_id))
        .where(VendorAdvance.is_active.is_(True))
        .where(VendorAdvance.status.in_(tuple(_COMMITTED_STATUSES)))
    )
    return sum((_money(row.amount) for row in rows), Decimal("0.00"))


def authorised_total(db: Session, project_id: UUID | str) -> Decimal:
    """What this project has authorised for disbursement in advances.

    Narrower than :func:`committed_total`: a merely *requested* advance
    reserves ceiling but authorises no money, so it must not reduce what a
    vendor may still invoice. Approved and settled advances are money the
    vendor is owed or has, and an invoice on top of them would pay twice.
    """

    rows = db.scalars(
        select(VendorAdvance)
        .where(VendorAdvance.project_id == coerce_uuid(project_id))
        .where(VendorAdvance.is_active.is_(True))
        .where(
            VendorAdvance.status.in_(
                (
                    VendorAdvanceStatus.approved.value,
                    VendorAdvanceStatus.settled.value,
                )
            )
        )
    )
    return sum((_money(row.amount) for row in rows), Decimal("0.00"))


def configured_max_percent(db: Session) -> Decimal:
    """The operator's percentage guard rail, or 0 when they have not set one."""
    raw = resolve_value(db, SettingDomain.projects, "vendor_advance_max_percent")
    try:
        percent = Decimal(str(raw or 0))
    except (InvalidOperation, ValueError):
        return NO_PERCENT_CAP
    return percent if Decimal("0") < percent <= Decimal("100") else NO_PERCENT_CAP


def advance_ceiling(
    quote: ProjectQuote, max_percent: Decimal = NO_PERCENT_CAP
) -> Decimal:
    """The most this project may carry in advances.

    The quote total is the hard bound. A configured percentage can only lower
    it — it never raises it, so a misconfigured 100%+ cannot authorise
    advancing more than the work is worth.
    """
    total = _money(quote.total)
    if max_percent <= Decimal("0"):
        return total
    return min(total, _money(total * max_percent / Decimal("100")))


def request_advance(db: Session, command: RequestVendorAdvance) -> VendorAdvance:
    """Stage one advance request against the project's approved quote."""

    amount = _money(command.amount)
    if amount <= Decimal("0.00"):
        raise _error("invalid_amount", "Advance amount must be greater than zero.")

    project = (
        db.query(InstallationProject)
        .filter(InstallationProject.id == coerce_uuid(command.project_id))
        .with_for_update(of=InstallationProject)
        .one_or_none()
    )
    if project is None or not project.is_active:
        raise _error(
            "project_not_found", "Installation project not found.", kind="not_found"
        )
    eligibility = request_eligibility(
        db,
        project_id=project.id,
        vendor_id=command.vendor_id,
    )
    if not eligibility.allowed:
        if str(project.assigned_vendor_id or "") != str(command.vendor_id):
            code = "project_not_assigned"
        elif project.status not in _ADVANCEABLE_PROJECT_STATUSES:
            code = "project_not_advanceable"
        elif eligibility.quote_id is None:
            code = "approved_quote_required"
        else:
            code = "advance_ceiling_exceeded"
        raise _error(code, str(eligibility.reason))
    quote = db.get(ProjectQuote, eligibility.quote_id)
    if quote is None:
        raise _error(
            "approved_quote_required",
            "An advance requires an approved quote to draw against.",
        )
    ceiling = eligibility.ceiling or Decimal("0.00")
    already = eligibility.committed
    if amount > eligibility.remaining:
        remaining = eligibility.remaining
        raise _error(
            "advance_ceiling_exceeded",
            (
                f"Advances on this project cannot exceed {ceiling} "
                f"{quote.currency}; {remaining} remains."
            ),
        )

    advance = VendorAdvance(
        project_id=project.id,
        vendor_id=coerce_uuid(command.vendor_id),
        quote_id=quote.id,
        status=VendorAdvanceStatus.requested.value,
        amount=amount,
        currency=quote.currency,
        reason=(command.reason or "").strip() or None,
        requested_by_person_id=(
            coerce_uuid(command.requested_by_person_id)
            if command.requested_by_person_id
            else None
        ),
        requested_at=_now(),
    )
    db.add(advance)
    db.flush()
    emit_event(
        db,
        EventType.vendor_advance_requested,
        {
            "schema_version": 1,
            "advance_id": str(advance.id),
            "project_id": str(advance.project_id),
            "vendor_id": str(advance.vendor_id),
            "quote_id": str(advance.quote_id),
            "amount": str(advance.amount),
            "currency": advance.currency,
        },
        actor=str(command.requested_by_person_id or "vendor"),
    )
    return advance


def approve(
    db: Session,
    advance_id: UUID | str,
    *,
    actor_id: UUID | str,
    notes: str | None = None,
) -> VendorAdvance:
    """Staff approve the advance. Payment itself belongs to the provider."""
    advance = _advance(db, advance_id, for_update=True)
    allowed, reason = review_eligibility(advance.status)
    if not allowed:
        raise _error("not_reviewable", str(reason))
    advance.status = VendorAdvanceStatus.approved.value
    advance.reviewed_by_person_id = coerce_uuid(actor_id)
    advance.reviewed_at = _now()
    advance.review_notes = (notes or "").strip() or None
    db.flush()
    _emit_review(db, advance, actor_id)
    return advance


def reject(
    db: Session,
    advance_id: UUID | str,
    *,
    actor_id: UUID | str,
    reason: str,
) -> VendorAdvance:
    advance = _advance(db, advance_id, for_update=True)
    allowed, blocked_reason = review_eligibility(advance.status)
    if not allowed:
        raise _error("not_reviewable", str(blocked_reason))
    normalized = (reason or "").strip()
    if not normalized:
        raise _error("reason_required", "A rejection reason is required.")
    advance.status = VendorAdvanceStatus.rejected.value
    advance.reviewed_by_person_id = coerce_uuid(actor_id)
    advance.reviewed_at = _now()
    advance.review_notes = normalized
    db.flush()
    _emit_review(db, advance, actor_id)
    return advance


def _emit_review(db: Session, advance, actor_id) -> None:
    emit_event(
        db,
        EventType.vendor_advance_reviewed,
        {
            "schema_version": 1,
            "advance_id": str(advance.id),
            "project_id": str(advance.project_id),
            "vendor_id": str(advance.vendor_id),
            "status": advance.status,
            "amount": str(advance.amount),
            "currency": advance.currency,
        },
        actor=str(actor_id),
    )


def apply_payables_observation(
    db: Session,
    advance_id: UUID | str,
    *,
    payables_system: str,
    payables_reference: str | None,
    payables_status: str | None,
) -> VendorAdvance:
    """Record what the payables provider says happened to an approved advance.

    Settlement is the provider's fact, so Sub stores it rather than deriving it.
    An unavailable or stale observation leaves the last good one in place and
    never reverses the Sub approval.
    """
    advance = _advance(db, advance_id, for_update=True)
    if advance.status not in {
        VendorAdvanceStatus.approved.value,
        VendorAdvanceStatus.settled.value,
    }:
        raise _error(
            "observation_not_applicable",
            "Only an approved advance can carry a payables observation.",
        )
    advance.payables_system = payables_system
    advance.payables_reference = payables_reference
    advance.payables_status = payables_status
    advance.payables_observed_at = _now()
    if (payables_status or "").strip().lower() in {"paid", "settled", "completed"}:
        advance.status = VendorAdvanceStatus.settled.value
        emit_event(
            db,
            EventType.vendor_advance_settled,
            {
                "schema_version": 1,
                "advance_id": str(advance.id),
                "project_id": str(advance.project_id),
                "vendor_id": str(advance.vendor_id),
                "amount": str(advance.amount),
                "currency": advance.currency,
                "payables_system": payables_system,
                "payables_reference": payables_reference,
            },
            actor=f"provider:{payables_system}",
        )
    db.flush()
    return advance


def list_for_project(db: Session, project_id: UUID | str) -> list[VendorAdvance]:
    return list(
        db.scalars(
            select(VendorAdvance)
            .where(VendorAdvance.project_id == coerce_uuid(project_id))
            .where(VendorAdvance.is_active.is_(True))
            .order_by(VendorAdvance.created_at.desc())
        )
    )


# --- committed wrappers -----------------------------------------------------
# The staging functions above are transaction-neutral so they can participate
# in a caller's transaction. These own the commit, so adapters never do —
# owning a transaction in a route is what the adapter boundary forbids.


def request_advance_committed(db: Session, command: RequestVendorAdvance):
    row = request_advance(db, command)
    db.commit()
    db.refresh(row)
    return row


def approve_committed(db: Session, row_id, *, actor_id, notes: str | None = None):
    row = approve(db, row_id, actor_id=actor_id, notes=notes)
    db.commit()
    db.refresh(row)
    return row


def reject_committed(db: Session, row_id, *, actor_id, reason: str):
    row = reject(db, row_id, actor_id=actor_id, reason=reason)
    db.commit()
    db.refresh(row)
    return row
