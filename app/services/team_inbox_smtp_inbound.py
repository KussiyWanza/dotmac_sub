from __future__ import annotations

import hashlib
import importlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models.team_inbox import InboxChannelType, InboxObservationKind
from app.services import (
    team_inbox_observations,
    team_inbox_processing,
    team_inbox_rfc822,
    team_inbox_routing,
)
from app.services.common import coerce_uuid
from app.services.owner_commands import (
    CommandContext,
)

logger = logging.getLogger(__name__)

SMTP_PROBE_HEADER_VALUE = "team_inbox_smtp_e2e"

SMTPController: Any = None
try:
    SMTPController = importlib.import_module("aiosmtpd.controller").Controller
except ModuleNotFoundError:
    SMTPController = None


@dataclass(frozen=True)
class SmtpInboundResult:
    kind: str
    conversation_id: str | None = None
    message_id: str | None = None
    reason: str | None = None


def normalize_recipient_set(
    values: set[str] | list[str] | tuple[str, ...] | None,
) -> set[str] | None:
    normalized = {
        address
        for address in (
            team_inbox_routing.normalize_email_address(value) for value in values or []
        )
        if address
    }
    return normalized or None


def envelope_matches_allowed_recipients(
    rcpt_to: list[str] | tuple[str, ...] | None,
    allowed_recipients: set[str] | None,
) -> bool:
    if not allowed_recipients:
        return True
    normalized_recipients = normalize_recipient_set(list(rcpt_to or [])) or set()
    return bool(normalized_recipients.intersection(allowed_recipients))


def handle_smtp_message(
    db: Session,
    *,
    mail_from: str | None,
    rcpt_to: list[str] | None,
    data: bytes,
    allowed_recipients: set[str] | None = None,
    fallback_service_team_id: str | None = None,
) -> SmtpInboundResult:
    if not envelope_matches_allowed_recipients(rcpt_to, allowed_recipients):
        return SmtpInboundResult(kind="skipped", reason="recipient_not_allowed")

    normalized_sender = team_inbox_routing.normalize_email_address(mail_from)
    if allowed_recipients and normalized_sender in allowed_recipients:
        return SmtpInboundResult(kind="skipped", reason="self_sender")

    try:
        parsed = team_inbox_rfc822.parse_rfc822_email(
            data,
            mail_from=mail_from,
            rcpt_to=rcpt_to or [],
            source="smtp",
            fallback_service_team_id=fallback_service_team_id,
        )
        payload = parsed.payload
        external_message_id = str(payload.message_id or "").strip() or (
            "sha256:" + hashlib.sha256(data).hexdigest()
        )
        # A missing Date header is explicit unknown provenance. Using a stable
        # sentinel keeps an exact SMTP retry fingerprint-equivalent; recorded_at
        # still captures when Sub admitted the observation.
        observed_at = payload.received_at or datetime.fromtimestamp(0, tz=UTC)
        account_scope = ",".join(sorted(payload.to_addresses))[:160] or "default"
        recorded = team_inbox_observations.record_provider_observation(
            db,
            team_inbox_observations.RecordProviderObservationCommand(
                context=CommandContext.system(
                    actor="transport:smtp",
                    scope="team-inbox:provider-observation",
                    reason="record normalized SMTP observation",
                    idempotency_key=external_message_id,
                ),
                provider=team_inbox_observations.InboxProvider.smtp,
                provider_account_scope=account_scope,
                provider_event_id=f"message:{external_message_id}",
                kind=InboxObservationKind.message,
                channel_type=InboxChannelType.email,
                external_message_id=external_message_id,
                observed_at=observed_at,
                payload=team_inbox_observations.InboundMessageObservation(
                    contact_address=payload.from_address,
                    body=payload.body or "",
                    subject=payload.subject,
                    to_addresses=tuple(payload.to_addresses),
                    cc_addresses=tuple(payload.cc_addresses),
                    in_reply_to=payload.in_reply_to,
                    references=payload.references,
                    smtp_probe=(
                        (payload.metadata or {}).get("smtp_probe")
                        == SMTP_PROBE_HEADER_VALUE
                    ),
                    authentication=(payload.metadata or {}).get("authentication"),
                    fallback_service_team_id=coerce_uuid(
                        payload.fallback_service_team_id
                    ),
                    attachments=tuple(
                        team_inbox_observations.InboundAttachmentObservation(
                            asset_type=str(item.get("type") or "file"),
                            file_name=str(item["file_name"])
                            if item.get("file_name")
                            else None,
                            mime_type=str(item["mime_type"])
                            if item.get("mime_type")
                            else None,
                            file_size=int(item["file_size"])
                            if item.get("file_size") is not None
                            else None,
                        )
                        for item in parsed.attachments
                    ),
                ),
            ),
        )
        result = team_inbox_processing.process_provider_observation(
            db,
            observation_id=recorded.observation_id,
            context=CommandContext.system(
                actor="system:team-inbox-observation-processor",
                scope="team-inbox:provider-consequence",
                reason="resolve committed SMTP observation",
                idempotency_key=str(recorded.observation_id),
            ),
        )
        return SmtpInboundResult(
            kind=result.consequence_kind or "duplicate",
            conversation_id=str(result.conversation_id)
            if result.conversation_id
            else None,
            message_id=str(result.message_id) if result.message_id else None,
        )
    except Exception:
        logger.exception("team_inbox_smtp_message_failed")
        return SmtpInboundResult(kind="failed", reason="processing_error")


class TeamInboxSMTPHandler:
    def __init__(
        self,
        *,
        allowed_recipients: set[str] | None = None,
        fallback_service_team_id: str | None = None,
    ):
        self.allowed_recipients = normalize_recipient_set(allowed_recipients)
        self.fallback_service_team_id = fallback_service_team_id

    async def handle_DATA(self, server, session, envelope):  # noqa: N802
        rcpt_to = list(getattr(envelope, "rcpt_tos", None) or [])
        if not envelope_matches_allowed_recipients(rcpt_to, self.allowed_recipients):
            logger.info(
                "team_inbox_smtp_skip_recipient from=%s to=%s",
                getattr(envelope, "mail_from", None),
                ",".join(rcpt_to),
            )
            return "250 OK"

        db = SessionLocal()
        try:
            result = handle_smtp_message(
                db,
                mail_from=getattr(envelope, "mail_from", None),
                rcpt_to=rcpt_to,
                data=getattr(envelope, "content", None) or b"",
                allowed_recipients=self.allowed_recipients,
                fallback_service_team_id=self.fallback_service_team_id,
            )
            if result.kind == "failed":
                return "451 Temporary local processing error"
            return "250 OK"
        finally:
            db.close()


_SMTP_CONTROLLER: Any | None = None


def smtp_inbound_enabled() -> bool:
    """Return whether the dedicated SMTP runtime is explicitly enabled."""
    return settings.team_inbox_smtp_inbound_enabled


def smtp_inbound_allowed_recipients() -> set[str]:
    """Return the normalized envelope recipients this intake may accept."""
    return (
        normalize_recipient_set(
            {
                value.strip()
                for value in settings.team_inbox_smtp_inbound_recipients.split(",")
                if value.strip()
            }
        )
        or set()
    )


def smtp_inbound_server_running() -> bool:
    """Return whether the process-local SMTP controller is alive."""
    controller = _SMTP_CONTROLLER
    if controller is None:
        return False
    thread = getattr(controller, "thread", None)
    return bool(thread is not None and thread.is_alive())


def start_smtp_inbound_server() -> bool:
    """Start the process-local controller once.

    Process supervision belongs to ``app.team_inbox_smtp``. This owner only
    manages the SMTP listener and inbox-ingestion callback.
    """
    global _SMTP_CONTROLLER
    if smtp_inbound_server_running():
        return True
    if SMTPController is None:
        logger.warning("team_inbox_smtp_unavailable reason=missing_aiosmtpd")
        return False
    if not smtp_inbound_enabled():
        return False

    host = settings.team_inbox_smtp_inbound_host
    recipients = smtp_inbound_allowed_recipients()
    if not recipients:
        logger.error("team_inbox_smtp_missing_allowed_recipients")
        return False
    port = settings.team_inbox_smtp_inbound_port
    fallback_service_team_id = settings.team_inbox_smtp_fallback_service_team_id or None
    controller = SMTPController(
        TeamInboxSMTPHandler(
            allowed_recipients=recipients or None,
            fallback_service_team_id=fallback_service_team_id,
        ),
        hostname=host,
        port=port,
    )
    try:
        controller.start()
    except Exception:
        logger.exception(
            "team_inbox_smtp_server_start_failed host=%s port=%s", host, port
        )
        return False
    _SMTP_CONTROLLER = controller
    logger.info("team_inbox_smtp_server_started host=%s port=%s", host, port)
    return True


def stop_smtp_inbound_server() -> None:
    global _SMTP_CONTROLLER
    if _SMTP_CONTROLLER is None:
        return
    try:
        _SMTP_CONTROLLER.stop()
    except Exception:
        logger.exception("team_inbox_smtp_server_stop_failed")
    finally:
        _SMTP_CONTROLLER = None
