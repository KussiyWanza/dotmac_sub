"""Realtime workqueue events: channel targeting and best-effort delivery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.service_team import (
    ServiceTeam,
    ServiceTeamMember,
    ServiceTeamMemberResponsibility,
    ServiceTeamResponsibilityKey,
)
from app.models.support import Ticket
from app.services import realtime_platform, workqueue
from app.services.realtime_platform import EventType
from app.services.workqueue import ItemKind, WorkqueuePrincipal, get_workqueue_scope
from app.services.workqueue.events import (
    channels_for_scope,
    emit_change,
    emit_item_change,
    org_channel,
    team_channel,
    user_channel,
)
from app.services.workqueue.permissions import AUDIENCE_TEAM_SCOPE
from tests.staff_identity_fixtures import add_bound_staff_user

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)


@pytest.fixture()
def published(monkeypatch):
    """Capture what would go on the wire."""
    sent: list[tuple[str, EventType, dict]] = []

    def _capture(topic, *, event_type, payload):
        sent.append((topic, event_type, payload))

    monkeypatch.setattr(realtime_platform, "publish_topic_event", _capture)
    return sent


def _principal(person_id=None, *, roles=(), scopes=()):
    return WorkqueuePrincipal(
        person_id=person_id or uuid4(),
        roles=frozenset(roles),
        scopes=frozenset(scopes),
        can_view=True,
        can_act=True,
    )


def _team(db, name="Support"):
    team = ServiceTeam(name=name)
    db.add(team)
    db.flush()
    return team


def _member(db, team, person_id, *, queue_lead=False):
    _user, person = add_bound_staff_user(db, system_user_id=person_id)
    member = ServiceTeamMember(team_id=team.id, person_id=person.id, role=None)
    db.add(member)
    db.flush()
    if queue_lead:
        db.add(
            ServiceTeamMemberResponsibility(
                membership_id=member.id,
                responsibility_key=ServiceTeamResponsibilityKey.queue_lead.value,
                is_active=True,
            )
        )
        db.flush()


def test_channel_names_match_the_documented_shape():
    person = uuid4()
    team = uuid4()
    assert user_channel(person) == f"workqueue:user:{person}"
    assert team_channel(team) == f"workqueue:audience:team:{team}"
    assert org_channel() == "workqueue:audience:org"


def test_a_plain_agent_only_listens_on_their_own_channel(db_session):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)

    scope = get_workqueue_scope(db_session, _principal(person))
    assert channels_for_scope(scope) == [user_channel(person)]


def test_a_team_lead_also_listens_on_their_team_channels(db_session):
    lead = uuid4()
    team = _team(db_session)
    _member(db_session, team, lead, queue_lead=True)

    scope = get_workqueue_scope(
        db_session,
        _principal(lead, scopes=(AUDIENCE_TEAM_SCOPE,)),
    )
    assert channels_for_scope(scope) == [user_channel(lead), team_channel(team.id)]


def test_an_admin_listens_on_the_org_channel(db_session):
    admin = _principal(roles=("admin",))
    scope = get_workqueue_scope(db_session, admin, requested_audience="org")
    channels = channels_for_scope(scope)
    assert channels[0] == user_channel(admin.person_id)
    assert org_channel() in channels


def test_emit_change_fans_out_to_every_affected_channel(published):
    person = uuid4()
    team = uuid4()
    item_id = uuid4()

    emit_change(
        item_kind=ItemKind.ticket,
        item_id=item_id,
        change="updated",
        affected_user_ids=[person],
        affected_team_ids=[team],
        affected_org=True,
        score=90,
        reason="sla_imminent",
    )

    topics = [topic for topic, _event, _payload in published]
    assert topics == [user_channel(person), team_channel(team), org_channel()]

    _topic, event_type, payload = published[0]
    assert event_type is EventType.WORKQUEUE_CHANGED
    assert payload["item_kind"] == "ticket"
    assert payload["item_id"] == str(item_id)
    assert payload["change"] == "updated"
    assert payload["score"] == 90
    assert payload["reason"] == "sla_imminent"


def test_a_reassignment_notifies_both_the_old_and_new_owner(published):
    previous = uuid4()
    current = uuid4()
    team = uuid4()

    emit_item_change(
        item_kind=ItemKind.ticket,
        item_id=uuid4(),
        change="updated",
        assignee_id=current,
        previous_assignee_id=previous,
        service_team_id=team,
    )

    topics = {topic for topic, _event, _payload in published}
    assert user_channel(previous) in topics
    assert user_channel(current) in topics
    assert team_channel(team) in topics
    assert org_channel() in topics


def test_a_dead_transport_never_breaks_the_write(monkeypatch):
    def _boom(topic, *, event_type, payload):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(realtime_platform, "publish_topic_event", _boom)

    emit_change(
        item_kind=ItemKind.ticket,
        item_id=uuid4(),
        change="added",
        affected_org=True,
    )  # must not raise


def test_snoozing_and_unsnoozing_push_to_the_owners_channel(db_session, published):
    person = uuid4()
    ticket = Ticket(title="Link down", status="open", priority="normal")
    db_session.add(ticket)
    db_session.commit()

    workqueue.snooze_item_committed(
        db_session,
        user_id=person,
        item_kind=ItemKind.ticket.value,
        item_id=ticket.id,
        snooze_until=NOW + timedelta(hours=1),
    )
    workqueue.clear_snooze_committed(
        db_session,
        user_id=person,
        item_kind=ItemKind.ticket.value,
        item_id=ticket.id,
    )

    assert [(topic, payload["change"]) for topic, _event, payload in published] == [
        (user_channel(person), "removed"),
        (user_channel(person), "added"),
    ]


def test_publishing_without_a_running_loop_is_safe():
    """The sync bridge is callable from ordinary service code."""
    realtime_platform.publish_topic_event(
        user_channel(uuid4()),
        event_type=EventType.WORKQUEUE_CHANGED,
        payload={"type": "workqueue.changed"},
    )
