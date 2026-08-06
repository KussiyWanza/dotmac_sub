"""Route-facing workflow helpers for admin catalog subscriptions."""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote_plus
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.audit import AuditActorType
from app.models.catalog import (
    NasDevice,
    NasDeviceStatus,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import IPAssignment, IpPool, IPv4Address, IPVersion
from app.models.subscriber import SubscriberCategory
from app.services import catalog as catalog_service
from app.services import subscriber as subscriber_service
from app.services import web_catalog_subscriptions as core
from app.services.action_forms import (
    ActionConfirmation,
    ActionForm,
    ActionHiddenValue,
    ActionTone,
)
from app.services.audit_helpers import build_audit_activities
from app.services.common import coerce_uuid
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.ip_assignment_lifecycle import (
    IPv4ServedProjectionDecision,
    RepairServiceIPv4ProjectionCommand,
    ServiceIPv4ProjectionOutcome,
    preview_service_ipv4_projection_repair,
    repair_service_ipv4_projection,
)
from app.services.owner_commands import CommandContext
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
)
from app.services.prepaid_recovery_billing import (
    PrepaidRecoveryDraftConfirmation,
    create_prepaid_recovery_draft,
    preview_prepaid_recovery_draft,
    resolve_prepaid_recovery_draft_eligibility,
)
from app.services.subscription_correction import (
    CorrectSubscriptionCommand,
    correct_subscription,
    list_correction_candidates,
    preview_subscription_correction,
)
from app.services.subscription_lifecycle import (
    SubscriptionCommandKind,
    SubscriptionCommandOutcomeStatus,
    SubscriptionEffectiveTiming,
    SubscriptionLifecycleCommand,
    SubscriptionLifecycleError,
    SubscriptionLifecycleState,
    preview_subscription_command,
    resolve_subscription_lifecycle,
)
from app.services.subscription_lifecycle_batch import (
    SubscriptionBatchOutcome,
    SubscriptionBatchPreview,
    SubscriptionLifecycleBatchError,
    execute_subscription_batch,
    preview_subscription_batch,
)
from app.services.subscription_lifecycle_commands import execute_subscription_command
from app.services.subscription_lifecycle_schedules import (
    SubscriptionLifecycleScheduleError,
    cancel_scheduled_subscription_status_command,
)
from app.services.subscription_nas_assignment import (
    MoveSubscriptionServiceAccessCommand,
    ServiceAccessMoveOutcome,
    ServiceAccessMovePreview,
    move_subscription_service_access,
    preview_subscription_service_access_move,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubscriptionIPv4ProjectionReconciliationCommand:
    """Typed web command for one owner-reviewed served-IP repair."""

    subscription_id: UUID
    assignment_id: UUID
    preview_fingerprint: str
    idempotency_key: str
    actor_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("preview_fingerprint", self.preview_fingerprint),
            ("idempotency_key", self.idempotency_key),
            ("actor_id", self.actor_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")


@dataclass(frozen=True, slots=True)
class SubscriptionServiceAccessMoveConfirmation:
    """Typed web confirmation for one reviewed service-access move."""

    subscription_id: UUID
    target_nas_device_id: UUID
    target_pool_id: UUID
    target_ipv4: str
    preview_fingerprint: str
    idempotency_key: str
    actor_id: str
    reason: str

    def __post_init__(self) -> None:
        for name, value in (
            ("target_ipv4", self.target_ipv4),
            ("preview_fingerprint", self.preview_fingerprint),
            ("idempotency_key", self.idempotency_key),
            ("actor_id", self.actor_id),
            ("reason", self.reason),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")


_IPV4_PROJECTION_BLOCKERS: dict[IPv4ServedProjectionDecision, str] = {
    IPv4ServedProjectionDecision.subscription_not_found: (
        "The subscription no longer exists."
    ),
    IPv4ServedProjectionDecision.subscription_not_active: (
        "Only an active subscription can reconcile its served IPv4."
    ),
    IPv4ServedProjectionDecision.missing_exact_assignment: (
        "No exact active IPAM assignment is linked to this subscription."
    ),
    IPv4ServedProjectionDecision.multiple_exact_assignments: (
        "More than one active IPv4 assignment is linked to this subscription. "
        "Review IPAM before reconciling."
    ),
    IPv4ServedProjectionDecision.assignment_subscriber_mismatch: (
        "The IPAM assignment belongs to a different subscriber."
    ),
    IPv4ServedProjectionDecision.missing_login: (
        "The subscription has no PPPoE login to project to RADIUS."
    ),
    IPv4ServedProjectionDecision.shared_login_not_selected: (
        "The RADIUS projection owner selected another subscription for this login."
    ),
    IPv4ServedProjectionDecision.radius_observation_unavailable: (
        "Current RADIUS evidence is unavailable. Try again after RADIUS recovers."
    ),
    IPv4ServedProjectionDecision.radius_projection_not_aligned: (
        "RADIUS does not match the currently served IPv4. Repair that evidence "
        "before changing the served projection."
    ),
    IPv4ServedProjectionDecision.session_observation_conflict: (
        "A live session is using an address other than the current or desired IPv4."
    ),
}


def prepaid_bill_now_preview_context(
    db: Session, *, subscription_id: str
) -> dict[str, object]:
    preview = preview_prepaid_recovery_draft(db, subscription_id=UUID(subscription_id))
    return {"prepaid_bill_now_preview": preview}


def confirm_prepaid_bill_now(
    db: Session,
    *,
    subscription_id: str,
    fingerprint: str,
    starts_at: datetime,
    actor_id: str | None,
) -> str:
    command_id = uuid4()
    result = create_prepaid_recovery_draft(
        db,
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor=actor_id or "admin:unknown",
            scope="billing:invoice:update",
            reason="Create prepaid recovery invoice from Bill Now confirmation",
            idempotency_key=f"prepaid-recovery-draft:{subscription_id}:{fingerprint}",
        ),
        confirmation=PrepaidRecoveryDraftConfirmation(
            subscription_id=UUID(subscription_id),
            starts_at=starts_at,
            fingerprint=fingerprint,
        ),
    )
    return f"/admin/billing/invoices/{result.invoice_id}"


SERVICE_CHANGE_FINANCIAL_POSITION_MESSAGE = (
    "The verified prepaid funding position is still under review. "
    "Complete the reviewed reconstruction before changing this service."
)


_CREATE_LIFECYCLE_COMMANDS = {
    SubscriptionStatus.active: SubscriptionCommandKind.activate,
    SubscriptionStatus.suspended: SubscriptionCommandKind.suspend,
    SubscriptionStatus.disabled: SubscriptionCommandKind.disable,
    SubscriptionStatus.canceled: SubscriptionCommandKind.cancel,
}


def _apply_requested_create_lifecycle(
    db: Session,
    *,
    created: Subscription,
    requested_status: SubscriptionStatus,
    actor_id: str | None,
) -> str | None:
    """Apply the selected post-create lifecycle action through its owner."""
    if requested_status == SubscriptionStatus.pending:
        return None

    source = f"admin:catalog:{actor_id or 'system'}"
    reason = f"Selected {requested_status.value} during subscription creation"
    command_kind = _CREATE_LIFECYCLE_COMMANDS[requested_status]
    snapshot = resolve_subscription_lifecycle(db, str(created.id))
    command = SubscriptionLifecycleCommand(
        subscription_id=str(created.id),
        kind=command_kind,
        source=source,
        reason=reason,
        expected_head=snapshot.head,
        idempotency_key=f"admin-create-{command_kind.value}:{created.id}",
    )
    outcome = execute_subscription_command(
        db,
        command,
        actor_id=actor_id,
        actor_type=(AuditActorType.user if actor_id else AuditActorType.system),
    )
    if outcome.status not in {
        SubscriptionCommandOutcomeStatus.applied,
        SubscriptionCommandOutcomeStatus.skipped,
    }:
        return outcome.message
    return None


def preview_lifecycle_command_response(
    db: Session,
    *,
    subscription_id: str,
    kind: SubscriptionCommandKind,
    actor_id: str | None,
    reason: str | None = None,
    target_offer_id: str | None = None,
    effective_timing: SubscriptionEffectiveTiming = (
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = None,
) -> tuple[dict[str, object], int]:
    """Preview one lifecycle command using the same contract as execution."""
    try:
        command = SubscriptionLifecycleCommand(
            subscription_id=subscription_id,
            kind=kind,
            source=f"admin:catalog:{actor_id or 'system'}",
            effective_timing=effective_timing,
            effective_at=effective_at,
            target_offer_id=target_offer_id,
            reason=reason,
        )
        preview = preview_subscription_command(db, command)
    except PrepaidFundingBaselineMissingError:
        return (
            {
                "status": "unavailable",
                "message": SERVICE_CHANGE_FINANCIAL_POSITION_MESSAGE,
                "error_code": "financial_position_unavailable",
            },
            409,
        )
    except (SubscriptionLifecycleError, ValueError) as exc:
        missing = "not found" in str(exc).lower()
        return (
            {
                "status": "rejected",
                "message": str(exc),
                "error_code": (
                    "subscription_not_found" if missing else "invalid_lifecycle_command"
                ),
            },
            404 if missing else 422,
        )
    return (
        {
            "status": "previewed",
            "expected_head": preview.current.head,
            "effective_at": preview.effective_at.isoformat(),
            "eligible": preview.eligible,
            "eligibility_reasons": list(preview.eligibility_reasons),
            "requires_confirmation": preview.requires_confirmation,
            "current": _serialize_lifecycle_state(preview.current.state),
            "proposed": _serialize_lifecycle_state(preview.proposed),
            "billing_impact": _json_value(preview.billing_impact),
            "access_impact": _json_value(preview.access_impact),
            "recommended_action_url": (
                "/admin/billing/invoices?status=draft"
                if "prepaid_financial_reconciliation_required"
                in preview.eligibility_reasons
                else None
            ),
        },
        200,
    )


def execute_lifecycle_command_response(
    db: Session,
    *,
    subscription_id: str,
    kind: SubscriptionCommandKind,
    actor_id: str | None,
    expected_head: str | None = None,
    preview_fingerprint: str | None = None,
    idempotency_key: str | None = None,
    reason: str | None = None,
    target_offer_id: str | None = None,
    effective_timing: SubscriptionEffectiveTiming = (
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = None,
) -> tuple[dict[str, object], int]:
    """Execute one reviewed admin lifecycle command and serialize its outcome."""
    try:
        command = SubscriptionLifecycleCommand(
            subscription_id=subscription_id,
            kind=kind,
            source=f"admin:catalog:{actor_id or 'system'}",
            effective_timing=effective_timing,
            effective_at=effective_at,
            target_offer_id=target_offer_id,
            reason=reason,
            expected_head=expected_head,
            expected_financial_fingerprint=preview_fingerprint,
            idempotency_key=idempotency_key,
        )
        if (
            command.kind == SubscriptionCommandKind.change_plan
            and command.effective_timing == SubscriptionEffectiveTiming.immediate
            and not command.expected_financial_fingerprint
        ):
            return (
                {
                    "status": "rejected",
                    "message": (
                        "Preview the financial plan change before confirming it"
                    ),
                    "previous_head": expected_head,
                    "current_head": expected_head,
                    "artifact_ids": [],
                    "error_code": "plan_change_preview_required",
                },
                422,
            )
        outcome = execute_subscription_command(
            db,
            command,
            actor_id=actor_id,
            actor_type=AuditActorType.user if actor_id else AuditActorType.system,
        )
    except PrepaidFundingBaselineMissingError:
        return (
            {
                "status": "unavailable",
                "message": SERVICE_CHANGE_FINANCIAL_POSITION_MESSAGE,
                "previous_head": expected_head,
                "current_head": expected_head,
                "artifact_ids": [],
                "error_code": "financial_position_unavailable",
                "replayed": False,
            },
            409,
        )
    except (SubscriptionLifecycleError, ValueError) as exc:
        missing = "not found" in str(exc).lower()
        return (
            {
                "status": "rejected",
                "message": str(exc),
                "previous_head": None,
                "current_head": None,
                "artifact_ids": [],
                "error_code": (
                    "subscription_not_found" if missing else "invalid_lifecycle_command"
                ),
                "replayed": False,
            },
            404 if missing else 422,
        )
    status_code = {
        "applied": 200,
        "scheduled": 202,
        "skipped": 200,
        "rejected": 409,
        "superseded": 409,
        "failed": 500,
    }[outcome.status.value]
    return (
        {
            "status": outcome.status.value,
            "message": outcome.message,
            "previous_head": outcome.previous_head,
            "current_head": outcome.current_head,
            "artifact_ids": list(outcome.artifact_ids),
            "error_code": outcome.error_code,
            "replayed": outcome.replayed,
        },
        status_code,
    )


def cancel_lifecycle_schedule_response(
    db: Session,
    *,
    subscription_id: str,
    schedule_id: str,
    actor_id: str | None,
) -> tuple[dict[str, object], int]:
    """Cancel one pending lifecycle status schedule."""
    try:
        schedule = cancel_scheduled_subscription_status_command(
            db,
            schedule_id,
            subscription_id=subscription_id,
            actor_id=actor_id,
        )
    except SubscriptionLifecycleScheduleError as exc:
        missing = "not found" in str(exc).lower()
        return (
            {
                "status": "rejected",
                "schedule_id": schedule_id,
                "message": str(exc),
                "error_code": (
                    "lifecycle_schedule_not_found"
                    if missing
                    else "lifecycle_schedule_not_cancelable"
                ),
            },
            404 if missing else 409,
        )
    return (
        {
            "status": schedule.status.value,
            "schedule_id": str(schedule.id),
            "message": "Lifecycle schedule canceled",
            "error_code": None,
        },
        200,
    )


def cancel_lifecycle_schedule_redirect(
    db: Session,
    *,
    subscription_id: str,
    schedule_id: str,
    actor_id: str | None,
) -> str:
    """Cancel one lifecycle schedule and return to its subscription detail."""
    payload, status_code = cancel_lifecycle_schedule_response(
        db,
        subscription_id=subscription_id,
        schedule_id=schedule_id,
        actor_id=actor_id,
    )
    base = f"/admin/catalog/subscriptions/{subscription_id}"
    message = str(payload["message"])
    query_name = "notice" if status_code == 200 else "error"
    return f"{base}?{query_name}={quote_plus(message)}"


def get_subscription_or_none(
    db: Session,
    subscription_id: str,
) -> Subscription | None:
    """Return a subscription for route-level 404 handling."""
    try:
        return catalog_service.subscriptions.get(
            db=db,
            subscription_id=subscription_id,
        )
    except Exception:
        return None


def subscription_edit_form_context(
    db: Session,
    subscription_id: str,
) -> dict[str, object] | None:
    """Build subscription edit form context, or None when missing."""
    subscription_obj = get_subscription_or_none(db, subscription_id)
    if subscription_obj is None:
        return None
    subscription = core.edit_form_data(db, subscription_obj)
    context = core.subscription_form_context(db, subscription)
    context["activities"] = build_audit_activities(
        db, "subscription", str(subscription_id)
    )
    context["action_url"] = f"/admin/catalog/subscriptions/{subscription_id}/edit"
    return context


def service_access_move_form_context(
    db: Session,
    subscription_id: str,
    *,
    preview: ServiceAccessMovePreview | None = None,
    reason: str = "",
    error: str | None = None,
) -> dict[str, object] | None:
    """Build the server-owned form context for one service-access move."""

    subscription = get_subscription_or_none(db, subscription_id)
    if subscription is None:
        return None
    current_nas = (
        db.get(NasDevice, subscription.provisioning_nas_device_id)
        if subscription.provisioning_nas_device_id
        else None
    )
    nas_devices = list(
        db.scalars(
            select(NasDevice)
            .where(
                NasDevice.is_active.is_(True),
                NasDevice.status == NasDeviceStatus.active,
                NasDevice.id != subscription.provisioning_nas_device_id,
            )
            .order_by(NasDevice.name, NasDevice.id)
        ).all()
    )
    pools = list(
        db.scalars(
            select(IpPool)
            .where(
                IpPool.is_active.is_(True),
                IpPool.ip_version == IPVersion.ipv4,
                IpPool.nas_device_id.is_not(None),
            )
            .order_by(IpPool.name, IpPool.id)
        ).all()
    )
    current_ipv4 = core.active_service_ipv4_address(db, subscription_id)
    return {
        "subscription": subscription,
        "current_nas": current_nas,
        "current_ipv4": current_ipv4,
        "target_nas_devices": nas_devices,
        "target_pools": pools,
        "preview": preview,
        "reason": reason,
        "error": error,
        "idempotency_key": f"admin-service-access-move:{uuid4()}",
    }


def service_access_move_available_ipv4(
    db: Session,
    *,
    target_nas_device_id: UUID,
    target_pool_id: UUID,
) -> tuple[str, ...]:
    """List materialized, currently free IPv4 addresses for one linked pool."""

    pool = db.get(IpPool, target_pool_id)
    if (
        pool is None
        or not pool.is_active
        or pool.ip_version is not IPVersion.ipv4
        or pool.nas_device_id != target_nas_device_id
    ):
        raise ValueError("The selected IPv4 pool is not linked to the target router.")
    active_address_ids = select(IPAssignment.ipv4_address_id).where(
        IPAssignment.is_active.is_(True),
        IPAssignment.ip_version == IPVersion.ipv4,
        IPAssignment.ipv4_address_id.is_not(None),
    )
    rows = list(
        db.scalars(
            select(IPv4Address)
            .where(
                IPv4Address.pool_id == pool.id,
                IPv4Address.is_reserved.is_(False),
                IPv4Address.ont_unit_id.is_(None),
                IPv4Address.id.not_in(active_address_ids),
            )
            .order_by(IPv4Address.address)
        ).all()
    )
    return tuple(
        str(row.address)
        for row in rows
        if str(row.allocation_type or "").strip().lower() != "management"
    )


def preview_subscription_service_access_move_form(
    db: Session,
    *,
    subscription_id: UUID,
    target_nas_device_id: UUID,
    target_pool_id: UUID,
    target_ipv4: str,
) -> ServiceAccessMovePreview:
    return preview_subscription_service_access_move(
        db,
        subscription_id=subscription_id,
        target_nas_device_id=target_nas_device_id,
        target_pool_id=target_pool_id,
        target_ipv4=target_ipv4,
    )


def execute_subscription_service_access_move(
    db: Session,
    *,
    confirmation: SubscriptionServiceAccessMoveConfirmation,
) -> ServiceAccessMoveOutcome:
    """Release adapter reads and delegate one confirmed move to its owner."""

    db_session_adapter.release_read_transaction(db)
    command_id = uuid4()
    return move_subscription_service_access(
        db,
        MoveSubscriptionServiceAccessCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"admin:{confirmation.actor_id}",
                scope="catalog:subscription:service-access:move",
                reason=confirmation.reason,
                idempotency_key=confirmation.idempotency_key,
            ),
            subscription_id=confirmation.subscription_id,
            target_nas_device_id=confirmation.target_nas_device_id,
            target_pool_id=confirmation.target_pool_id,
            target_ipv4=confirmation.target_ipv4,
            preview_fingerprint=confirmation.preview_fingerprint,
        ),
    )


def subscription_detail_page_context(
    db: Session,
    subscription_id: str,
) -> dict[str, object] | None:
    """Build subscription detail page context, or None when missing."""
    subscription = get_subscription_or_none(db, subscription_id)
    if subscription is None:
        return None
    context: dict[str, object] = {
        "subscription": subscription,
        "activities": build_audit_activities(db, "subscription", str(subscription_id)),
        "offer_options": core.active_offer_options(db),
        "scheduled_plan_change": _scheduled_plan_change_context(db, subscription_id),
        "scheduled_status_changes": _scheduled_status_change_context(
            db, subscription_id
        ),
        "correction_action_forms": _subscription_correction_action_forms(
            db, subscription_id
        ),
        "ipv4_projection_reconciliation_action": (
            _subscription_ipv4_projection_reconciliation_action(
                db,
                subscription=subscription,
            )
        ),
    }
    context.update(core.subscription_detail_context(db, subscription))
    if (
        subscription.status == SubscriptionStatus.suspended
        and subscription.billing_mode.value == "prepaid"
    ):
        context["prepaid_bill_now_eligibility"] = (
            resolve_prepaid_recovery_draft_eligibility(
                db, subscription_id=subscription.id
            )
        )
    return context


def _disabled_ipv4_projection_action(
    *,
    subscription_id: UUID,
    description: str,
    reason: str,
) -> ActionForm:
    return ActionForm(
        key="admin.subscription_ipv4_projection_reconciliation",
        title="Reconcile served IPv4",
        description=description,
        action_url=(f"/admin/catalog/subscriptions/{subscription_id}/ipv4/reconcile"),
        submit_label="Reconcile served IPv4",
        fields=(),
        tone=ActionTone.neutral,
        allowed=False,
        disabled_reason=reason,
    )


def _subscription_ipv4_projection_reconciliation_action(
    db: Session,
    *,
    subscription: Subscription,
) -> ActionForm | None:
    """Project the exact owner preview into one server-owned admin action."""

    rows = list(
        db.execute(
            select(IPAssignment, IPv4Address.address)
            .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
            .where(
                IPAssignment.subscription_id == subscription.id,
                IPAssignment.ip_version == IPVersion.ipv4,
                IPAssignment.is_active.is_(True),
            )
            .order_by(IPAssignment.id)
        ).all()
    )
    served_address = str(subscription.ipv4_address or "").strip() or "not set"
    if not rows:
        if served_address == "not set":
            return None
        return _disabled_ipv4_projection_action(
            subscription_id=subscription.id,
            description=(
                f"The served IPv4 is {served_address}, but no exact active IPAM "
                "assignment is linked to this subscription."
            ),
            reason=_IPV4_PROJECTION_BLOCKERS[
                IPv4ServedProjectionDecision.missing_exact_assignment
            ],
        )
    if len(rows) > 1:
        return _disabled_ipv4_projection_action(
            subscription_id=subscription.id,
            description=(
                f"The served IPv4 is {served_address}; IPAM has {len(rows)} active "
                "assignments for this subscription."
            ),
            reason=_IPV4_PROJECTION_BLOCKERS[
                IPv4ServedProjectionDecision.multiple_exact_assignments
            ],
        )

    assignment, desired_address = rows[0]
    try:
        preview = preview_service_ipv4_projection_repair(
            db,
            subscription_id=subscription.id,
            assignment_id=assignment.id,
        )
    except Exception:
        logger.exception(
            "IPv4 projection preview unavailable for subscription %s",
            subscription.id,
        )
        return _disabled_ipv4_projection_action(
            subscription_id=subscription.id,
            description=(
                f"IPAM owns {desired_address}; the served IPv4 is {served_address}."
            ),
            reason="Projection evidence is temporarily unavailable.",
        )

    if preview.decision is IPv4ServedProjectionDecision.noop:
        return None

    radius_address = preview.observed_radius_address or "unavailable"
    description = (
        f"IPAM owns {preview.desired_address or desired_address}; the served IPv4 "
        f"is {preview.served_address or 'not set'}, and RADIUS reports "
        f"{radius_address}."
    )
    if not preview.applicable:
        return _disabled_ipv4_projection_action(
            subscription_id=subscription.id,
            description=description,
            reason=_IPV4_PROJECTION_BLOCKERS.get(
                preview.decision,
                f"The owner preview blocked this repair ({preview.decision.value}).",
            ),
        )

    session_label = (
        f"{preview.old_address_session_count} old-address session(s)"
        if preview.old_address_session_count
        else "no old-address sessions"
    )
    return ActionForm(
        key="admin.subscription_ipv4_projection_reconciliation",
        title="Reconcile served IPv4",
        description=description,
        action_url=(f"/admin/catalog/subscriptions/{subscription.id}/ipv4/reconcile"),
        submit_label="Reconcile served IPv4",
        fields=(),
        hidden_values=(
            ActionHiddenValue(key="assignment_id", value=str(assignment.id)),
            ActionHiddenValue(
                key="preview_fingerprint",
                value=preview.fingerprint,
            ),
            ActionHiddenValue(
                key="idempotency_key",
                value=f"admin-ipv4-projection:{uuid4()}",
            ),
        ),
        tone=ActionTone.neutral,
        impact=(
            f"Set the served and RADIUS IPv4 to {preview.desired_address}, then "
            f"reauthenticate only {session_label}. Billing, plan, add-ons, and "
            "service period remain unchanged."
        ),
        confirmation=ActionConfirmation(
            title="Confirm this exact IPv4 reconciliation",
            message=(
                "I reviewed the IPAM, served-IP, RADIUS, and live-session evidence. "
                "The customer may reconnect briefly if an old-address session exists."
            ),
        ),
    )


def execute_subscription_ipv4_projection_reconciliation(
    db: Session,
    *,
    command: SubscriptionIPv4ProjectionReconciliationCommand,
) -> ServiceIPv4ProjectionOutcome:
    """Delegate one confirmed web repair to the canonical projection owner."""

    command_id = uuid4()
    return repair_service_ipv4_projection(
        db,
        RepairServiceIPv4ProjectionCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"admin:{command.actor_id}",
                scope="catalog:subscription:ipv4:reconcile",
                reason=(
                    "Reconcile the exact service served IPv4 to its reviewed active "
                    "IPAM assignment"
                ),
                idempotency_key=command.idempotency_key,
            ),
            subscription_id=command.subscription_id,
            assignment_id=command.assignment_id,
            preview_fingerprint=command.preview_fingerprint,
        ),
    )


def _subscription_correction_action_forms(
    db: Session, active_subscription_id: str
) -> tuple[ActionForm, ...]:
    """Project one exact, server-owned review form per restorable sibling."""
    forms: list[ActionForm] = []
    for candidate in list_correction_candidates(db, active_subscription_id):
        preview = preview_subscription_correction(
            db,
            active_subscription_id=active_subscription_id,
            target_subscription_id=str(candidate.subscription_id),
        )
        issue_text = " ".join(issue.message for issue in preview.issues)
        fup_present = bool(preview.active_fup_status or preview.target_fup_status)
        target_created_at = preview.target_created_at.isoformat()
        target_identity = (
            f"subscription {preview.target_subscription_id}, created "
            f"{target_created_at}"
        )
        impact = (
            f"Cancel {preview.active_offer_name}; restore {preview.target_offer_name} "
            f"({preview.target_status.value}; {target_identity}). Move PPPoE credential "
            f"{preview.credential_username or 'unavailable'} to "
            f"{preview.target_radius_profile_name or 'an unavailable profile'} at "
            f"{preview.target_speed_label or 'an unconfigured speed'}. "
            f"{'Clear existing FUP runtime state. ' if fup_present else 'No active FUP runtime state was found. '}"
            "Preserve all financial history with no automatic credit or invoice adjustment."
        )
        hidden_values: tuple[ActionHiddenValue, ...] = ()
        confirmation: ActionConfirmation | None = None
        if preview.eligible:
            hidden_values = (
                ActionHiddenValue(
                    key="target_subscription_id",
                    value=str(preview.target_subscription_id),
                ),
                ActionHiddenValue(key="preview_fingerprint", value=preview.fingerprint),
                ActionHiddenValue(
                    key="idempotency_key",
                    value=f"subscription-correction:{uuid4()}",
                ),
            )
            confirmation = ActionConfirmation(
                title="Confirm this exact subscription correction",
                message=(
                    "I reviewed the mistaken and restored plans, PPPoE profile, FUP "
                    "cleanup, and the absence of automatic financial adjustments."
                ),
            )
        forms.append(
            ActionForm(
                key=f"admin.subscription_correction.{candidate.subscription_id}",
                title=(
                    f"Correct mistake: restore {preview.target_offer_name} "
                    f"({str(preview.target_subscription_id)[:8]})"
                ),
                description=(
                    "Use this only to repair an accidental duplicate activation. "
                    f"Target {target_identity}. "
                    "The command owner rechecks every item under lock."
                ),
                action_url=(
                    f"/admin/catalog/subscriptions/{active_subscription_id}/"
                    "correction/execute"
                ),
                submit_label="Apply reviewed correction",
                fields=(),
                hidden_values=hidden_values,
                tone=ActionTone.neutral,
                impact=impact,
                confirmation=confirmation,
                allowed=preview.eligible,
                disabled_reason=None if preview.eligible else issue_text,
            )
        )
    return tuple(forms)


def execute_subscription_correction_response(
    db: Session,
    *,
    active_subscription_id: str,
    target_subscription_id: str,
    preview_fingerprint: str,
    idempotency_key: str,
    actor_id: str | None,
) -> tuple[dict[str, object], int]:
    """Execute one reviewed correction and map domain failures for the route."""
    command_id = uuid4()
    try:
        outcome = correct_subscription(
            db,
            CorrectSubscriptionCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=f"admin:{actor_id or 'unknown'}",
                    scope="catalog:write",
                    reason="Correct mistaken active subscription",
                    idempotency_key=idempotency_key,
                ),
                active_subscription_id=UUID(active_subscription_id),
                target_subscription_id=UUID(target_subscription_id),
                preview_fingerprint=preview_fingerprint,
            ),
        )
    except (DomainError, ValueError) as exc:
        code = getattr(exc, "code", "access.subscription_correction.invalid_command")
        message = getattr(exc, "message", str(exc))
        status_code = 404 if str(code).endswith("subscription_not_found") else 409
        if "invalid_" in str(code):
            status_code = 422
        return {
            "status": "rejected",
            "message": message,
            "error_code": code,
        }, status_code
    return (
        {
            "status": "applied",
            "message": "Subscription correction applied; connectivity reconciliation has been requested.",
            "active_subscription_id": str(outcome.active_subscription_id),
            "target_subscription_id": str(outcome.target_subscription_id),
            "credential_id": str(outcome.credential_id),
            "radius_profile_id": str(outcome.radius_profile_id),
            "cleared_fup_subscription_ids": [
                str(item) for item in outcome.cleared_fup_subscription_ids
            ],
            "replayed": outcome.replayed,
        },
        200,
    )


def _scheduled_plan_change_context(
    db: Session,
    subscription_id: str,
) -> dict[str, object] | None:
    """Summarize the outstanding scheduled (next-cycle) plan change, if any."""
    from app.models.catalog import CatalogOffer
    from app.services.subscription_changes import subscription_change_requests

    scheduled = subscription_change_requests.get_scheduled_for_subscription(
        db, subscription_id
    )
    if scheduled is None:
        return None
    target_offer = db.get(CatalogOffer, scheduled.requested_offer_id)
    return {
        "id": str(scheduled.id),
        "offer_name": target_offer.name if target_offer else "New plan",
        "effective_date": scheduled.effective_date,
    }


def _scheduled_status_change_context(
    db: Session,
    subscription_id: str,
) -> list[dict[str, object]]:

    from app.models.subscription_lifecycle_schedule import (
        SubscriptionLifecycleSchedule,
        SubscriptionLifecycleScheduleStatus,
    )
    from app.services.common import coerce_uuid

    schedules = db.scalars(
        select(SubscriptionLifecycleSchedule)
        .where(
            SubscriptionLifecycleSchedule.subscription_id
            == coerce_uuid(subscription_id)
        )
        .where(
            SubscriptionLifecycleSchedule.status.in_(
                {
                    SubscriptionLifecycleScheduleStatus.pending,
                    SubscriptionLifecycleScheduleStatus.processing,
                }
            )
        )
        .order_by(SubscriptionLifecycleSchedule.effective_at.asc())
    ).all()
    return [
        {
            "id": str(schedule.id),
            "kind": schedule.command_kind,
            "status": schedule.status.value,
            "effective_at": schedule.effective_at,
            "reason": schedule.reason,
            "cancelable": (
                schedule.status == SubscriptionLifecycleScheduleStatus.pending
            ),
        }
        for schedule in schedules
    ]


def _serialize_lifecycle_state(
    state: SubscriptionLifecycleState,
) -> dict[str, object]:
    return {
        "status": state.status,
        "offer_id": state.offer_id,
        "offer_name": state.offer_name,
        "billing_mode": state.billing_mode,
        "billing_collectible": state.billing_collectible,
        "mrr_countable": state.mrr_countable,
        "radius_access_state": state.radius_access_state,
        "radius_allowed": state.radius_allowed,
        "radius_blocked": state.radius_blocked,
        "access_block_reason": state.access_block_reason,
        "terminal": state.terminal,
    }


def _json_value(value: object) -> object:
    return jsonable_encoder(value, custom_encoder={Decimal: str})


def customer_detail_url_for_subscriber_id(db: Session, subscriber_id: str) -> str:
    """Return the admin customer services URL for a subscriber."""
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
    if subscriber.category == SubscriberCategory.business:
        return f"/admin/customers/business/{subscriber.id}#subscriptions"
    return f"/admin/customers/person/{subscriber.id}#subscriptions"


def _selected_ipv4_values_from_form(form: FormData) -> tuple[list[str], list[str]]:
    block_ids = [
        str(value).strip()
        for value in form.getlist("ipv4_block_ids")
        if str(value).strip()
    ][:1]
    addresses = [
        str(value).strip()
        for value in form.getlist("ipv4_addresses")
        if str(value).strip()
    ][:1]
    return block_ids, addresses


def _selected_additional_route_values_from_form(
    form: FormData,
) -> tuple[list[str], list[str]]:
    cidrs = [str(value).strip() for value in form.getlist("additional_route_cidrs")]
    metrics = [str(value).strip() for value in form.getlist("additional_route_metrics")]
    return cidrs, metrics


def handle_subscription_create_form(
    db: Session,
    *,
    form: FormData,
    request: object,
    actor_id: str | None,
) -> dict[str, object]:
    """Validate and create a subscription from the admin form."""
    subscription = core.parse_subscription_form(form)
    error = core.resolve_account_id(db, subscription)
    if not error:
        error = core.validate_subscription_form(subscription, for_create=True)
    if not error:
        try:
            block_ids, addresses = _selected_ipv4_values_from_form(form)
            if block_ids or addresses:
                core.ensure_ipv4_blocks_allocatable(db, block_ids, addresses)
            route_cidrs, route_metrics = _selected_additional_route_values_from_form(
                form
            )
            core.normalize_additional_routes(route_cidrs, route_metrics)
            core.validate_additional_route_billing(
                db, cidrs=route_cidrs, metrics=route_metrics
            )
            core.validate_public_ip_addon_selection(
                db,
                add_on_id=str(form.get("ip_addon_id") or ""),
                quantity=str(form.get("ip_addon_quantity") or "1"),
            )
        except Exception as exc:
            error = core.error_message(exc)
    if error:
        return {
            "form_context": core.subscription_form_context(db, subscription, error),
        }

    subscriber_id = str(
        subscription.get("subscriber_id") or subscription.get("account_id") or ""
    )
    try:
        created = core.create_subscription_with_audit(
            db,
            core.build_payload_data(subscription),
            form,
            request,
            actor_id,
        )
        lifecycle_error = _apply_requested_create_lifecycle(
            db,
            created=created,
            requested_status=SubscriptionStatus(
                str(subscription.get("requested_status") or "pending")
            ),
            actor_id=actor_id,
        )
        if lifecycle_error:
            return {
                "redirect_url": (
                    f"/admin/catalog/subscriptions/{created.id}"
                    f"?error={quote_plus(lifecycle_error)}"
                )
            }
        redirect_url = (
            customer_detail_url_for_subscriber_id(db, subscriber_id)
            if subscriber_id
            else "/admin/catalog/subscriptions"
        )
        return {"redirect_url": redirect_url}
    except ValidationError as exc:
        db.rollback()
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        db.rollback()
        error = core.error_message(exc)

    return {
        "form_context": core.subscription_form_context(
            db,
            subscription,
            error or "Please correct the highlighted fields.",
        ),
    }


def handle_subscription_update_form(
    db: Session,
    *,
    subscription_id: str,
    form: FormData,
    request: object,
    actor_id: str | None,
) -> dict[str, object]:
    """Validate and update a subscription from the admin form."""
    subscription = core.parse_subscription_form(form, subscription_id=subscription_id)
    current = catalog_service.subscriptions.get(db, subscription_id)
    error = None
    if str(subscription.get("offer_id") or "") != str(current.offer_id):
        error = (
            "Use Change Plan from the subscription detail page so the owner "
            "preview, confirmation, audit, and exact result are preserved."
        )
    if not error and str(subscription.get("provisioning_nas_device_id") or "") != str(
        current.provisioning_nas_device_id or ""
    ):
        error = (
            "Use Move service access to change the router and primary IPv4 "
            "together. That reviewed action updates IPAM and RADIUS without "
            "entering billing."
        )
    if not error:
        error = core.resolve_account_id(db, subscription)
    if not error:
        error = core.validate_subscription_form(subscription, for_create=False)
    if not error:
        try:
            route_cidrs, route_metrics = _selected_additional_route_values_from_form(
                form
            )
            core.normalize_additional_routes(route_cidrs, route_metrics)
            core.validate_additional_route_billing(
                db, cidrs=route_cidrs, metrics=route_metrics
            )
            core.validate_public_ip_addon_selection(
                db,
                add_on_id=str(form.get("ip_addon_id") or ""),
                quantity=str(form.get("ip_addon_quantity") or "1"),
            )
        except Exception as exc:
            error = core.error_message(exc)
    if error:
        context = core.subscription_form_context(db, subscription, error)
        context["action_url"] = f"/admin/catalog/subscriptions/{subscription_id}/edit"
        return {"form_context": context}

    try:
        block_ids, addresses = _selected_ipv4_values_from_form(form)
        selected_ipv4 = addresses[0] if addresses else None
        current_ipv4 = core.active_service_ipv4_address(db, subscription_id)
        if selected_ipv4 and selected_ipv4 != current_ipv4:
            error = (
                "Use Replace service IPv4 in the IPv4 Allocation section. "
                "That action changes IPAM and RADIUS without entering billing."
            )
            context = core.subscription_form_context(db, subscription, error)
            context["action_url"] = (
                f"/admin/catalog/subscriptions/{subscription_id}/edit"
            )
            return {"form_context": context}
        updated = core.update_subscription_with_audit(
            db,
            subscription_id,
            core.build_payload_data(subscription),
            str(subscription.get("service_password") or ""),
            [],
            [],
            request,
            actor_id,
            additional_route_cidrs=route_cidrs,
            additional_route_metrics=route_metrics,
            ip_addon_id=str(form.get("ip_addon_id") or ""),
            ip_addon_quantity=str(form.get("ip_addon_quantity") or "1"),
            ipv4_assignment_submitted=False,
        )
        subscriber_id = getattr(updated, "subscriber_id", None)
        redirect_url = (
            customer_detail_url_for_subscriber_id(db, str(subscriber_id))
            if subscriber_id
            else "/admin/catalog/subscriptions"
        )
        return {"redirect_url": redirect_url}
    except ValidationError as exc:
        db.rollback()
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        db.rollback()
        error = core.error_message(exc)

    context = core.subscription_form_context(
        db,
        subscription,
        error or "Please correct the highlighted fields.",
    )
    context["action_url"] = f"/admin/catalog/subscriptions/{subscription_id}/edit"
    return {"form_context": context}


def handle_subscription_ipv4_replacement(
    db: Session,
    *,
    subscription_id: str,
    form: FormData,
    actor_id: str | None,
) -> str:
    """Apply the dedicated, non-commercial service IPv4 replacement action."""

    block_ids, addresses = _selected_ipv4_values_from_form(form)
    base_url = f"/admin/catalog/subscriptions/{subscription_id}/edit"
    if len(block_ids) != 1 or len(addresses) != 1:
        message = quote_plus("Select one IPv4 block and address to replace.")
        return f"{base_url}?error={message}"
    try:
        replaced_ip = core.replace_subscription_ipv4_with_owner(
            db,
            subscription_id=subscription_id,
            selector=block_ids[0],
            requested_ip=addresses[0],
            actor_id=actor_id,
        )
        notice = quote_plus(
            f"Service IPv4 replaced with {replaced_ip}; billing unchanged."
        )
        return f"{base_url}?notice={notice}"
    except Exception as exc:
        db.rollback()
        return f"{base_url}?error={quote_plus(core.error_message(exc))}"


def send_subscription_credentials_redirect(
    db: Session,
    *,
    subscription_id: str,
) -> str:
    """Send service credentials and return the edit-page redirect URL."""
    try:
        result = core.send_subscription_credentials(
            db,
            subscription_id=subscription_id,
        )
        notice = (
            f"Sent credentials to {result['email_sent']} email target(s) "
            f"and {result['sms_sent']} SMS target(s)."
        )
        query = f"notice={quote_plus(notice)}"
    except Exception as exc:
        logger.error(
            "Failed to send credentials for subscription %s: %s",
            subscription_id,
            exc,
        )
        query = f"error={quote_plus(str(exc))}"
    return f"/admin/catalog/subscriptions/{subscription_id}/edit?{query}"


def admin_resume_vacation_hold_redirect(
    db: Session,
    *,
    subscription_id: str,
    actor_id: str | None,
) -> str:
    """Admin action to resume a customer vacation hold and return redirect URL."""
    from app.models.audit import AuditActorType
    from app.models.enforcement_lock import EnforcementLock
    from app.services.subscription_lifecycle import (
        SubscriptionCommandKind,
        SubscriptionEffectiveTiming,
        SubscriptionLifecycleCommand,
        resolve_subscription_lifecycle,
        resolve_vacation_hold_policy,
    )
    from app.services.subscription_lifecycle_commands import (
        execute_subscription_command,
    )

    admin_ref = f"admin:{actor_id or 'unknown'}"
    try:
        subscription = db.get(Subscription, coerce_uuid(subscription_id))
        if subscription is None:
            raise ValueError("Subscription not found")
        decision = resolve_vacation_hold_policy(
            db,
            subscription,
            command_kind=SubscriptionCommandKind.vacation_resume,
        )
        if not decision.eligible or decision.active_lock_id is None:
            raise ValueError("No active vacation hold exists")
        lock = db.get(EnforcementLock, coerce_uuid(decision.active_lock_id))
        if lock is None:
            raise ValueError("Vacation-hold evidence is missing")
        snapshot = resolve_subscription_lifecycle(db, subscription_id)
        outcome = execute_subscription_command(
            db,
            SubscriptionLifecycleCommand(
                subscription_id=subscription_id,
                kind=SubscriptionCommandKind.vacation_resume,
                source=admin_ref,
                effective_timing=SubscriptionEffectiveTiming.immediate,
                reason="Administrator resumed customer vacation hold",
                expected_head=snapshot.head,
                idempotency_key=f"admin-vacation-resume:{lock.id}",
            ),
            actor_id=actor_id,
            actor_type=AuditActorType.user,
        )
        if outcome.status.value not in {"applied", "skipped"}:
            raise ValueError(outcome.message)
        refreshed = db.get(Subscription, coerce_uuid(subscription_id))
        if refreshed is not None and refreshed.status == SubscriptionStatus.active:
            notice = "Vacation hold has been cleared. Service resumed successfully."
        else:
            # Never claim success the owner did not give us. It declines for real
            # reasons — another active lock it cannot clear, or an active login on
            # the same login name.
            notice = (
                "Vacation hold could not be resumed. The service may still be held "
                "by another enforcement lock — check the subscription's locks."
            )
        query = f"notice={quote_plus(notice)}"
    except Exception as exc:
        logger.error(
            "Failed to resume vacation hold for subscription %s: %s",
            subscription_id,
            exc,
            exc_info=True,
        )
        query = f"error={quote_plus(str(exc))}"
    return f"/admin/catalog/subscriptions/{subscription_id}?{query}"


def preview_bulk_lifecycle_response(
    db: Session,
    *,
    subscription_ids: str,
    kind: SubscriptionCommandKind,
    actor_id: str | None,
    target_offer_id: str | None = None,
    effective_timing: SubscriptionEffectiveTiming = (
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = None,
    reason: str | None = None,
) -> tuple[dict[str, object], int]:
    """Preview a canonical subscription batch for admin confirmation."""
    try:
        preview = preview_subscription_batch(
            db,
            subscription_ids,
            kind=kind,
            source=f"admin:catalog:{actor_id or 'system'}",
            target_offer_id=target_offer_id,
            effective_timing=effective_timing,
            effective_at=effective_at,
            reason=reason,
        )
    except SubscriptionLifecycleBatchError as exc:
        return {"status": "rejected", "message": str(exc)}, 422
    return _batch_preview_payload(preview), 200


def execute_bulk_lifecycle_response(
    db: Session,
    *,
    subscription_ids: str,
    kind: SubscriptionCommandKind,
    actor_id: str | None,
    target_offer_id: str | None = None,
    effective_timing: SubscriptionEffectiveTiming = (
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = None,
    reason: str | None = None,
    reviewed_heads: str | Mapping[str, str] | None = None,
    idempotency_key: str | None = None,
    require_reviewed_heads: bool = True,
) -> tuple[dict[str, object], int]:
    """Execute a reviewed batch and preserve every per-item outcome."""
    try:
        heads = _parse_reviewed_heads(reviewed_heads)
        if require_reviewed_heads and not idempotency_key:
            raise SubscriptionLifecycleBatchError(
                "An Idempotency-Key is required for a reviewed lifecycle batch"
            )
        outcome = execute_subscription_batch(
            db,
            subscription_ids,
            kind=kind,
            source=f"admin:catalog:{actor_id or 'system'}",
            actor_id=actor_id,
            target_offer_id=target_offer_id,
            effective_timing=effective_timing,
            effective_at=effective_at,
            reason=reason,
            reviewed_heads=heads,
            idempotency_key=idempotency_key,
            require_reviewed_heads=require_reviewed_heads,
        )
    except SubscriptionLifecycleBatchError as exc:
        return {"status": "rejected", "message": str(exc)}, 422
    return _batch_outcome_payload(outcome), 200


def bulk_activate_response(
    db: Session,
    *,
    subscription_ids: str,
    request: object,
    actor_id: str | None,
    effective_timing: SubscriptionEffectiveTiming = (
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = None,
    reason: str | None = None,
    reviewed_heads: str | Mapping[str, str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """Compatibility route backed by the canonical batch executor."""
    del request
    payload, _ = execute_bulk_lifecycle_response(
        db,
        subscription_ids=subscription_ids,
        kind=SubscriptionCommandKind.activate,
        actor_id=actor_id,
        effective_timing=effective_timing,
        effective_at=effective_at,
        reason=reason,
        reviewed_heads=reviewed_heads,
        idempotency_key=idempotency_key,
        require_reviewed_heads=reviewed_heads is not None,
    )
    return payload


def bulk_suspend_response(
    db: Session,
    *,
    subscription_ids: str,
    request: object,
    actor_id: str | None,
    effective_timing: SubscriptionEffectiveTiming = (
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = None,
    reason: str | None = None,
    reviewed_heads: str | Mapping[str, str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """Compatibility route backed by the canonical batch executor."""
    del request
    payload, _ = execute_bulk_lifecycle_response(
        db,
        subscription_ids=subscription_ids,
        kind=SubscriptionCommandKind.suspend,
        actor_id=actor_id,
        effective_timing=effective_timing,
        effective_at=effective_at,
        reason=reason,
        reviewed_heads=reviewed_heads,
        idempotency_key=idempotency_key,
        require_reviewed_heads=reviewed_heads is not None,
    )
    return payload


def bulk_restore_response(
    db: Session,
    *,
    subscription_ids: str,
    actor_id: str | None,
    effective_timing: SubscriptionEffectiveTiming = (
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = None,
    reason: str | None = None,
    reviewed_heads: str | Mapping[str, str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """Restore subscriptions through the canonical batch executor."""
    payload, _ = execute_bulk_lifecycle_response(
        db,
        subscription_ids=subscription_ids,
        kind=SubscriptionCommandKind.restore,
        actor_id=actor_id,
        effective_timing=effective_timing,
        effective_at=effective_at,
        reason=reason,
        reviewed_heads=reviewed_heads,
        idempotency_key=idempotency_key,
        require_reviewed_heads=reviewed_heads is not None,
    )
    return payload


def bulk_cancel_response(
    db: Session,
    *,
    subscription_ids: str,
    request: object,
    actor_id: str | None,
    effective_timing: SubscriptionEffectiveTiming = (
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = None,
    reason: str | None = None,
    reviewed_heads: str | Mapping[str, str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """Compatibility route backed by the canonical batch executor."""
    del request
    payload, _ = execute_bulk_lifecycle_response(
        db,
        subscription_ids=subscription_ids,
        kind=SubscriptionCommandKind.cancel,
        actor_id=actor_id,
        effective_timing=effective_timing,
        effective_at=effective_at,
        reason=reason,
        reviewed_heads=reviewed_heads,
        idempotency_key=idempotency_key,
        require_reviewed_heads=reviewed_heads is not None,
    )
    return payload


def bulk_change_plan_response(
    db: Session,
    *,
    subscription_ids: str,
    target_offer_id: str,
    request: object,
    actor_id: str | None,
    effective_timing: SubscriptionEffectiveTiming = (
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = None,
    reason: str | None = None,
    reviewed_heads: str | Mapping[str, str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """Compatibility route backed by the canonical batch executor."""
    del request
    payload, _ = execute_bulk_lifecycle_response(
        db,
        subscription_ids=subscription_ids,
        kind=SubscriptionCommandKind.change_plan,
        target_offer_id=target_offer_id,
        actor_id=actor_id,
        effective_timing=effective_timing,
        effective_at=effective_at,
        reason=reason,
        reviewed_heads=reviewed_heads,
        idempotency_key=idempotency_key,
        require_reviewed_heads=reviewed_heads is not None,
    )
    return payload


def _batch_preview_payload(preview: SubscriptionBatchPreview) -> dict[str, object]:
    billing_actions: Counter[str] = Counter()
    access_actions: Counter[str] = Counter()
    net_amounts: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for item in preview.items:
        if not item.eligible:
            continue
        if item.billing_impact is not None:
            billing_actions[item.billing_impact.action] += 1
            currency = item.billing_impact.currency or "N/A"
            net_amounts[currency] += item.billing_impact.net_amount
        if item.access_impact is not None:
            access_actions[item.access_impact.session_action.value] += 1
    return {
        "status": "previewed",
        "kind": preview.kind.value,
        "total": preview.total,
        "eligible_count": preview.eligible_count,
        "ineligible_count": preview.ineligible_count,
        "reviewed_heads": preview.reviewed_heads,
        "billing_impact": {
            "actions": dict(billing_actions),
            "net_amounts": _json_value(dict(net_amounts)),
        },
        "access_impact": {"session_actions": dict(access_actions)},
        "items": _json_value(preview.items),
    }


def _batch_outcome_payload(outcome: SubscriptionBatchOutcome) -> dict[str, object]:
    counts = {
        status.value: outcome.count(status)
        for status in SubscriptionCommandOutcomeStatus
    }
    rejected_statuses = {
        SubscriptionCommandOutcomeStatus.rejected,
        SubscriptionCommandOutcomeStatus.superseded,
    }
    changed = counts["applied"] + counts["scheduled"]
    return {
        "status": outcome.status,
        "kind": outcome.kind.value,
        "message": f"{outcome.succeeded} of {outcome.total} subscriptions succeeded",
        "total": outcome.total,
        "succeeded": outcome.succeeded,
        "counts": counts,
        "items": _json_value(outcome.items),
        "count": changed,
        "changed": changed,
        "skipped_ids": [
            item.subscription_id
            for item in outcome.items
            if item.status in rejected_statuses
        ],
        "failed_ids": [
            item.subscription_id
            for item in outcome.items
            if item.status == SubscriptionCommandOutcomeStatus.failed
        ],
    }


def _parse_reviewed_heads(
    reviewed_heads: str | Mapping[str, str] | None,
) -> dict[str, str]:
    if reviewed_heads is None:
        return {}
    if isinstance(reviewed_heads, str):
        try:
            parsed = json.loads(reviewed_heads)
        except json.JSONDecodeError as exc:
            raise SubscriptionLifecycleBatchError(
                "reviewed_heads must be a JSON object"
            ) from exc
    else:
        parsed = reviewed_heads
    if not isinstance(parsed, Mapping):
        raise SubscriptionLifecycleBatchError("reviewed_heads must be a JSON object")
    return {
        str(subscription_id): str(head)
        for subscription_id, head in parsed.items()
        if str(subscription_id).strip() and str(head).strip()
    }


def cancel_scheduled_plan_change_redirect(
    db: Session,
    *,
    subscription_id: str,
    request_id: str,
    actor_id: str | None,
) -> str:
    """Cancel a scheduled next-cycle plan change; return a redirect URL."""
    from app.services.audit_adapter import record_audit_event
    from app.services.subscription_changes import subscription_change_requests

    base = f"/admin/catalog/subscriptions/{subscription_id}"
    try:
        subscription_change_requests.cancel_scheduled(
            db,
            request_id=request_id,
            notes="Canceled via admin subscription detail",
        )
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return f"{base}?error={quote_plus(str(detail))}"
    record_audit_event(
        db,
        action="cancel_scheduled_plan_change",
        entity_type="subscription",
        entity_id=subscription_id,
        actor_id=actor_id,
        metadata={"change_request_id": request_id},
    )
    return f"{base}?notice={quote_plus('Scheduled plan change canceled.')}"


def change_plan_quote_response(
    db: Session,
    *,
    subscription_id: str,
    target_offer_id: str,
) -> dict[str, object]:
    """Proration quote for an admin change-plan preview.

    Reuses the customer-portal quote builder so the admin modal shows the same
    credit/charge/net numbers the change will actually produce.
    """
    from fastapi import HTTPException

    from app.models.catalog import CatalogOffer
    from app.services.common import coerce_uuid
    from app.services.customer_portal_flow_changes import (
        _build_plan_change_quote,
        _serialize_plan_change_quote,
    )

    subscription = catalog_service.subscriptions.get(db, subscription_id)
    target = db.get(CatalogOffer, coerce_uuid(target_offer_id))
    if not target:
        raise HTTPException(status_code=404, detail="Target offer not found")
    quote = _build_plan_change_quote(db, subscription, target)
    return {
        "quote": _serialize_plan_change_quote(quote),
        "target_offer_name": target.name,
    }
