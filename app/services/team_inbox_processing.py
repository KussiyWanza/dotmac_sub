"""Consequence coordinator for already-committed Team Inbox observations."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxChannelType,
    InboxMessage,
    InboxObservationKind,
    InboxObservationStatus,
    InboxProviderObservation,
)
from app.services import (
    team_inbox_channel_receive,
    team_inbox_delivery_receipts,
    team_inbox_media,
    team_inbox_observations,
    team_inbox_receive,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "communications.team_inbox_processing"
_PROCESS_OBSERVATION = OwnerCommandDefinition(
    owner=OWNER,
    concern="provider observation consequence coordination",
    name="process_team_inbox_provider_observation",
)


def _message_payload(
    row: InboxProviderObservation,
) -> team_inbox_observations.InboundMessageObservation:
    data = row.normalized_payload
    raw_attachments = data.get("attachments")
    attachments = raw_attachments if isinstance(raw_attachments, list) else []
    return team_inbox_observations.InboundMessageObservation(
        contact_address=str(data.get("contact_address") or ""),
        body=str(data.get("body") or ""),
        contact_name=str(data["contact_name"]) if data.get("contact_name") else None,
        subject=str(data["subject"]) if data.get("subject") else None,
        external_thread_id=(
            str(data["external_thread_id"]) if data.get("external_thread_id") else None
        ),
        subscriber_id=UUID(str(data["subscriber_id"]))
        if data.get("subscriber_id")
        else None,
        fallback_service_team_id=UUID(str(data["fallback_service_team_id"]))
        if data.get("fallback_service_team_id")
        else None,
        to_addresses=tuple(str(item) for item in data.get("to_addresses") or ()),
        cc_addresses=tuple(str(item) for item in data.get("cc_addresses") or ()),
        in_reply_to=str(data["in_reply_to"]) if data.get("in_reply_to") else None,
        references=str(data["references"]) if data.get("references") else None,
        smtp_probe=data.get("smtp_probe") is True,
        authentication=(
            data["authentication"]
            if isinstance(data.get("authentication"), dict)
            else None
        ),
        attachments=tuple(
            team_inbox_observations.InboundAttachmentObservation(
                asset_type=str(item.get("asset_type") or "file"),
                file_name=str(item["file_name"]) if item.get("file_name") else None,
                mime_type=str(item["mime_type"]) if item.get("mime_type") else None,
                provider_media_id=(
                    str(item["provider_media_id"])
                    if item.get("provider_media_id")
                    else None
                ),
                source_url=str(item["source_url"]) if item.get("source_url") else None,
                caption=str(item["caption"]) if item.get("caption") else None,
                file_size=int(item["file_size"])
                if item.get("file_size") is not None
                else None,
            )
            for item in attachments
            if isinstance(item, dict)
        ),
    )


def process_provider_observation(
    db: Session,
    *,
    observation_id: UUID,
    context: CommandContext,
) -> team_inbox_observations.ProviderObservationOutcome:
    """Resolve one already-committed observation into authoritative Inbox state."""

    def operation() -> team_inbox_observations.ProviderObservationOutcome:
        row = db.execute(
            select(InboxProviderObservation)
            .where(InboxProviderObservation.id == observation_id)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise team_inbox_observations.TeamInboxObservationError(
                code=f"{OWNER}.observation_not_found",
                message="Inbox provider observation was not found.",
                details={"observation_id": str(observation_id)},
            )
        if row.processing_status == InboxObservationStatus.processed.value:
            return team_inbox_observations.ProviderObservationOutcome(
                observation_id=row.id,
                outcome=team_inbox_observations.ObservationProcessingOutcome.already_processed,
                conversation_id=row.conversation_id,
                message_id=row.message_id,
                processing_status=InboxObservationStatus.processed,
            )

        consequence_kind: str | None
        subscriber_id: UUID | None
        reseller_id: UUID | None
        resolution_status: str | None
        observed_at = (
            row.observed_at
            if row.observed_at.tzinfo is not None
            else row.observed_at.replace(tzinfo=UTC)
        )
        if row.observation_kind == InboxObservationKind.message.value:
            payload = _message_payload(row)
            inbound_result: (
                team_inbox_receive.InboundEmailReceiveResult
                | team_inbox_channel_receive.InboundChannelReceiveResult
            )
            if row.channel_type == InboxChannelType.email.value:
                email_result = team_inbox_receive.receive_inbound_email(
                    db,
                    team_inbox_receive.InboundEmailPayload(
                        from_address=payload.contact_address,
                        to_addresses=list(payload.to_addresses),
                        cc_addresses=list(payload.cc_addresses),
                        subject=payload.subject,
                        body=payload.body,
                        message_id=row.external_message_id,
                        in_reply_to=payload.in_reply_to,
                        references=payload.references,
                        received_at=observed_at,
                        fallback_service_team_id=payload.fallback_service_team_id,
                        metadata={
                            "provider": row.provider,
                            "observation_id": str(row.id),
                            "smtp_probe": "team_inbox_smtp_e2e"
                            if payload.smtp_probe
                            else None,
                            # Carried onto the message so the evidence sits
                            # beside the claim it would be used to judge.
                            "authentication": payload.authentication,
                            "attachments": [
                                {
                                    "type": item.asset_type,
                                    "filename": item.file_name,
                                    "mime_type": item.mime_type,
                                    "id": item.provider_media_id,
                                    "url": item.source_url,
                                    "caption": item.caption,
                                    "file_size": item.file_size,
                                }
                                for item in payload.attachments
                            ],
                        },
                    ),
                )
                message = db.get(InboxMessage, UUID(email_result.message_id))
                if message is not None and payload.attachments:
                    team_inbox_media.promote_message_attachments(
                        db, message=message, provider=row.provider
                    )
                inbound_result = email_result
            else:
                inbound_result = team_inbox_channel_receive.receive_inbound_channel(
                    db,
                    team_inbox_channel_receive.InboundChannelPayload(
                        channel_type=row.channel_type,
                        contact_address=payload.contact_address,
                        body=payload.body,
                        contact_name=payload.contact_name,
                        external_message_id=row.external_message_id,
                        external_thread_id=payload.external_thread_id,
                        subject=payload.subject,
                        received_at=observed_at,
                        subscriber_id=payload.subscriber_id,
                        fallback_service_team_id=payload.fallback_service_team_id,
                        metadata={
                            "provider": row.provider,
                            "observation_id": str(row.id),
                            "attachments": [
                                {
                                    "type": item.asset_type,
                                    "filename": item.file_name,
                                    "mime_type": item.mime_type,
                                    "id": item.provider_media_id,
                                    "url": item.source_url,
                                    "caption": item.caption,
                                    "file_size": item.file_size,
                                }
                                for item in payload.attachments
                            ],
                        },
                    ),
                )
            row.conversation_id = UUID(inbound_result.conversation_id)
            row.message_id = UUID(inbound_result.message_id)
            consequence_kind = inbound_result.kind
            subscriber_value = getattr(inbound_result, "subscriber_id", None)
            reseller_value = getattr(inbound_result, "reseller_id", None)
            subscriber_id = UUID(subscriber_value) if subscriber_value else None
            reseller_id = UUID(reseller_value) if reseller_value else None
            resolution_status = getattr(inbound_result, "resolution_status", None)
        else:
            data = row.normalized_payload
            receipt_result = team_inbox_delivery_receipts.apply_delivery_receipt(
                db,
                provider=row.provider,
                provider_message_id=str(row.external_message_id or ""),
                status=str(data.get("status") or ""),
                observed_at=observed_at,
                recipient_id=str(data["recipient_id"])
                if data.get("recipient_id")
                else None,
                error_codes=tuple(str(item) for item in data.get("error_codes") or ()),
                observation_id=row.id,
            )
            message_id = receipt_result.get("message_id")
            if isinstance(message_id, str):
                row.message_id = UUID(message_id)
            consequence_kind = str(receipt_result.get("kind") or "") or None
            subscriber_id = None
            reseller_id = None
            resolution_status = None
        row.processing_status = InboxObservationStatus.processed.value
        row.processed_at = datetime.now(UTC)
        row.error_code = None
        db.flush()
        return team_inbox_observations.ProviderObservationOutcome(
            observation_id=row.id,
            outcome=team_inbox_observations.ObservationProcessingOutcome.processed,
            conversation_id=row.conversation_id,
            message_id=row.message_id,
            processing_status=InboxObservationStatus.processed,
            consequence_kind=consequence_kind,
            subscriber_id=subscriber_id,
            reseller_id=reseller_id,
            resolution_status=resolution_status,
        )

    return execute_owner_command(
        db,
        definition=_PROCESS_OBSERVATION,
        context=context,
        operation=operation,
    )
