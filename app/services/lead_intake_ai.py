"""AI observation and delivery adapter for the ``sales.lead_intake`` owner."""

import logging
from uuid import UUID

from sqlalchemy.orm import Session

from app.db import finish_read_transaction
from app.models.team_inbox import InboxMessage
from app.schemas.lead_intake import (
    AiLeadIntakeClassification,
    LeadIntakeIntent,
    LeadIntakePartyType,
)
from app.services import team_inbox_commands
from app.services.email import _get_app_url
from app.services.owner_commands import CommandContext
from app.services.sales import lead_intake

logger = logging.getLogger(__name__)


def _context(message_id: UUID, reason: str) -> CommandContext:
    return CommandContext.system(
        actor="system:lead-intake-ai",
        scope="sales.lead_intake:auto",
        reason=reason,
        idempotency_key=f"lead-intake:{message_id}",
    )


def _send_text(
    db: Session, *, conversation_id: UUID, message_id: UUID, body: str, purpose: str
):
    return team_inbox_commands.reply(
        db,
        command=team_inbox_commands.ReplyCommand(
            conversation_id=conversation_id,
            body_text=body,
            actor_person_id=None,
            idempotency_key=f"lead-intake:{purpose}:{message_id}",
        ),
    )


def render_invitation_message(
    db: Session, outcome: lead_intake.InvitationOutcome
) -> str:
    """Render the one-time link without exposing its token to persistence."""
    if not outcome.token or not outcome.invitation_message:
        raise ValueError("The Lead intake invitation has no deliverable token.")
    base = (_get_app_url(db) or "https://selfcare.dotmac.io").rstrip("/")
    return outcome.invitation_message.replace(
        "{link}", f"{base}/lead-intake/{outcome.token}"
    )


def apply_shared_classification(
    db: Session,
    *,
    conversation_id: UUID,
    message_id: UUID,
    classification: AiLeadIntakeClassification,
    provider_label: str | None,
    model_label: str | None,
) -> lead_intake.InvitationOutcome:
    """Apply the shared CRM classifier's sales-only handoff.

    This adapter performs no AI call. General intent, confidence, follow-up,
    fallback and department routing remain owned by ``ai.intake``.
    """

    outcome = lead_intake.assess_inbound(
        db,
        lead_intake.AssessInboundCommand(
            context=_context(message_id, "apply shared customer AI sales handoff"),
            conversation_id=conversation_id,
            message_id=message_id,
            classification=classification,
            provider_label=provider_label,
            model_label=model_label,
        ),
    )
    if outcome.replayed:
        return outcome
    if outcome.clarification_question:
        _send_text(
            db,
            conversation_id=conversation_id,
            message_id=message_id,
            body=outcome.clarification_question,
            purpose="clarification",
        )
        return outcome
    if not outcome.token or not outcome.invitation_message or not outcome.invitation_id:
        return outcome
    body = render_invitation_message(db, outcome)
    finish_read_transaction(db)
    try:
        reply = _send_text(
            db,
            conversation_id=conversation_id,
            message_id=message_id,
            body=body,
            purpose="invitation",
        )
        status, reply_id, error = (
            reply.kind,
            UUID(reply.message_id) if reply.message_id else None,
            None,
        )
    except team_inbox_commands.InboxCommandError as exc:
        status, reply_id, error = "failed", None, exc.code
    lead_intake.record_invitation_delivery(
        db,
        lead_intake.InvitationDeliveryCommand(
            context=_context(message_id, "record Lead intake invitation delivery"),
            invitation_id=outcome.invitation_id,
            message_id=reply_id,
            delivery_status=status,
            error_code=error,
        ),
    )
    return outcome


def apply_inbox_intake_handoff(
    db: Session, *, conversation_id: UUID, message_id: UUID
) -> lead_intake.InvitationOutcome | None:
    """Apply a classified sales result persisted by the shared intake owner.

    The Inbox message metadata is the transactionally persisted handoff. This
    adapter never classifies text and ignores non-sales, follow-up, fallback,
    and failed outcomes.
    """

    message = db.get(InboxMessage, message_id)
    if message is None or message.conversation_id != conversation_id:
        finish_read_transaction(db)
        return None
    metadata = dict(message.metadata_ or {})
    if (
        metadata.get("ai_intake_status") != "classified"
        or bool(metadata.get("ai_intake_requires_follow_up"))
        or metadata.get("ai_intent") not in lead_intake.QUALIFYING_INTENTS
    ):
        finish_read_transaction(db)
        return None
    try:
        classification = AiLeadIntakeClassification(
            intent=LeadIntakeIntent(str(metadata["ai_intent"])),
            intent_confidence=float(metadata["ai_confidence"]),
            party_type=LeadIntakePartyType(
                str(metadata.get("ai_party_type") or "unknown")
            ),
            party_type_confidence=float(
                metadata.get("ai_party_type_confidence") or 0.0
            ),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "lead_intake_handoff_metadata_invalid",
            extra={
                "conversation_id": str(conversation_id),
                "message_id": str(message_id),
            },
        )
        finish_read_transaction(db)
        return None
    provider_label = str(metadata.get("ai_intake_provider") or "")[:80] or None
    model_label = str(metadata.get("ai_intake_model") or "")[:160] or None
    finish_read_transaction(db)
    return apply_shared_classification(
        db,
        conversation_id=conversation_id,
        message_id=message_id,
        classification=classification,
        provider_label=provider_label,
        model_label=model_label,
    )


def send_completion_confirmation(
    db: Session, *, outcome: lead_intake.SubmitLeadIntakeOutcome
) -> None:
    try:
        team_inbox_commands.reply(
            db,
            command=team_inbox_commands.ReplyCommand(
                conversation_id=outcome.conversation_id,
                body_text=outcome.confirmation_message,
                actor_person_id=None,
                idempotency_key=(f"lead-intake:confirmation:{outcome.invitation_id}"),
            ),
        )
    except team_inbox_commands.InboxCommandError:
        logger.warning(
            "lead_intake_confirmation_delivery_failed",
            extra={
                "conversation_id": str(outcome.conversation_id),
                "invitation_id": str(outcome.invitation_id),
                "lead_id": str(outcome.lead_id),
            },
        )
