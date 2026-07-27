"""Temporary authenticated live-chat broker for CRM-authority mode."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.subscriber import Reseller, ResellerUser, Subscriber
from app.services import team_inbox_widget
from app.services.common import coerce_uuid
from app.services.crm_client import CRMClientError
from app.services.crm_portal import resolve_crm_subscriber_id
from app.services.integrations.connectors.dotmac_crm import (
    CRM_CHAT_SESSION_CAPABILITY,
)
from app.services.integrations.crm_capability import active_config, capability_client
from app.services.integrations.installations import InstallationError


def _error(suffix: str, message: str) -> team_inbox_widget.TeamInboxWidgetError:
    return team_inbox_widget.TeamInboxWidgetError(
        code=f"communications.chat_session.{suffix}",
        message=message,
    )


def _require_live() -> None:
    if not settings.chat_live_enabled:
        raise _error("disabled", "Live chat is not enabled.")


def _transport_urls(config: dict[str, Any]) -> tuple[str, str]:
    base_url = str(config.get("base_url") or "").rstrip("/")
    config_id = str(config.get("chat_widget_config_id") or "").strip()
    if not base_url or not config_id:
        raise _error("not_configured", "Live chat is not configured.")
    ws_url = str(config.get("chat_ws_url") or "").strip()
    if not ws_url:
        ws_base = base_url
        if ws_base.startswith("https://"):
            ws_base = "wss://" + ws_base.removeprefix("https://")
        elif ws_base.startswith("http://"):
            ws_base = "ws://" + ws_base.removeprefix("http://")
        ws_url = f"{ws_base}/ws/widget"
    return f"{base_url}/widget", ws_url


def _mint(
    db: Session,
    *,
    email: str,
    name: str | None,
    crm_subscriber_id: str | None,
    metadata: dict[str, Any],
) -> dict[str, str | None]:
    clean_email = email.strip()
    if not clean_email:
        raise _error("identity_incomplete", "Account has no email on file for chat.")
    try:
        config = active_config(db, CRM_CHAT_SESSION_CAPABILITY)
    except InstallationError as exc:
        raise _error("not_configured", "Live chat is not configured.") from exc
    api_base, ws_url = _transport_urls(config)
    try:
        data = capability_client(db).create_widget_session(
            config_id=str(config["chat_widget_config_id"]),
            email=clean_email,
            name=name,
            crm_subscriber_id=crm_subscriber_id,
            metadata=metadata,
        )
    except (CRMClientError, InstallationError, KeyError) as exc:
        raise _error(
            "transport_unavailable", "Chat service is temporarily unavailable."
        ) from exc
    token = str(data.get("visitor_token") or "")
    session_id = str(data.get("session_id") or "")
    if not token or not session_id:
        raise _error("invalid_response", "Chat service returned an invalid session.")
    conversation_id = data.get("conversation_id")
    return {
        "session_id": session_id,
        "visitor_token": token,
        "conversation_id": str(conversation_id) if conversation_id else None,
        "ws_url": ws_url,
        "api_base": api_base,
    }


def broker_customer_session(
    db: Session,
    subscriber_id: str,
    *,
    ticket_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, str | None]:
    _require_live()
    subscriber_uuid = coerce_uuid(subscriber_id)
    subscriber = db.get(Subscriber, subscriber_uuid) if subscriber_uuid else None
    if subscriber is None:
        raise _error("subscriber_not_found", "Subscriber not found.")
    ticket_id, project_id = team_inbox_widget.resolve_customer_context(
        db,
        subscriber.id,
        ticket_id=ticket_id,
        project_id=project_id,
    )
    metadata = {
        key: value
        for key, value in {
            "surface": "customer",
            "subscriber_id": str(subscriber.id),
            "ticket_id": ticket_id,
            "project_id": project_id,
            "authority": "crm",
            "source": "dotmac_sub_portal",
        }.items()
        if value
    }
    name = (
        subscriber.display_name
        or " ".join(
            part for part in (subscriber.first_name, subscriber.last_name) if part
        ).strip()
    )
    return _mint(
        db,
        email=subscriber.email or "",
        name=name or None,
        crm_subscriber_id=resolve_crm_subscriber_id(db, str(subscriber.id)),
        metadata=metadata,
    )


def broker_reseller_session(
    db: Session,
    reseller_id: str,
    principal: dict[str, object],
    *,
    ticket_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, str | None]:
    _require_live()
    reseller_uuid = coerce_uuid(reseller_id)
    reseller = db.get(Reseller, reseller_uuid) if reseller_uuid else None
    if reseller is None:
        raise _error("reseller_not_found", "Reseller not found.")
    ticket_id, project_id = team_inbox_widget.resolve_reseller_context(
        db,
        reseller.id,
        ticket_id=ticket_id,
        project_id=project_id,
    )
    email = reseller.contact_email or ""
    name = reseller.name
    if principal.get("principal_type") == "reseller_user":
        reseller_user_id = coerce_uuid(principal.get("principal_id"))
        reseller_user = (
            db.get(ResellerUser, reseller_user_id) if reseller_user_id else None
        )
        if reseller_user is not None and reseller_user.is_active:
            email = reseller_user.email or email
            name = reseller_user.full_name or name
    metadata = {
        key: value
        for key, value in {
            "surface": "reseller_portal",
            "reseller_id": str(reseller.id),
            "ticket_id": ticket_id,
            "project_id": project_id,
            "authority": "crm",
            "source": "dotmac_sub_portal",
        }.items()
        if value
    }
    return _mint(
        db,
        email=email,
        name=name,
        crm_subscriber_id=None,
        metadata=metadata,
    )
