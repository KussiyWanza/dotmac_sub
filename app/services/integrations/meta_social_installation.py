"""Installation-owned Meta social configuration command and read projection."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationInstallation,
    IntegrationInstallationState,
)
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.integrations import installations
from app.services.integrations.connectors.meta_social_runtime import (
    FACEBOOK_TOKEN_BINDING,
    INSTAGRAM_TOKEN_BINDING,
    META_OAUTH_TOKEN_BINDING,
    META_SOCIAL_AUTH_MODE_INDIVIDUAL,
    META_SOCIAL_AUTH_MODE_OAUTH,
    META_SOCIAL_RECEIVE_CAPABILITY,
    META_SOCIAL_SEND_CAPABILITY,
    WEBHOOK_SIGNING_SECRET_BINDING,
    WEBHOOK_VERIFY_TOKEN_BINDING,
)
from app.services.integrations.meta_social_capability import META_SOCIAL_CONNECTOR_KEY
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

META_SOCIAL_CONFIGURATION_SCOPE = "integration-installation:configure-meta-social"
_CONFIGURE_META_SOCIAL = OwnerCommandDefinition(
    owner="integration.installations",
    concern="Meta social installation configuration",
    name="configure_meta_social_installation",
)


class MetaSocialInstallationError(DomainError, ValueError):
    """Stable rejection from the Meta social installation command owner."""


@dataclass(frozen=True, slots=True)
class ConfigureMetaSocialInstallationCommand:
    auth_mode: str
    app_id: str
    facebook_page_id: str
    instagram_account_id: str
    graph_version: str
    webhook_url: str
    meta_oauth_access_token_ref: str
    facebook_page_access_token_ref: str
    instagram_login_access_token_ref: str
    webhook_signing_secret_ref: str
    webhook_verify_token_ref: str
    environment: str = "production"


@dataclass(frozen=True, slots=True)
class MetaSocialInstallationResult:
    installation_id: UUID
    config_revision_id: UUID
    installation_state: str
    connector_version: str
    replayed_revision: bool


@dataclass(frozen=True, slots=True)
class MetaSocialInstallationProjection:
    installation_id: UUID | None
    installation_state: str
    connector_version: str | None
    auth_mode: str
    app_id: str
    facebook_page_id: str
    instagram_account_id: str
    graph_version: str
    webhook_url: str
    meta_oauth_token_bound: bool
    facebook_token_bound: bool
    instagram_token_bound: bool
    signing_secret_bound: bool
    verify_token_bound: bool


def _error(suffix: str, message: str, **details: object) -> MetaSocialInstallationError:
    return MetaSocialInstallationError(
        code=f"integration.installations.{suffix}",
        message=message,
        details=details,
    )


def _single_installation(
    db: Session, *, lock: bool = False
) -> IntegrationInstallation | None:
    query = select(IntegrationInstallation).where(
        IntegrationInstallation.connector_key == META_SOCIAL_CONNECTOR_KEY,
        IntegrationInstallation.state != IntegrationInstallationState.retired.value,
    )
    if lock:
        query = query.with_for_update()
    rows = list(db.scalars(query.order_by(IntegrationInstallation.created_at)).all())
    if len(rows) > 1:
        raise _error(
            "meta_configuration_ambiguous",
            "Multiple active Meta social installations require operator repair.",
            installation_ids=tuple(str(row.id) for row in rows),
        )
    return rows[0] if rows else None


def get_meta_social_installation_projection(
    db: Session,
) -> MetaSocialInstallationProjection:
    installation = _single_installation(db)
    revision = installation.current_config_revision if installation else None
    config = dict(revision.config_json or {}) if revision else {}
    refs = dict(revision.secret_refs or {}) if revision else {}
    return MetaSocialInstallationProjection(
        installation_id=installation.id if installation else None,
        installation_state=(installation.state if installation else "not_configured"),
        connector_version=(installation.connector_version if installation else None),
        auth_mode=str(
            config.get("auth_mode") or META_SOCIAL_AUTH_MODE_INDIVIDUAL
        ).strip()
        or META_SOCIAL_AUTH_MODE_INDIVIDUAL,
        app_id=str(config.get("app_id") or ""),
        facebook_page_id=str(config.get("facebook_page_id") or ""),
        instagram_account_id=str(config.get("instagram_account_id") or ""),
        graph_version=str(config.get("graph_version") or "v21.0"),
        webhook_url=str(config.get("webhook_url") or ""),
        meta_oauth_token_bound=bool(refs.get(META_OAUTH_TOKEN_BINDING)),
        facebook_token_bound=bool(refs.get(FACEBOOK_TOKEN_BINDING)),
        instagram_token_bound=bool(refs.get(INSTAGRAM_TOKEN_BINDING)),
        signing_secret_bound=bool(refs.get(WEBHOOK_SIGNING_SECRET_BINDING)),
        verify_token_bound=bool(refs.get(WEBHOOK_VERIFY_TOKEN_BINDING)),
    )


def configure_meta_social_installation(
    db: Session,
    command: ConfigureMetaSocialInstallationCommand,
    *,
    context: CommandContext,
) -> MetaSocialInstallationResult:
    return execute_owner_command(
        db,
        definition=_CONFIGURE_META_SOCIAL,
        context=context,
        operation=lambda: _configure_meta_social_installation(
            db,
            command=command,
            context=context,
        ),
    )


def _required(value: str, *, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise _error(
            "meta_configuration_invalid",
            f"{field.replace('_', ' ').title()} is required.",
            field=field,
        )
    return normalized


def _configure_meta_social_installation(
    db: Session,
    *,
    command: ConfigureMetaSocialInstallationCommand,
    context: CommandContext,
) -> MetaSocialInstallationResult:
    if context.scope != META_SOCIAL_CONFIGURATION_SCOPE:
        raise _error(
            "meta_configuration_scope_invalid",
            "Meta social configuration requires its dedicated command scope.",
            scope=context.scope,
        )
    installation = _single_installation(db, lock=True)
    if installation is None:
        installation = installations.create_draft(
            db,
            connector_key=META_SOCIAL_CONNECTOR_KEY,
            name="Meta Social Inbox",
            environment=command.environment,
            actor=context.actor,
        )
    previous_revision_id = (
        installation.current_config_revision.id
        if installation.current_config_revision is not None
        else None
    )
    previous_refs = (
        dict(installation.current_config_revision.secret_refs or {})
        if installation.current_config_revision is not None
        else {}
    )

    def reference(candidate: str, *, binding: str, field: str) -> str:
        return _required(
            candidate.strip() or str(previous_refs.get(binding) or ""),
            field=field,
        )

    auth_mode = command.auth_mode.strip().lower() or META_SOCIAL_AUTH_MODE_INDIVIDUAL
    if auth_mode not in {META_SOCIAL_AUTH_MODE_OAUTH, META_SOCIAL_AUTH_MODE_INDIVIDUAL}:
        raise _error(
            "meta_configuration_invalid",
            "Meta social auth mode is not supported.",
            field="auth_mode",
            auth_mode=command.auth_mode,
        )
    secret_refs = {
        WEBHOOK_SIGNING_SECRET_BINDING: reference(
            command.webhook_signing_secret_ref,
            binding=WEBHOOK_SIGNING_SECRET_BINDING,
            field="webhook_signing_secret_ref",
        ),
        WEBHOOK_VERIFY_TOKEN_BINDING: reference(
            command.webhook_verify_token_ref,
            binding=WEBHOOK_VERIFY_TOKEN_BINDING,
            field="webhook_verify_token_ref",
        ),
    }
    if auth_mode == META_SOCIAL_AUTH_MODE_OAUTH:
        secret_refs[META_OAUTH_TOKEN_BINDING] = reference(
            command.meta_oauth_access_token_ref,
            binding=META_OAUTH_TOKEN_BINDING,
            field="meta_oauth_access_token_ref",
        )
    else:
        secret_refs[FACEBOOK_TOKEN_BINDING] = reference(
            command.facebook_page_access_token_ref,
            binding=FACEBOOK_TOKEN_BINDING,
            field="facebook_page_access_token_ref",
        )
        secret_refs[INSTAGRAM_TOKEN_BINDING] = reference(
            command.instagram_login_access_token_ref,
            binding=INSTAGRAM_TOKEN_BINDING,
            field="instagram_login_access_token_ref",
        )

    revision = installations.create_config_revision(
        db,
        installation_id=installation.id,
        config={
            "provider": "meta_social",
            "auth_mode": auth_mode,
            "app_id": _required(command.app_id, field="app_id"),
            "facebook_page_id": _required(
                command.facebook_page_id, field="facebook_page_id"
            ),
            "facebook_auth_mode": (
                "meta_oauth"
                if auth_mode == META_SOCIAL_AUTH_MODE_OAUTH
                else "page_access_token"
            ),
            "instagram_account_id": _required(
                command.instagram_account_id, field="instagram_account_id"
            ),
            "instagram_auth_mode": (
                "meta_oauth"
                if auth_mode == META_SOCIAL_AUTH_MODE_OAUTH
                else "instagram_login"
            ),
            "webhook_url": command.webhook_url.strip(),
            "graph_version": _required(command.graph_version, field="graph_version"),
            "timeout_seconds": 10,
        },
        secret_refs=secret_refs,
        actor=context.actor,
    )
    for capability_id in (
        META_SOCIAL_SEND_CAPABILITY,
        META_SOCIAL_RECEIVE_CAPABILITY,
    ):
        installations.bind_capability(
            db,
            installation_id=installation.id,
            capability_id=capability_id,
            scope={
                "channels": ["facebook_messenger", "instagram_dm"],
                "facebook_page_id": command.facebook_page_id.strip(),
                "instagram_account_id": command.instagram_account_id.strip(),
            },
            policy={"default": True},
            actor=context.actor,
        )
    validation = installations.validate_static(
        db,
        installation_id=installation.id,
        actor=context.actor,
    )
    if not validation.valid:
        raise _error(
            "meta_configuration_invalid",
            "Meta social installation failed static validation.",
            error_codes=validation.error_codes,
        )
    emit_event(
        db,
        EventType.integration_installation_meta_social_configured,
        {
            "schema_version": 1,
            "installation_id": str(installation.id),
            "connector_version": installation.connector_version,
            "config_revision_id": str(revision.id),
            "facebook_page_id": command.facebook_page_id.strip(),
            "instagram_account_id": command.instagram_account_id.strip(),
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
            "idempotency_key": context.idempotency_key,
            "reason": context.reason,
        },
        actor=context.actor,
    )
    return MetaSocialInstallationResult(
        installation_id=installation.id,
        config_revision_id=revision.id,
        installation_state=installation.state,
        connector_version=installation.connector_version,
        replayed_revision=previous_revision_id == revision.id,
    )
