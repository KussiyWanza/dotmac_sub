"""Durable staff notification delivery through the Nextcloud Talk capability."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.integration_platform import (
    IntegrationCapabilityBinding,
    IntegrationInstallation,
)
from app.models.nextcloud_talk import (
    NextcloudTalkNotificationRoom,
    NextcloudTalkStaffAccount,
)
from app.models.notification import (
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
)
from app.models.system_user import SystemUser
from app.schemas.notification import NotificationCreate
from app.services.branding_config import get_brand
from app.services.domain_errors import DomainError
from app.services.integrations import installations, nextcloud_talk_capability
from app.services.integrations.runtime import OperationStatus
from app.services.notification import notifications as notifications_svc
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

FEATURE_SETTING = "nextcloud_talk_staff_notifications_enabled"
OWNER = "communications.nextcloud_talk_staff"
COMMAND_SCOPE = "communications:nextcloud-talk-staff"
MAX_RETRIES = 3
STALE_ROOM_ERROR_CODES = frozenset({"provider_resource_not_found", "room_forbidden"})
_SET_MAPPING_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="staff-to-Nextcloud username mapping",
    name="set_staff_account_mapping",
)
_DISABLE_MAPPING_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="staff-to-Nextcloud username mapping",
    name="disable_staff_account_mapping",
)
_TEST_CONNECTION_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="staff direct-room token projection",
    name="test_staff_talk_connection",
)


class StaffTalkEventType(StrEnum):
    ticket_assignment = "ticket.assignment"
    project_task_assignment = "project.task_assignment"
    ticket_comment_mention = "ticket.comment_mention"
    project_comment_mention = "project.comment_mention"
    project_task_comment_mention = "project_task.comment_mention"


@dataclass(frozen=True, slots=True)
class StageStaffTalkNotification:
    system_user_id: UUID
    source_event_id: UUID
    event_type: StaffTalkEventType
    subject: str
    body: str
    target_url: str
    source_entity_type: str
    source_entity_id: UUID


@dataclass(frozen=True, slots=True)
class TalkDeliveryBatchResult:
    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    failed: int = 0
    reconciled: int = 0


@dataclass(frozen=True, slots=True)
class TalkConnectionTestResult:
    succeeded: bool
    error_code: str | None = None


class NextcloudTalkStaffCommandError(DomainError):
    """Safe failure returned by a contracted staff-Talk owner command."""


@dataclass(frozen=True, slots=True)
class NextcloudUserId:
    """Exact immutable Nextcloud login ID used by Talk participant APIs."""

    value: str

    def __post_init__(self) -> None:
        user_id = self.value.strip()
        if not user_id:
            raise NextcloudTalkStaffCommandError(
                code=f"{OWNER}.invalid_mapping",
                message="Nextcloud user ID is required.",
                details={"field": "nextcloud_username", "reason": "required"},
            )
        if len(user_id) > 255:
            raise NextcloudTalkStaffCommandError(
                code=f"{OWNER}.invalid_mapping",
                message="Nextcloud user ID must be at most 255 characters.",
                details={"field": "nextcloud_username", "reason": "too_long"},
            )
        if any(
            ord(character) < 32
            or ord(character) == 127
            or (character.isspace() and character != " ")
            for character in user_id
        ):
            raise NextcloudTalkStaffCommandError(
                code=f"{OWNER}.invalid_mapping",
                message=(
                    "Nextcloud user ID contains unsupported whitespace or "
                    "control characters."
                ),
                details={
                    "field": "nextcloud_username",
                    "reason": "unsupported_character",
                },
            )
        object.__setattr__(self, "value", user_id)

    @classmethod
    def parse(cls, value: str) -> Self:
        return cls(value)

    @property
    def normalized(self) -> str:
        return self.value.casefold()

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SetStaffAccountMappingCommand:
    context: CommandContext
    system_user_id: UUID
    integration_installation_id: UUID
    nextcloud_user_id: NextcloudUserId


@dataclass(frozen=True, slots=True)
class SetStaffAccountMappingResult:
    mapping_id: UUID
    system_user_id: UUID
    integration_installation_id: UUID
    nextcloud_user_id: NextcloudUserId
    is_active: bool


@dataclass(frozen=True, slots=True)
class DisableStaffAccountMappingCommand:
    context: CommandContext
    system_user_id: UUID
    integration_installation_id: UUID


@dataclass(frozen=True, slots=True)
class TestStaffTalkConnectionCommand:
    context: CommandContext
    system_user_id: UUID
    integration_installation_id: UUID


def _enabled(db: Session) -> bool:
    value = resolve_value(db, SettingDomain.notification, FEATURE_SETTING)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def absolute_selfcare_url(path_or_url: str) -> str:
    value = str(path_or_url or "").strip()
    if value.startswith(("https://", "http://")):
        return value
    base_url = str(get_brand().get("app_url") or "").rstrip("/")
    return f"{base_url}/{value.lstrip('/')}" if base_url else value


def _dedupe_key(command: StageStaffTalkNotification) -> str:
    raw = ":".join(
        (
            command.event_type.value,
            str(command.source_event_id),
            str(command.system_user_id),
        )
    )
    return f"staff-talk:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def stage_staff_talk_notification(
    db: Session,
    command: StageStaffTalkNotification,
) -> Notification | None:
    """Stage one idempotent Talk delivery in the caller's transaction."""

    if not _enabled(db):
        return None
    user = db.get(SystemUser, command.system_user_id)
    if user is None or not user.is_active:
        return None

    dedupe_key = _dedupe_key(command)
    existing = (
        db.query(Notification)
        .filter(
            Notification.channel == NotificationChannel.nextcloud_talk,
            Notification.dedupe_key == dedupe_key,
        )
        .one_or_none()
    )
    if existing is not None:
        return existing

    binding: IntegrationCapabilityBinding | None
    failure: str | None = None
    try:
        binding = nextcloud_talk_capability.require_binding(db)
    except installations.InstallationError:
        binding = None
        failure = "nextcloud_talk_binding_unavailable"

    target_url = absolute_selfcare_url(command.target_url)
    notification = notifications_svc.queue_internal_notification(
        db,
        NotificationCreate(
            channel=NotificationChannel.nextcloud_talk,
            event_type=command.event_type.value,
            category="staff_operations",
            recipient=str(user.id),
            audience_type="system_user",
            audience_id=user.id,
            subject=command.subject[:200],
            body=command.body,
            metadata_={
                "source_event_id": str(command.source_event_id),
                "source_entity_type": command.source_entity_type,
                "source_entity_id": str(command.source_entity_id),
                "target_url": target_url,
            },
            status=(
                NotificationStatus.failed if failure else NotificationStatus.queued
            ),
            last_error=failure,
            retry_count=MAX_RETRIES if failure else 0,
        ),
    )
    notification.integration_capability_binding_id = binding.id if binding else None
    notification.dedupe_key = dedupe_key
    db.flush()
    return notification


def _require_talk_installation(
    db: Session, installation_id: UUID
) -> IntegrationInstallation:
    installation = db.get(IntegrationInstallation, installation_id)
    if (
        installation is None
        or installation.connector_key
        != nextcloud_talk_capability.NEXTCLOUD_TALK_CONNECTOR_KEY
    ):
        raise ValueError("Nextcloud Talk installation not found")
    return installation


def set_staff_account_mapping(
    db: Session,
    *,
    system_user_id: UUID,
    integration_installation_id: UUID,
    nextcloud_user_id: NextcloudUserId,
    actor: str,
) -> NextcloudTalkStaffAccount:
    """Create or update one explicit staff-to-Nextcloud identity mapping."""

    user = db.get(SystemUser, system_user_id)
    if user is None:
        raise ValueError("System user not found")
    _require_talk_installation(db, integration_installation_id)
    mapping = (
        db.query(NextcloudTalkStaffAccount)
        .filter(
            NextcloudTalkStaffAccount.system_user_id == system_user_id,
            NextcloudTalkStaffAccount.integration_installation_id
            == integration_installation_id,
        )
        .one_or_none()
    )
    if mapping is None:
        mapping = NextcloudTalkStaffAccount(
            system_user_id=system_user_id,
            integration_installation_id=integration_installation_id,
            nextcloud_username=nextcloud_user_id.value,
            nextcloud_username_normalized=nextcloud_user_id.normalized,
            created_by=actor,
        )
        db.add(mapping)
    else:
        if mapping.nextcloud_username_normalized != nextcloud_user_id.normalized:
            _invalidate_room(
                db,
                system_user_id=system_user_id,
                installation_id=integration_installation_id,
                failure_code="username_mapping_changed",
            )
        mapping.nextcloud_username = nextcloud_user_id.value
        mapping.nextcloud_username_normalized = nextcloud_user_id.normalized
        mapping.is_active = True
        mapping.updated_by = actor
    db.flush()
    return mapping


def execute_set_staff_account_mapping(
    db: Session,
    command: SetStaffAccountMappingCommand,
) -> SetStaffAccountMappingResult:
    """Persist one mapping through the contracted owner transaction."""

    def operation() -> SetStaffAccountMappingResult:
        mapping = set_staff_account_mapping(
            db,
            system_user_id=command.system_user_id,
            integration_installation_id=command.integration_installation_id,
            nextcloud_user_id=command.nextcloud_user_id,
            actor=command.context.actor,
        )
        return SetStaffAccountMappingResult(
            mapping_id=mapping.id,
            system_user_id=mapping.system_user_id,
            integration_installation_id=mapping.integration_installation_id,
            nextcloud_user_id=command.nextcloud_user_id,
            is_active=mapping.is_active,
        )

    return execute_owner_command(
        db,
        definition=_SET_MAPPING_COMMAND,
        context=command.context,
        operation=operation,
    )


def disable_staff_account_mapping(
    db: Session,
    *,
    system_user_id: UUID,
    integration_installation_id: UUID,
    actor: str,
) -> bool:
    mapping = (
        db.query(NextcloudTalkStaffAccount)
        .filter(
            NextcloudTalkStaffAccount.system_user_id == system_user_id,
            NextcloudTalkStaffAccount.integration_installation_id
            == integration_installation_id,
        )
        .one_or_none()
    )
    if mapping is None:
        return False
    mapping.is_active = False
    mapping.updated_by = actor
    _invalidate_room(
        db,
        system_user_id=system_user_id,
        installation_id=integration_installation_id,
        failure_code="username_mapping_disabled",
    )
    db.flush()
    return True


def execute_disable_staff_account_mapping(
    db: Session,
    command: DisableStaffAccountMappingCommand,
) -> bool:
    """Disable one mapping through the contracted owner transaction."""

    return execute_owner_command(
        db,
        definition=_DISABLE_MAPPING_COMMAND,
        context=command.context,
        operation=lambda: disable_staff_account_mapping(
            db,
            system_user_id=command.system_user_id,
            integration_installation_id=command.integration_installation_id,
            actor=command.context.actor,
        ),
    )


def staff_account_mapping_rows(
    db: Session, *, system_user_id: UUID
) -> list[dict[str, object]]:
    """Return safe mapping state for the staff-user administration page."""

    installations_rows = (
        db.query(IntegrationInstallation)
        .filter(
            IntegrationInstallation.connector_key
            == nextcloud_talk_capability.NEXTCLOUD_TALK_CONNECTOR_KEY,
            IntegrationInstallation.state != "retired",
        )
        .order_by(IntegrationInstallation.name.asc())
        .all()
    )
    mappings = {
        row.integration_installation_id: row
        for row in db.query(NextcloudTalkStaffAccount)
        .filter(NextcloudTalkStaffAccount.system_user_id == system_user_id)
        .all()
    }
    return [
        {
            "installation_id": installation.id,
            "installation_name": installation.name,
            "installation_state": installation.state,
            "username": (
                mappings[installation.id].nextcloud_username
                if installation.id in mappings
                else ""
            ),
            "is_active": bool(
                installation.id in mappings and mappings[installation.id].is_active
            ),
        }
        for installation in installations_rows
    ]


def _invalidate_room(
    db: Session,
    *,
    system_user_id: UUID,
    installation_id: UUID,
    failure_code: str,
) -> None:
    room = (
        db.query(NextcloudTalkNotificationRoom)
        .filter(
            NextcloudTalkNotificationRoom.system_user_id == system_user_id,
            NextcloudTalkNotificationRoom.integration_installation_id
            == installation_id,
        )
        .one_or_none()
    )
    if room is not None:
        room.invalidated_at = datetime.now(UTC)
        room.last_failure_code = failure_code[:120]


def _reference_id(notification_id: UUID) -> str:
    return hashlib.sha256(
        f"selfcare-nextcloud-talk:{notification_id}".encode()
    ).hexdigest()


def _claim_due(db: Session, *, batch_size: int) -> list[tuple[UUID, bool]]:
    now = datetime.now(UTC)
    stuck_before = now - timedelta(minutes=10)
    rows = (
        db.query(Notification)
        .filter(
            Notification.channel == NotificationChannel.nextcloud_talk,
            Notification.is_active.is_(True),
            or_(
                Notification.status == NotificationStatus.queued,
                (
                    (Notification.status == NotificationStatus.failed)
                    & (Notification.retry_count < MAX_RETRIES)
                ),
                (
                    (Notification.status == NotificationStatus.sending)
                    & (Notification.updated_at < stuck_before)
                ),
            ),
            or_(Notification.send_at.is_(None), Notification.send_at <= now),
        )
        .order_by(Notification.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(batch_size)
        .all()
    )
    claimed: list[tuple[UUID, bool]] = []
    for row in rows:
        reconcile = row.status in {
            NotificationStatus.failed,
            NotificationStatus.sending,
        }
        row.status = NotificationStatus.sending
        row.last_error = None
        claimed.append((row.id, reconcile))
    db.commit()
    return claimed


def _record_success(
    db: Session,
    notification: Notification,
    *,
    message_id: str | None,
    reference_id: str,
) -> None:
    now = datetime.now(UTC)
    notification.status = NotificationStatus.delivered
    notification.sent_at = now
    notification.send_at = None
    notification.last_error = None
    db.add(
        NotificationDelivery(
            notification_id=notification.id,
            provider="nextcloud_talk",
            provider_message_id=message_id or reference_id,
            status=DeliveryStatus.delivered,
            response_code="ocs_success",
            response_body=None,
            occurred_at=now,
        )
    )
    db.commit()


def _record_failure(
    db: Session,
    notification: Notification,
    *,
    error_code: str,
    retryable: bool,
) -> bool:
    notification.retry_count = (notification.retry_count or 0) + 1
    will_retry = retryable and notification.retry_count < MAX_RETRIES
    notification.status = NotificationStatus.failed
    notification.last_error = error_code[:500]
    notification.send_at = (
        datetime.now(UTC)
        + timedelta(minutes=(1, 5, 15)[min(notification.retry_count - 1, 2)])
        if will_retry
        else None
    )
    if not will_retry:
        notification.retry_count = MAX_RETRIES
    db.add(
        NotificationDelivery(
            notification_id=notification.id,
            provider="nextcloud_talk",
            status=DeliveryStatus.failed,
            response_code=error_code[:60],
            response_body=None,
        )
    )
    db.commit()
    return will_retry


def _retryable_error(error_code: str | None) -> bool:
    return error_code in {
        "provider_connect_timeout",
        "provider_timeout",
        "provider_unavailable",
        "provider_rate_limited",
        "provider_outcome_ambiguous",
    }


def _room_for_delivery(
    db: Session,
    *,
    correlation_id: str,
    binding: IntegrationCapabilityBinding,
    mapping: NextcloudTalkStaffAccount,
) -> tuple[NextcloudTalkNotificationRoom | None, str | None]:
    room = (
        db.query(NextcloudTalkNotificationRoom)
        .filter(
            NextcloudTalkNotificationRoom.system_user_id == mapping.system_user_id,
            NextcloudTalkNotificationRoom.integration_installation_id
            == binding.installation_id,
        )
        .one_or_none()
    )
    if room is not None and (
        room.invalidated_at is not None
        or room.invite_target.casefold() != mapping.nextcloud_username_normalized
    ):
        room = None
    if room is not None:
        return room, None

    outcome = nextcloud_talk_capability.create_direct_room(
        db,
        capability_binding_id=binding.id,
        invite=mapping.nextcloud_username,
        correlation_id=correlation_id,
    )
    if not outcome.succeeded or not outcome.room_token:
        return None, outcome.error_code or "room_create_failed"
    room = (
        db.query(NextcloudTalkNotificationRoom)
        .filter(
            NextcloudTalkNotificationRoom.system_user_id == mapping.system_user_id,
            NextcloudTalkNotificationRoom.integration_installation_id
            == binding.installation_id,
        )
        .one_or_none()
    )
    if room is None:
        room = NextcloudTalkNotificationRoom(
            system_user_id=mapping.system_user_id,
            integration_installation_id=binding.installation_id,
            invite_target=mapping.nextcloud_username,
            room_token=outcome.room_token,
        )
        db.add(room)
    else:
        room.invite_target = mapping.nextcloud_username
        room.room_token = outcome.room_token
        room.invalidated_at = None
        room.last_failure_code = None
    room.last_verified_at = datetime.now(UTC)
    db.flush()
    return room, None


def _deliver_one(db: Session, notification_id: UUID, *, reconcile: bool) -> str:
    notification = db.get(Notification, notification_id)
    if notification is None:
        return "failed"
    binding = (
        db.get(
            IntegrationCapabilityBinding,
            notification.integration_capability_binding_id,
        )
        if notification.integration_capability_binding_id
        else None
    )
    if binding is None or notification.audience_id is None:
        _record_failure(
            db,
            notification,
            error_code="nextcloud_talk_binding_unavailable",
            retryable=False,
        )
        return "failed"

    mapping = (
        db.query(NextcloudTalkStaffAccount)
        .filter(
            NextcloudTalkStaffAccount.system_user_id == notification.audience_id,
            NextcloudTalkStaffAccount.integration_installation_id
            == binding.installation_id,
            NextcloudTalkStaffAccount.is_active.is_(True),
        )
        .with_for_update()
        .one_or_none()
    )
    if mapping is None:
        _record_failure(
            db,
            notification,
            error_code="nextcloud_username_mapping_missing",
            retryable=False,
        )
        return "failed"

    room, room_error = _room_for_delivery(
        db,
        correlation_id=str(notification.id),
        binding=binding,
        mapping=mapping,
    )
    if room is None:
        will_retry = _record_failure(
            db,
            notification,
            error_code=room_error or "room_create_failed",
            retryable=_retryable_error(room_error),
        )
        return "retried" if will_retry else "failed"

    reference_id = _reference_id(notification.id)
    if reconcile:
        found = nextcloud_talk_capability.find_message(
            db,
            capability_binding_id=binding.id,
            room_token=room.room_token,
            reference_id=reference_id,
            correlation_id=str(notification.id),
        )
        if found.succeeded:
            if found.found:
                _record_success(
                    db,
                    notification,
                    message_id=found.message_id,
                    reference_id=reference_id,
                )
                return "reconciled"
        elif found.error_code in STALE_ROOM_ERROR_CODES:
            room.invalidated_at = datetime.now(UTC)
            room.last_failure_code = found.error_code
            room, room_error = _room_for_delivery(
                db,
                correlation_id=str(notification.id),
                binding=binding,
                mapping=mapping,
            )
            if room is None:
                will_retry = _record_failure(
                    db,
                    notification,
                    error_code=room_error or "room_recreate_failed",
                    retryable=_retryable_error(room_error),
                )
                return "retried" if will_retry else "failed"
        else:
            retryable = found.status in {
                OperationStatus.retryable,
                OperationStatus.reconciliation_required,
            }
            will_retry = _record_failure(
                db,
                notification,
                error_code=found.error_code or "talk_reconciliation_failed",
                retryable=retryable,
            )
            return "retried" if will_retry else "failed"

    target_url = str((notification.metadata_ or {}).get("target_url") or "").strip()
    body = str(notification.body or "").strip()
    message = "\n".join(
        part
        for part in (
            str(notification.subject or "").strip(),
            body,
            f"Open: {target_url}" if target_url and target_url not in body else "",
        )
        if part
    )
    outcome = nextcloud_talk_capability.post_message(
        db,
        capability_binding_id=binding.id,
        room_token=room.room_token,
        message=message,
        reference_id=reference_id,
        correlation_id=str(notification.id),
    )
    if outcome.succeeded:
        _record_success(
            db,
            notification,
            message_id=outcome.message_id,
            reference_id=reference_id,
        )
        return "delivered"
    if outcome.error_code in STALE_ROOM_ERROR_CODES:
        room.invalidated_at = datetime.now(UTC)
        room.last_failure_code = outcome.error_code
        db.flush()
        room, room_error = _room_for_delivery(
            db,
            correlation_id=str(notification.id),
            binding=binding,
            mapping=mapping,
        )
        if room is not None:
            outcome = nextcloud_talk_capability.post_message(
                db,
                capability_binding_id=binding.id,
                room_token=room.room_token,
                message=message,
                reference_id=reference_id,
                correlation_id=str(notification.id),
            )
            if outcome.succeeded:
                _record_success(
                    db,
                    notification,
                    message_id=outcome.message_id,
                    reference_id=reference_id,
                )
                return "delivered"
        elif room_error:
            outcome = nextcloud_talk_capability.TalkOperationOutcome(
                status=(
                    OperationStatus.retryable
                    if _retryable_error(room_error)
                    else OperationStatus.rejected
                ),
                error_code=room_error,
            )

    retryable = outcome.status in {
        OperationStatus.retryable,
        OperationStatus.reconciliation_required,
    }
    will_retry = _record_failure(
        db,
        notification,
        error_code=outcome.error_code or "talk_delivery_failed",
        retryable=retryable,
    )
    return "retried" if will_retry else "failed"


def test_staff_talk_connection(
    db: Session,
    *,
    system_user_id: UUID,
    integration_installation_id: UUID,
) -> TalkConnectionTestResult:
    """Create/reuse a staff DM and post one safe operator-requested test message."""

    binding = (
        db.query(IntegrationCapabilityBinding)
        .filter(
            IntegrationCapabilityBinding.installation_id == integration_installation_id,
            IntegrationCapabilityBinding.capability_id
            == nextcloud_talk_capability.NEXTCLOUD_TALK_CAPABILITY,
            IntegrationCapabilityBinding.state == "enabled",
        )
        .one_or_none()
    )
    if binding is None or binding.installation.state != "enabled":
        return TalkConnectionTestResult(False, "nextcloud_talk_binding_unavailable")
    mapping = (
        db.query(NextcloudTalkStaffAccount)
        .filter(
            NextcloudTalkStaffAccount.system_user_id == system_user_id,
            NextcloudTalkStaffAccount.integration_installation_id
            == integration_installation_id,
            NextcloudTalkStaffAccount.is_active.is_(True),
        )
        .with_for_update()
        .one_or_none()
    )
    if mapping is None:
        return TalkConnectionTestResult(False, "nextcloud_username_mapping_missing")

    test_id = uuid4()
    room, room_error = _room_for_delivery(
        db,
        correlation_id=str(test_id),
        binding=binding,
        mapping=mapping,
    )
    if room is None:
        return TalkConnectionTestResult(False, room_error or "room_create_failed")
    reference_id = hashlib.sha256(
        f"selfcare-nextcloud-talk-test:{test_id}".encode()
    ).hexdigest()
    outcome = nextcloud_talk_capability.post_message(
        db,
        capability_binding_id=binding.id,
        room_token=room.room_token,
        message=(
            "Selfcare Nextcloud Talk connection test\n"
            "This confirms that staff notifications can reach this account."
        ),
        reference_id=reference_id,
        correlation_id=str(test_id),
    )
    if outcome.succeeded:
        room.last_verified_at = datetime.now(UTC)
        room.last_failure_code = None
        db.flush()
        return TalkConnectionTestResult(True)
    if outcome.error_code in STALE_ROOM_ERROR_CODES:
        room.invalidated_at = datetime.now(UTC)
        room.last_failure_code = outcome.error_code
        room, room_error = _room_for_delivery(
            db,
            correlation_id=str(test_id),
            binding=binding,
            mapping=mapping,
        )
        if room is not None:
            outcome = nextcloud_talk_capability.post_message(
                db,
                capability_binding_id=binding.id,
                room_token=room.room_token,
                message=(
                    "Selfcare Nextcloud Talk connection test\n"
                    "This confirms that staff notifications can reach this account."
                ),
                reference_id=reference_id,
                correlation_id=str(test_id),
            )
            if outcome.succeeded:
                room.last_verified_at = datetime.now(UTC)
                room.last_failure_code = None
                db.flush()
                return TalkConnectionTestResult(True)
        elif room_error:
            return TalkConnectionTestResult(False, room_error)
    return TalkConnectionTestResult(
        False,
        outcome.error_code or "talk_connection_test_failed",
    )


def execute_test_staff_talk_connection(
    db: Session,
    command: TestStaffTalkConnectionCommand,
) -> TalkConnectionTestResult:
    """Run the operator-requested test in one complete owner transaction."""

    def operation() -> TalkConnectionTestResult:
        result = test_staff_talk_connection(
            db,
            system_user_id=command.system_user_id,
            integration_installation_id=command.integration_installation_id,
        )
        if not result.succeeded:
            error_code = result.error_code or "talk_connection_test_failed"
            raise NextcloudTalkStaffCommandError(
                code=f"{OWNER}.{error_code}",
                message=error_code,
            )
        return result

    return execute_owner_command(
        db,
        definition=_TEST_CONNECTION_COMMAND,
        context=command.context,
        operation=operation,
    )


def deliver_due_staff_talk_notifications(
    db: Session, *, batch_size: int = 50
) -> TalkDeliveryBatchResult:
    """Claim and deliver due Talk notifications outside business transactions."""

    if not _enabled(db):
        return TalkDeliveryBatchResult()
    claimed = _claim_due(db, batch_size=batch_size)
    counts = {"delivered": 0, "retried": 0, "failed": 0, "reconciled": 0}
    for notification_id, reconcile in claimed:
        try:
            outcome = _deliver_one(db, notification_id, reconcile=reconcile)
        except Exception:  # noqa: BLE001 - one delivery cannot stop the outbox
            db.rollback()
            notification = db.get(Notification, notification_id)
            if notification is not None:
                will_retry = _record_failure(
                    db,
                    notification,
                    error_code="nextcloud_talk_delivery_internal_error",
                    retryable=True,
                )
                outcome = "retried" if will_retry else "failed"
            else:
                outcome = "failed"
            logger.exception(
                "nextcloud_talk_delivery_failed notification_id=%s",
                notification_id,
            )
        counts[outcome] += 1
    return TalkDeliveryBatchResult(claimed=len(claimed), **counts)
