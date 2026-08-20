"""End-to-end journey gaps found reviewing the inbox, and the rules that close them.

Each test names the journey it protects rather than the function it calls: the
defects here were all cases where two parts of the system each looked correct
on their own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
    InboxTeamRole,
    InboxTeamSource,
)
from app.services import (
    team_inbox_channel_receive,
    team_inbox_operations,
    team_inbox_read,
    team_inbox_receive,
    team_inbox_routing,
)


@pytest.fixture
def default_channel_team():
    """Set the channel fallback team for one test.

    `Settings` is a frozen dataclass, so `monkeypatch.setattr` cannot touch a
    field on it; the write goes through `object.__setattr__` and is restored
    afterwards.
    """
    from app.config import settings

    field = "team_inbox_channel_fallback_service_team_id"
    original = getattr(settings, field)

    def apply(value: object) -> None:
        object.__setattr__(settings, field, str(value or ""))

    yield apply
    object.__setattr__(settings, field, original)


def _team(db_session, name: str = "Support") -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def _subscriber(db_session, *, email: str, phone: str = "+2348030000001") -> Subscriber:
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Nwosu",
        email=email,
        phone=phone,
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


# --- Journey: inbound email reaches the customer record -----------------------


def test_inbound_email_resolves_its_sender_to_a_subscriber(db_session):
    """The email path used to skip contact resolution entirely.

    Only the channel path resolved a contact, so every inbound email landed
    with a null subscriber and no `contact_resolution` — invisible to the
    contact filter and to the customer record's communications section, which
    keys on `InboxConversation.subscriber_id`.
    """
    subscriber = _subscriber(db_session, email="ada@example.com")
    db_session.commit()

    result = team_inbox_receive.receive_inbound_email(
        db_session,
        team_inbox_receive.InboundEmailPayload(
            from_address="Ada <ada@example.com>",
            to_addresses=["support@dotmac.io"],
            subject="Line is down",
            body="No internet since noon.",
            message_id="<one@example.com>",
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    assert conversation.subscriber_id == subscriber.id
    assert conversation.metadata_["contact_resolution"]["status"] == "linked_subscriber"
    assert result.resolution_status == "linked_subscriber"
    assert result.subscriber_id == str(subscriber.id)


def test_an_unmatched_sender_is_recorded_as_unmatched_not_left_blank(db_session):
    result = team_inbox_receive.receive_inbound_email(
        db_session,
        team_inbox_receive.InboundEmailPayload(
            from_address="stranger@example.com",
            to_addresses=["support@dotmac.io"],
            body="Hello.",
            message_id="<two@example.com>",
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    assert conversation.subscriber_id is None
    assert conversation.metadata_["contact_resolution"]["status"] == "unmatched"
    assert result.resolution_status == "unmatched"


def test_an_email_conversation_reaches_the_customer_scoped_read(db_session):
    """The join the customer 360 communications section actually uses."""
    subscriber = _subscriber(db_session, email="ada@example.com")
    db_session.commit()

    team_inbox_receive.receive_inbound_email(
        db_session,
        team_inbox_receive.InboundEmailPayload(
            from_address="ada@example.com",
            to_addresses=["support@dotmac.io"],
            body="Still down.",
            message_id="<three@example.com>",
        ),
    )
    db_session.commit()

    result = team_inbox_read.list_conversations(db_session, subscriber_id=subscriber.id)
    assert result.count == 1


# --- Journey: a reply only joins a live thread -------------------------------


def _inbound(db_session, *, message_id: str, in_reply_to: str | None = None):
    return team_inbox_receive.receive_inbound_email(
        db_session,
        team_inbox_receive.InboundEmailPayload(
            from_address="ada@example.com",
            to_addresses=["support@dotmac.io"],
            subject="Line fault",
            body="Body.",
            message_id=message_id,
            in_reply_to=in_reply_to,
        ),
    )


def test_a_reply_joins_the_thread_it_references(db_session):
    first = _inbound(db_session, message_id="<a@example.com>")
    db_session.commit()
    second = _inbound(
        db_session, message_id="<b@example.com>", in_reply_to="<a@example.com>"
    )
    db_session.commit()

    assert second.conversation_id == first.conversation_id
    assert second.continued_from_conversation_id is None


def test_a_reply_to_a_resolved_thread_opens_a_new_one(db_session):
    """It used to attach, and inbound email never reopens a thread.

    So the message landed in a closed conversation nobody was watching. The
    channel path has always started a fresh thread in this case; email is the
    one that differed.
    """
    first = _inbound(db_session, message_id="<c@example.com>")
    conversation = db_session.get(InboxConversation, first.conversation_id)
    conversation.status = InboxConversationStatus.resolved.value
    db_session.commit()

    second = _inbound(
        db_session, message_id="<d@example.com>", in_reply_to="<c@example.com>"
    )
    db_session.commit()

    assert second.conversation_id != first.conversation_id
    assert second.continued_from_conversation_id == first.conversation_id
    continued = db_session.get(InboxConversation, second.conversation_id)
    assert continued.continued_from_conversation_id == conversation.id
    timeline = team_inbox_read.get_conversation_timeline(
        db_session, conversation_id=continued.id
    )
    assert timeline is not None
    assert timeline.continued_from_conversation_id == str(conversation.id)
    assert timeline.continued_from_url == f"/admin/inbox?c={conversation.id}"
    db_session.refresh(conversation)
    assert conversation.status == InboxConversationStatus.resolved.value


def test_a_reply_to_a_deleted_thread_opens_a_new_one(db_session):
    first = _inbound(db_session, message_id="<e@example.com>")
    conversation = db_session.get(InboxConversation, first.conversation_id)
    conversation.is_active = False
    db_session.commit()

    second = _inbound(
        db_session, message_id="<f@example.com>", in_reply_to="<e@example.com>"
    )
    db_session.commit()

    assert second.conversation_id != first.conversation_id


# --- Journey: inbound social traffic lands on a team --------------------------


def test_a_whatsapp_thread_lands_on_the_configured_default_team(
    db_session, default_channel_team
):
    """WhatsApp and the Meta social channels carry no address to route on.

    The webhooks pass no fallback, so before this every such thread had no
    `InboxConversationTeam` row at all and was absent from every team filter.
    """
    support = _team(db_session)
    db_session.commit()
    default_channel_team(support.id)

    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="+2348030000009",
            body="My router is offline.",
            external_message_id="wa-1",
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    assert conversation.primary_service_team_id == support.id
    links = (
        db_session.query(InboxConversationTeam)
        .filter(InboxConversationTeam.conversation_id == conversation.id)
        .all()
    )
    assert [link.role for link in links] == [InboxTeamRole.owner.value]


def test_an_unset_default_team_leaves_the_thread_unrouted_rather_than_guessing(
    db_session, default_channel_team
):
    default_channel_team("")
    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="+2348030000010",
            body="Hello.",
            external_message_id="wa-2",
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    assert conversation.primary_service_team_id is None


def test_a_deactivated_default_team_is_not_used(db_session, default_channel_team):
    support = _team(db_session)
    support.is_active = False
    db_session.commit()
    default_channel_team(support.id)
    assert team_inbox_routing.default_service_team_id(db_session) is None


# --- Journey: the "My team" cohort ------------------------------------------


def _conversation_on_teams(db_session, *teams: ServiceTeam) -> InboxConversation:
    conversation = InboxConversation(
        channel_type=InboxChannelType.email.value,
        status=InboxConversationStatus.open.value,
        contact_address="someone@example.com",
        primary_service_team_id=teams[0].id if teams else None,
    )
    db_session.add(conversation)
    db_session.flush()
    for team in teams:
        db_session.add(
            InboxConversationTeam(
                conversation_id=conversation.id,
                service_team_id=team.id,
                role=InboxTeamRole.participant.value,
                source=InboxTeamSource.routing_rule.value,
                is_active=True,
            )
        )
    db_session.flush()
    return conversation


def test_a_thread_shared_by_two_of_my_teams_is_listed_once(db_session):
    """The multi-team join returned one row per matching team.

    The badge counted distinct conversations and the list did not, so the two
    disagreed — the exact disagreement the multi-team scope exists to prevent.
    """
    first = _team(db_session, "Support")
    second = _team(db_session, "Field")
    _conversation_on_teams(db_session, first, second)
    db_session.commit()

    result = team_inbox_read.list_conversations(
        db_session, service_team_ids=(str(first.id), str(second.id))
    )
    assert result.count == 1
    assert len(result.items) == 1


def test_both_team_filters_at_once_intersect(db_session):
    """Two filters over the same relation used to be two joins, which failed.

    They are subqueries now, so setting both narrows rather than erroring.
    """
    support = _team(db_session, "Support")
    field = _team(db_session, "Field")
    _conversation_on_teams(db_session, support, field)
    _conversation_on_teams(db_session, field)
    db_session.commit()

    both = team_inbox_read.list_conversations(
        db_session,
        service_team_id=str(support.id),
        service_team_ids=(str(field.id),),
    )
    assert both.count == 1

    scope_only = team_inbox_read.list_conversations(
        db_session, service_team_ids=(str(field.id),)
    )
    assert scope_only.count == 2


# --- Journey: snooze ---------------------------------------------------------


def _snoozed(db_session, *, wake_at: datetime | None) -> InboxConversation:
    conversation = InboxConversation(
        channel_type=InboxChannelType.email.value,
        status="snoozed",
        contact_address="sleeper@example.com",
        snoozed_until=wake_at,
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def test_a_passed_wake_time_is_not_still_snoozed(db_session):
    """`snoozed` meant "has a wake time", not "is asleep".

    A conversation snoozed until Tuesday stayed filed as snoozed indefinitely,
    and disagreed with the workqueue provider, which has always read a passed
    wake time as awake.
    """
    _snoozed(db_session, wake_at=datetime.now(UTC) - timedelta(hours=1))
    db_session.commit()

    assert team_inbox_read.list_conversations(db_session, snoozed=True).count == 0
    assert team_inbox_read.list_conversations(db_session, snoozed=False).count == 1


def test_a_future_wake_time_is_still_snoozed(db_session):
    _snoozed(db_session, wake_at=datetime.now(UTC) + timedelta(hours=1))
    db_session.commit()

    assert team_inbox_read.list_conversations(db_session, snoozed=True).count == 1


def test_snoozed_until_reply_counts_as_asleep(db_session):
    """It deliberately stores no wake time, so a NOT NULL test missed it."""
    conversation = _snoozed(db_session, wake_at=None)
    team_inbox_operations.snooze_until_reply(db_session, conversation=conversation)
    db_session.commit()

    assert team_inbox_read.list_conversations(db_session, snoozed=True).count == 1


def test_the_waker_returns_an_expired_snooze_to_the_open_queue(db_session):
    conversation = _snoozed(db_session, wake_at=datetime.now(UTC) - timedelta(hours=1))
    db_session.commit()

    woken = team_inbox_operations.wake_due_snoozed_conversations(db_session)
    db_session.commit()
    db_session.refresh(conversation)

    assert woken == 1
    assert conversation.snoozed_until is None
    assert conversation.status == "open"


def test_the_waker_is_idempotent(db_session):
    _snoozed(db_session, wake_at=datetime.now(UTC) - timedelta(hours=1))
    db_session.commit()

    assert team_inbox_operations.wake_due_snoozed_conversations(db_session) == 1
    db_session.commit()
    assert team_inbox_operations.wake_due_snoozed_conversations(db_session) == 0


def test_the_waker_leaves_a_resolved_conversation_resolved(db_session):
    """A wake time passing is not a reason to reopen closed work."""
    conversation = _snoozed(db_session, wake_at=datetime.now(UTC) - timedelta(hours=1))
    conversation.status = InboxConversationStatus.resolved.value
    db_session.commit()

    team_inbox_operations.wake_due_snoozed_conversations(db_session)
    db_session.commit()
    db_session.refresh(conversation)

    assert conversation.status == InboxConversationStatus.resolved.value
    assert conversation.snoozed_until is None


def test_the_waker_does_not_touch_a_future_snooze(db_session):
    conversation = _snoozed(db_session, wake_at=datetime.now(UTC) + timedelta(hours=2))
    db_session.commit()

    assert team_inbox_operations.wake_due_snoozed_conversations(db_session) == 0
    db_session.refresh(conversation)
    assert conversation.snoozed_until is not None


# --- Journey: unread ---------------------------------------------------------


def test_unread_filter_and_unread_count_agree(db_session):
    """They are now one rule; the filter asks the read-state owner for it."""
    from uuid import uuid4

    from app.services import team_inbox_read_state

    person_id = uuid4()
    conversation = InboxConversation(
        channel_type=InboxChannelType.email.value,
        status=InboxConversationStatus.open.value,
        contact_address="unread@example.com",
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type=InboxChannelType.email.value,
            direction=InboxMessageDirection.inbound.value,
            body="Anyone there?",
            received_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    listed = team_inbox_read.list_conversations(
        db_session, operator_person_id=person_id, unread_only=True
    )
    counted = team_inbox_read_state.unread_conversation_count(
        db_session, person_id=person_id
    )
    assert listed.count == 1
    assert counted == 1
    assert listed.items[0].is_unread is True


def test_unread_without_an_operator_returns_nothing_rather_than_everything(db_session):
    conversation = InboxConversation(
        channel_type=InboxChannelType.email.value,
        status=InboxConversationStatus.open.value,
        contact_address="anon@example.com",
    )
    db_session.add(conversation)
    db_session.commit()

    result = team_inbox_read.list_conversations(db_session, unread_only=True)
    assert result.count == 0


# --- Journey: the maintenance loop actually runs ------------------------------


@pytest.mark.parametrize(
    "task_name",
    [
        "app.tasks.team_inbox.release_scheduled_replies",
        "app.tasks.team_inbox.wake_due_snoozed_conversations",
        "app.tasks.team_inbox.retry_failed_outbound_messages",
        "app.tasks.team_inbox.promote_message_media_assets",
        "app.tasks.team_inbox.auto_resolve_stale_conversations",
    ],
)
def test_every_inbox_maintenance_task_is_registered_with_a_schedule(task_name):
    """These tasks existed, had reliability policies, and were never scheduled.

    Nothing seeded them into the DB scheduler and nothing named them in
    `build_beat_schedule`, so a reply the composer reported as "scheduled" was
    never sent.
    """
    import inspect

    from app.services import scheduler_config

    source = inspect.getsource(scheduler_config.build_beat_schedule)
    assert task_name in source
