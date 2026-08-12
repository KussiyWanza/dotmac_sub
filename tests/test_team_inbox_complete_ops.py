from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxMediaAsset,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import (
    team_inbox_media,
    team_inbox_operations,
    team_inbox_outbound,
    team_inbox_read,
)
from app.web.admin import inbox as inbox_web


def _request() -> Request:
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/"}, receive)


def _team(db_session) -> ServiceTeam:
    team = ServiceTeam(name="Support", team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def _conversation(db_session, *, subject: str = "Need help") -> InboxConversation:
    conversation = InboxConversation(
        channel_type="email",
        subject=subject,
        status=InboxConversationStatus.open.value,
        contact_address="ada@example.com",
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def _message(
    db_session,
    conversation: InboxConversation,
    *,
    direction: str = InboxMessageDirection.inbound.value,
    body: str = "Hello",
    metadata: dict | None = None,
) -> InboxMessage:
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type="email",
        direction=direction,
        body=body,
        from_address="ada@example.com"
        if direction == "inbound"
        else "support@dotmac.ng",
        to_addresses=["support@dotmac.ng"]
        if direction == "inbound"
        else ["ada@example.com"],
        metadata_=metadata or {},
    )
    db_session.add(message)
    db_session.flush()
    return message


def test_message_metadata_attachments_promote_to_timeline_assets(db_session):
    conversation = _conversation(db_session)
    message = _message(
        db_session,
        conversation,
        metadata={
            "attachments": [
                {
                    "type": "image",
                    "provider": "meta",
                    "id": "media-1",
                    "filename": "drop.jpg",
                    "mime_type": "image/jpeg",
                    "url": "https://example.test/drop.jpg",
                    "caption": "Drop point",
                }
            ]
        },
    )

    assets = team_inbox_media.promote_message_attachments(
        db_session,
        message=message,
    )
    timeline = team_inbox_read.get_conversation_timeline(db_session, conversation.id)

    assert len(assets) == 1
    assert db_session.query(InboxMediaAsset).count() == 1
    assert timeline is not None
    assert timeline.messages[0].attachments[0].provider_media_id == "media-1"
    assert timeline.messages[0].attachments[0].download_status == "metadata_only"
    assert timeline.messages[0].attachments[0].url is not None
    assert timeline.messages[0].attachments[0].url.startswith("/admin/inbox/media/")


def test_location_without_coordinates_never_uses_media_content_route(db_session):
    conversation = _conversation(db_session)
    message = _message(
        db_session,
        conversation,
        metadata={"attachments": [{"type": "location"}]},
    )

    team_inbox_media.promote_message_attachments(db_session, message=message)
    timeline = team_inbox_read.get_conversation_timeline(db_session, conversation.id)

    assert timeline is not None
    attachment = timeline.messages[0].attachments[0]
    assert attachment.type == "location"
    assert attachment.location is None
    assert attachment.url is None
    assert attachment.content_available is False


def test_outbound_attachment_upload_does_not_use_staff_id_as_subscriber(
    db_session, monkeypatch
):
    conversation = _conversation(db_session)
    staff_person_id = uuid.uuid4()
    captured: dict[str, object] = {}

    def fake_stage_upload(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            original_filename="proof.png",
            file_size=len(kwargs["data"]),
            storage_key_or_relative_path="attachments/public/inbox/proof.png",
        )

    monkeypatch.setattr(
        team_inbox_media.file_uploads, "stage_upload", fake_stage_upload
    )

    asset = team_inbox_media.stage_outbound_attachment(
        db_session,
        conversation=conversation,
        file_name="proof.png",
        content_type="image/png",
        data=b"png-bytes",
        uploaded_by=str(staff_person_id),
    )

    assert captured["uploaded_by"] is None
    assert asset.metadata_["uploaded_by_person_id"] == str(staff_person_id)
    assert asset.metadata_["stored_file_id"]


def test_comments_are_first_class_and_searchable(db_session):
    conversation = _conversation(db_session, subject="Router issue")
    _message(db_session, conversation, body="Initial inbound")
    comment = team_inbox_operations.create_comment(
        db_session,
        conversation=conversation,
        body="NOC says this is a backbone outage",
        author_person_id=uuid.uuid4(),
    )

    result = team_inbox_read.list_conversations(db_session, search="backbone")
    timeline = team_inbox_read.get_conversation_timeline(db_session, conversation.id)

    assert [item.id for item in result.items] == [str(conversation.id)]
    assert timeline is not None
    assert timeline.comments[0].id == str(comment.id)
    assert timeline.comments[0].is_resolved is False


def test_admin_comment_resolve_updates_comment(db_session):
    conversation = _conversation(db_session)
    comment = team_inbox_operations.create_comment(
        db_session,
        conversation=conversation,
        body="Check light levels",
    )

    response = inbox_web.team_inbox_comment_resolve(
        comment.id,
        _request(),
        db_session,
    )

    db_session.refresh(comment)
    assert response.status_code == 303
    assert comment.is_resolved is True


def test_batch_retry_failed_outbound_messages(db_session, monkeypatch):
    conversation = _conversation(db_session)
    failed = _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        body="Retry me",
        metadata={"delivery_status": "failed", "send_error": "SMTP rejected"},
    )
    calls: list[str] = []

    def _fake_retry(db, *, message, sent_by_person_id=None, now=None):
        calls.append(str(message.id))
        metadata = dict(message.metadata_ or {})
        metadata["delivery_status"] = "retried"
        message.metadata_ = metadata
        return team_inbox_outbound.InboxReplyResult(
            kind="sent",
            conversation_id=str(message.conversation_id),
            message_id=str(uuid.uuid4()),
        )

    monkeypatch.setattr(
        team_inbox_operations.team_inbox_outbound,
        "retry_outbound_message",
        _fake_retry,
    )

    result = team_inbox_operations.retry_failed_outbound_batch(db_session)

    assert result["retried"] == [str(failed.id)]
    assert calls == [str(failed.id)]


def test_queue_metrics_counts_open_work(db_session):
    team = _team(db_session)
    open_conversation = _conversation(db_session, subject="Open")
    _message(db_session, open_conversation, body="Needs help")
    assigned_conversation = _conversation(db_session, subject="Assigned")
    _message(db_session, assigned_conversation, body="Already assigned")
    db_session.add(
        InboxConversationAssignment(
            conversation_id=assigned_conversation.id,
            service_team_id=team.id,
            person_id=uuid.uuid4(),
            is_active=True,
        )
    )
    failed_conversation = _conversation(db_session, subject="Failed")
    _message(
        db_session,
        failed_conversation,
        direction=InboxMessageDirection.outbound.value,
        body="Failed reply",
        metadata={"delivery_status": "failed"},
    )
    db_session.flush()

    metrics = team_inbox_operations.queue_metrics(db_session)

    assert metrics.total_open == 3
    assert metrics.needs_response == 2
    assert metrics.unassigned_open == 2
    assert metrics.failed_outbound == 1


def test_queue_metric_drilldown_filters_match_exact_open_cohorts(db_session):
    team = _team(db_session)
    open_unassigned = _conversation(db_session, subject="Open unassigned")
    open_unassigned.is_muted = True
    open_unassigned.snoozed_until = datetime.now(UTC) + timedelta(hours=1)
    resolved_unassigned = _conversation(db_session, subject="Resolved unassigned")
    resolved_unassigned.status = InboxConversationStatus.resolved.value
    resolved_unassigned.is_muted = True
    resolved_unassigned.snoozed_until = datetime.now(UTC) + timedelta(hours=1)
    open_assigned = _conversation(db_session, subject="Open assigned")
    db_session.add(
        InboxConversationAssignment(
            conversation_id=open_assigned.id,
            service_team_id=team.id,
            person_id=uuid.uuid4(),
            is_active=True,
        )
    )
    db_session.flush()

    metrics = team_inbox_operations.queue_metrics(db_session)
    open_rows = team_inbox_read.list_conversations(db_session, open_only=True)
    unassigned_rows = team_inbox_read.list_conversations(
        db_session, open_only=True, unassigned=True
    )
    muted_rows = team_inbox_read.list_conversations(
        db_session, open_only=True, muted=True
    )
    snoozed_rows = team_inbox_read.list_conversations(
        db_session, open_only=True, snoozed=True
    )

    assert open_rows.count == metrics.total_open == 2
    assert unassigned_rows.count == metrics.unassigned_open == 1
    assert muted_rows.count == metrics.muted_open == 1
    assert snoozed_rows.count == metrics.snoozed_open == 1
    assert unassigned_rows.items[0].id == str(open_unassigned.id)


def test_queue_metric_cards_link_to_exact_cohorts():
    template = Path("templates/admin/inbox/_sidebar.html").read_text()

    assert 'href="/admin/inbox?open_only=true"' in template
    assert 'href="/admin/inbox?open_only=true&unassigned=true"' in template
    assert 'href="/admin/inbox/reports/outbox-failures"' in template
    assert 'href="/admin/inbox?open_only=true&snoozed=true"' in template
    assert 'href="/admin/inbox?open_only=true&muted=true"' in template
