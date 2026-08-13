"""Typed Meta social capability facade with no legacy token fallback."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models.integration_platform import IntegrationCapabilityBinding
from app.services.integrations import installations
from app.services.integrations.connectors.meta_social_runtime import (
    META_SOCIAL_RECEIVE_CAPABILITY,
    META_SOCIAL_SEND_CAPABILITY,
    WEBHOOK_SIGNING_SECRET_BINDING,
    WEBHOOK_VERIFY_TOKEN_BINDING,
    MetaSocialRuntimeRunner,
)
from app.services.integrations.meta_social_contracts import (
    MetaContactProfile,
    MetaDirectMessageCommand,
    MetaDirectMessageOutcome,
    MetaSocialChannel,
    MetaWebhookSecretMaterial,
)
from app.services.integrations.runtime import OperationStatus, OperationTrigger
from app.services.integrations.runtime_execution import (
    RuntimeExecutionContext,
    build_execution_context,
    make_operation_executor,
)
from app.services.secrets import resolve_secret

META_SOCIAL_CONNECTOR_KEY = "meta.social"


def require_binding(db: Session, *, capability_id: str) -> IntegrationCapabilityBinding:
    return installations.require_enabled_capability_binding(
        db,
        connector_key=META_SOCIAL_CONNECTOR_KEY,
        capability_id=capability_id,
    )


def execution_context(
    db: Session,
    *,
    capability_id: str,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> RuntimeExecutionContext:
    binding = require_binding(db, capability_id=capability_id)
    return build_execution_context(
        db,
        capability_binding_id=binding.id,
        runner_override=MetaSocialRuntimeRunner(),
        secret_resolver=secret_resolver,
    )


def inbound_secret_material(
    db: Session,
    *,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> MetaWebhookSecretMaterial:
    material = execution_context(
        db,
        capability_id=META_SOCIAL_RECEIVE_CAPABILITY,
        secret_resolver=secret_resolver,
    ).secret_material
    return MetaWebhookSecretMaterial(
        signing_secret=str(material.get(WEBHOOK_SIGNING_SECRET_BINDING) or ""),
        verify_token=str(material.get(WEBHOOK_VERIFY_TOKEN_BINDING) or ""),
    )


def send_direct_message(
    db: Session,
    command: MetaDirectMessageCommand,
    *,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> MetaDirectMessageOutcome:
    context = execution_context(
        db,
        capability_id=META_SOCIAL_SEND_CAPABILITY,
        secret_resolver=secret_resolver,
    )
    executor = make_operation_executor(
        context,
        correlation_id=command.correlation_id,
        trigger=(
            OperationTrigger.interactive if command.preview else OperationTrigger.event
        ),
        actor="integration.meta_social",
        timeout_seconds=int(context.config.get("timeout_seconds") or 10) + 5,
    )
    result = executor(
        "send_direct_message",
        {
            "channel": command.channel.value,
            "provider_account_id": command.provider_account_id,
            "recipient_id": command.recipient_id,
            "body": command.body,
            "preview": command.preview,
        },
    )
    provider_message_id = result.external_receipt.get("provider_message_id")
    provider_recipient_id = result.external_receipt.get("provider_recipient_id")
    return MetaDirectMessageOutcome(
        accepted=result.status is OperationStatus.succeeded,
        operation_status=result.status.value,
        provider_message_id=(
            str(provider_message_id) if provider_message_id is not None else None
        ),
        provider_recipient_id=(
            str(provider_recipient_id) if provider_recipient_id is not None else None
        ),
        error_code=result.error_code,
    )


def fetch_contact_profile(
    db: Session,
    *,
    channel: MetaSocialChannel,
    contact_id: str,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> MetaContactProfile | None:
    context = execution_context(
        db,
        capability_id=META_SOCIAL_SEND_CAPABILITY,
        secret_resolver=secret_resolver,
    )
    executor = make_operation_executor(
        context,
        correlation_id=f"meta-profile:{channel.value}:{contact_id}",
        trigger=OperationTrigger.interactive,
        actor="integration.meta_social",
        timeout_seconds=6,
    )
    result = executor(
        "fetch_profile",
        {
            "channel": channel.value,
            "contact_id": contact_id,
        },
    )
    if result.status is not OperationStatus.succeeded:
        return None
    output = result.output if isinstance(result.output, dict) else {}
    profile = output.get("profile")
    if not isinstance(profile, dict):
        return None
    return MetaContactProfile(
        display_name=profile.get("display_name"),
        username=profile.get("username"),
        profile_pic=profile.get("profile_pic"),
    )
