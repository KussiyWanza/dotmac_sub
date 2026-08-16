from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxMediaAsset,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import team_inbox_outbound, team_inbox_projection


def _post_conversation(
    db_session,
    *,
    channel_type: str,
    external_thread_id: str,
    subject: str,
    metadata: dict[str, object],
) -> InboxConversation:
    conversation = InboxConversation(
        channel_type=channel_type,
        subject=subject,
        status=InboxConversationStatus.open.value,
        contact_address="social-user",
        external_thread_id=external_thread_id,
        first_message_at=datetime(2026, 8, 1, 8, 0, tzinfo=UTC),
        last_message_at=datetime(2026, 8, 1, 8, 0, tzinfo=UTC),
        metadata_=metadata,
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def _comment(
    db_session,
    conversation: InboxConversation,
    *,
    provider_comment_id: str,
    body: str,
    author: str = "Public Customer",
    parent_provider_comment_id: str | None = None,
    direction: str = InboxMessageDirection.inbound.value,
    minutes: int = 0,
    metadata: dict[str, object] | None = None,
) -> InboxMessage:
    message_metadata = {
        "provider_comment_id": provider_comment_id,
        "comment_id": provider_comment_id,
        "commenter_name": author,
        "post_id": "fb-post-1",
        "parent_provider_comment_id": parent_provider_comment_id,
    }
    message_metadata.update(metadata or {})
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=conversation.channel_type,
        direction=direction,
        body=body,
        external_message_id=provider_comment_id
        if direction == InboxMessageDirection.inbound.value
        else None,
        from_address=author
        if direction == InboxMessageDirection.inbound.value
        else "Support",
        received_at=datetime(2026, 8, 1, 8, minutes, tzinfo=UTC)
        if direction == InboxMessageDirection.inbound.value
        else None,
        sent_at=datetime(2026, 8, 1, 8, minutes, tzinfo=UTC)
        if direction == InboxMessageDirection.outbound.value
        else None,
        metadata_=message_metadata,
    )
    db_session.add(message)
    db_session.flush()
    conversation.last_message_at = (
        message.received_at or message.sent_at or message.created_at
    )
    return message


def test_social_workspace_groups_facebook_and_instagram_posts(db_session):
    facebook = _post_conversation(
        db_session,
        channel_type=InboxChannelType.facebook_comment.value,
        external_thread_id="facebook_comment:fb-post-1",
        subject="Facebook launch post",
        metadata={"page_id": "page-1"},
    )
    instagram = _post_conversation(
        db_session,
        channel_type=InboxChannelType.instagram_comment.value,
        external_thread_id="instagram_comment:ig-media-1",
        subject="Instagram launch reel",
        metadata={"instagram_account_id": "ig-1"},
    )
    _comment(db_session, facebook, provider_comment_id="fb-comment-1", body="FB")
    _comment(
        db_session,
        instagram,
        provider_comment_id="ig-comment-1",
        body="IG",
        metadata={"media_id": "ig-media-1", "instagram_account_id": "ig-1"},
    )
    db_session.commit()

    projection = team_inbox_projection.build_social_comments_projection(db_session)

    assert {row.row.id for row in projection.post_rows} == {
        str(facebook.id),
        str(instagram.id),
    }
    assert {row.platform for row in projection.post_rows} == {"Facebook", "Instagram"}


def test_social_workspace_platform_filter_and_selection_scope(db_session):
    facebook = _post_conversation(
        db_session,
        channel_type=InboxChannelType.facebook_comment.value,
        external_thread_id="facebook_comment:fb-post-1",
        subject="Facebook outage post",
        metadata={"page_id": "page-1"},
    )
    instagram = _post_conversation(
        db_session,
        channel_type=InboxChannelType.instagram_comment.value,
        external_thread_id="instagram_comment:ig-media-1",
        subject="Instagram promo post",
        metadata={"instagram_account_id": "ig-1"},
    )
    _comment(db_session, facebook, provider_comment_id="fb-comment-1", body="FB")
    _comment(
        db_session,
        instagram,
        provider_comment_id="ig-comment-1",
        body="IG",
        metadata={"media_id": "ig-media-1"},
    )
    db_session.commit()

    projection = team_inbox_projection.build_social_comments_projection(
        db_session,
        channel_type=InboxChannelType.instagram_comment.value,
        selected_conversation_id=instagram.id,
    )

    assert [row.row.id for row in projection.post_rows] == [str(instagram.id)]
    assert projection.selected_post is not None
    assert projection.selected_post.timeline.id == str(instagram.id)
    assert all(node.message.body != "FB" for node in projection.selected_post.comments)


def test_social_workspace_stale_selection_falls_back_to_visible_post(db_session):
    facebook = _post_conversation(
        db_session,
        channel_type=InboxChannelType.facebook_comment.value,
        external_thread_id="facebook_comment:fb-post-1",
        subject="Facebook outage post",
        metadata={"page_id": "page-1"},
    )
    instagram = _post_conversation(
        db_session,
        channel_type=InboxChannelType.instagram_comment.value,
        external_thread_id="instagram_comment:ig-media-1",
        subject="Instagram promo post",
        metadata={"instagram_account_id": "ig-1"},
    )
    _comment(db_session, facebook, provider_comment_id="fb-comment-1", body="FB")
    _comment(
        db_session,
        instagram,
        provider_comment_id="ig-comment-1",
        body="IG",
        metadata={"media_id": "ig-media-1"},
    )
    db_session.commit()

    projection = team_inbox_projection.build_social_comments_projection(
        db_session,
        channel_type=InboxChannelType.instagram_comment.value,
        selected_conversation_id=facebook.id,
    )

    assert [row.row.id for row in projection.post_rows] == [str(instagram.id)]
    assert projection.selected_post is not None
    assert projection.selected_post.timeline.id == str(instagram.id)
    assert projection.selected_id == str(instagram.id)


def test_social_comment_hierarchy_and_dotmac_replies_are_preserved(db_session):
    conversation = _post_conversation(
        db_session,
        channel_type=InboxChannelType.facebook_comment.value,
        external_thread_id="facebook_comment:fb-post-1",
        subject="Facebook comments",
        metadata={"page_id": "page-1", "post_id": "fb-post-1"},
    )
    root = _comment(
        db_session,
        conversation,
        provider_comment_id="fb-root-1",
        body="Top level",
        metadata={"page_id": "page-1", "post_id": "fb-post-1"},
    )
    _comment(
        db_session,
        conversation,
        provider_comment_id="fb-reply-1",
        body="Nested reply",
        parent_provider_comment_id="fb-root-1",
        minutes=1,
        metadata={"page_id": "page-1", "post_id": "fb-post-1"},
    )
    _comment(
        db_session,
        conversation,
        provider_comment_id="dotmac-reply-1",
        body="Official reply",
        parent_provider_comment_id="fb-root-1",
        direction=InboxMessageDirection.outbound.value,
        minutes=2,
        metadata={
            "message_kind": "social_comment_reply",
            "provider_message_id": "dotmac-reply-1",
            "parent_provider_comment_id": "fb-root-1",
            "page_id": "page-1",
            "post_id": "fb-post-1",
        },
    )
    db_session.commit()

    projection = team_inbox_projection.build_social_comments_projection(
        db_session,
        selected_conversation_id=conversation.id,
    )

    assert projection.selected_post is not None
    [node] = projection.selected_post.comments
    assert node.message.id == str(root.id)
    assert [reply.message.body for reply in node.replies] == [
        "Nested reply",
        "Official reply",
    ]
    assert node.replies[1].is_dotmac_reply is True
    assert node.reply_context is not None
    assert node.reply_context.provider_comment_id == "fb-root-1"
    assert node.reply_context.provider_post_id == "fb-post-1"
    assert node.reply_context.provider_account_id == "page-1"


def test_social_post_media_projection_covers_image_video_and_carousel(db_session):
    conversation = _post_conversation(
        db_session,
        channel_type=InboxChannelType.instagram_comment.value,
        external_thread_id="instagram_comment:ig-media-1",
        subject="Instagram carousel",
        metadata={"instagram_account_id": "ig-1", "media_id": "ig-media-1"},
    )
    message = _comment(
        db_session,
        conversation,
        provider_comment_id="ig-comment-1",
        body="Great",
        metadata={"media_id": "ig-media-1"},
    )
    for asset_type, mime_type, filename in (
        ("image", "image/jpeg", "one.jpg"),
        ("video", "video/mp4", "two.mp4"),
        ("image", "image/png", "three.png"),
    ):
        db_session.add(
            InboxMediaAsset(
                conversation_id=conversation.id,
                message_id=message.id,
                channel_type=conversation.channel_type,
                direction=InboxMessageDirection.inbound.value,
                provider="instagram",
                provider_media_id=f"{filename}-provider",
                asset_type=asset_type,
                file_name=filename,
                mime_type=mime_type,
                source_url=f"https://media.example.test/{filename}",
                download_status="remote_available",
            )
        )
    db_session.commit()

    projection = team_inbox_projection.build_social_comments_projection(
        db_session,
        selected_conversation_id=conversation.id,
    )

    assert projection.selected_post is not None
    assert [item.media_type for item in projection.selected_post.media_items] == [
        "image",
        "video",
        "image",
    ]
    assert all(item.url for item in projection.selected_post.media_items)


def test_top_level_comment_and_private_message_actions_are_unavailable(db_session):
    conversation = _post_conversation(
        db_session,
        channel_type=InboxChannelType.facebook_comment.value,
        external_thread_id="facebook_comment:fb-post-1",
        subject="Facebook comments",
        metadata={"page_id": "page-1"},
    )
    _comment(db_session, conversation, provider_comment_id="fb-comment-1", body="Hi")
    db_session.commit()

    projection = team_inbox_projection.build_social_comments_projection(
        db_session,
        selected_conversation_id=conversation.id,
    )

    assert projection.selected_post is not None
    assert projection.selected_post.top_level_comment_supported is False
    assert projection.selected_post.private_message_supported is False


def test_social_comment_reply_metadata_carries_exact_target_context(
    db_session, monkeypatch
):
    conversation = _post_conversation(
        db_session,
        channel_type=InboxChannelType.instagram_comment.value,
        external_thread_id="instagram_comment:ig-media-1",
        subject="Instagram comments",
        metadata={"instagram_account_id": "ig-1", "media_id": "ig-media-1"},
    )
    target = _comment(
        db_session,
        conversation,
        provider_comment_id="ig-comment-1",
        body="Can you explain?",
        metadata={
            "instagram_account_id": "ig-1",
            "media_id": "ig-media-1",
            "post_id": "ig-media-1",
        },
    )

    def _submit(_db, intent):
        notification = Notification(
            channel=NotificationChannel.instagram_comment,
            recipient="ig-comment-1",
            status=NotificationStatus.queued,
            subject=intent.subject,
            body=intent.body,
            metadata_=dict(intent.metadata),
        )
        db_session.add(notification)
        db_session.flush()
        return type("Result", (), {"queued": [notification], "suppressed": []})()

    monkeypatch.setattr(team_inbox_outbound, "submit", _submit)

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Yes.</p>",
            body_text="Yes.",
            sent_by_person_id=uuid4(),
            metadata={"reply_to": {"message_id": str(target.id)}},
        ),
    )

    assert result.kind == "queued"
    queued_intents = (
        db_session.query(Notification)
        .filter(Notification.channel == NotificationChannel.instagram_comment)
        .all()
    )
    [notification] = queued_intents
    metadata = notification.metadata_
    assert metadata["provider_account_id"] == "ig-1"
    assert metadata["parent_provider_comment_id"] == "ig-comment-1"
    assert metadata["root_provider_comment_id"] == "ig-comment-1"
    assert metadata["provider_media_id"] == "ig-media-1"
    assert metadata["provider_post_id"] == "ig-media-1"
    assert metadata["target_inbox_message_id"] == str(target.id)
