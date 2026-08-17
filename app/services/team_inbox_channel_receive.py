from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.team_inbox import (
    InboxAutomationTrigger,
    InboxChannelType,
    InboxContactLink,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
    InboxObservationKind,
)
from app.schemas.ai_intake import (
    AiIntakeContextMessage,
    AiIntakeOutcome,
    AiIntakeReason,
    AiIntakeRequest,
    AiIntakeStatus,
    DataCleaningEligibility,
    DataCleaningEligibilityReason,
)
from app.services import (
    ai_conversation_intake,
    ai_intake,
    team_inbox_automation,
    team_inbox_media,
    team_inbox_operations,
    team_inbox_participants,
    team_inbox_realtime,
    team_inbox_routing,
    team_inbox_status,
)
from app.services.common import coerce_uuid
from app.services.customer_identity_normalization import (
    default_country_code,
    normalize_channel_address,
)
from app.services.integrations.connectors import whatsapp_runtime
from app.services.owner_commands import CommandContext
from app.services.realtime_platform import EventType

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.services.team_inbox_observations import InboundAttachmentObservation

_INACTIVE_SUBSCRIBER_STATUSES = {
    SubscriberStatus.disabled.value,
    SubscriberStatus.canceled.value,
}
_OPAQUE_CONTACT_CHANNELS = {
    InboxChannelType.facebook_messenger.value,
    InboxChannelType.instagram_dm.value,
    InboxChannelType.facebook_comment.value,
    InboxChannelType.instagram_comment.value,
    InboxChannelType.chat_widget.value,
}


def _inbound_attachment_observation(
    item: dict[str, object],
) -> InboundAttachmentObservation:
    from app.services import team_inbox_observations

    return team_inbox_observations.InboundAttachmentObservation(
        asset_type=str(item.get("type") or item.get("asset_type") or "file"),
        file_name=(
            str(item.get("filename") or item.get("file_name"))
            if item.get("filename") or item.get("file_name")
            else None
        ),
        mime_type=str(item["mime_type"]) if item.get("mime_type") else None,
        provider_media_id=(
            str(item.get("id") or item.get("provider_media_id"))
            if item.get("id") or item.get("provider_media_id")
            else None
        ),
        source_url=(
            str(item.get("url") or item.get("source_url"))
            if item.get("url") or item.get("source_url")
            else None
        ),
        caption=str(item["caption"]) if item.get("caption") else None,
        file_size=(
            int(str(item["file_size"])) if item.get("file_size") is not None else None
        ),
        download_status=(
            str(item["download_status"]) if item.get("download_status") else None
        ),
        location=team_inbox_observations.inbound_location_observation(
            latitude=item.get("latitude"),
            longitude=item.get("longitude"),
            name=item.get("name"),
            address=item.get("address"),
        ),
    )


@dataclass(frozen=True)
class InboundChannelPayload:
    channel_type: str
    contact_address: str
    body: str
    contact_name: str | None = None
    external_message_id: str | None = None
    external_thread_id: str | None = None
    subject: str | None = None
    received_at: datetime | None = None
    subscriber_id: str | UUID | None = None
    fallback_service_team_id: str | UUID | None = None
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class InboundChannelReceiveResult:
    kind: str
    conversation_id: str
    message_id: str
    duplicate: bool
    subscriber_id: str | None = None
    reseller_id: str | None = None
    resolution_status: str = "unmatched"


@dataclass(frozen=True)
class ContactResolution:
    status: str
    normalized_contact: str | None
    subscriber_id: UUID | None
    reseller_id: UUID | None
    matched_subscriber_ids: list[str]
    suppressed_subscriber_ids: list[str]
    matched_reseller_ids: list[str]

    def as_metadata(self) -> dict[str, object]:
        return {
            "status": self.status,
            "normalized_contact": self.normalized_contact,
            "subscriber_id": str(self.subscriber_id) if self.subscriber_id else None,
            "reseller_id": str(self.reseller_id) if self.reseller_id else None,
            "matched_subscriber_ids": self.matched_subscriber_ids,
            "suppressed_subscriber_ids": self.suppressed_subscriber_ids,
            "matched_reseller_ids": self.matched_reseller_ids,
        }


def _run_sales_lead_intake_after_commit(
    db: Session, *, conversation_id: UUID | None, message_id: UUID | None
) -> None:
    """Run the optional sales consequence after Inbox has committed the message."""

    if conversation_id is None or message_id is None:
        return
    try:
        from app.services import lead_intake_ai

        lead_intake_ai.apply_inbox_intake_handoff(
            db, conversation_id=conversation_id, message_id=message_id
        )
    except Exception as exc:
        # Inbox persistence and routing are authoritative and already committed.
        # A Sales consequence must never make the transport retry that work.
        logger.warning(
            "sales lead intake consequence failed",
            extra={
                "event": "sales_lead_intake_consequence_failed",
                "conversation_id": str(conversation_id),
                "message_id": str(message_id),
                "error_type": type(exc).__name__,
            },
        )


def _status_value(subscriber: Subscriber) -> str:
    return str(getattr(subscriber.status, "value", subscriber.status) or "")


def _subscriber_is_linkable(subscriber: Subscriber) -> bool:
    return (
        bool(subscriber.is_active)
        and _status_value(subscriber) not in _INACTIVE_SUBSCRIBER_STATUSES
    )


def _normalize_contact(db: Session, channel_type: str, value: str | None) -> str | None:
    return _normalize_contact_with_country(
        channel_type,
        value,
        country_code=default_country_code(db),
    )


def _normalize_contact_with_country(
    channel_type: str,
    value: str | None,
    *,
    country_code: str,
) -> str | None:
    if channel_type in _OPAQUE_CONTACT_CHANNELS:
        normalized = str(value or "").strip()
        return normalized or None
    return normalize_channel_address(
        channel_type,
        value,
        default_country_code=country_code,
    )


def _subscriber_contact(subscriber: Subscriber, channel_type: str) -> str | None:
    if channel_type == InboxChannelType.email.value:
        return subscriber.email
    return subscriber.phone


def _reseller_contact(reseller: Reseller, channel_type: str) -> str | None:
    if channel_type == InboxChannelType.email.value:
        return reseller.contact_email
    return reseller.contact_phone


def _candidate_subscribers(db: Session, channel_type: str, normalized: str):
    """Narrow the rows the Python matcher has to normalize.

    Every inbound message runs this, and it used to load the whole subscriber
    table and normalize each row in Python — now on the email path too.

    Email normalization is exactly ``strip().lower()``, so the database can do
    the comparison. Phone normalization applies a default country code and
    strips separators, and no SQL prefilter on the raw column is a guaranteed
    superset of that, so phone-like channels still scan. Narrowing them wants a
    stored normalized contact column and a backfill, which is its own change —
    the Python pass below stays the decider either way.
    """
    query = db.query(Subscriber)
    if channel_type == InboxChannelType.email.value:
        return query.filter(func.lower(func.trim(Subscriber.email)) == normalized).all()
    return query.all()


def _candidate_resellers(db: Session, channel_type: str, normalized: str):
    query = db.query(Reseller).filter(Reseller.is_active.is_(True))
    if channel_type == InboxChannelType.email.value:
        return query.filter(
            func.lower(func.trim(Reseller.contact_email)) == normalized
        ).all()
    return query.all()


def resolve_contact_context(
    db: Session,
    *,
    channel_type: str,
    contact_address: str,
    subscriber_id: str | UUID | None = None,
) -> ContactResolution:
    country_code = default_country_code(db)
    normalized = _normalize_contact_with_country(
        channel_type, contact_address, country_code=country_code
    )
    explicit_subscriber_id = coerce_uuid(subscriber_id)
    if explicit_subscriber_id is not None:
        subscriber = db.get(Subscriber, explicit_subscriber_id)
        reseller_id = subscriber.reseller_id if subscriber is not None else None
        return ContactResolution(
            status="explicit_subscriber" if subscriber is not None else "unmatched",
            normalized_contact=normalized,
            subscriber_id=subscriber.id if subscriber is not None else None,
            reseller_id=reseller_id,
            matched_subscriber_ids=[str(subscriber.id)]
            if subscriber is not None
            else [],
            suppressed_subscriber_ids=[],
            matched_reseller_ids=[str(reseller_id)] if reseller_id is not None else [],
        )

    active_link = None
    if normalized:
        active_link = (
            db.query(InboxContactLink)
            .filter(InboxContactLink.channel_type == channel_type)
            .filter(InboxContactLink.normalized_contact == normalized)
            .filter(InboxContactLink.is_active.is_(True))
            .first()
        )
    if active_link is not None:
        subscriber = (
            db.get(Subscriber, active_link.subscriber_id)
            if active_link.subscriber_id is not None
            else None
        )
        reseller = (
            db.get(Reseller, active_link.reseller_id)
            if active_link.reseller_id is not None
            else None
        )
        if subscriber is not None and _subscriber_is_linkable(subscriber):
            return ContactResolution(
                status="linked_subscriber",
                normalized_contact=normalized,
                subscriber_id=subscriber.id,
                reseller_id=subscriber.reseller_id,
                matched_subscriber_ids=[str(subscriber.id)],
                suppressed_subscriber_ids=[],
                matched_reseller_ids=[str(subscriber.reseller_id)]
                if subscriber.reseller_id is not None
                else [],
            )
        if subscriber is not None:
            return ContactResolution(
                status="suppressed_inactive",
                normalized_contact=normalized,
                subscriber_id=None,
                reseller_id=None,
                matched_subscriber_ids=[],
                suppressed_subscriber_ids=[str(subscriber.id)],
                matched_reseller_ids=[],
            )
        if reseller is not None and reseller.is_active:
            return ContactResolution(
                status="linked_reseller",
                normalized_contact=normalized,
                subscriber_id=None,
                reseller_id=reseller.id,
                matched_subscriber_ids=[],
                suppressed_subscriber_ids=[],
                matched_reseller_ids=[str(reseller.id)],
            )

    matched_subscribers: list[Subscriber] = []
    suppressed_subscribers: list[Subscriber] = []
    if normalized:
        for subscriber in _candidate_subscribers(db, channel_type, normalized):
            candidate = _normalize_contact_with_country(
                channel_type,
                _subscriber_contact(subscriber, channel_type),
                country_code=country_code,
            )
            if candidate != normalized:
                continue
            if _subscriber_is_linkable(subscriber):
                matched_subscribers.append(subscriber)
            else:
                suppressed_subscribers.append(subscriber)

    matched_resellers: list[Reseller] = []
    if normalized:
        for reseller in _candidate_resellers(db, channel_type, normalized):
            candidate = _normalize_contact_with_country(
                channel_type,
                _reseller_contact(reseller, channel_type),
                country_code=country_code,
            )
            if candidate == normalized:
                matched_resellers.append(reseller)

    selected_subscriber = (
        matched_subscribers[0] if len(matched_subscribers) == 1 else None
    )
    selected_reseller_id = None
    if selected_subscriber is not None:
        selected_reseller_id = selected_subscriber.reseller_id
    elif len(matched_resellers) == 1:
        selected_reseller_id = matched_resellers[0].id

    if selected_subscriber is not None:
        status = "linked_subscriber"
    elif selected_reseller_id is not None:
        status = "linked_reseller"
    elif matched_subscribers or matched_resellers:
        status = "ambiguous"
    elif suppressed_subscribers:
        status = "suppressed_inactive"
    else:
        status = "unmatched"

    return ContactResolution(
        status=status,
        normalized_contact=normalized,
        subscriber_id=selected_subscriber.id if selected_subscriber else None,
        reseller_id=selected_reseller_id,
        matched_subscriber_ids=[
            str(subscriber.id) for subscriber in matched_subscribers
        ],
        suppressed_subscriber_ids=[
            str(subscriber.id) for subscriber in suppressed_subscribers
        ],
        matched_reseller_ids=[str(reseller.id) for reseller in matched_resellers],
    )


def _find_duplicate_message(
    db: Session,
    *,
    channel_type: str,
    external_message_id: str | None,
) -> InboxMessage | None:
    if not external_message_id:
        return None
    return (
        db.query(InboxMessage)
        .filter(InboxMessage.channel_type == channel_type)
        .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .filter(InboxMessage.external_message_id == external_message_id)
        .first()
    )


def _find_open_conversation(
    db: Session,
    *,
    channel_type: str,
    external_thread_id: str,
) -> InboxConversation | None:
    return (
        db.query(InboxConversation)
        .filter(InboxConversation.channel_type == channel_type)
        .filter(InboxConversation.external_thread_id == external_thread_id)
        .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
        .filter(InboxConversation.is_active.is_(True))
        .order_by(InboxConversation.last_message_at.desc().nullslast())
        .with_for_update()
        .first()
    )


def _thread_lock_key(channel_type: str, external_thread_id: str) -> int:
    digest = hashlib.sha256(
        f"team-inbox-thread:{channel_type}:{external_thread_id}".encode()
    ).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


def _acquire_thread_lock(
    db: Session, *, channel_type: str, external_thread_id: str
) -> None:
    """Serialize conversation lookup, intake state, and message creation."""

    if db.get_bind().dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _thread_lock_key(channel_type, external_thread_id)},
    )


def _thread_id(channel_type: str, normalized_contact: str | None, fallback: str) -> str:
    contact = normalized_contact or fallback.strip()
    return f"{channel_type}:{contact}"[:255]


def _message_body(value: object) -> str:
    if isinstance(value, dict):
        body = value.get("body") or value.get("text")
        return str(body or "").strip()
    return str(value or "").strip()


def _campaign_attributed(metadata: dict[str, object]) -> bool:
    return any(
        metadata.get(key) not in (None, "", False, [], {})
        for key in (
            "campaign_id",
            "campaign_attributed",
            "campaign_attribution",
            "campaign_ref",
            "referral_campaign_id",
            "referral",
        )
    )


def _recent_intake_context(
    db: Session, *, conversation_id: UUID
) -> tuple[AiIntakeContextMessage, ...]:
    rows = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation_id)
        .order_by(InboxMessage.created_at.desc())
        .limit(ai_intake.MAX_RECENT_MESSAGES)
        .all()
    )
    context: list[AiIntakeContextMessage] = []
    for row in reversed(rows):
        body = _message_body(row.body)
        if not body:
            continue
        context.append(
            AiIntakeContextMessage(
                direction=(
                    "inbound"
                    if row.direction == InboxMessageDirection.inbound.value
                    else "outbound"
                ),
                body=body[: ai_intake.MAX_CONTEXT_CHARS],
            )
        )
    return tuple(context)


def _existing_intake_state(conversation: InboxConversation) -> dict[str, object]:
    metadata = (
        conversation.metadata_ if isinstance(conversation.metadata_, dict) else {}
    )
    value = metadata.get("ai_intake")
    return dict(value) if isinstance(value, dict) else {}


def _record_data_cleaning_eligibility(
    conversation: InboxConversation,
    eligibility: DataCleaningEligibility,
) -> None:
    metadata = dict(conversation.metadata_ or {})
    metadata["ai_data_cleaning"] = {
        "eligible": eligibility.eligible,
        "state": eligibility.state.value,
        "reason": eligibility.reason.value,
        "config_id": str(eligibility.config_id) if eligibility.config_id else None,
        "support_team_id": (
            str(eligibility.support_team_id) if eligibility.support_team_id else None
        ),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    conversation.metadata_ = metadata


def _classify_inbound(
    db: Session,
    *,
    conversation: InboxConversation,
    created_conversation: bool,
    payload: InboundChannelPayload,
    body: str,
    metadata: dict[str, object],
) -> tuple[AiIntakeRequest | None, AiIntakeOutcome]:
    if not body:
        return None, AiIntakeOutcome(
            status=AiIntakeStatus.skipped,
            reason=AiIntakeReason.no_text_content,
        )
    provider = str(metadata.get("provider") or "default")[:80]
    account_scope = str(
        metadata.get("provider_account_scope")
        or metadata.get("page_or_account_id")
        or metadata.get("phone_number_id")
        or "default"
    )[:160]
    routing_gate = team_inbox_routing.resolve_channel_routing_decision(
        db,
        channel_type=payload.channel_type,
        provider=provider,
        account_scope=account_scope,
        fallback_service_team_id=(
            payload.fallback_service_team_id
            or team_inbox_routing.default_service_team_id(db)
        ),
        metadata={},
    )
    state = _existing_intake_state(conversation)
    awaiting_follow_up = state.get("status") == "awaiting_follow_up"
    raw_follow_up_count = state.get("ai_intake_follow_up_count")
    try:
        follow_up_count = max(
            int(str(raw_follow_up_count)) if raw_follow_up_count is not None else 0,
            0,
        )
    except (TypeError, ValueError):
        follow_up_count = 0
    has_active_assignment = (
        db.query(InboxConversationAssignment.id)
        .filter(InboxConversationAssignment.conversation_id == conversation.id)
        .filter(InboxConversationAssignment.is_active.is_(True))
        .first()
        is not None
    )
    tags_value = (conversation.metadata_ or {}).get("tags")
    tags = (
        tuple(str(item)[:80] for item in tags_value[:10])
        if isinstance(tags_value, list)
        else ()
    )
    request = AiIntakeRequest(
        channel_type=payload.channel_type,
        provider=provider,
        account_scope=account_scope,
        inbound_message_id=str(payload.external_message_id or "local")[:255],
        body=body[:4000],
        conversation_id=conversation.id,
        recent_messages=_recent_intake_context(db, conversation_id=conversation.id),
        conversation_tags=tags,
        campaign_attributed=_campaign_attributed(
            {**dict(conversation.metadata_ or {}), **metadata}
        ),
        routing_allows_ai=routing_gate.ai_routing_allowed,
        created_conversation=created_conversation,
        has_active_assignment=has_active_assignment,
        awaiting_follow_up=awaiting_follow_up,
        follow_up_count=follow_up_count,
    )
    return request, ai_intake.prepare_async_intake(db, request)


def receive_inbound_channel(
    db: Session,
    payload: InboundChannelPayload,
) -> InboundChannelReceiveResult:
    channel_type = str(payload.channel_type or "").strip()
    if channel_type not in {item.value for item in InboxChannelType}:
        raise ValueError("Unsupported inbox channel_type")
    body = _message_body(payload.body)
    metadata = dict(payload.metadata or {})
    attachments = metadata.get("attachments")
    has_attachment = isinstance(attachments, (list, tuple)) and any(
        isinstance(item, dict) for item in attachments
    )
    if not body and not has_attachment:
        raise ValueError("Inbound message content is required")

    duplicate = _find_duplicate_message(
        db,
        channel_type=channel_type,
        external_message_id=payload.external_message_id,
    )
    if duplicate is not None:
        # A suppressed duplicate is the signature of a provider retry storm;
        # counting it here (the web-process webhook path) makes redelivery
        # pressure visible without any duplicate reaching an agent's inbox.
        from app.metrics import record_inbound_dedup_suppressed

        record_inbound_dedup_suppressed(channel_type)
        return InboundChannelReceiveResult(
            kind="duplicate",
            conversation_id=str(duplicate.conversation_id),
            message_id=str(duplicate.id),
            duplicate=True,
        )

    resolution = resolve_contact_context(
        db,
        channel_type=channel_type,
        contact_address=payload.contact_address,
        subscriber_id=payload.subscriber_id,
    )
    external_thread_id = payload.external_thread_id or _thread_id(
        channel_type, resolution.normalized_contact, payload.contact_address
    )
    _acquire_thread_lock(
        db,
        channel_type=channel_type,
        external_thread_id=external_thread_id,
    )
    # The fast duplicate check above can race the first delivery. Recheck after
    # the transaction-scoped thread lock so the database winner is observed.
    duplicate = _find_duplicate_message(
        db,
        channel_type=channel_type,
        external_message_id=payload.external_message_id,
    )
    if duplicate is not None:
        from app.metrics import record_inbound_dedup_suppressed

        record_inbound_dedup_suppressed(channel_type)
        return InboundChannelReceiveResult(
            kind="duplicate",
            conversation_id=str(duplicate.conversation_id),
            message_id=str(duplicate.id),
            duplicate=True,
        )
    received_at = payload.received_at or datetime.now(UTC)
    conversation = _find_open_conversation(
        db,
        channel_type=channel_type,
        external_thread_id=external_thread_id,
    )
    created_conversation = conversation is None
    if conversation is None:
        conversation_metadata: dict[str, object] = {
            "contact_resolution": resolution.as_metadata()
        }
        if contact_name := str(payload.contact_name or "").strip():
            conversation_metadata["contact_name"] = contact_name[:200]
            conversation_metadata["contact_name_source"] = "provider_observation"
        conversation = InboxConversation(
            subscriber_id=resolution.subscriber_id,
            channel_type=channel_type,
            status=InboxConversationStatus.open.value,
            subject=payload.subject or payload.contact_name,
            contact_address=resolution.normalized_contact or payload.contact_address,
            external_thread_id=external_thread_id,
            first_message_at=received_at,
            last_message_at=received_at,
            metadata_=conversation_metadata,
        )
        db.add(conversation)
        db.flush()
    else:
        conversation.last_message_at = received_at
        if resolution.subscriber_id and not conversation.subscriber_id:
            conversation.subscriber_id = resolution.subscriber_id
        conversation_metadata = dict(conversation.metadata_ or {})
        conversation_metadata["contact_resolution"] = resolution.as_metadata()
        if contact_name := str(payload.contact_name or "").strip():
            conversation_metadata["contact_name"] = contact_name[:200]
            conversation_metadata["contact_name_source"] = "provider_observation"
        conversation.metadata_ = conversation_metadata

    # The shared intake owner runs after normalization/idempotency and before
    # destination-team routing. It returns metadata only: queue position and
    # individual assignment remain outside this service.
    intake_request: AiIntakeRequest | None = None
    try:
        intake_request, intake_outcome = _classify_inbound(
            db,
            conversation=conversation,
            created_conversation=created_conversation,
            payload=payload,
            body=body,
            metadata=metadata,
        )
    except Exception as exc:
        logger.warning(
            "AI intake context or classification failed",
            extra={
                "event": "ai_intake_context_failure",
                "channel": channel_type,
                "error_type": type(exc).__name__,
            },
        )
        intake_outcome = AiIntakeOutcome(
            status=AiIntakeStatus.failed,
            reason=AiIntakeReason.context_error,
        )
    metadata.update(ai_intake.route_metadata(intake_outcome))
    ai_session_context = None
    if intake_request is not None:
        conversation_metadata = dict(conversation.metadata_ or {})
        conversation_metadata["ai_intake"] = ai_intake.conversation_state(
            intake_request, intake_outcome
        )
        conversation.metadata_ = conversation_metadata
        try:
            ai_session_context = ai_conversation_intake.ensure_session_for_outcome(
                db,
                conversation=conversation,
                outcome=intake_outcome,
                provider=intake_request.provider,
                account_scope=intake_request.account_scope,
                created_conversation=created_conversation,
            )
            if ai_session_context is not None:
                ai_conversation_intake.transition_conversation_status(
                    db,
                    conversation=conversation,
                    status=InboxConversationStatus.pending,
                    reason=team_inbox_status.InboxStatusReason.ai_intake_started,
                    source_id=f"ai-intake-started:{ai_session_context.session.id}",
                )
                ai_conversation_intake.mark_conversation_ai_metadata(
                    conversation,
                    session=ai_session_context.session,
                    active=True,
                )
        except Exception as exc:
            logger.warning(
                "AI intake session creation failed",
                extra={
                    "event": "ai_intake_session_failure",
                    "conversation_id": str(conversation.id),
                    "error_type": type(exc).__name__,
                },
            )
        cleaning_eligibility = ai_intake.evaluate_data_cleaning_eligibility(
            db,
            request=intake_request,
            primary_service_team_id=conversation.primary_service_team_id,
        )
        _record_data_cleaning_eligibility(conversation, cleaning_eligibility)
    else:
        _record_data_cleaning_eligibility(
            conversation,
            DataCleaningEligibility(
                eligible=False,
                reason=DataCleaningEligibilityReason.invalid_configuration,
            ),
        )

    # Resolve push-channel ownership before creating the message so WhatsApp,
    # Messenger and Instagram threads enter the normal team queue path.
    routing_decision = team_inbox_routing.resolve_channel_routing_decision(
        db,
        channel_type=channel_type,
        provider=str(metadata.get("provider") or "") or None,
        account_scope=str(
            metadata.get("provider_account_scope")
            or metadata.get("page_or_account_id")
            or metadata.get("phone_number_id")
            or ""
        )
        or None,
        fallback_service_team_id=(
            payload.fallback_service_team_id
            or team_inbox_routing.default_service_team_id(db)
        ),
        metadata=metadata,
    )
    logger.info(
        "AI intake destination resolved",
        extra={
            "event": "ai_intake_destination_resolved",
            "channel": channel_type,
            "status": intake_outcome.status.value,
            "reason": intake_outcome.reason.value,
            "destination_team_id": routing_decision.primary_service_team_id,
            "routing_reason": routing_decision.reason,
            "duration_ms": intake_outcome.duration_ms,
        },
    )
    participant_ids = [
        team_id
        for team_id in (
            routing_decision.primary_service_team_id,
            routing_decision.channel_service_team_id,
        )
        if team_id
    ]
    routing_plan = team_inbox_routing.EmailTeamRoutingPlan(
        primary_service_team_id=routing_decision.primary_service_team_id,
        participant_service_team_ids=list(dict.fromkeys(participant_ids)),
        matches=[],
        unmatched_recipients=[],
    )
    team_inbox_routing.apply_email_routing_plan(
        db,
        conversation=conversation,
        plan=routing_plan,
    )

    metadata["contact_resolution"] = resolution.as_metadata()
    metadata["routing"] = {
        "primary_service_team_id": routing_decision.primary_service_team_id,
        "channel_service_team_id": routing_decision.channel_service_team_id,
        "ai_service_team_id": routing_decision.ai_service_team_id,
        "channel_route_id": routing_decision.channel_route_id,
        "ai_route_id": routing_decision.ai_route_id,
        "ai_routing_allowed": routing_decision.ai_routing_allowed,
        "ai_intent_key": routing_decision.ai_intent_key,
        "ai_confidence": routing_decision.ai_confidence,
        "reason": routing_decision.reason,
    }
    metadata["ai_intake_destination_team_id"] = routing_decision.primary_service_team_id
    conversation_metadata = dict(conversation.metadata_ or {})
    intake_state = conversation_metadata.get("ai_intake")
    if not isinstance(intake_state, dict):
        intake_state = ai_intake.route_metadata(intake_outcome)
        intake_state.update(
            {
                "status": intake_outcome.status.value,
                "reason": intake_outcome.reason.value,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
    intake_state["destination_team_id"] = routing_decision.primary_service_team_id
    intake_state["routing_reason"] = routing_decision.reason
    conversation_metadata["ai_intake"] = intake_state
    conversation.metadata_ = conversation_metadata
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=channel_type,
        direction=InboxMessageDirection.inbound.value,
        subject=payload.subject,
        body=body,
        external_message_id=payload.external_message_id,
        external_thread_id=external_thread_id,
        from_address=resolution.normalized_contact or payload.contact_address,
        received_at=received_at,
        metadata_=metadata,
    )
    db.add(message)
    db.flush()
    # Shadow projection: record which endpoints took part. Nothing reads it for
    # a threading or export decision yet, so a failure here must not cost us an
    # ingested message.
    team_inbox_participants.record_message_participants(
        db, conversation=conversation, message=message
    )
    team_inbox_media.promote_message_attachments(
        db,
        message=message,
        provider=str(metadata.get("provider") or "") or None,
    )
    conversation.last_message_at = received_at
    # A conversation snoozed "until the customer replies" wakes here — this is
    # the reply.
    team_inbox_operations.wake_on_inbound(db, conversation=conversation)
    if created_conversation:
        team_inbox_automation.execute_matching_rules(
            db,
            conversation=conversation,
            trigger=InboxAutomationTrigger.conversation_created,
        )
    team_inbox_automation.execute_matching_rules(
        db,
        conversation=conversation,
        trigger=InboxAutomationTrigger.inbound_message_received,
    )
    team_inbox_realtime.publish_conversation_event(
        db,
        str(conversation.id),
        event_type=EventType.MESSAGE_NEW,
        payload=team_inbox_realtime.message_event_payload(
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            body=message.body,
            direction=message.direction,
            channel_type=message.channel_type,
            created_at=message.created_at,
            extra={"sender_type": "visitor", "from_customer": True},
        ),
    )
    team_inbox_realtime.publish_queue_event(
        db,
        conversation_id=str(conversation.id),
        created=created_conversation,
    )
    return InboundChannelReceiveResult(
        kind="received",
        conversation_id=str(conversation.id),
        message_id=str(message.id),
        duplicate=False,
        subscriber_id=str(resolution.subscriber_id)
        if resolution.subscriber_id
        else None,
        reseller_id=str(resolution.reseller_id) if resolution.reseller_id else None,
        resolution_status=resolution.status,
    )


def receive_whatsapp_webhook(
    db: Session,
    *,
    provider: str,
    payload: dict,
    fallback_service_team_id: str | UUID | None = None,
) -> InboundChannelReceiveResult:
    normalized = whatsapp_runtime.normalize_inbound_webhook(
        provider=provider,
        payload=payload,
    )
    return receive_inbound_channel(
        db,
        InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address=str(normalized.get("from") or ""),
            contact_name=(
                str(normalized["contact_name"])
                if normalized.get("contact_name")
                else None
            ),
            body=_message_body(normalized.get("text")),
            external_message_id=(
                str(normalized.get("external_id"))
                if normalized.get("external_id")
                else None
            ),
            fallback_service_team_id=fallback_service_team_id,
            metadata={
                "provider": normalized.get("provider"),
                "provider_account_scope": (
                    payload.get("phone_number_id")
                    or payload.get("display_phone_number")
                    or payload.get("provider_account_scope")
                ),
                "attachments": payload.get("attachments")
                if isinstance(payload.get("attachments"), list)
                else [],
            },
        ),
    )


def receive_result_payload(
    result: InboundChannelReceiveResult,
) -> dict[str, object]:
    return {
        "kind": result.kind,
        "conversation_id": result.conversation_id,
        "message_id": result.message_id,
        "resolution_status": result.resolution_status,
        "subscriber_id": result.subscriber_id,
        "reseller_id": result.reseller_id,
    }


def receive_whatsapp_webhook_batch_committed(
    db: Session,
    *,
    provider: str,
    payloads: list[dict[str, Any]],
    status_items: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    from app.services import team_inbox_observations, team_inbox_processing

    provider_name = team_inbox_observations.InboxProvider(provider)
    message_results: list[dict[str, object]] = []
    receipt_results: list[dict[str, object]] = []
    for payload in payloads:
        message = payload.get("message")
        message_data = message if isinstance(message, dict) else {}
        external_message_id = str(message_data.get("id") or "").strip()
        observed_at = payload.get("observed_at")
        if not isinstance(observed_at, datetime):
            observed_at = datetime.fromtimestamp(0, tz=UTC)
        metadata = payload.get("metadata")
        metadata_data = metadata if isinstance(metadata, dict) else {}
        provider_scope = str(
            metadata_data.get("phone_number_id")
            or metadata_data.get("display_phone_number")
            or "default"
        ).strip()
        if not external_message_id:
            evidence = "|".join(
                (
                    str(message_data.get("from") or ""),
                    str(message_data.get("text") or ""),
                    observed_at.astimezone(UTC).isoformat(),
                )
            )
            external_message_id = (
                "derived:" + hashlib.sha256(evidence.encode()).hexdigest()
            )
        context = CommandContext.system(
            actor=f"transport:{provider}",
            scope="team-inbox:provider-observation",
            reason="record normalized inbound message observation",
            idempotency_key=external_message_id,
        )
        recorded = team_inbox_observations.record_provider_observation(
            db,
            team_inbox_observations.RecordProviderObservationCommand(
                context=context,
                provider=provider_name,
                provider_account_scope=provider_scope,
                provider_event_id=f"message:{external_message_id}",
                kind=InboxObservationKind.message,
                channel_type=InboxChannelType.whatsapp,
                external_message_id=external_message_id,
                observed_at=observed_at,
                payload=team_inbox_observations.InboundMessageObservation(
                    contact_address=str(message_data.get("from") or ""),
                    body=_message_body(message_data.get("text")),
                    contact_name=(
                        str(payload["contact_name"])
                        if payload.get("contact_name")
                        else None
                    ),
                    campaign_attributed=_campaign_attributed(
                        {**metadata_data, **payload}
                    ),
                    attachments=tuple(
                        _inbound_attachment_observation(item)
                        for item in (payload.get("attachments") or ())
                        if isinstance(item, dict)
                    ),
                ),
            ),
        )
        processed = team_inbox_processing.process_provider_observation(
            db,
            observation_id=recorded.observation_id,
            context=CommandContext.system(
                actor="system:team-inbox-observation-processor",
                scope="team-inbox:provider-consequence",
                reason="resolve committed inbound message observation",
                idempotency_key=str(recorded.observation_id),
            ),
        )
        _run_sales_lead_intake_after_commit(
            db,
            conversation_id=processed.conversation_id,
            message_id=processed.message_id,
        )
        message_results.append(
            {
                "kind": processed.consequence_kind
                or (
                    "duplicate"
                    if processed.outcome.value == "already_processed"
                    else "processed"
                ),
                "conversation_id": str(processed.conversation_id)
                if processed.conversation_id
                else None,
                "message_id": str(processed.message_id)
                if processed.message_id
                else None,
                "resolution_status": processed.resolution_status or "unmatched",
                "subscriber_id": str(processed.subscriber_id)
                if processed.subscriber_id
                else None,
                "reseller_id": str(processed.reseller_id)
                if processed.reseller_id
                else None,
            }
        )

    for item in status_items or []:
        external_message_id = str(item.get("message_id") or "").strip()
        clean_status = str(item.get("status") or "").strip().lower()
        raw_timestamp = item.get("timestamp")
        try:
            observed_at = datetime.fromtimestamp(float(str(raw_timestamp)), tz=UTC)
        except (TypeError, ValueError, OSError):
            observed_at = datetime.fromtimestamp(0, tz=UTC)
        raw_errors = item.get("errors")
        errors: list[object] = raw_errors if isinstance(raw_errors, list) else []
        error_codes = tuple(
            str(error.get("code"))[:80]
            for error in errors
            if isinstance(error, dict) and error.get("code") is not None
        )
        provider_event_id = (
            f"receipt:{external_message_id}:{clean_status}:"
            f"{observed_at.astimezone(UTC).isoformat()}"
        )
        recorded = team_inbox_observations.record_provider_observation(
            db,
            team_inbox_observations.RecordProviderObservationCommand(
                context=CommandContext.system(
                    actor=f"transport:{provider}",
                    scope="team-inbox:provider-observation",
                    reason="record normalized provider delivery receipt",
                    idempotency_key=provider_event_id,
                ),
                provider=provider_name,
                provider_account_scope=str(
                    item.get("provider_account_scope") or "default"
                ),
                provider_event_id=provider_event_id,
                kind=InboxObservationKind.delivery_receipt,
                channel_type=InboxChannelType.whatsapp,
                external_message_id=external_message_id,
                observed_at=observed_at,
                payload=team_inbox_observations.DeliveryReceiptObservation(
                    status=clean_status,
                    recipient_id=(
                        str(item["recipient_id"]) if item.get("recipient_id") else None
                    ),
                    error_codes=error_codes,
                ),
            ),
        )
        processed = team_inbox_processing.process_provider_observation(
            db,
            observation_id=recorded.observation_id,
            context=CommandContext.system(
                actor="system:team-inbox-observation-processor",
                scope="team-inbox:provider-consequence",
                reason="reconcile committed provider delivery receipt",
                idempotency_key=str(recorded.observation_id),
            ),
        )
        receipt_result: dict[str, object] = {
            "kind": processed.consequence_kind
            or (
                "duplicate"
                if processed.outcome.value == "already_processed"
                else "processed"
            ),
            "provider_message_id": external_message_id,
            "status": clean_status,
        }
        if processed.message_id:
            receipt_result["message_id"] = str(processed.message_id)
        receipt_results.append(receipt_result)
    return message_results, receipt_results


def receive_whatsapp_webhook_batch(
    db: Session,
    *,
    provider: str,
    payloads: list[dict[str, Any]],
    status_items: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Apply verified observations without taking transaction ownership."""

    from app.services import team_inbox_delivery_receipts

    results = [
        receive_result_payload(
            receive_whatsapp_webhook(db, provider=provider, payload=payload)
        )
        for payload in payloads
    ]
    statuses = [
        team_inbox_delivery_receipts.apply_whatsapp_delivery_status(db, item)
        for item in (status_items or [])
    ]
    return results, statuses


def receive_inbound_channel_batch_committed(
    db: Session,
    payloads: list[InboundChannelPayload],
) -> list[dict[str, object]]:
    from app.services import team_inbox_observations, team_inbox_processing

    results: list[dict[str, object]] = []
    for payload in payloads:
        observed_at = payload.received_at or datetime.now(UTC)
        evidence = "|".join(
            (
                payload.channel_type,
                payload.contact_address,
                payload.body,
                observed_at.astimezone(UTC).isoformat(),
            )
        )
        external_message_id = payload.external_message_id or (
            "derived:" + hashlib.sha256(evidence.encode()).hexdigest()
        )
        metadata = payload.metadata or {}
        provider_value = str(metadata.get("provider") or "meta_social")
        provider = team_inbox_observations.InboxProvider(provider_value)
        provider_scope = str(
            metadata.get("provider_account_scope")
            or metadata.get("provider_account_id")
            or metadata.get("page_or_account_id")
            or metadata.get("account_scope")
            or "default"
        )
        contact_profile = metadata.get("contact_profile")
        raw_attachments = metadata.get("attachments")
        attachments = (
            raw_attachments if isinstance(raw_attachments, (list, tuple)) else ()
        )
        recorded = team_inbox_observations.record_provider_observation(
            db,
            team_inbox_observations.RecordProviderObservationCommand(
                context=CommandContext.system(
                    actor=f"transport:{provider.value}",
                    scope="team-inbox:provider-observation",
                    reason="record normalized inbound channel observation",
                    idempotency_key=external_message_id,
                ),
                provider=provider,
                provider_account_scope=provider_scope,
                provider_event_id=f"message:{external_message_id}",
                kind=InboxObservationKind.message,
                channel_type=InboxChannelType(payload.channel_type),
                external_message_id=external_message_id,
                observed_at=observed_at,
                payload=team_inbox_observations.InboundMessageObservation(
                    contact_address=payload.contact_address,
                    body=payload.body,
                    contact_name=payload.contact_name,
                    subject=payload.subject,
                    external_thread_id=payload.external_thread_id,
                    subscriber_id=coerce_uuid(payload.subscriber_id),
                    fallback_service_team_id=coerce_uuid(
                        payload.fallback_service_team_id
                    ),
                    campaign_attributed=_campaign_attributed(metadata),
                    provider_account_id=(
                        str(metadata["provider_account_id"])
                        if metadata.get("provider_account_id")
                        else None
                    ),
                    external_account_id=(
                        str(metadata["external_account_id"])
                        if metadata.get("external_account_id")
                        else None
                    ),
                    page_id=str(metadata["page_id"])
                    if metadata.get("page_id")
                    else None,
                    instagram_account_id=(
                        str(metadata["instagram_account_id"])
                        if metadata.get("instagram_account_id")
                        else None
                    ),
                    provider_comment_id=(
                        str(metadata["provider_comment_id"])
                        if metadata.get("provider_comment_id")
                        else None
                    ),
                    comment_id=str(metadata["comment_id"])
                    if metadata.get("comment_id")
                    else None,
                    post_id=str(metadata["post_id"])
                    if metadata.get("post_id")
                    else None,
                    media_id=str(metadata["media_id"])
                    if metadata.get("media_id")
                    else None,
                    parent_provider_comment_id=(
                        str(metadata["parent_provider_comment_id"])
                        if metadata.get("parent_provider_comment_id")
                        else None
                    ),
                    commenter_id=str(metadata["commenter_id"])
                    if metadata.get("commenter_id")
                    else None,
                    commenter_name=str(metadata["commenter_name"])
                    if metadata.get("commenter_name")
                    else None,
                    commenter_username=(
                        str(metadata["commenter_username"])
                        if metadata.get("commenter_username")
                        else None
                    ),
                    surface=str(metadata["surface"])
                    if metadata.get("surface")
                    else None,
                    permalink_url=(
                        str(metadata["permalink_url"])
                        if metadata.get("permalink_url")
                        else None
                    ),
                    media_url=str(metadata["media_url"])
                    if metadata.get("media_url")
                    else None,
                    contact_profile=(
                        {
                            "display_name": (
                                str(contact_profile["display_name"])
                                if contact_profile.get("display_name")
                                else None
                            ),
                            "username": (
                                str(contact_profile["username"])
                                if contact_profile.get("username")
                                else None
                            ),
                            "profile_pic": (
                                str(contact_profile["profile_pic"])
                                if contact_profile.get("profile_pic")
                                else None
                            ),
                        }
                        if isinstance(contact_profile, dict)
                        else None
                    ),
                    attachments=tuple(
                        _inbound_attachment_observation(item)
                        for item in attachments
                        if isinstance(item, dict)
                    ),
                ),
            ),
        )
        processed = team_inbox_processing.process_provider_observation(
            db,
            observation_id=recorded.observation_id,
            context=CommandContext.system(
                actor="system:team-inbox-observation-processor",
                scope="team-inbox:provider-consequence",
                reason="resolve committed inbound channel observation",
                idempotency_key=str(recorded.observation_id),
            ),
        )
        _run_sales_lead_intake_after_commit(
            db,
            conversation_id=processed.conversation_id,
            message_id=processed.message_id,
        )
        results.append(
            {
                "kind": processed.consequence_kind or "duplicate",
                "conversation_id": str(processed.conversation_id)
                if processed.conversation_id
                else None,
                "message_id": str(processed.message_id)
                if processed.message_id
                else None,
                "resolution_status": processed.resolution_status or "unmatched",
                "subscriber_id": str(processed.subscriber_id)
                if processed.subscriber_id
                else None,
                "reseller_id": str(processed.reseller_id)
                if processed.reseller_id
                else None,
            }
        )
    return results
