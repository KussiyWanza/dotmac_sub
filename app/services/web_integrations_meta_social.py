"""Admin projection and adapter contract for the Meta social installation."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.integration_platform import IntegrationInstallation
from app.services.integrations import installations
from app.services.integrations.connectors.meta_social_runtime import (
    FACEBOOK_TOKEN_BINDING,
    INSTAGRAM_TOKEN_BINDING,
    META_OAUTH_TOKEN_BINDING,
    META_SOCIAL_AUTH_MODE_INDIVIDUAL,
    META_SOCIAL_AUTH_MODE_OAUTH,
    WEBHOOK_SIGNING_SECRET_BINDING,
    WEBHOOK_VERIFY_TOKEN_BINDING,
)
from app.services.integrations.meta_social_capability import META_SOCIAL_CONNECTOR_KEY
from app.services.integrations.meta_social_installation import (
    ConfigureMetaSocialInstallationCommand,
    MetaSocialInstallationResult,
    configure_meta_social_installation,
    get_meta_social_installation_projection,
)
from app.services.owner_commands import CommandContext


@dataclass(frozen=True, slots=True)
class MetaSocialConfigFormCommand:
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


@dataclass(frozen=True, slots=True)
class MetaSocialConfigPage:
    installation_id: str | None
    installation_state: str
    connector_version: str | None
    auth_mode: str
    auth_mode_options: tuple[dict[str, str], ...]
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
    meta_oauth_token_ref_masked: str
    facebook_token_ref_masked: str
    instagram_token_ref_masked: str
    signing_secret_ref_masked: str
    verify_token_ref_masked: str


def _single_installation(db: Session) -> IntegrationInstallation | None:
    rows = installations.list_installations(
        db,
        connector_key=META_SOCIAL_CONNECTOR_KEY,
        limit=10,
    )
    active = [row for row in rows if row.state != "retired"]
    if len(active) > 1:
        raise ValueError("Multiple Meta social installations require operator repair")
    return active[0] if active else None


def _masked(reference: str) -> str:
    if not reference:
        return ""
    if len(reference) <= 14:
        return "*" * len(reference)
    return f"{reference[:10]}…{reference[-4:]}"


def build_config_page(db: Session) -> MetaSocialConfigPage:
    projection = get_meta_social_installation_projection(db)
    installation = _single_installation(db)
    revision = installation.current_config_revision if installation else None
    refs = dict(revision.secret_refs or {}) if revision else {}
    return MetaSocialConfigPage(
        installation_id=(
            str(projection.installation_id)
            if projection.installation_id is not None
            else None
        ),
        installation_state=projection.installation_state,
        connector_version=projection.connector_version,
        auth_mode=projection.auth_mode,
        auth_mode_options=(
            {"id": META_SOCIAL_AUTH_MODE_OAUTH, "label": "Meta OAuth"},
            {"id": META_SOCIAL_AUTH_MODE_INDIVIDUAL, "label": "Individual tokens"},
        ),
        app_id=projection.app_id,
        facebook_page_id=projection.facebook_page_id,
        instagram_account_id=projection.instagram_account_id,
        graph_version=projection.graph_version,
        webhook_url=projection.webhook_url,
        meta_oauth_token_bound=projection.meta_oauth_token_bound,
        facebook_token_bound=projection.facebook_token_bound,
        instagram_token_bound=projection.instagram_token_bound,
        signing_secret_bound=projection.signing_secret_bound,
        verify_token_bound=projection.verify_token_bound,
        meta_oauth_token_ref_masked=_masked(
            str(refs.get(META_OAUTH_TOKEN_BINDING) or "")
        ),
        facebook_token_ref_masked=_masked(str(refs.get(FACEBOOK_TOKEN_BINDING) or "")),
        instagram_token_ref_masked=_masked(
            str(refs.get(INSTAGRAM_TOKEN_BINDING) or "")
        ),
        signing_secret_ref_masked=_masked(
            str(refs.get(WEBHOOK_SIGNING_SECRET_BINDING) or "")
        ),
        verify_token_ref_masked=_masked(
            str(refs.get(WEBHOOK_VERIFY_TOKEN_BINDING) or "")
        ),
    )


def save_config(
    db: Session,
    form: MetaSocialConfigFormCommand,
    *,
    context: CommandContext,
) -> MetaSocialInstallationResult:
    return configure_meta_social_installation(
        db,
        ConfigureMetaSocialInstallationCommand(
            auth_mode=form.auth_mode,
            app_id=form.app_id,
            facebook_page_id=form.facebook_page_id,
            instagram_account_id=form.instagram_account_id,
            graph_version=form.graph_version,
            webhook_url=form.webhook_url,
            meta_oauth_access_token_ref=form.meta_oauth_access_token_ref,
            facebook_page_access_token_ref=form.facebook_page_access_token_ref,
            instagram_login_access_token_ref=form.instagram_login_access_token_ref,
            webhook_signing_secret_ref=form.webhook_signing_secret_ref,
            webhook_verify_token_ref=form.webhook_verify_token_ref,
        ),
        context=context,
    )
