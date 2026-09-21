"""Customer-facing reboot and Wi-Fi command outcomes.

Reboot retains its tracked-operation path. Wi-Fi delegates authenticated scope,
desired state, revision, and durable dispatch to the ONT service-configuration
owner. This adapter presents their lifecycle through one typed customer outcome.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app import metrics
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.network import OntAssignment, OntUnit
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.ont_service_configuration import OntServiceConfigurationPhase
from app.services.domain_errors import DomainError
from app.services.network.ont_actions import OntActions
from app.services.network.ont_service_configuration import (
    ConfigureCustomerWifiCommand,
    CustomerWifiConfigurationChange,
    OntConfigurationSection,
    configure_customer_wifi,
    get_latest_ont_configuration_section_delivery,
)
from app.services.network_operations import commit_tracked_action, run_tracked_action
from app.services.owner_commands import CommandContext
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


class CustomerDeviceCommandKind(StrEnum):
    reboot = "reboot"
    wifi_update = "wifi_update"


class CustomerDeviceCommandStatus(StrEnum):
    queued = "queued"
    succeeded = "succeeded"
    waiting = "waiting"
    needs_verification = "needs_verification"
    failed = "failed"


@dataclass(frozen=True)
class CustomerDeviceCommandOutcome:
    command: CustomerDeviceCommandKind
    status: CustomerDeviceCommandStatus
    subscription_id: UUID
    device_id: UUID | None
    operation_id: UUID | None
    message: str

    @property
    def success(self) -> bool:
        return self.status in {
            CustomerDeviceCommandStatus.queued,
            CustomerDeviceCommandStatus.succeeded,
            CustomerDeviceCommandStatus.needs_verification,
        }


class CustomerDeviceCommandError(ValueError):
    """Stable, transport-neutral customer command rejection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details: dict[str, object] = dict(details or {})


def record_device_command_refusal(
    *,
    kind: CustomerDeviceCommandKind,
    code: str,
    correlation_id: str | None,
    details: Mapping[str, object] | None = None,
) -> None:
    """Durably observe a customer device command refusal.

    The caller MUST invoke this after the refusing call has already returned
    control past any owner-command transaction (``execute_owner_command``
    rolls its whole transaction back on any exception, which would erase
    anything written from inside it) -- never from inside a
    ``configure_customer_wifi``/``execute_owner_command`` operation callback.
    This performs only an in-process structured log line and a Prometheus
    counter increment; a persisted, queryable refusal record is later work.
    """

    safe_details = dict(details or {})
    logger.warning(
        "customer_device_command_refused",
        extra={
            "event": "customer_device_command_refused",
            "command": kind.value,
            "code": code,
            "correlation_id": correlation_id,
            "details": safe_details,
        },
    )
    metrics.record_customer_device_command_refusal(command=kind.value, code=code)


def _assigned_ont(
    db: Session, *, subscriber_id: UUID, subscription_id: UUID
) -> tuple[Subscription, OntUnit]:
    subscription = db.get(Subscription, subscription_id)
    if subscription is None or subscription.subscriber_id != subscriber_id:
        raise CustomerDeviceCommandError("subscription_not_found", "Service not found")
    if subscription.status != SubscriptionStatus.active:
        raise CustomerDeviceCommandError(
            "subscription_inactive", "Only active services support device commands"
        )
    assignments = (
        db.query(OntAssignment)
        .filter(
            OntAssignment.subscription_id == subscription.id,
            OntAssignment.subscriber_id == subscriber_id,
            OntAssignment.active.is_(True),
        )
        .order_by(OntAssignment.id.asc())
        .limit(2)
        .all()
    )
    if not assignments:
        raise CustomerDeviceCommandError(
            "device_not_assigned", "No active device is linked to this service"
        )
    if len(assignments) > 1:
        raise CustomerDeviceCommandError(
            "device_assignment_ambiguous",
            "Multiple active devices are linked to this service; contact support",
        )
    assignment = assignments[0]
    if assignment.ont_unit is None:
        raise CustomerDeviceCommandError(
            "device_not_assigned", "No active device is linked to this service"
        )
    if assignment.ont_unit.uisp_device_id:
        raise CustomerDeviceCommandError(
            "device_command_unsupported",
            "Self-service device commands are not supported for this device",
        )
    return subscription, assignment.ont_unit


def _outcome(
    *,
    command: CustomerDeviceCommandKind,
    subscription_id: UUID,
    device_id: UUID,
    result: object,
) -> CustomerDeviceCommandOutcome:
    data = getattr(result, "data", None) or {}
    raw_operation_id = data.get("operation_id")
    return CustomerDeviceCommandOutcome(
        command=command,
        status=(
            CustomerDeviceCommandStatus.waiting
            if bool(getattr(result, "waiting", False))
            else CustomerDeviceCommandStatus.succeeded
            if bool(getattr(result, "success", False))
            else CustomerDeviceCommandStatus.failed
        ),
        subscription_id=subscription_id,
        device_id=device_id,
        operation_id=UUID(str(raw_operation_id)) if raw_operation_id else None,
        message=str(getattr(result, "message", "Device command submitted")),
    )


def reboot_subscription_device(
    db: Session,
    *,
    subscriber_id: UUID,
    subscription_id: UUID,
    actor_id: str,
) -> CustomerDeviceCommandOutcome:
    """Reboot the exact non-UISP ONT currently assigned to the subscription."""
    _subscription, ont = _assigned_ont(
        db, subscriber_id=subscriber_id, subscription_id=subscription_id
    )
    cooldown_value = resolve_value(
        db, SettingDomain.network, "customer_ont_reboot_cooldown_seconds"
    )
    try:
        cooldown_seconds = (
            int(str(cooldown_value)) if cooldown_value is not None else 300
        )
    except (TypeError, ValueError):
        cooldown_seconds = 300
    if cooldown_seconds > 0:
        latest = (
            db.query(NetworkOperation.created_at)
            .filter(
                NetworkOperation.operation_type == NetworkOperationType.ont_reboot,
                NetworkOperation.target_type == NetworkOperationTargetType.ont,
                NetworkOperation.target_id == ont.id,
                NetworkOperation.status.notin_(
                    [NetworkOperationStatus.failed, NetworkOperationStatus.canceled]
                ),
            )
            .order_by(NetworkOperation.created_at.desc())
            .first()
        )
        if latest and latest.created_at:
            created_at = latest.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            remaining = int(
                cooldown_seconds - (datetime.now(UTC) - created_at).total_seconds()
            )
            if remaining > 0:
                minutes = max(1, -(-remaining // 60))
                raise CustomerDeviceCommandError(
                    "reboot_cooldown",
                    "Your device was restarted recently. Please wait about "
                    f"{minutes} minute{'s' if minutes != 1 else ''} before trying again.",
                )
    result = run_tracked_action(
        db,
        NetworkOperationType.ont_reboot,
        NetworkOperationTargetType.ont,
        str(ont.id),
        lambda: OntActions.reboot(db, str(ont.id)),
        correlation_key=f"customer:{subscription_id}:reboot:{ont.id}",
        initiated_by=f"customer:{actor_id}",
    )
    outcome = _outcome(
        command=CustomerDeviceCommandKind.reboot,
        subscription_id=subscription_id,
        device_id=ont.id,
        result=result,
    )
    commit_tracked_action(db)
    return outcome


# Maps the ONT service-configuration owner's distinct admission-scope domain
# codes (see ``_load_customer_wifi_admission_scope``) onto this adapter's
# stable customer-command codes. Deliberately coarser than the domain layer:
# a customer does not need to know an ONT row disagreed with its assignment
# vs. was simply missing, but a support agent reading the recorded refusal
# (``record_device_command_refusal``) sees the original ``domain_code`` and
# ``domain_details`` untouched.
_WIFI_ADMISSION_DOMAIN_CODES: dict[str, str] = {
    "customer_active_assignment_required": "device_not_assigned",
    "customer_ambiguous_assignment": "device_assignment_ambiguous",
    "customer_assigned_ont_missing": "device_not_assigned",
    "customer_assignment_inconsistent": "device_assignment_ambiguous",
    "customer_subscription_not_found": "subscription_not_found",
    "customer_device_unsupported": "device_command_unsupported",
}


def update_subscription_wifi(
    db: Session,
    *,
    subscriber_id: UUID,
    subscription_id: UUID,
    context: CommandContext,
    ssid: str,
    password: str | None,
) -> CustomerDeviceCommandOutcome:
    """Save and durably queue Wi-Fi state for the exact customer service."""
    ssid_value = ssid.strip()
    password_value = password.strip() if password else None
    if not 1 <= len(ssid_value) <= 32:
        raise CustomerDeviceCommandError(
            "invalid_wifi_name", "WiFi name must be 1-32 characters"
        )
    if password_value is not None and not 8 <= len(password_value) <= 63:
        raise CustomerDeviceCommandError(
            "invalid_wifi_password", "WiFi password must be 8-63 characters"
        )
    try:
        queued = configure_customer_wifi(
            db,
            ConfigureCustomerWifiCommand(
                context=context,
                subscriber_id=subscriber_id,
                subscription_id=subscription_id,
                change=CustomerWifiConfigurationChange(
                    ssid=ssid_value,
                    password=password_value,
                ),
            ),
        )
    except DomainError as exc:
        suffix = exc.code.rsplit(".", 1)[-1]
        code = _WIFI_ADMISSION_DOMAIN_CODES.get(suffix, "wifi_update_rejected")
        raise CustomerDeviceCommandError(
            code,
            exc.message,
            details={"domain_code": exc.code, "domain_details": exc.details},
        ) from exc
    return CustomerDeviceCommandOutcome(
        command=CustomerDeviceCommandKind.wifi_update,
        status=CustomerDeviceCommandStatus.queued,
        subscription_id=subscription_id,
        device_id=queued.ont_unit_id,
        operation_id=queued.operation_id,
        message="WiFi update queued. We will apply it in the background.",
    )


def get_subscription_wifi_status(
    db: Session,
    *,
    subscriber_id: UUID,
    subscription_id: UUID,
) -> CustomerDeviceCommandOutcome:
    """Return the latest customer-visible WiFi delivery status."""

    _subscription, ont = _assigned_ont(
        db, subscriber_id=subscriber_id, subscription_id=subscription_id
    )
    projection = get_latest_ont_configuration_section_delivery(
        db,
        ont_unit_id=ont.id,
        section=OntConfigurationSection.wifi,
    )
    if projection is None:
        raise CustomerDeviceCommandError(
            "wifi_operation_not_found", "No WiFi update has been submitted."
        )
    status_by_phase = {
        OntServiceConfigurationPhase.saved: CustomerDeviceCommandStatus.queued,
        OntServiceConfigurationPhase.queued: CustomerDeviceCommandStatus.queued,
        OntServiceConfigurationPhase.applying: CustomerDeviceCommandStatus.waiting,
        OntServiceConfigurationPhase.readback_pending: (
            CustomerDeviceCommandStatus.waiting
        ),
        OntServiceConfigurationPhase.delivered_unverified: (
            CustomerDeviceCommandStatus.needs_verification
        ),
        OntServiceConfigurationPhase.verified: CustomerDeviceCommandStatus.succeeded,
        OntServiceConfigurationPhase.failed: CustomerDeviceCommandStatus.failed,
        OntServiceConfigurationPhase.superseded: CustomerDeviceCommandStatus.failed,
        OntServiceConfigurationPhase.retired: CustomerDeviceCommandStatus.failed,
    }
    message_by_phase = {
        OntServiceConfigurationPhase.saved: "WiFi update saved.",
        OntServiceConfigurationPhase.queued: "WiFi update queued.",
        OntServiceConfigurationPhase.applying: "WiFi update is being applied.",
        OntServiceConfigurationPhase.readback_pending: (
            "WiFi update applied; waiting for the device to confirm it."
        ),
        OntServiceConfigurationPhase.delivered_unverified: (
            "WiFi update delivered; exact device readback is unavailable."
        ),
        OntServiceConfigurationPhase.verified: "WiFi update completed.",
        OntServiceConfigurationPhase.failed: (
            projection.failure_message or "WiFi update failed."
        ),
        OntServiceConfigurationPhase.superseded: (
            "WiFi update was replaced by a newer configuration."
        ),
        OntServiceConfigurationPhase.retired: "WiFi update is no longer current.",
    }
    return CustomerDeviceCommandOutcome(
        command=CustomerDeviceCommandKind.wifi_update,
        status=status_by_phase[projection.phase],
        subscription_id=subscription_id,
        device_id=ont.id,
        operation_id=projection.operation_id,
        message=message_by_phase[projection.phase],
    )
