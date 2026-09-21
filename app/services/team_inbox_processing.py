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
from app.schemas.fiber_inquiry import FiberInquiryRequest
from app.services import (
    team_inbox_channel_receive,
    team_inbox_delivery_receipts,
    team_inbox_fiber_receive,
    team_inbox_media,
    team_inbox_observations,
    team_inbox_receive,
)
from app.services.file_storage import PreparedFileUpload, file_uploads
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
_MARK_OBSERVATION_PROCESSING_FAILED = OwnerCommandDefinition(
    owner=OWNER,
    concern="provider observation consequence coordination",
    name="mark_team_inbox_provider_observation_processing_failed",
)


def _attachment_observation(
    item: dict[str, object],
) -> team_inbox_observations.InboundAttachmentObservation:
    location_data = item.get("location")
    location = (
        team_inbox_observations.inbound_location_observation(
            latitude=location_data.get("latitude"),
            longitude=location_data.get("longitude"),
            name=location_data.get("name"),
            address=location_data.get("address"),
        )
        if isinstance(location_data, dict)
        else None
    )
    prepared_data = item.get("prepared_file")
    prepared_file = (
        team_inbox_observations.InboundPreparedFileObservation(
            entity_type=str(prepared_data["entity_type"]),
            entity_id=str(prepared_data["entity_id"]),
            original_filename=str(prepared_data["original_filename"]),
            storage_key=str(prepared_data["storage_key"]),
            file_size=int(str(prepared_data["file_size"])),
            content_type=str(prepared_data["content_type"]),
            checksum=str(prepared_data["checksum"])
            if prepared_data.get("checksum")
            else None,
        )
        if isinstance(prepared_data, dict)
        else None
    )
    return team_inbox_observations.InboundAttachmentObservation(
        asset_type=str(item.get("asset_type") or "file"),
        file_name=str(item["file_name"]) if item.get("file_name") else None,
        mime_type=str(item["mime_type"]) if item.get("mime_type") else None,
        provider_media_id=(
            str(item["provider_media_id"]) if item.get("provider_media_id") else None
        ),
        source_url=str(item["source_url"]) if item.get("source_url") else None,
        content_base64=(
            str(item["content_base64"]) if item.get("content_base64") else None
        ),
        caption=str(item["caption"]) if item.get("caption") else None,
        file_size=(
            int(str(item["file_size"])) if item.get("file_size") is not None else None
        ),
        download_status=(
            str(item["download_status"]) if item.get("download_status") else None
        ),
        location=location,
        prepared_file=prepared_file,
    )


def _attachment_metadata(
    item: team_inbox_observations.InboundAttachmentObservation,
) -> dict[str, object]:
    return {
        "type": item.asset_type,
        "filename": item.file_name,
        "mime_type": item.mime_type,
        "id": item.provider_media_id,
        "url": item.source_url,
        "source_url": item.source_url,
        "content_base64": item.content_base64,
        "caption": item.caption,
        "file_size": item.file_size,
        "download_status": item.download_status,
        "location": item.location.to_metadata() if item.location else None,
    }


def _materialize_email_attachment_metadata(
    db: Session,
    attachments: tuple[team_inbox_observations.InboundAttachmentObservation, ...],
    *,
    provider: str,
) -> list[dict[str, object]]:
    metadata: list[dict[str, object]] = []
    for item in attachments:
        values = _attachment_metadata(item)
        values["provider"] = provider
        prepared = item.prepared_file
        if prepared is not None:
            stored = file_uploads.stage_prepared_upload(
                db=db,
                prepared=PreparedFileUpload(
                    owner_subscriber_id=None,
                    entity_type=prepared.entity_type,
                    entity_id=prepared.entity_id,
                    original_filename=prepared.original_filename,
                    storage_key=prepared.storage_key,
                    file_size=prepared.file_size,
                    content_type=prepared.content_type,
                    checksum=prepared.checksum,
                    uploaded_by=None,
                ),
            )
            values.update(
                {
                    "filename": stored.original_filename,
                    "mime_type": stored.content_type,
                    "file_size": stored.file_size,
                    "storage_url": stored.storage_key_or_relative_path,
                    "stored_file_id": str(stored.id),
                    "checksum_sha256": stored.checksum,
                    "download_status": "stored",
                }
            )
        metadata.append(values)
    return metadata


def _message_payload(
    row: InboxProviderObservation,
) -> team_inbox_observations.InboundMessageObservation:
    data = row.normalized_payload
    raw_attachments = data.get("attachments")
    attachments = raw_attachments if isinstance(raw_attachments, list) else []
    return team_inbox_observations.InboundMessageObservation(
        contact_address=str(data.get("contact_address") or ""),
        body=str(data.get("body") or ""),
        body_text=str(data["body_text"]) if data.get("body_text") else None,
        html_body=str(data["html_body"]) if data.get("html_body") else None,
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
        smtp_probe_id=(
            str(data["smtp_probe_id"]) if data.get("smtp_probe_id") else None
        ),
        campaign_attributed=data.get("campaign_attributed") is True,
        authentication=(
            data["authentication"]
            if isinstance(data.get("authentication"), dict)
            else None
        ),
        provider_account_id=(
            str(data["provider_account_id"])
            if data.get("provider_account_id")
            else None
        ),
        external_account_id=(
            str(data["external_account_id"])
            if data.get("external_account_id")
            else None
        ),
        page_id=str(data["page_id"]) if data.get("page_id") else None,
        instagram_account_id=(
            str(data["instagram_account_id"])
            if data.get("instagram_account_id")
            else None
        ),
        provider_comment_id=(
            str(data["provider_comment_id"])
            if data.get("provider_comment_id")
            else None
        ),
        comment_id=str(data["comment_id"]) if data.get("comment_id") else None,
        post_id=str(data["post_id"]) if data.get("post_id") else None,
        media_id=str(data["media_id"]) if data.get("media_id") else None,
        parent_provider_comment_id=(
            str(data["parent_provider_comment_id"])
            if data.get("parent_provider_comment_id")
            else None
        ),
        commenter_id=str(data["commenter_id"]) if data.get("commenter_id") else None,
        commenter_name=(
            str(data["commenter_name"]) if data.get("commenter_name") else None
        ),
        commenter_username=(
            str(data["commenter_username"]) if data.get("commenter_username") else None
        ),
        surface=str(data["surface"]) if data.get("surface") else None,
        permalink_url=(
            str(data["permalink_url"]) if data.get("permalink_url") else None
        ),
        media_url=str(data["media_url"]) if data.get("media_url") else None,
        contact_profile=(
            {
                "display_name": (
                    str(data["contact_profile"]["display_name"])
                    if data["contact_profile"].get("display_name")
                    else None
                ),
                "username": (
                    str(data["contact_profile"]["username"])
                    if data["contact_profile"].get("username")
                    else None
                ),
                "profile_pic": (
                    str(data["contact_profile"]["profile_pic"])
                    if data["contact_profile"].get("profile_pic")
                    else None
                ),
            }
            if isinstance(data.get("contact_profile"), dict)
            else None
        ),
        attachments=tuple(
            _attachment_observation(item)
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
            message = db.get(InboxMessage, row.message_id) if row.message_id else None
            metadata = message.metadata_ if message and message.metadata_ else {}
            lead_value = metadata.get("lead_id")
            contact_resolution = metadata.get("contact_resolution")
            return team_inbox_observations.ProviderObservationOutcome(
                observation_id=row.id,
                outcome=team_inbox_observations.ObservationProcessingOutcome.already_processed,
                conversation_id=row.conversation_id,
                message_id=row.message_id,
                processing_status=InboxObservationStatus.processed,
                resolution_status=(
                    str(contact_resolution.get("status") or "unmatched")
                    if isinstance(contact_resolution, dict)
                    else None
                ),
                lead_id=UUID(str(lead_value)) if lead_value else None,
                reference=(
                    str(metadata["reference"]) if metadata.get("reference") else None
                ),
                coverage=(
                    {
                        "status": str(metadata["coverage"]["status"]),
                        "summary": str(metadata["coverage"]["summary"]),
                    }
                    if isinstance(metadata.get("coverage"), dict)
                    else None
                ),
            )

        consequence_kind: str | None
        subscriber_id: UUID | None
        reseller_id: UUID | None
        resolution_status: str | None
        lead_id: UUID | None
        reference: str | None
        coverage: dict[str, str] | None
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
                | team_inbox_fiber_receive.FiberInquiryReceiveResult
            )
            if row.channel_type == InboxChannelType.website_fiber.value:
                data = row.normalized_payload
                request_data: dict[str, object] = {
                    "form_version": str(data.get("form_version") or ""),
                    "full_name": str(data.get("full_name") or ""),
                    "phone": str(data["phone"]) if data.get("phone") else None,
                    "email": str(data["email"]) if data.get("email") else None,
                    "interest": str(data.get("interest") or ""),
                    "message": (str(data["message"]) if data.get("message") else None),
                    "submitted_at": observed_at,
                }
                if request_data["form_version"] == "fiber-coverage-v1":
                    request_data.update(
                        {
                            "attribution": {
                                "journey_id": data.get("journey_id"),
                                "utm_source": data.get("utm_source"),
                                "utm_medium": data.get("utm_medium"),
                                "utm_campaign": data.get("utm_campaign"),
                                "utm_content": data.get("utm_content"),
                                "utm_term": data.get("utm_term"),
                                "campaign_id": data.get("external_campaign_id"),
                                "ad_set_id": data.get("external_ad_set_id"),
                                "ad_id": data.get("external_ad_id"),
                                "click_id": data.get("external_click_id"),
                                "landing_path": data.get("landing_path"),
                                "captured_at": data.get("captured_at"),
                            },
                            "location": {
                                "address": data.get("address"),
                                "area": data.get("area"),
                                "latitude": data.get("latitude"),
                                "longitude": data.get("longitude"),
                            },
                            "selected_plan": {"name": data.get("selected_plan_name")},
                        }
                    )
                inbound_result = team_inbox_receive.receive_fiber_inquiry(
                    db,
                    payload=FiberInquiryRequest.model_validate(
                        request_data, strict=False
                    ),
                    delivery_id=str(row.external_message_id or ""),
                    site_id=row.provider_account_scope,
                    observation_id=row.id,
                    integration_inbox_id=UUID(str(data["integration_inbox_id"])),
                    context=context,
                )
            elif row.channel_type == InboxChannelType.email.value:
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
                            "campaign_attributed": payload.campaign_attributed,
                            "smtp_probe": "team_inbox_smtp_e2e"
                            if payload.smtp_probe
                            else None,
                            "smtp_probe_id": payload.smtp_probe_id,
                            # Carried onto the message so the evidence sits
                            # beside the claim it would be used to judge.
                            "authentication": payload.authentication,
                            "body_text": payload.body_text or payload.body,
                            "html_body": payload.html_body,
                            "attachments": _materialize_email_attachment_metadata(
                                db, payload.attachments, provider=row.provider
                            ),
                        },
                    ),
                )
                message = db.get(InboxMessage, UUID(email_result.message_id))
                if message is not None and payload.attachments:
                    team_inbox_media.promote_message_attachments(db, message=message)
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
                            "provider_account_id": payload.provider_account_id,
                            "provider_account_scope": row.provider_account_scope,
                            "external_account_id": payload.external_account_id,
                            "page_id": payload.page_id,
                            "instagram_account_id": payload.instagram_account_id,
                            "provider_comment_id": payload.provider_comment_id,
                            "comment_id": payload.comment_id,
                            "post_id": payload.post_id,
                            "media_id": payload.media_id,
                            "parent_provider_comment_id": payload.parent_provider_comment_id,
                            "commenter_id": payload.commenter_id,
                            "commenter_name": payload.commenter_name,
                            "commenter_username": payload.commenter_username,
                            "surface": payload.surface,
                            "permalink_url": payload.permalink_url,
                            "media_url": payload.media_url,
                            "contact_profile": payload.contact_profile,
                            "observation_id": str(row.id),
                            "campaign_attributed": payload.campaign_attributed,
                            "attachments": [
                                _attachment_metadata(item)
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
            lead_value = getattr(inbound_result, "lead_id", None)
            lead_id = UUID(lead_value) if lead_value else None
            reference = getattr(inbound_result, "reference", None)
            coverage = getattr(inbound_result, "coverage", None)
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
            lead_id = None
            reference = None
            coverage = None
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
            lead_id=lead_id,
            reference=reference,
            coverage=coverage,
        )

    return execute_owner_command(
        db,
        definition=_PROCESS_OBSERVATION,
        context=context,
        operation=operation,
    )


def mark_provider_observation_processing_failed(
    db: Session,
    *,
    observation_id: UUID,
    context: CommandContext,
    error_code: str,
) -> team_inbox_observations.ProviderObservationOutcome:
    """Record a post-admission processing failure for internal replay/repair."""

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
        row.processing_status = InboxObservationStatus.rejected.value
        row.error_code = (error_code or "processing_failed")[:120]
        db.flush()
        return team_inbox_observations.ProviderObservationOutcome(
            observation_id=row.id,
            outcome=team_inbox_observations.ObservationProcessingOutcome.replayed,
            conversation_id=row.conversation_id,
            message_id=row.message_id,
            processing_status=InboxObservationStatus.rejected,
        )

    return execute_owner_command(
        db,
        definition=_MARK_OBSERVATION_PROCESSING_FAILED,
        context=context,
        operation=operation,
    )
