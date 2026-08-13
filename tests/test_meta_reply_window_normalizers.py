from app.api.inbox_webhooks import (
    _iter_meta_whatsapp_messages,
    _iter_meta_whatsapp_statuses,
)
from app.api.meta_inbox_webhooks import (
    _iter_meta_social_comments,
    _iter_meta_social_messages,
)
from app.models.team_inbox import InboxChannelType


def _whatsapp_payload(*, messages=None, statuses=None):
    return {
        "entry": [
            {
                "id": "business-1",
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-1"},
                            "contacts": [
                                {
                                    "wa_id": "2348035550114",
                                    "profile": {"name": "Ada Customer"},
                                }
                            ],
                            "messages": list(messages or []),
                            "statuses": list(statuses or []),
                        }
                    }
                ],
            }
        ]
    }


def _social_payload(*, object_name="page", messaging=None, changes=None):
    return {
        "object": object_name,
        "entry": [
            {
                "id": "account-1",
                "messaging": list(messaging or []),
                "changes": list(changes or []),
            }
        ],
    }


def _social_message(message):
    return {
        "sender": {"id": "customer-1"},
        "recipient": {"id": "account-1"},
        "timestamp": 1_787_654_321_000,
        "message": message,
    }


def test_whatsapp_customer_text_media_and_interactive_messages_qualify():
    messages = [
        {
            "from": "2348035550114",
            "id": "wamid.text",
            "timestamp": "1787654321",
            "type": "text",
            "text": {"body": "Hello"},
        },
        {
            "from": "2348035550114",
            "id": "wamid.image",
            "timestamp": "1787654322",
            "type": "image",
            "image": {"id": "media-1", "mime_type": "image/jpeg"},
        },
        {
            "from": "2348035550114",
            "id": "wamid.interactive",
            "timestamp": "1787654323",
            "type": "interactive",
            "interactive": {"type": "button_reply"},
        },
    ]

    items = list(_iter_meta_whatsapp_messages(_whatsapp_payload(messages=messages)))

    assert [item["message"]["text"] for item in items] == [
        "Hello",
        "[image]",
        "[interactive]",
    ]
    assert {item["metadata"]["provider_message_type"] for item in items} == {
        "text",
        "image",
        "interactive",
    }
    assert all(item["metadata"]["reply_window_qualifying"] is True for item in items)


def test_whatsapp_status_callbacks_and_non_messages_do_not_qualify():
    payload = _whatsapp_payload(
        messages=[
            {
                "from": "2348035550114",
                "id": "wamid.reaction",
                "timestamp": "1787654321",
                "type": "reaction",
                "reaction": {"message_id": "wamid.text", "emoji": "ok"},
            },
            {
                "from": "2348035550114",
                "id": "wamid.unsupported",
                "timestamp": "1787654322",
                "type": "unsupported",
            },
            {"id": "wamid.malformed", "timestamp": "1787654323", "type": "text"},
        ],
        statuses=[
            {
                "id": "wamid.outbound",
                "status": "read",
                "timestamp": "1787654324",
                "recipient_id": "2348035550114",
            }
        ],
    )

    assert list(_iter_meta_whatsapp_messages(payload)) == []
    assert [item["status"] for item in _iter_meta_whatsapp_statuses(payload)] == [
        "read"
    ]


def test_meta_social_customer_text_attachment_and_quick_reply_qualify():
    payload = _social_payload(
        messaging=[
            _social_message({"mid": "m.text", "text": "Hello"}),
            _social_message(
                {
                    "mid": "m.media",
                    "attachments": [
                        {"type": "image", "payload": {"url": "https://cdn.test/a.jpg"}}
                    ],
                }
            ),
            _social_message({"mid": "m.quick", "quick_reply": {"payload": "yes"}}),
        ]
    )

    items = list(_iter_meta_social_messages(payload))

    assert [item["body"] for item in items] == [
        "Hello",
        "[image]",
        "[quick reply]",
    ]
    assert {item["channel_type"] for item in items} == {
        InboxChannelType.facebook_messenger.value
    }
    assert all(item["metadata"]["reply_window_qualifying"] is True for item in items)


def test_meta_social_echo_reaction_delete_and_control_events_do_not_qualify():
    payload = _social_payload(
        messaging=[
            _social_message({"mid": "m.echo", "text": "Agent echo", "is_echo": True}),
            _social_message(
                {"mid": "m.reaction", "text": "Like", "reaction": {"emoji": "ok"}}
            ),
            _social_message({"mid": "m.deleted", "is_deleted": True}),
            {
                "sender": {"id": "customer-1"},
                "recipient": {"id": "account-1"},
                "timestamp": 1_787_654_321_000,
                "delivery": {"mids": ["m.outbound"]},
            },
            {
                "sender": {"id": "customer-1"},
                "recipient": {"id": "account-1"},
                "timestamp": 1_787_654_321_000,
                "read": {"watermark": 1_787_654_321_000},
            },
            {
                "sender": {"id": "customer-1"},
                "recipient": {"id": "account-1"},
                "timestamp": 1_787_654_321_000,
                "postback": {"payload": "START"},
            },
        ]
    )

    assert list(_iter_meta_social_messages(payload)) == []


def test_instagram_dm_messages_qualify_but_public_comments_do_not_feed_dm_window():
    dm_payload = _social_payload(
        object_name="instagram",
        messaging=[_social_message({"mid": "ig.m1", "text": "Hello"})],
    )
    comment_payload = _social_payload(
        object_name="instagram",
        changes=[
            {
                "field": "comments",
                "value": {
                    "id": "comment-1",
                    "media_id": "media-1",
                    "from": {"id": "commenter-1", "username": "commenter"},
                    "message": "Public comment",
                    "created_time": 1_787_654_321,
                },
            }
        ],
    )

    dm_items = list(_iter_meta_social_messages(dm_payload))
    comment_items = list(_iter_meta_social_comments(comment_payload))

    assert dm_items[0]["channel_type"] == InboxChannelType.instagram_dm.value
    assert dm_items[0]["metadata"]["reply_window_qualifying"] is True
    assert list(_iter_meta_social_messages(comment_payload)) == []
    assert comment_items[0]["channel_type"] == InboxChannelType.instagram_comment.value
