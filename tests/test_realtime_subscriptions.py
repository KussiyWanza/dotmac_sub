from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.subscriber import Subscriber
from app.models.team_inbox import InboxConversation
from app.services.realtime_platform import conversation_topic, operation_topic
from app.services.realtime_subscriptions import (
    RealtimeSubscriptionError,
    authorize_topic,
)


def _conversation(db_session, subscriber_id=None) -> InboxConversation:
    row = InboxConversation(subscriber_id=subscriber_id)
    db_session.add(row)
    db_session.flush()
    return row


def test_widget_can_only_subscribe_to_its_bound_conversation(db_session) -> None:
    own = _conversation(db_session)
    other = _conversation(db_session)
    auth = {
        "principal_id": "chat_widget:test",
        "principal_type": "chat_widget",
        "conversation_id": str(own.id),
        "roles": [],
        "scopes": [],
    }

    assert authorize_topic(db_session, auth, str(own.id)) == conversation_topic(own.id)
    with pytest.raises(RealtimeSubscriptionError, match="only subscribe"):
        authorize_topic(db_session, auth, str(other.id))


def test_subscriber_can_only_subscribe_to_an_owned_conversation(db_session) -> None:
    owner = Subscriber(first_name="Ada", last_name="Nwosu", email="ada@realtime.test")
    stranger = Subscriber(
        first_name="Other", last_name="Customer", email="other@realtime.test"
    )
    db_session.add_all([owner, stranger])
    db_session.flush()
    own = _conversation(db_session, owner.id)
    other = _conversation(db_session, stranger.id)
    auth = {
        "principal_id": str(owner.id),
        "principal_type": "subscriber",
        "roles": [],
        "scopes": [],
    }

    assert authorize_topic(
        db_session, auth, f"conversation:{own.id}"
    ) == conversation_topic(own.id)
    with pytest.raises(RealtimeSubscriptionError, match="cannot subscribe"):
        authorize_topic(db_session, auth, f"conversation:{other.id}")


def test_operation_topic_requires_its_target_read_permission(db_session) -> None:
    operation = NetworkOperation(
        operation_type=NetworkOperationType.ont_authorize,
        target_type=NetworkOperationTargetType.ont,
        target_id=uuid4(),
    )
    db_session.add(operation)
    db_session.flush()
    admin = {
        "principal_id": str(uuid4()),
        "principal_type": "system_user",
        "roles": ["admin"],
        "scopes": [],
    }
    unprivileged = {
        "principal_id": str(uuid4()),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }

    assert authorize_topic(db_session, admin, str(operation.id)) == operation_topic(
        operation.id
    )
    with pytest.raises(RealtimeSubscriptionError, match="cannot subscribe"):
        authorize_topic(db_session, unprivileged, f"operation:{operation.id}")


@pytest.mark.parametrize(
    "topic",
    ["principal:someone-else", "audience:staff", "workqueue:audience:org"],
)
def test_server_owned_topics_cannot_be_selected_by_clients(db_session, topic) -> None:
    auth = {
        "principal_id": str(uuid4()),
        "principal_type": "subscriber",
        "roles": [],
        "scopes": [],
    }

    with pytest.raises(RealtimeSubscriptionError) as exc:
        authorize_topic(db_session, auth, topic)

    assert exc.value.code == "topic_not_client_subscribable"
