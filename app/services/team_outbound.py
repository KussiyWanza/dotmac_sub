from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam
from app.services import email as email_service

OUTBOUND_EMAIL_ACTIVITY_METADATA_KEY = "outbound_email_activity"
OUTBOUND_EMAIL_SENDER_METADATA_KEY = "outbound_email_sender_key"
LEGACY_EMAIL_SENDER_METADATA_KEYS = ("email_sender_key", "smtp_sender_key")


@dataclass(frozen=True)
class TeamEmailSenderResolution:
    service_team_id: str | None
    sender_key: str | None
    activity: str | None
    config: dict[str, Any]


def _coerce_uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _metadata(team: ServiceTeam | None) -> dict[str, Any]:
    if team is None or not isinstance(team.metadata_, dict):
        return {}
    return team.metadata_


def _metadata_string(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def get_team_outbound_sender_key(
    team: ServiceTeam | None,
    *,
    metadata_override: dict[str, Any] | None = None,
) -> str | None:
    metadata = metadata_override or {}
    configured = _metadata_string(metadata, OUTBOUND_EMAIL_SENDER_METADATA_KEY)
    if configured:
        return configured.lower()
    for key in LEGACY_EMAIL_SENDER_METADATA_KEYS:
        configured = _metadata_string(metadata, key)
        if configured:
            return configured.lower()
    metadata = _metadata(team)
    configured = _metadata_string(metadata, OUTBOUND_EMAIL_SENDER_METADATA_KEY)
    if configured:
        return configured.lower()
    for key in LEGACY_EMAIL_SENDER_METADATA_KEYS:
        configured = _metadata_string(metadata, key)
        if configured:
            return configured.lower()
    return None


def get_team_outbound_activity(
    team: ServiceTeam | None,
    *,
    activity: str | None = None,
    metadata_override: dict[str, Any] | None = None,
) -> str | None:
    """Return the notification activity for a team's outbound delivery.

    The domain caller declares the activity; the notification layer owns the
    activity-to-channel/sender mapping. Team capability never decides delivery
    behavior — teams only carry an operator-configured metadata override.
    """

    metadata = metadata_override or {}
    configured = _metadata_string(metadata, OUTBOUND_EMAIL_ACTIVITY_METADATA_KEY)
    if configured:
        return configured
    metadata = _metadata(team)
    configured = _metadata_string(metadata, OUTBOUND_EMAIL_ACTIVITY_METADATA_KEY)
    if configured:
        return configured
    return activity


def resolve_team_email_sender(
    db: Session,
    *,
    service_team_id: str | UUID | None = None,
    team: ServiceTeam | None = None,
    activity: str | None = None,
    metadata_override: dict[str, Any] | None = None,
) -> TeamEmailSenderResolution:
    resolved_team = team
    if resolved_team is None:
        team_id = _coerce_uuid(service_team_id)
        if team_id is not None:
            resolved_team = db.get(ServiceTeam, team_id)

    sender_key = get_team_outbound_sender_key(
        resolved_team, metadata_override=metadata_override
    )
    resolved_activity = get_team_outbound_activity(
        resolved_team,
        activity=activity,
        metadata_override=metadata_override,
    )
    config = email_service.get_smtp_config(
        db,
        sender_key=sender_key,
        activity=resolved_activity,
    )
    return TeamEmailSenderResolution(
        service_team_id=str(resolved_team.id) if resolved_team is not None else None,
        sender_key=sender_key,
        activity=resolved_activity,
        config=config,
    )
