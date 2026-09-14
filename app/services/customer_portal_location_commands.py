"""Owned transaction boundary for customer-portal location mutations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.location_capture_prompt import LocationCapturePromptState
from app.models.subscriber import Subscriber
from app.schemas.subscriber import CustomerServiceLocationUpdate
from app.services import location_capture
from app.services import subscriber as subscriber_service
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.geocode_reconciler import FieldKey
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

PORTAL_LOCATION_WRITE_SCOPE = "customer:service-location:write"

_UPDATE_COMMAND = OwnerCommandDefinition(
    owner="customer.portal_location_commands",
    concern="customer portal service-address and pin update coordination",
    name="update_service_location",
)
_CONFIRM_COMMAND = OwnerCommandDefinition(
    owner="customer.portal_location_commands",
    concern="customer portal location confirmation coordination",
    name="confirm_location",
)
_SNOOZE_COMMAND = OwnerCommandDefinition(
    owner="customer.portal_location_commands",
    concern="customer portal location-prompt snooze coordination",
    name="snooze_location_prompt",
)


class PortalLocationCommandError(DomainError):
    """Stable customer-portal location command failure."""


def _error(suffix: str, message: str, **details: object) -> PortalLocationCommandError:
    return PortalLocationCommandError(
        code=f"customer.portal_location_commands.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class UpdateServiceLocationCommand:
    context: CommandContext
    location: CustomerServiceLocationUpdate


@dataclass(frozen=True, slots=True)
class UpdateServiceLocationOutcome:
    subscriber_id: UUID
    address_id: UUID
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class ConfirmLocationCommand:
    context: CommandContext
    subscriber_id: UUID
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    claimed_state: str | None = None
    claimed_lga: str | None = None
    claimed_postcode: str | None = None
    actor_name: str | None = None


@dataclass(frozen=True, slots=True)
class ConfirmLocationOutcome:
    subscriber_id: UUID
    captured_keys: tuple[FieldKey, ...]
    needs_human_review: bool


@dataclass(frozen=True, slots=True)
class SnoozeLocationPromptCommand:
    context: CommandContext
    subscriber_id: UUID


@dataclass(frozen=True, slots=True)
class SnoozeLocationPromptOutcome:
    subscriber_id: UUID
    snoozed_until: datetime | None
    dismiss_count: int


def _validate_context(context: CommandContext) -> None:
    if context.scope != PORTAL_LOCATION_WRITE_SCOPE:
        raise _error(
            "invalid_scope", "Customer service-location write scope is required"
        )


def _lock_subscriber(db: Session, subscriber_id: UUID) -> Subscriber:
    subscriber = db.scalar(
        select(Subscriber).where(Subscriber.id == subscriber_id).with_for_update()
    )
    if subscriber is None:
        raise _error(
            "subscriber_not_found",
            "Subscriber not found",
            subscriber_id=str(subscriber_id),
        )
    return subscriber


def update_service_location(
    db: Session, command: UpdateServiceLocationCommand
) -> UpdateServiceLocationOutcome:
    """Atomically update the canonical service address and its map pin."""

    def operation() -> UpdateServiceLocationOutcome:
        _validate_context(command.context)
        _lock_subscriber(db, command.location.subscriber_id)
        address = subscriber_service.update_customer_service_location(
            db=db,
            payload=command.location,
        )
        emit_event(
            db,
            EventType.subscriber_service_location_updated,
            {
                "schema_version": 1,
                "subscriber_id": str(command.location.subscriber_id),
                "address_id": str(address.id),
                "latitude": command.location.latitude,
                "longitude": command.location.longitude,
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
                "reason": command.context.reason,
            },
            actor=command.context.actor,
            subscriber_id=command.location.subscriber_id,
            account_id=command.location.subscriber_id,
        )
        return UpdateServiceLocationOutcome(
            subscriber_id=command.location.subscriber_id,
            address_id=address.id,
            latitude=command.location.latitude,
            longitude=command.location.longitude,
        )

    return execute_owner_command(
        db,
        definition=_UPDATE_COMMAND,
        context=command.context,
        operation=operation,
    )


def confirm_location(
    db: Session, command: ConfirmLocationCommand
) -> ConfirmLocationOutcome:
    """Atomically reconcile a customer-confirmed pin and persist its evidence."""

    def operation() -> ConfirmLocationOutcome:
        _validate_context(command.context)
        _lock_subscriber(db, command.subscriber_id)
        try:
            result = location_capture.capture(
                db,
                str(command.subscriber_id),
                lat=command.latitude,
                lng=command.longitude,
                accuracy_m=command.accuracy_m,
                source=location_capture.SOURCE_CUSTOMER_PORTAL,
                actor_id=str(command.subscriber_id),
                actor_name=command.actor_name,
                claimed_state=command.claimed_state,
                claimed_lga=command.claimed_lga,
                claimed_postcode=command.claimed_postcode,
            )
        except location_capture.LocationCaptureDisabled as exc:
            raise _error("capture_disabled", str(exc)) from exc
        emit_event(
            db,
            EventType.subscriber_location_confirmed,
            {
                "schema_version": 1,
                "subscriber_id": str(command.subscriber_id),
                "captured_keys": [key.value for key in result.captured_keys],
                "needs_human_review": bool(result.reconciliation.needs_human),
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
                "reason": command.context.reason,
            },
            actor=command.context.actor,
            subscriber_id=command.subscriber_id,
            account_id=command.subscriber_id,
        )
        return ConfirmLocationOutcome(
            subscriber_id=command.subscriber_id,
            captured_keys=result.captured_keys,
            needs_human_review=bool(result.reconciliation.needs_human),
        )

    return execute_owner_command(
        db,
        definition=_CONFIRM_COMMAND,
        context=command.context,
        operation=operation,
    )


def snooze_location_prompt(
    db: Session, command: SnoozeLocationPromptCommand
) -> SnoozeLocationPromptOutcome:
    """Atomically record the customer's explicit location-prompt snooze."""

    def operation() -> SnoozeLocationPromptOutcome:
        _validate_context(command.context)
        _lock_subscriber(db, command.subscriber_id)
        try:
            state: LocationCapturePromptState = location_capture.snooze_prompt(
                db, str(command.subscriber_id)
            )
        except location_capture.LocationCaptureDisabled as exc:
            raise _error("capture_disabled", str(exc)) from exc
        emit_event(
            db,
            EventType.subscriber_location_prompt_snoozed,
            {
                "schema_version": 1,
                "subscriber_id": str(command.subscriber_id),
                "snoozed_until": (
                    state.snoozed_until.isoformat() if state.snoozed_until else None
                ),
                "dismiss_count": int(state.dismiss_count or 0),
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
                "reason": command.context.reason,
            },
            actor=command.context.actor,
            subscriber_id=command.subscriber_id,
            account_id=command.subscriber_id,
        )
        return SnoozeLocationPromptOutcome(
            subscriber_id=command.subscriber_id,
            snoozed_until=state.snoozed_until,
            dismiss_count=int(state.dismiss_count or 0),
        )

    return execute_owner_command(
        db,
        definition=_SNOOZE_COMMAND,
        context=command.context,
        operation=operation,
    )
