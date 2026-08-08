"""Typed Nextcloud Talk capability facade with no legacy credential fallback."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.integration_platform import IntegrationCapabilityBinding
from app.services.integrations import installations
from app.services.integrations.connectors.nextcloud_talk import (
    NEXTCLOUD_TALK_CAPABILITY,
    NextcloudTalkRuntimeRunner,
)
from app.services.integrations.runtime import (
    OperationResult,
    OperationStatus,
    OperationTrigger,
    ValidationResult,
)
from app.services.integrations.runtime_execution import (
    RuntimeExecutionContext,
    build_execution_context,
    make_operation_executor,
    validate_connection,
)
from app.services.secrets import resolve_secret

NEXTCLOUD_TALK_CONNECTOR_KEY = "nextcloud.talk"


@dataclass(frozen=True, slots=True)
class TalkOperationOutcome:
    status: OperationStatus
    error_code: str | None
    room_token: str | None = None
    message_id: str | None = None
    reference_id: str | None = None
    found: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status is OperationStatus.succeeded


def require_binding(db: Session) -> IntegrationCapabilityBinding:
    return installations.require_enabled_capability_binding(
        db,
        connector_key=NEXTCLOUD_TALK_CONNECTOR_KEY,
        capability_id=NEXTCLOUD_TALK_CAPABILITY,
    )


def execution_context(
    db: Session,
    *,
    capability_binding_id: UUID | None = None,
    allow_disabled: bool = False,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> RuntimeExecutionContext:
    binding = (
        db.get(IntegrationCapabilityBinding, capability_binding_id)
        if capability_binding_id is not None
        else require_binding(db)
    )
    if binding is None:
        raise installations.InstallationError("Nextcloud Talk binding not found")
    if (
        binding.capability_id != NEXTCLOUD_TALK_CAPABILITY
        or binding.installation.connector_key != NEXTCLOUD_TALK_CONNECTOR_KEY
    ):
        raise installations.InstallationError("Nextcloud Talk binding is invalid")
    return build_execution_context(
        db,
        capability_binding_id=binding.id,
        allow_disabled=allow_disabled,
        runner_override=NextcloudTalkRuntimeRunner(),
        secret_resolver=secret_resolver,
    )


def validate_installation_connection(
    db: Session,
    *,
    capability_binding_id: UUID,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> ValidationResult:
    return validate_connection(
        execution_context(
            db,
            capability_binding_id=capability_binding_id,
            allow_disabled=True,
            secret_resolver=secret_resolver,
        )
    )


def create_direct_room(
    db: Session,
    *,
    capability_binding_id: UUID,
    invite: str,
    correlation_id: str,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> TalkOperationOutcome:
    return _execute(
        db,
        capability_binding_id=capability_binding_id,
        action="create_direct_room",
        params={"invite": invite},
        correlation_id=correlation_id,
        secret_resolver=secret_resolver,
    )


def post_message(
    db: Session,
    *,
    capability_binding_id: UUID,
    room_token: str,
    message: str,
    reference_id: str,
    correlation_id: str,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> TalkOperationOutcome:
    return _execute(
        db,
        capability_binding_id=capability_binding_id,
        action="post_message",
        params={
            "room_token": room_token,
            "message": message,
            "reference_id": reference_id,
        },
        correlation_id=correlation_id,
        secret_resolver=secret_resolver,
    )


def find_message(
    db: Session,
    *,
    capability_binding_id: UUID,
    room_token: str,
    reference_id: str,
    correlation_id: str,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> TalkOperationOutcome:
    return _execute(
        db,
        capability_binding_id=capability_binding_id,
        action="find_message",
        params={"room_token": room_token, "reference_id": reference_id},
        correlation_id=correlation_id,
        secret_resolver=secret_resolver,
    )


def _execute(
    db: Session,
    *,
    capability_binding_id: UUID,
    action: str,
    params: dict[str, str],
    correlation_id: str,
    secret_resolver: Callable[[str | None], str | None],
) -> TalkOperationOutcome:
    context = execution_context(
        db,
        capability_binding_id=capability_binding_id,
        secret_resolver=secret_resolver,
    )
    configured_timeout = int(context.config.get("timeout_seconds") or 30)
    executor = make_operation_executor(
        context,
        correlation_id=correlation_id,
        trigger=OperationTrigger.event,
        actor="communications.nextcloud_talk",
        timeout_seconds=min(max(configured_timeout + 5, 6), 125),
    )
    return _outcome(executor(action, params))


def _outcome(result: OperationResult) -> TalkOperationOutcome:
    output = result.output
    room_token = output.get("room_token")
    message_id = output.get("message_id")
    reference_id = output.get("reference_id")
    return TalkOperationOutcome(
        status=result.status,
        error_code=result.error_code,
        room_token=str(room_token) if room_token else None,
        message_id=str(message_id) if message_id else None,
        reference_id=str(reference_id) if reference_id else None,
        found=bool(output.get("found")),
    )
