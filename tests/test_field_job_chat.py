"""The job chat: one live conversation, opened by departure.

This replaces a suite that asserted the old store's behaviour. That store held
only one side of the conversation — every writer set ``direction="staff"``,
both endpoints were staff-authenticated, and no customer surface read it — so
"the chat works" was never actually true of the thing customers would use.

The design decision under test is that the chat opens when the technician
*departs*, not when the job is *assigned*: a technician holds several assigned
jobs at once, and a chat per assignment would put every one of those customers
in front of them while they can only be at one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.party import (
    Party,
    PartyDataClassification,
    PartyIdentityStatus,
    PartyType,
)
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamMember,
    ServiceTeamMemberRole,
    ServiceTeamType,
)
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
)
from app.models.work_order import WorkOrder
from app.services import customer_field_job_chat, team_inbox_field_job
from app.services.auth_dependencies import require_user_auth
from app.services.field.chat import field_job_chat


def _user(db_session, name: str = "Chat") -> SystemUser:
    person = Party(
        party_type=PartyType.person.value,
        display_name=f"{name} Tech",
        status=PartyIdentityStatus.active.value,
        data_classification=PartyDataClassification.test.value,
    )
    db_session.add(person)
    db_session.flush()
    user = SystemUser(
        first_name=name,
        last_name="Tech",
        display_name=f"{name} Tech",
        email=f"{name.lower()}-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
        person_party_id=person.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="field-job-chat-test",
        party_binding_reason="Reviewed field-job staff fixture",
    )
    db_session.add(user)
    db_session.flush()
    return user


def _auth(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _team_member(db_session, person_id) -> ServiceTeam:
    team = ServiceTeam(
        name=f"Field Ops {uuid4().hex[:6]}",
        team_type=ServiceTeamType.support.value,
    )
    db_session.add(team)
    db_session.flush()
    db_session.add(
        ServiceTeamMember(
            team_id=team.id,
            person_id=person_id,
            role=ServiceTeamMemberRole.member.value,
            is_active=True,
        )
    )
    db_session.flush()
    return team


def _profile(
    db_session,
    user: SystemUser,
    crm_person_id: str = "crm-chat-tech",
    *,
    with_team: bool = True,
) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=crm_person_id,
        title="Installer",
    )
    db_session.add(profile)
    db_session.flush()
    if with_team:
        assert user.person_party_id is not None
        _team_member(db_session, user.person_party_id)
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Chat",
        last_name="Customer",
        email=f"chat-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id=overrides.pop("crm_work_order_id", "wo-chat"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Chat job"),
        status=overrides.pop("status", "in_progress"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-chat-tech"
        ),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _depart(db_session, work_order, profile):
    conversation, outcome = team_inbox_field_job.open_for_departure(
        db_session, work_order=work_order, profile=profile
    )
    db_session.commit()
    return conversation, outcome


# --- when the chat exists ------------------------------------------------


def test_no_chat_before_the_technician_departs(db_session):
    """An assigned job is not yet a conversation."""
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-assigned")
    db_session.commit()

    thread = field_job_chat.get_thread(db_session, _auth(user), "wo-assigned")

    assert thread["available"] is False
    assert thread["messages"] == []
    assert thread["conversation_id"] is None


def test_departure_opens_the_chat(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-depart")
    db_session.commit()

    conversation, outcome = _depart(db_session, work_order, profile)

    assert outcome == team_inbox_field_job.OPENED
    assert conversation.channel_type == InboxChannelType.field_job.value
    assert conversation.external_thread_id == "wo-depart"
    assert conversation.subscriber_id == subscriber.id


def test_departure_fails_closed_when_staff_has_multiple_active_teams(db_session):
    user = _user(db_session, "Ambiguous")
    profile = _profile(db_session, user)
    assert user.person_party_id is not None
    _team_member(db_session, user.person_party_id)
    subscriber = _subscriber(db_session)
    work_order = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-ambiguous-team",
    )
    db_session.commit()

    conversation, outcome = _depart(db_session, work_order, profile)

    assert conversation is None
    assert outcome == team_inbox_field_job.NO_TEAM


def test_the_departing_technician_holds_the_conversation(db_session):
    """1:1 means the assignment names the technician, not just their team."""
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-assign")
    db_session.commit()

    conversation, _ = _depart(db_session, work_order, profile)

    active = [a for a in conversation.assignments if a.is_active]
    assert len(active) == 1
    assert active[0].person_id == profile.person_id


def test_departing_again_for_the_same_job_is_idempotent(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-again")
    db_session.commit()

    first, _ = _depart(db_session, work_order, profile)
    second, _ = _depart(db_session, work_order, profile)

    assert first.id == second.id


def test_departing_for_another_job_closes_the_previous_chat(db_session):
    """The invariant is one live chat per technician.

    ``arrive_movement`` falls back to the latest open ``en_route`` movement, so
    a technician who never taps Arrive would otherwise accumulate live chats.
    """
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    first_job = _work_order(db_session, subscriber, crm_work_order_id="wo-first")
    second_job = _work_order(db_session, subscriber, crm_work_order_id="wo-second")
    db_session.commit()

    first_conversation, _ = _depart(db_session, first_job, profile)
    _depart(db_session, second_job, profile)
    db_session.refresh(first_conversation)

    assert first_conversation.status == InboxConversationStatus.resolved.value


def test_a_job_with_no_team_membership_gets_no_chat(db_session):
    """The assignment row requires a team; an unheld chat is worse than none."""
    user = _user(db_session)
    profile = _profile(db_session, user, with_team=False)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-noteam")
    db_session.commit()

    conversation, outcome = _depart(db_session, work_order, profile)

    assert conversation is None
    assert outcome == team_inbox_field_job.NO_TEAM


# --- sending -------------------------------------------------------------


def test_technician_and_customer_both_appear_in_the_thread(db_session):
    """The half that never existed on the old store: the customer's side."""
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-two-sided")
    db_session.commit()
    _depart(db_session, work_order, profile)

    field_job_chat.send_message(
        db_session, _auth(user), "wo-two-sided", body="  On my way  "
    )
    customer_field_job_chat.send_message(
        db_session,
        str(subscriber.id),
        "wo-two-sided",
        body="The gate is locked, use the side entrance",
    )

    thread = field_job_chat.get_thread(db_session, _auth(user), "wo-two-sided")
    assert [(m["direction"], m["body"]) for m in thread["messages"]] == [
        ("staff", "On my way"),
        ("customer", "The gate is locked, use the side entrance"),
    ]


def test_a_technician_message_is_delivered_not_queued(db_session):
    """In-app delivery has no external transport to wait on."""
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-delivered")
    db_session.commit()
    conversation, _ = _depart(db_session, work_order, profile)

    field_job_chat.send_message(db_session, _auth(user), "wo-delivered", body="Outside")

    message = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .one()
    )
    assert message.sent_at is not None
    assert message.notification_id is None
    assert (message.metadata_ or {})["delivery_status"] == "delivered"


def test_the_technician_name_survives_onto_the_message(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-author")
    db_session.commit()
    _depart(db_session, work_order, profile)

    sent = field_job_chat.send_message(
        db_session, _auth(user), "wo-author", body="Five minutes away"
    )

    assert sent["author_name"] == "Chat Tech"


def test_an_empty_message_is_refused(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-empty")
    db_session.commit()
    _depart(db_session, work_order, profile)

    with pytest.raises(HTTPException) as exc:
        field_job_chat.send_message(db_session, _auth(user), "wo-empty", body="   ")
    assert exc.value.status_code == 422


def test_sending_before_departure_is_refused(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-early")
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_job_chat.send_message(db_session, _auth(user), "wo-early", body="Hello")
    assert exc.value.status_code == 409


def test_chat_scoped_to_assigned_technician(db_session):
    """Unchanged from the old store: another technician's job is not visible."""
    user = _user(db_session)
    _profile(db_session, user)
    other = _user(db_session, "Other")
    _profile(db_session, other, crm_person_id="other-chat-tech")
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-chat-hidden",
        assigned_to_crm_person_id="other-chat-tech",
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_job_chat.get_thread(db_session, _auth(user), "wo-chat-hidden")
    assert exc.value.status_code == 404


# --- the customer's side -------------------------------------------------


def test_a_customer_cannot_reach_another_subscribers_visit(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    stranger = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-scoped")
    db_session.commit()
    _depart(db_session, work_order, profile)

    thread = customer_field_job_chat.get_thread(
        db_session, str(stranger.id), "wo-scoped"
    )

    assert thread["available"] is False
    assert thread["reason"] == "not_found"


def test_the_customer_is_told_the_chat_has_not_opened_yet(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-waiting")
    db_session.commit()

    thread = customer_field_job_chat.get_thread(
        db_session, str(subscriber.id), "wo-waiting"
    )

    assert thread["available"] is False
    assert thread["reason"] == "not_departed"


def test_a_customer_message_lands_as_inbound(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-inbound")
    db_session.commit()
    conversation, _ = _depart(db_session, work_order, profile)

    customer_field_job_chat.send_message(
        db_session, str(subscriber.id), "wo-inbound", body="I'm at the back"
    )

    message = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .one()
    )
    assert message.direction == InboxMessageDirection.inbound.value
    assert message.received_at is not None


# --- closing -------------------------------------------------------------


def test_completing_the_visit_closes_the_chat(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-complete")
    db_session.commit()
    conversation, _ = _depart(db_session, work_order, profile)
    field_job_chat.send_message(db_session, _auth(user), "wo-complete", body="All done")

    team_inbox_field_job.close_for_work_order(
        db_session, work_order=work_order, reason="complete"
    )

    assert conversation.status == InboxConversationStatus.resolved.value


def test_a_closed_chat_keeps_its_history_readable(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-history")
    db_session.commit()
    _depart(db_session, work_order, profile)
    field_job_chat.send_message(db_session, _auth(user), "wo-history", body="Finished")
    team_inbox_field_job.close_for_work_order(
        db_session, work_order=work_order, reason="complete"
    )
    db_session.commit()

    thread = field_job_chat.get_thread(db_session, _auth(user), "wo-history")

    assert thread["available"] is True
    assert thread["can_send"] is False
    assert [m["body"] for m in thread["messages"]] == ["Finished"]


def test_an_unanswered_customer_goes_to_the_queue_instead_of_closing(db_session):
    """A job chat is kept out of the triage queue, so nothing else watches it.

    Resolving here would drop the customer's last message silently.
    """
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-unanswered")
    db_session.commit()
    conversation, _ = _depart(db_session, work_order, profile)
    customer_field_job_chat.send_message(
        db_session, str(subscriber.id), "wo-unanswered", body="Are you still coming?"
    )

    team_inbox_field_job.close_for_work_order(
        db_session, work_order=work_order, reason="complete"
    )

    assert conversation.status == InboxConversationStatus.open.value
    assert (conversation.metadata_ or {})[
        team_inbox_field_job.QUEUE_FOLLOWUP_KEY
    ] is True
    assert not [a for a in conversation.assignments if a.is_active]


def test_an_answered_customer_closes_normally(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-answered")
    db_session.commit()
    conversation, _ = _depart(db_session, work_order, profile)
    customer_field_job_chat.send_message(
        db_session, str(subscriber.id), "wo-answered", body="Are you close?"
    )
    field_job_chat.send_message(db_session, _auth(user), "wo-answered", body="Two mins")

    team_inbox_field_job.close_for_work_order(
        db_session, work_order=work_order, reason="complete"
    )

    assert conversation.status == InboxConversationStatus.resolved.value


# --- the field app's endpoints ------------------------------------------


def test_chat_api(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-chat-api")
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    before = client.get("/api/v1/field/jobs/wo-chat-api/chat")
    assert before.status_code == 200
    assert before.json()["available"] is False

    _depart(db_session, work_order, profile)

    sent = client.post(
        "/api/v1/field/jobs/wo-chat-api/chat/messages",
        json={"body": "Hello from the field"},
    )
    assert sent.status_code == 201
    assert sent.json()["direction"] == "staff"
    assert sent.json()["body"] == "Hello from the field"

    thread = client.get("/api/v1/field/jobs/wo-chat-api/chat")
    assert thread.status_code == 200
    assert len(thread.json()["messages"]) == 1
    assert thread.json()["messages"][0]["author_name"] == "Chat Tech"

    blank = client.post(
        "/api/v1/field/jobs/wo-chat-api/chat/messages", json={"body": "   "}
    )
    assert blank.status_code == 422
