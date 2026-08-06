"""Installation-owned WhatsApp pre-activation verification query."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationInstallation,
    IntegrationInstallationState,
    IntegrationValidationStatus,
)
from app.services.domain_errors import DomainError
from app.services.secrets import is_secret_ref, resolve_secret

WHATSAPP_CONNECTOR_KEY = "whatsapp"
WEBHOOK_VERIFY_TOKEN_BINDING = "webhook_verify_token"  # nosec B105


class WhatsAppWebhookVerificationError(DomainError):
    """Stable, secret-free failure from the pre-activation verification owner."""


@dataclass(frozen=True, slots=True, repr=False)
class VerifyWhatsAppWebhookChallengeQuery:
    """One provider-presented token; its value must never appear in repr or logs."""

    presented_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class VerifyWhatsAppWebhookChallengeResult:
    """Sanitized result from the installation-owned token comparison."""

    installation_id: UUID
    accepted: bool


SecretResolver = Callable[[str | None], str | None]


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> WhatsAppWebhookVerificationError:
    return WhatsAppWebhookVerificationError(
        code=f"integration.installations.{suffix}",
        message=message,
        details=details,
    )


def verify_whatsapp_webhook_challenge(
    *,
    db: Session,
    query: VerifyWhatsAppWebhookChallengeQuery,
    secret_resolver: SecretResolver = resolve_secret,
) -> VerifyWhatsAppWebhookChallengeResult:
    """Compare a setup challenge without granting inbound runtime capability."""

    rows = list(
        db.scalars(
            select(IntegrationInstallation)
            .where(
                IntegrationInstallation.connector_key == WHATSAPP_CONNECTOR_KEY,
                IntegrationInstallation.state
                != IntegrationInstallationState.retired.value,
            )
            .order_by(IntegrationInstallation.created_at)
        ).all()
    )
    if not rows:
        raise _error(
            "whatsapp_webhook_not_configured",
            "WhatsApp webhook verification is not configured.",
        )
    if len(rows) > 1:
        raise _error(
            "whatsapp_webhook_configuration_ambiguous",
            "Multiple WhatsApp installations require operator repair.",
            installation_ids=tuple(str(row.id) for row in rows),
        )

    installation = rows[0]
    allowed_states = {
        IntegrationInstallationState.disabled.value,
        IntegrationInstallationState.enabled.value,
    }
    if installation.state not in allowed_states:
        raise _error(
            "whatsapp_webhook_installation_not_ready",
            "WhatsApp webhook verification is not ready.",
            installation_id=str(installation.id),
            installation_state=installation.state,
        )

    revision = installation.current_config_revision
    if (
        revision is None
        or revision.validation_status != IntegrationValidationStatus.valid.value
    ):
        raise _error(
            "whatsapp_webhook_configuration_invalid",
            "WhatsApp webhook verification configuration is invalid.",
            installation_id=str(installation.id),
        )
    reference = str(
        (revision.secret_refs or {}).get(WEBHOOK_VERIFY_TOKEN_BINDING) or ""
    ).strip()
    if not is_secret_ref(reference):
        raise _error(
            "whatsapp_webhook_secret_reference_missing",
            "WhatsApp webhook verify-token reference is missing.",
            installation_id=str(installation.id),
        )
    try:
        expected_token = str(secret_resolver(reference) or "").strip()
    except Exception as exc:
        raise _error(
            "whatsapp_webhook_secret_unavailable",
            "WhatsApp webhook verify token is unavailable.",
            installation_id=str(installation.id),
        ) from exc
    if not expected_token:
        raise _error(
            "whatsapp_webhook_secret_unavailable",
            "WhatsApp webhook verify token is unavailable.",
            installation_id=str(installation.id),
        )
    return VerifyWhatsAppWebhookChallengeResult(
        installation_id=installation.id,
        accepted=hmac.compare_digest(query.presented_token, expected_token),
    )
