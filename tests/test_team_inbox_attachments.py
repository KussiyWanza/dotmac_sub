"""Operator attachment upload.

`promote_message_attachments` promotes *inbound* provider-hosted media. An
operator upload has no provider, so the bytes go through the shared
file-storage participant and the asset is created unbound — it is attached to
the message only when the reply that carries it is actually sent.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5, slice 4.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
    InboxMediaAsset,
)
from app.services import team_inbox_commands, team_inbox_media, team_inbox_projection
from app.services.object_storage import StreamResult

CONVERSATION = Path("templates/admin/inbox/_conversation.html").read_text()
JAVASCRIPT = Path("static/js/admin-inbox.js").read_text()
ROUTES = Path("app/web/admin/inbox.py").read_text()

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture(autouse=True)
def _stub_object_storage(monkeypatch):
    """Object storage is not reachable from the unit suite.

    Patched at the source module because the media owner imports
    `file_uploads` inside the function, so a module-attribute patch on the
    caller would not be seen.
    """
    from types import SimpleNamespace

    from app.services import file_storage

    def _fake_stage_upload(**kwargs):
        return SimpleNamespace(
            id="stored-file-1",
            original_filename=kwargs["original_filename"],
            content_type=kwargs["content_type"],
            file_size=len(kwargs["data"]),
            storage_key_or_relative_path=(
                f"attachments/inbox_conversation/{kwargs['entity_id']}/"
                f"{kwargs['original_filename']}"
            ),
        )

    monkeypatch.setattr(file_storage.file_uploads, "stage_upload", _fake_stage_upload)


def _conversation_id(db_session):
    conversation = InboxConversation(
        channel_type="email",
        subject="Fault photo",
        contact_address="customer@example.com",
        status=InboxConversationStatus.open.value,
    )
    db_session.add(conversation)
    db_session.flush()
    captured = conversation.id
    db_session.commit()
    return captured


def test_staging_an_upload_records_an_unbound_asset(db_session):
    conversation_id = _conversation_id(db_session)

    staged = team_inbox_commands.stage_attachments(
        db_session,
        conversation_id=conversation_id,
        uploads=[("diagram.png", "image/png", PNG)],
    )

    assert len(staged) == 1
    asset = db_session.get(InboxMediaAsset, staged[0])
    assert asset.conversation_id == conversation_id
    assert asset.direction == "outbound"
    assert asset.asset_type == "image"
    assert asset.file_name == "diagram.png"
    # Unbound until a reply carries it.
    assert asset.message_id is None


def test_an_oversized_file_is_refused(db_session):
    conversation_id = _conversation_id(db_session)
    too_big = b"0" * (team_inbox_media.MAX_OUTBOUND_ATTACHMENT_BYTES + 1)

    with pytest.raises(team_inbox_media.MediaUploadError) as exc:
        team_inbox_commands.stage_attachments(
            db_session,
            conversation_id=conversation_id,
            uploads=[("huge.png", "image/png", too_big)],
        )
    assert "larger than" in str(exc.value)


def test_a_disallowed_type_is_refused(db_session):
    conversation_id = _conversation_id(db_session)

    with pytest.raises(team_inbox_media.MediaUploadError) as exc:
        team_inbox_commands.stage_attachments(
            db_session,
            conversation_id=conversation_id,
            uploads=[("payload.exe", "application/x-msdownload", b"MZ")],
        )
    assert "not an allowed file type" in str(exc.value)


def test_an_empty_file_is_refused(db_session):
    conversation_id = _conversation_id(db_session)

    with pytest.raises(team_inbox_media.MediaUploadError):
        team_inbox_commands.stage_attachments(
            db_session,
            conversation_id=conversation_id,
            uploads=[("empty.png", "image/png", b"")],
        )


def test_content_type_parameters_do_not_defeat_the_allowlist(db_session):
    """`text/plain; charset=utf-8` is still text/plain."""
    conversation_id = _conversation_id(db_session)

    staged = team_inbox_commands.stage_attachments(
        db_session,
        conversation_id=conversation_id,
        uploads=[("notes.txt", "text/plain; charset=utf-8", b"hello")],
    )

    assert db_session.get(InboxMediaAsset, staged[0]).mime_type == "text/plain"


@pytest.mark.parametrize(
    ("file_name", "content_type", "asset_type"),
    (
        ("voice.ogg", "audio/ogg", "audio"),
        ("clip.mp4", "video/mp4", "video"),
    ),
)
def test_audio_and_video_uploads_are_allowed(
    db_session, file_name, content_type, asset_type
):
    conversation_id = _conversation_id(db_session)

    staged = team_inbox_commands.stage_attachments(
        db_session,
        conversation_id=conversation_id,
        uploads=[(file_name, content_type, b"media-bytes")],
    )

    asset = db_session.get(InboxMediaAsset, staged[0])
    assert asset.mime_type == content_type
    assert asset.asset_type == asset_type


def test_transient_stage_lock_is_a_retryable_owner_error(db_session, monkeypatch):
    from sqlalchemy.exc import OperationalError

    conversation_id = _conversation_id(db_session)

    def raise_lock_timeout(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, RuntimeError("lock timeout"))

    monkeypatch.setattr(
        team_inbox_commands,
        "_active_conversation",
        raise_lock_timeout,
    )

    with pytest.raises(team_inbox_commands.ConversationBusyError) as exc:
        team_inbox_commands.stage_attachments(
            db_session,
            conversation_id=conversation_id,
            uploads=[("diagram.png", "image/png", PNG)],
        )

    assert exc.value.code == ("communications.team_inbox_commands.conversation_busy")
    assert not db_session.in_transaction()


def test_pending_assets_are_listed_until_a_message_carries_them(db_session):
    conversation_id = _conversation_id(db_session)
    team_inbox_commands.stage_attachments(
        db_session,
        conversation_id=conversation_id,
        uploads=[("a.png", "image/png", PNG)],
    )

    pending = team_inbox_media.pending_outbound_assets(db_session, conversation_id)

    assert [a.file_name for a in pending] == ["a.png"]


@pytest.mark.parametrize(
    ("asset_content_type", "response_content_type", "expected_presentation"),
    [
        (
            "image/avif",
            "image/avif",
            team_inbox_projection.InboxMediaBrowserPresentation.inline,
        ),
        (
            "image/gif",
            "image/gif",
            team_inbox_projection.InboxMediaBrowserPresentation.inline,
        ),
        (
            "image/jpeg",
            "image/jpeg",
            team_inbox_projection.InboxMediaBrowserPresentation.inline,
        ),
        (
            "image/png",
            "image/png; charset=binary",
            team_inbox_projection.InboxMediaBrowserPresentation.inline,
        ),
        (
            "image/svg+xml",
            "image/svg+xml",
            team_inbox_projection.InboxMediaBrowserPresentation.attachment,
        ),
        (
            "application/octet-stream",
            "image/webp",
            team_inbox_projection.InboxMediaBrowserPresentation.inline,
        ),
        (
            "video/mp4",
            "video/mp4",
            team_inbox_projection.InboxMediaBrowserPresentation.inline,
        ),
        (
            "audio/mpeg",
            "audio/mpeg",
            team_inbox_projection.InboxMediaBrowserPresentation.inline,
        ),
        (
            "image/png",
            "text/html",
            team_inbox_projection.InboxMediaBrowserPresentation.attachment,
        ),
    ],
)
def test_media_content_selects_a_safe_browser_presentation(
    db_session,
    monkeypatch,
    asset_content_type,
    response_content_type,
    expected_presentation,
):
    conversation_id = _conversation_id(db_session)
    asset = InboxMediaAsset(
        conversation_id=conversation_id,
        channel_type="whatsapp",
        direction="inbound",
        asset_type="image",
        file_name="customer-image.png",
        mime_type=asset_content_type,
        source_url="https://media.example.test/customer-image",
        download_status="remote_available",
    )
    db_session.add(asset)
    db_session.flush()
    monkeypatch.setattr(
        team_inbox_media,
        "_remote_media_content",
        lambda _asset: StreamResult(
            chunks=iter([PNG]),
            content_type=response_content_type,
            content_length=len(PNG),
        ),
    )

    content = team_inbox_projection.get_media_content_projection(
        db_session,
        asset_id=asset.id,
    )

    assert isinstance(content, team_inbox_projection.InboxMediaContentProjection)
    assert content.presentation is expected_presentation
    assert content.content_type == response_content_type.split(";", 1)[0]
    assert content.file_name == "customer-image.png"


def test_remote_media_streaming_requires_known_provider_and_host(db_session):
    conversation_id = _conversation_id(db_session)
    trusted = InboxMediaAsset(
        conversation_id=conversation_id,
        channel_type="instagram_comment",
        direction="inbound",
        asset_type="video",
        file_name="clip.mp4",
        mime_type="video/mp4",
        provider="instagram",
        source_url="https://scontent.cdninstagram.com/v/t50.2886-16/clip.mp4",
        download_status="remote_available",
    )
    untrusted = InboxMediaAsset(
        conversation_id=conversation_id,
        channel_type="email",
        direction="inbound",
        asset_type="video",
        file_name="clip.mp4",
        mime_type="video/mp4",
        provider="email",
        source_url="https://example.test/clip.mp4",
        download_status="remote_available",
    )

    assert team_inbox_media.can_stream_remote_media(trusted) is True
    assert team_inbox_media.can_stream_remote_media(untrusted) is False


@pytest.mark.parametrize(
    ("presentation", "expected_prefix"),
    [
        (team_inbox_projection.InboxMediaBrowserPresentation.inline, "inline;"),
        (
            team_inbox_projection.InboxMediaBrowserPresentation.attachment,
            "attachment;",
        ),
    ],
)
def test_media_route_maps_the_typed_presentation_to_safe_headers(
    monkeypatch,
    presentation,
    expected_prefix,
):
    from app.web.admin import inbox as admin_inbox

    asset_id = uuid4()
    monkeypatch.setattr(
        team_inbox_projection,
        "get_media_content_projection",
        lambda _db, *, asset_id: team_inbox_projection.InboxMediaContentProjection(
            asset_id=asset_id,
            file_name="../customer image.png",
            content_type="image/png",
            content_length=len(PNG),
            presentation=presentation,
            chunks=iter([PNG]),
        ),
    )

    response = admin_inbox.team_inbox_media_content(asset_id, db=object())

    assert response.headers["content-disposition"].startswith(expected_prefix)
    assert 'filename="customer image.png"' in response.headers["content-disposition"]
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-length"] == str(len(PNG))
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_binding_ignores_assets_from_another_conversation(db_session):
    """An id from another thread must not be attachable here."""
    from app.models.team_inbox import InboxMessage

    mine = _conversation_id(db_session)
    theirs = _conversation_id(db_session)
    foreign = team_inbox_commands.stage_attachments(
        db_session,
        conversation_id=theirs,
        uploads=[("theirs.png", "image/png", PNG)],
    )

    message = InboxMessage(
        conversation_id=mine,
        channel_type="email",
        direction="outbound",
        body="hi",
    )
    db_session.add(message)
    db_session.flush()

    bound = team_inbox_media.bind_assets_to_message(
        db_session, message=message, asset_ids=foreign
    )

    assert bound == []
    assert db_session.get(InboxMediaAsset, foreign[0]).message_id is None


def test_staged_asset_validation_rejects_another_conversation(db_session):
    mine = _conversation_id(db_session)
    theirs = _conversation_id(db_session)
    foreign = team_inbox_commands.stage_attachments(
        db_session,
        conversation_id=theirs,
        uploads=[("theirs.png", "image/png", PNG)],
    )

    with pytest.raises(team_inbox_media.MediaUploadError, match="unavailable"):
        team_inbox_media.validate_staged_asset_ids(
            db_session, conversation_id=mine, asset_ids=foreign
        )

    assert db_session.get(InboxMediaAsset, foreign[0]).message_id is None


def test_an_already_sent_asset_cannot_be_reattached(db_session):
    from app.models.team_inbox import InboxMessage

    conversation_id = _conversation_id(db_session)
    staged = team_inbox_commands.stage_attachments(
        db_session,
        conversation_id=conversation_id,
        uploads=[("a.png", "image/png", PNG)],
    )
    first = InboxMessage(
        conversation_id=conversation_id,
        channel_type="email",
        direction="outbound",
        body="one",
    )
    second = InboxMessage(
        conversation_id=conversation_id,
        channel_type="email",
        direction="outbound",
        body="two",
    )
    db_session.add_all([first, second])
    db_session.flush()

    team_inbox_media.bind_assets_to_message(db_session, message=first, asset_ids=staged)
    rebound = team_inbox_media.bind_assets_to_message(
        db_session, message=second, asset_ids=staged
    )

    assert rebound == []
    assert db_session.get(InboxMediaAsset, staged[0]).message_id == first.id


# --- surface ------------------------------------------------------------


def test_the_composer_uploads_and_submits_the_ids():
    assert "demo state until the upload API" not in CONVERSATION
    assert 'name="attachment_ids"' in CONVERSATION
    assert "attachmentIds()" in JAVASCRIPT
    assert "syncAttachmentInput(event.currentTarget)" in JAVASCRIPT
    assert "querySelector('[name=\"attachment_ids\"]')" in JAVASCRIPT
    assert "this.files.some((file) => !file.id)" in JAVASCRIPT
    assert "/attachments" in JAVASCRIPT
    assert '"X-CSRF-Token": csrfToken()' in JAVASCRIPT


def test_the_upload_route_reads_bytes_before_entering_the_command():
    """The command boundary is synchronous and must not await."""
    marker = ROUTES.index("async def team_inbox_stage_attachments")
    body = ROUTES[marker : marker + 1200]
    assert body.index("await upload.read()") < body.index("_prepare_mutation")


def test_the_upload_route_reports_transient_conversation_locks_as_retryable():
    marker = ROUTES.index("async def team_inbox_stage_attachments")
    body = ROUTES[marker : marker + 1800]
    assert "except team_inbox_commands.ConversationBusyError as exc" in body
    assert ".rollback(" not in body
    assert "status_code=409" in body
    assert '"Retry-After": "2"' in body
