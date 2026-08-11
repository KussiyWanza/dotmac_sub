"""Service helpers for admin ONT assignment web routes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import OntAssignment, PonPort
from app.models.ont_autofind import OltAutofindCandidate
from app.services import network as network_service
from app.services import subscriber as subscriber_service
from app.services.common import coerce_uuid
from app.services.network.olt_autofind import parse_fsp_parts
from app.services.network.ont_assignment_commands import (
    OntAssignmentCommandError,
    ReassignActiveOntCommand,
)
from app.services.network.ont_reassignment_read import (
    active_assignment_for_reassignment,
    eligible_reassignment_targets,
)
from app.services.network_subscriber_bridge import (
    AssignmentSubscriptionSnapshot,
    assignment_subscription_snapshot,
)
from app.services.owner_commands import CommandContext
from app.services.web_network_ont_autofind import (
    _normalize_serial,
    _normalized_serial_expr,
)

logger = logging.getLogger(__name__)


@dataclass
class AssignmentFormResult:
    ont: Any | None = None
    assignment: Any | None = None
    values: dict[str, object] | None = None
    error: str | None = None
    not_found: bool = False
    not_found_message: str = "ONT not found"


@dataclass(frozen=True, slots=True)
class AssignmentSubscriptionOption:
    """One exact subscription choice for the selected subscriber."""

    id: UUID
    offer_name: str
    service_login: str | None
    status: SubscriptionStatus

    @property
    def is_active(self) -> bool:
        return self.status is SubscriptionStatus.active

    @property
    def status_indicator(self) -> str:
        if self.is_active:
            return "Active"
        status_name = self.status.value.replace("_", " ").title()
        return f"Inactive ({status_name})"


@dataclass(frozen=True, slots=True)
class ChangeOntFormContext:
    subscription: AssignmentSubscriptionSnapshot | None
    current_assignment: OntAssignment | None
    eligible_onts: tuple[Any, ...]
    selected_ont_id: str | None = None
    error: str | None = None

    @property
    def not_found(self) -> bool:
        return self.subscription is None


def assignment_subscription_options(
    db: Session, *, subscriber_id: UUID
) -> tuple[AssignmentSubscriptionOption, ...]:
    """Return every subscription owned by one exact selected subscriber."""

    subscriptions = db.scalars(
        select(Subscription)
        .options(joinedload(Subscription.offer))
        .where(Subscription.subscriber_id == subscriber_id)
        .order_by(
            case(
                (Subscription.status == SubscriptionStatus.active, 0),
                else_=1,
            ),
            Subscription.created_at.desc(),
            Subscription.id,
        )
    ).all()
    return tuple(
        AssignmentSubscriptionOption(
            id=subscription.id,
            offer_name=(
                subscription.offer.name
                if subscription.offer is not None
                else "Subscription"
            ),
            service_login=(subscription.login or "").strip() or None,
            status=subscription.status,
        )
        for subscription in subscriptions
    )


def change_ont_form_context(
    db: Session,
    *,
    subscription_id: str,
    selected_ont_id: str | None = None,
    error: str | None = None,
) -> ChangeOntFormContext:
    subscription_uuid = coerce_uuid(subscription_id)
    if subscription_uuid is None:
        return ChangeOntFormContext(
            subscription=None,
            current_assignment=None,
            eligible_onts=(),
            error="Subscription not found",
        )
    subscription = assignment_subscription_snapshot(db, subscription_uuid)
    if subscription is None:
        return ChangeOntFormContext(
            subscription=None,
            current_assignment=None,
            eligible_onts=(),
            error="Subscription not found",
        )
    assignment_choice = active_assignment_for_reassignment(
        db,
        subscription_id=subscription.id,
    )
    assignment = assignment_choice.assignment
    resolved_error = error or assignment_choice.error
    eligible_onts = eligible_reassignment_targets(
        db,
        exclude_ont_unit_id=assignment.ont_unit_id if assignment else None,
    )
    return ChangeOntFormContext(
        subscription=subscription,
        current_assignment=assignment,
        eligible_onts=eligible_onts,
        selected_ont_id=selected_ont_id,
        error=resolved_error,
    )


def reassign_subscription_ont_from_form(
    db: Session,
    *,
    subscription_id: str,
    form,
    actor_id: str | None,
) -> tuple[bool, str]:
    context = change_ont_form_context(
        db,
        subscription_id=subscription_id,
        selected_ont_id=str(form.get("target_ont_unit_id", "") or ""),
    )
    if context.subscription is None:
        return False, "Subscription not found"
    if context.current_assignment is None:
        return False, context.error or "No single active ONT assignment is available."
    if not actor_id:
        return False, "Authenticated actor required"
    target_raw = str(form.get("target_ont_unit_id", "") or "").strip()
    if not target_raw:
        return False, "Select the replacement ONT from inventory."
    if str(form.get("confirm_change", "") or "").strip() != "change_ont":
        return False, "Confirm the ONT access-path change before continuing."
    target_id = coerce_uuid(target_raw)
    if target_id is None:
        return False, "Select a valid replacement ONT from inventory."
    current_assignment_id = coerce_uuid(
        str(form.get("current_assignment_id", "") or "").strip()
    )
    if current_assignment_id is None:
        return (
            False,
            "Current ONT assignment evidence is missing. Refresh and try again.",
        )
    eligible_ids = {choice.ont_unit_id for choice in context.eligible_onts}
    if target_id not in eligible_ids:
        return False, "Selected ONT is no longer eligible. Refresh and try again."
    subscription_uuid = context.subscription.id
    subscriber_uuid = context.subscription.subscriber_id
    confirmed_assignment_uuid = current_assignment_id
    command_id = uuid4()
    db.rollback()
    try:
        network_service.ont_assignment_commands.reassign_active_ont(
            db,
            command=ReassignActiveOntCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=actor_id,
                    scope=f"subscription:{subscription_uuid}",
                    reason="admin_subscription_change_ont",
                    idempotency_key=(
                        "admin_subscription_change_ont:"
                        f"{confirmed_assignment_uuid}:{target_id}"
                    ),
                ),
                subscriber_id=subscriber_uuid,
                subscription_id=subscription_uuid,
                current_assignment_id=confirmed_assignment_uuid,
                target_ont_unit_id=target_id,
            ),
        )
    except OntAssignmentCommandError as exc:
        return False, str(exc)
    return (
        True,
        "ONT assignment changed. The access path now resolves through the selected ONT.",
    )


def resolve_pon_port_for_ont(db: Session, ont) -> dict[str, object]:
    """Resolve PON port from ONT's board/port or autofind candidate.

    Checks in order:
    1. ONT's board/port fields (set after authorization)
    2. Active autofind candidate (known from discovery before authorization)

    Returns dict with:
        - pon_port_id: UUID if resolved, None otherwise
        - pon_port_label: Human-readable label for display
        - pon_port_resolved: True if auto-resolved
    """
    olt_device_id = getattr(ont, "olt_device_id", None)
    board = getattr(ont, "board", None)
    port = getattr(ont, "port", None)

    # If ONT doesn't have board/port, check autofind candidate
    if not board or not port:
        # First try by ont_unit_id (direct link)
        candidate = db.scalars(
            select(OltAutofindCandidate)
            .where(
                OltAutofindCandidate.ont_unit_id == ont.id,
                OltAutofindCandidate.is_active.is_(True),
            )
            .order_by(OltAutofindCandidate.last_seen_at.desc())
            .limit(1)
        ).first()
        # Fallback to serial number match
        if not candidate:
            normalized_serial = _normalize_serial(ont.serial_number)
            candidate = db.scalars(
                select(OltAutofindCandidate)
                .where(
                    _normalized_serial_expr(OltAutofindCandidate.serial_number)
                    == normalized_serial,
                    OltAutofindCandidate.is_active.is_(True),
                )
                .order_by(OltAutofindCandidate.last_seen_at.desc())
                .limit(1)
            ).first()
        if candidate:
            olt_device_id = candidate.olt_id
            board, port = parse_fsp_parts(candidate.fsp)

    if not olt_device_id or not board or not port:
        return {
            "pon_port_id": None,
            "pon_port_label": None,
            "pon_port_resolved": False,
        }

    fsp = f"{board}/{port}"
    try:
        pon_port = db.scalars(
            select(PonPort)
            .where(
                PonPort.olt_id == olt_device_id,
                PonPort.name == fsp,
                PonPort.is_active.is_(True),
            )
            .limit(1)
        ).first()
        if pon_port is None:
            return {
                "pon_port_id": None,
                "pon_port_label": fsp,
                "pon_port_resolved": False,
            }
        # Get OLT name for display
        olt_name = ""
        olt_device = getattr(ont, "olt_device", None)
        if not olt_device:
            from app.models.network import OLTDevice

            olt_device = db.get(OLTDevice, olt_device_id)
        if olt_device:
            olt_name = f" ({olt_device.name})"
        return {
            "pon_port_id": str(pon_port.id),
            "pon_port_label": f"{fsp}{olt_name}",
            "pon_port_resolved": True,
        }
    except Exception:
        logger.exception("Failed to resolve PON port for ONT %s", ont.id)
        return {
            "pon_port_id": None,
            "pon_port_label": None,
            "pon_port_resolved": False,
        }


def assignment_form_dependencies(db: Session, ont=None) -> dict[str, object]:
    """Return form context for ONT assignment.

    When ont is provided, auto-resolves PON port from discovered board/port.
    Subscriber accounts are fetched via HTMX typeahead search.
    """

    result: dict[str, object] = {
        # Accounts fetched via HTMX typeahead, not static dropdown
        "accounts": [],
        # Addresses will be resolved from selected subscriber
        "addresses": [],
        # Subscriptions will be resolved from selected subscriber
        "subscriptions": [],
    }

    # Auto-resolve PON port from ONT discovery data
    if ont is not None:
        result.update(resolve_pon_port_for_ont(db, ont))

    else:
        result["pon_port_id"] = None
        result["pon_port_label"] = None
        result["pon_port_resolved"] = False

    return result


def get_available_mgmt_ips_for_vlan(
    db: Session, vlan_id: str | None, limit: int = 50
) -> list[dict[str, str]]:
    """Return available management IP addresses from the VLAN's IP pool.

    Source of truth:
    - VLAN has ip_pools relationship
    - IpPool contains IPv4Address records
    - Available = ont_unit_id IS NULL and is_reserved = False
    """
    from app.models.network import IpPool, IPv4Address

    if not vlan_id:
        return []

    vlan_uuid = coerce_uuid(vlan_id)
    if not vlan_uuid:
        return []

    # Find IP pools linked to this VLAN
    pools = db.scalars(
        select(IpPool).where(
            IpPool.vlan_id == vlan_uuid,
            IpPool.is_active.is_(True),
        )
    ).all()

    if not pools:
        return []

    pool_ids = [p.id for p in pools]

    # Get available IPs from those pools
    available_ips = db.scalars(
        select(IPv4Address)
        .where(
            IPv4Address.pool_id.in_(pool_ids),
            IPv4Address.ont_unit_id.is_(None),
            IPv4Address.is_reserved.is_(False),
        )
        .order_by(IPv4Address.address)
        .limit(limit)
    ).all()

    return [{"address": ip.address, "id": str(ip.id)} for ip in available_ips]


def parse_form_values(form) -> dict[str, object]:
    """Parse ONT assignment form values."""
    return {
        "pon_port_id": form.get("pon_port_id", "").strip(),
        "account_id": form.get("account_id", "").strip() or None,
        "subscription_id": form.get("subscription_id", "").strip() or None,
        "service_address_id": form.get("service_address_id", "").strip() or None,
        "notes": form.get("notes", "").strip() or None,
    }


def validate_form_values(values: dict[str, object]) -> str | None:
    """Require the exact normal-provisioning identity selected by the operator."""
    if not values.get("account_id"):
        return "Subscriber account is required"
    if not values.get("subscription_id"):
        return "Exact service subscription is required"
    if not values.get("pon_port_id"):
        return "A modeled PON port is required before assignment"
    return None


def has_active_assignment(db: Session, ont_id: str) -> bool:
    """Return True when ONT already has an active assignment."""
    assignments = network_service.ont_assignments.list(
        db=db,
        ont_unit_id=ont_id,
        pon_port_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=20,
        offset=0,
    )
    return any(a.active for a in assignments)


def active_assignment_for_ont_id(db: Session, ont_id) -> Any | None:
    from app.models.network import OntAssignment

    return db.scalars(
        select(OntAssignment)
        .where(OntAssignment.ont_unit_id == ont_id)
        .where(OntAssignment.active.is_(True))
        .limit(1)
    ).first()


def resolve_pon_port_id_for_assignment(
    db: Session, ont, values: dict[str, object]
) -> str | None:
    """Resolve PON port ID for assignment, auto-detecting from ONT if possible."""
    # Use explicitly provided value first
    if values.get("pon_port_id"):
        return str(values["pon_port_id"])

    # Auto-resolve from ONT's discovered board/port
    resolved = resolve_pon_port_for_ont(db, ont)
    if resolved.get("pon_port_id"):
        return str(resolved["pon_port_id"])

    # For TR-069-only devices, PON port is optional
    return None


def create_assignment(
    db: Session,
    ont,
    values: dict[str, object],
    *,
    actor_id: str | None = None,
) -> Any:
    """Delegate an exact normal assignment to its canonical command owner."""
    pon_port_id_str = resolve_pon_port_id_for_assignment(db, ont, values)
    if not pon_port_id_str or not values.get("subscription_id"):
        raise ValueError("Exact subscription and modeled PON are required")
    return network_service.ont_assignment_commands.assign(
        db,
        ont_unit_id=ont.id,
        subscription_id=str(values["subscription_id"]),
        pon_port_id=pon_port_id_str,
        subscriber_id=str(values["account_id"]),
        service_address_id=(
            str(values["service_address_id"])
            if values.get("service_address_id")
            else None
        ),
        notes=str(values.get("notes")) if values.get("notes") else None,
        actor_id=actor_id,
        source="admin_assignment_form",
    )


def form_payload(
    values: dict[str, object], db: Session | None = None
) -> dict[str, object]:
    """Return template-friendly form payload.

    If db is provided and account_id is set, looks up the subscriber label
    so the typeahead field can be repopulated on validation errors.
    """
    result = {
        "pon_port_id": values.get("pon_port_id"),
        "account_id": values.get("account_id"),
        "subscription_id": values.get("subscription_id"),
        "subscription_label": "",
        "account_label": "",
        "service_address_id": values.get("service_address_id"),
        "notes": values.get("notes"),
    }
    # Look up subscriber label for typeahead repopulation
    if db and values.get("account_id"):
        try:
            subscriber = subscriber_service.subscribers.get(
                db, str(values["account_id"])
            )
            if subscriber:
                if subscriber.category == subscriber.category.business:
                    result["account_label"] = (
                        subscriber.company_name
                        or subscriber.display_name
                        or subscriber.full_name
                    )
                else:
                    label = f"{subscriber.first_name} {subscriber.last_name}"
                    if subscriber.email:
                        label = f"{label} ({subscriber.email})"
                    result["account_label"] = label
        except Exception:
            pass  # Keep empty label on lookup failure
    if db and values.get("subscription_id"):
        try:
            from app.models.catalog import Subscription

            subscription = db.get(Subscription, values["subscription_id"])
            if subscription:
                offer = getattr(subscription, "offer", None)
                result["subscription_label"] = (
                    getattr(subscription, "login", None)
                    or getattr(offer, "name", None)
                    or str(subscription.id)
                )
        except Exception:
            pass
    return result


def assignment_form_payload_from_assignment(assignment) -> dict[str, object]:
    """Return template-friendly form defaults for an existing assignment."""
    subscriber = getattr(assignment, "subscriber", None)
    account_label = getattr(subscriber, "name", "") if subscriber else ""
    return {
        "pon_port_id": str(assignment.pon_port_id) if assignment.pon_port_id else "",
        "account_id": str(assignment.subscriber_id) if assignment.subscriber_id else "",
        "subscription_id": (
            str(assignment.subscription_id) if assignment.subscription_id else ""
        ),
        "subscription_label": (
            getattr(getattr(assignment, "subscription", None), "login", None) or ""
        ),
        "account_label": account_label,
        "service_address_id": (
            str(assignment.service_address_id) if assignment.service_address_id else ""
        ),
        "notes": assignment.notes or "",
    }


def get_ont_for_assignment_form(db: Session, ont_id: str) -> AssignmentFormResult:
    """Load the ONT for a new assignment form."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return AssignmentFormResult(not_found=True, not_found_message="ONT not found")
    return AssignmentFormResult(ont=ont)


def get_assignment_edit_form(
    db: Session,
    *,
    ont_id: str,
    assignment_id: str,
) -> AssignmentFormResult:
    """Load an existing ONT assignment for the edit form."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return AssignmentFormResult(not_found=True, not_found_message="ONT not found")

    try:
        assignment = network_service.ont_assignments.get(db, assignment_id)
    except HTTPException:
        return AssignmentFormResult(
            not_found=True,
            not_found_message="Assignment not found for this ONT",
        )

    if str(assignment.ont_unit_id) != str(ont.id):
        return AssignmentFormResult(
            not_found=True,
            not_found_message="Assignment not found for this ONT",
        )

    return AssignmentFormResult(
        ont=ont,
        assignment=assignment,
        values=assignment_form_payload_from_assignment(assignment),
    )


def create_assignment_from_form(
    db: Session,
    ont_id: str,
    form,
    *,
    actor_id: str | None = None,
) -> AssignmentFormResult:
    """Validate and create an ONT assignment from submitted web form data."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return AssignmentFormResult(not_found=True)

    values = parse_form_values(form)
    error = validate_form_values(values)
    active_assignment = active_assignment_for_ont_id(db, ont.id)
    if (
        not error
        and active_assignment is not None
        and (
            getattr(active_assignment, "subscription_id", None) is not None
            or getattr(active_assignment, "subscriber_id", None) is not None
        )
    ):
        error = "This ONT is already assigned"
    if error:
        return AssignmentFormResult(ont=ont, values=values, error=error)

    try:
        create_assignment(db, ont, values, actor_id=actor_id)
    except IntegrityError as exc:
        db.rollback()
        msg = (
            "This ONT is already assigned. Refresh the page and try again."
            if "ix_ont_assignments_active_unit" in str(exc)
            else "Could not create assignment due to a data conflict."
        )
        return AssignmentFormResult(ont=ont, values=values, error=msg)
    except (HTTPException, ValueError) as exc:
        db.rollback()
        detail = getattr(exc, "detail", None) or str(exc)
        return AssignmentFormResult(ont=ont, values=values, error=str(detail))
    return AssignmentFormResult(ont=ont, values=values)


def update_assignment_from_form(
    db: Session,
    *,
    ont_id: str,
    assignment_id: str,
    form,
) -> AssignmentFormResult:
    """Retire direct identity edits in favor of reviewed repair."""
    loaded = get_assignment_edit_form(db, ont_id=ont_id, assignment_id=assignment_id)
    if loaded.not_found:
        return loaded

    assignment = loaded.assignment
    values = parse_form_values(form)
    return AssignmentFormResult(
        ont=loaded.ont,
        assignment=assignment,
        values=values,
        error=(
            "Direct assignment identity edits are retired. Use the ONT identity "
            "review workflow for corrections."
        ),
    )


def remove_assignment(
    db: Session,
    *,
    ont_id: str,
    assignment_id: str,
    actor_id: str | None = None,
) -> AssignmentFormResult:
    """Close an exact normal assignment through the command owner."""
    loaded = get_assignment_edit_form(db, ont_id=ont_id, assignment_id=assignment_id)
    if loaded.not_found:
        return loaded

    try:
        network_service.ont_assignment_commands.release(
            db,
            assignment_id=assignment_id,
            reason="admin_released",
            actor_id=actor_id,
            source="admin_assignment_form",
        )
    except (HTTPException, ValueError) as exc:
        db.rollback()
        return AssignmentFormResult(
            ont=loaded.ont,
            assignment=loaded.assignment,
            error=str(getattr(exc, "detail", None) or exc),
        )
    return loaded
