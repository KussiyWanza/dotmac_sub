"""Operator attachment upload.

`promote_message_attachments` promotes *inbound* provider-hosted media. An
operator upload has no provider, so the bytes go through the shared
file-storage participant and the asset is created unbound — it is attached to
the message only when the reply that carries it is actually sent.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5, slice 4.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
    InboxMediaAsset,
)
from app.services import team_inbox_commands, team_inbox_media

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


def test_pending_assets_are_listed_until_a_message_carries_them(db_session):
    conversation_id = _conversation_id(db_session)
    team_inbox_commands.stage_attachments(
        db_session,
        conversation_id=conversation_id,
        uploads=[("a.png", "image/png", PNG)],
    )

    pending = team_inbox_media.pending_outbound_assets(db_session, conversation_id)

    assert [a.file_name for a in pending] == ["a.png"]


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
    assert "/attachments" in JAVASCRIPT
    assert '"X-CSRF-Token": csrfToken()' in JAVASCRIPT


def test_the_upload_route_reads_bytes_before_entering_the_command():
    """The command boundary is synchronous and must not await."""
    marker = ROUTES.index("async def team_inbox_stage_attachments")
    body = ROUTES[marker : marker + 1200]
    assert body.index("await upload.read()") < body.index("_prepare_mutation")
