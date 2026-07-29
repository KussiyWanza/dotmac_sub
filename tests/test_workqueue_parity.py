"""Workqueue: providers, aggregation/ranking, SLA bands, permissions and scope."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamMember,
    ServiceTeamMemberResponsibility,
    ServiceTeamResponsibilityKey,
)
from app.models.support import Ticket, TicketPriority, TicketStatus
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
)
from app.models.ticket_workflow import (
    SlaClock,
    SlaClockStatus,
    SlaPolicy,
    WorkflowEntityType,
)
from app.models.work_order import WorkOrder
from app.services import workqueue
from app.services.workqueue import (
    ActionKind,
    ItemKind,
    WorkqueueAudience,
    WorkqueueItem,
    WorkqueuePermissionError,
    WorkqueuePrincipal,
    WorkqueueScoringConfig,
    build_workqueue,
    get_workqueue_scope,
    list_workqueue,
    load_scoring_config,
)
from app.services.workqueue.permissions import AUDIENCE_TEAM_SCOPE
from app.services.workqueue.providers import all_providers, register
from app.services.workqueue.providers.conversations import conversation_provider
from app.services.workqueue.providers.tickets import ticket_provider
from app.services.workqueue.providers.work_orders import work_order_provider
from tests.staff_identity_fixtures import add_bound_staff_user

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
CONFIG = WorkqueueScoringConfig()


# --- factories ---------------------------------------------------------------


def _principal(
    person_id: UUID | None = None,
    *,
    roles: tuple[str, ...] = (),
    scopes: tuple[str, ...] = (),
    can_view: bool = True,
    can_act: bool = True,
) -> WorkqueuePrincipal:
    return WorkqueuePrincipal(
        person_id=person_id or uuid4(),
        roles=frozenset(roles),
        scopes=frozenset(scopes),
        can_view=can_view,
        can_act=can_act,
    )


def _team(db, name: str = "Support") -> ServiceTeam:
    team = ServiceTeam(name=name)
    db.add(team)
    db.flush()
    return team


def _member(
    db,
    team: ServiceTeam,
    person_id: UUID,
    *,
    responsibility: ServiceTeamResponsibilityKey | None = None,
) -> ServiceTeamMember:
    _user, person = add_bound_staff_user(db, system_user_id=person_id)
    member = ServiceTeamMember(team_id=team.id, person_id=person.id, role=None)
    db.add(member)
    db.flush()
    if responsibility is not None:
        db.add(
            ServiceTeamMemberResponsibility(
                membership_id=member.id,
                responsibility_key=responsibility.value,
                is_active=True,
            )
        )
        db.flush()
    return member


def _ticket(
    db,
    *,
    title: str = "Link down",
    team: ServiceTeam | None = None,
    assigned_to: UUID | None = None,
    due_at: datetime | None = None,
    priority: str = TicketPriority.normal.value,
    status: str = TicketStatus.open.value,
) -> Ticket:
    ticket = Ticket(
        title=title,
        status=status,
        priority=priority,
        due_at=due_at,
        service_team_id=team.id if team else None,
        assigned_to_person_id=assigned_to,
        updated_at=NOW - timedelta(minutes=5),
    )
    db.add(ticket)
    db.flush()
    return ticket


def _conversation(
    db,
    *,
    team: ServiceTeam | None = None,
    assigned_to: UUID | None = None,
    subject: str = "Where is my invoice?",
    last_message_at: datetime | None = None,
    priority: int = 100,
) -> InboxConversation:
    conversation = InboxConversation(
        subject=subject,
        status=InboxConversationStatus.open.value,
        priority=priority,
        primary_service_team_id=team.id if team else None,
        last_message_at=last_message_at or NOW - timedelta(minutes=1),
    )
    db.add(conversation)
    db.flush()
    if assigned_to is not None:
        assert team is not None, "an inbox assignment always carries a team"
        db.add(
            InboxConversationAssignment(
                conversation_id=conversation.id,
                service_team_id=team.id,
                person_id=assigned_to,
            )
        )
        db.flush()
    return conversation


def _message(
    db,
    conversation: InboxConversation,
    *,
    direction: str = InboxMessageDirection.inbound.value,
    created_at: datetime,
) -> InboxMessage:
    message = InboxMessage(
        conversation_id=conversation.id,
        direction=direction,
        body="hello",
        created_at=created_at,
    )
    db.add(message)
    db.flush()
    return message


def _work_order(
    db,
    subscriber,
    *,
    status: str = "scheduled",
    scheduled_start: datetime | None = None,
    assigned_to_crm_person_id: str | None = None,
) -> WorkOrder:
    work_order = WorkOrder(
        crm_work_order_id=f"WO-{uuid4().hex[:8]}",
        subscriber_id=subscriber.id,
        title="Fibre install",
        status=status,
        scheduled_start=scheduled_start,
        assigned_to_crm_person_id=assigned_to_crm_person_id,
        updated_at=NOW - timedelta(minutes=10),
    )
    db.add(work_order)
    db.flush()
    return work_order


def _sla_clock(
    db, ticket: Ticket, *, due_at: datetime, breached: bool = False
) -> SlaClock:
    policy = SlaPolicy(
        name=f"policy-{uuid4().hex[:6]}",
        entity_type=WorkflowEntityType.ticket.value,
    )
    db.add(policy)
    db.flush()
    clock = SlaClock(
        policy_id=policy.id,
        entity_type=WorkflowEntityType.ticket.value,
        entity_id=ticket.id,
        status=(
            SlaClockStatus.breached.value if breached else SlaClockStatus.running.value
        ),
        started_at=NOW - timedelta(hours=4),
        due_at=due_at,
        breached_at=due_at if breached else None,
    )
    db.add(clock)
    db.flush()
    return clock


def _scope(db, principal, **kwargs):
    return get_workqueue_scope(db, principal, **kwargs)


def _fetch(provider, db, scope, *, snoozed=None, now=NOW, config=CONFIG):
    return provider.fetch(
        db,
        scope=scope,
        config=config,
        snoozed_ids=snoozed or set(),
        now=now,
        limit=config.provider_limit,
    )


# --- provider isolation ------------------------------------------------------


def test_registry_exposes_one_provider_per_kind():
    kinds = [provider.kind for provider in all_providers()]
    assert kinds == list(ItemKind)


def test_each_provider_only_emits_its_own_kind(db_session, subscriber):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    db_session.add(TechnicianProfile(person_id=person, crm_person_id="crm-me"))
    _ticket(db_session, team=team)
    _conversation(db_session, team=team)
    _work_order(db_session, subscriber, assigned_to_crm_person_id="crm-me")

    scope = _scope(db_session, _principal(person))
    for provider in all_providers():
        items = _fetch(provider, db_session, scope)
        assert items, f"{provider.kind} produced nothing"
        assert {item.item_kind for item in items} == {provider.kind}


def test_a_new_provider_plugs_in_without_touching_the_aggregator(db_session):
    """The aggregator ranks whatever providers hand it — no per-source code."""

    class StubProvider:
        kind = ItemKind.work_order

        def fetch(self, db, *, scope, config, snoozed_ids, now, limit):
            return [
                WorkqueueItem(
                    item_kind=ItemKind.work_order,
                    item_id=uuid4(),
                    title="Synthetic",
                    subtitle=None,
                    status="scheduled",
                    priority=40,
                    score=99,
                    reason="synthetic",
                    urgency=config.urgency_for_score(99),
                    happened_at=now,
                    actions=(ActionKind.open,),
                )
            ]

    stub = StubProvider()
    view = build_workqueue(
        db_session,
        _principal(),
        providers=(stub,),
        config=CONFIG,
        now=NOW,
    )
    assert [item.reason for item in view.right_now] == ["synthetic"]

    # …and registering it makes it a first-class source.
    registered = register(stub)
    try:
        assert registered in all_providers()
    finally:
        register(work_order_provider)


# --- SLA scoring / band boundaries -------------------------------------------


@pytest.mark.parametrize(
    ("seconds_to_due", "expected_reason", "expected_urgency"),
    [
        (-1, "sla_breach", "critical"),
        (0, "sla_breach", "critical"),
        (CONFIG.ticket_sla.imminent_seconds, "sla_imminent", "critical"),
        (CONFIG.ticket_sla.imminent_seconds + 1, "sla_soon", "high"),
        (CONFIG.ticket_sla.soon_seconds, "sla_soon", "high"),
        (CONFIG.ticket_sla.soon_seconds + 1, "in_queue", "low"),
    ],
)
def test_ticket_sla_band_boundaries(
    db_session, seconds_to_due, expected_reason, expected_urgency
):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    _ticket(
        db_session,
        team=team,
        assigned_to=person,
        status=TicketStatus.pending.value,  # keep `awaiting_triage` out of it
        due_at=NOW + timedelta(seconds=seconds_to_due),
    )

    scope = _scope(db_session, _principal(person))
    [item] = _fetch(ticket_provider, db_session, scope)
    assert item.reason == expected_reason
    assert item.urgency == expected_urgency


def test_sla_thresholds_are_configurable_by_env(monkeypatch):
    monkeypatch.setenv("WORKQUEUE_TICKET_SLA_IMMINENT_SECONDS", "3600")
    monkeypatch.setenv("WORKQUEUE_TICKET_SLA_SOON_SCORE", "42")
    config = load_scoring_config()

    assert config.ticket_sla.imminent_seconds == 3600
    assert config.ticket_sla.band(1800) == ("sla_imminent", 90)
    assert config.ticket_sla.band(5400) == ("sla_soon", 42)


def test_ticket_sla_clock_beats_the_tickets_own_due_at(db_session):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    ticket = _ticket(
        db_session,
        team=team,
        assigned_to=person,
        status=TicketStatus.pending.value,
        due_at=NOW + timedelta(days=3),  # nowhere near due
    )
    _sla_clock(db_session, ticket, due_at=NOW - timedelta(minutes=5), breached=True)

    scope = _scope(db_session, _principal(person))
    [item] = _fetch(ticket_provider, db_session, scope)
    assert item.reason == "sla_breach"
    assert item.score == CONFIG.ticket_sla.breach_score


def test_urgent_priority_outranks_a_quiet_queue_item(db_session):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    _ticket(
        db_session,
        title="Quiet",
        team=team,
        assigned_to=person,
        status=TicketStatus.pending.value,
    )
    _ticket(
        db_session,
        title="Urgent",
        team=team,
        assigned_to=person,
        status=TicketStatus.pending.value,
        priority=TicketPriority.urgent.value,
    )

    scope = _scope(db_session, _principal(person))
    items = _fetch(ticket_provider, db_session, scope)
    by_title = {item.title: item for item in items}
    assert by_title["Urgent"].reason == "priority_urgent"
    assert by_title["Urgent"].score > by_title["Quiet"].score


# --- operational inbox integration -------------------------------------------


def test_unanswered_conversation_gets_an_sla_band(db_session):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    conversation = _conversation(db_session, team=team)
    # Inbound 40 minutes ago, reply target 15m => 25m past due.
    _message(db_session, conversation, created_at=NOW - timedelta(minutes=40))

    scope = _scope(db_session, _principal(person))
    [item] = _fetch(conversation_provider, db_session, scope)
    assert item.reason == "sla_breach"
    assert item.urgency == "critical"
    assert item.metadata["awaiting_reply_since"] is not None


def test_answered_conversation_drops_off_the_sla_clock(db_session):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    conversation = _conversation(db_session, team=team, assigned_to=person)
    _message(db_session, conversation, created_at=NOW - timedelta(minutes=40))
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        created_at=NOW - timedelta(minutes=30),
    )

    scope = _scope(db_session, _principal(person))
    [item] = _fetch(conversation_provider, db_session, scope)
    assert item.reason == "in_inbox"
    assert item.due_at is None


def test_the_inbox_is_a_section_of_the_workqueue(db_session):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    conversation = _conversation(db_session, team=team)
    _message(db_session, conversation, created_at=NOW - timedelta(minutes=40))

    view = build_workqueue(db_session, _principal(person), config=CONFIG, now=NOW)
    sections = {section.item_kind: section for section in view.sections}
    assert sections[ItemKind.conversation].total == 1
    assert sections[ItemKind.conversation].items[0].item_id == conversation.id


# --- aggregation / ranking ---------------------------------------------------


def test_hero_band_is_ranked_by_score_and_capped(db_session, subscriber):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    db_session.add(TechnicianProfile(person_id=person, crm_person_id="crm-me"))

    breaching = _ticket(
        db_session,
        title="Breaching",
        team=team,
        assigned_to=person,
        status=TicketStatus.pending.value,
        due_at=NOW - timedelta(minutes=1),
    )
    quiet = _ticket(
        db_session,
        title="Quiet",
        team=team,
        assigned_to=person,
        status=TicketStatus.pending.value,
    )
    conversation = _conversation(db_session, team=team)
    _message(db_session, conversation, created_at=NOW - timedelta(minutes=8))
    _work_order(
        db_session,
        subscriber,
        scheduled_start=NOW + timedelta(days=2),
        assigned_to_crm_person_id="crm-me",
    )

    view = build_workqueue(
        db_session,
        _principal(person),
        config=CONFIG,
        now=NOW,
        hero_band_size=2,
    )
    assert len(view.right_now) == 2
    assert view.right_now[0].item_id == breaching.id  # sla_breach (100)
    assert view.right_now[1].item_id == conversation.id  # sla_imminent (90)

    scores = [item.score for item in view.right_now]
    assert scores == sorted(scores, reverse=True)
    assert view.total == 4
    assert quiet.id not in {item.item_id for item in view.right_now}


def test_list_workqueue_paginates_the_ranked_queue(db_session):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    for index in range(3):
        _ticket(
            db_session,
            title=f"T{index}",
            team=team,
            assigned_to=person,
            status=TicketStatus.pending.value,
            due_at=NOW - timedelta(minutes=index),
        )

    first = list_workqueue(
        db_session, _principal(person), limit=2, offset=0, config=CONFIG, now=NOW
    )
    second = list_workqueue(
        db_session, _principal(person), limit=2, offset=2, config=CONFIG, now=NOW
    )
    assert len(first) == 2
    assert len(second) == 1
    assert {item.item_id for item in first}.isdisjoint({i.item_id for i in second})


def test_list_workqueue_fetches_enough_candidates_for_later_pages(db_session):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    for index in range(5):
        _ticket(
            db_session,
            title=f"T{index}",
            team=team,
            assigned_to=person,
            status=TicketStatus.pending.value,
            due_at=NOW - timedelta(minutes=index),
        )

    page = list_workqueue(
        db_session,
        _principal(person),
        limit=2,
        offset=3,
        config=WorkqueueScoringConfig(provider_limit=2),
        now=NOW,
    )
    assert len(page) == 2


# --- scope: items a user must not see ----------------------------------------


def test_another_teams_work_is_invisible(db_session):
    person = uuid4()
    mine = _team(db_session, "Mine")
    theirs = _team(db_session, "Theirs")
    _member(db_session, mine, person)
    _member(db_session, theirs, uuid4())

    my_ticket = _ticket(db_session, title="Mine", team=mine)
    _ticket(db_session, title="Theirs", team=theirs)
    my_conversation = _conversation(db_session, team=mine, subject="Mine")
    _conversation(db_session, team=theirs, subject="Theirs")

    view = build_workqueue(db_session, _principal(person), config=CONFIG, now=NOW)
    visible = {item.item_id for section in view.sections for item in section.items}
    assert my_ticket.id in visible
    assert my_conversation.id in visible
    assert len(visible) == 2  # nothing from the other team leaked in


def test_self_audience_hides_a_teammates_assigned_work(db_session):
    person = uuid4()
    teammate = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    _member(db_session, team, teammate)

    mine = _ticket(db_session, title="Mine", team=team, assigned_to=person)
    unclaimed = _ticket(db_session, title="Unclaimed", team=team)
    theirs = _ticket(db_session, title="Theirs", team=team, assigned_to=teammate)

    scope = _scope(db_session, _principal(person))
    assert scope.audience is WorkqueueAudience.self_
    seen = {item.item_id for item in _fetch(ticket_provider, db_session, scope)}
    assert seen == {mine.id, unclaimed.id}
    assert theirs.id not in seen


def test_team_lead_sees_the_whole_team_queue(db_session):
    lead = uuid4()
    teammate = uuid4()
    team = _team(db_session)
    _member(
        db_session,
        team,
        lead,
        responsibility=ServiceTeamResponsibilityKey.queue_lead,
    )
    _member(db_session, team, teammate)
    theirs = _ticket(db_session, title="Theirs", team=team, assigned_to=teammate)

    scope = _scope(
        db_session,
        _principal(lead, scopes=(AUDIENCE_TEAM_SCOPE,)),
    )
    assert scope.audience is WorkqueueAudience.team
    seen = {item.item_id for item in _fetch(ticket_provider, db_session, scope)}
    assert theirs.id in seen


def test_a_lead_responsibility_without_the_rbac_scope_stays_self(db_session):
    """Operational responsibility alone never widens the audience."""
    lead = uuid4()
    teammate = uuid4()
    team = _team(db_session)
    _member(
        db_session,
        team,
        lead,
        responsibility=ServiceTeamResponsibilityKey.queue_lead,
    )
    _member(db_session, team, teammate)
    theirs = _ticket(db_session, title="Theirs", team=team, assigned_to=teammate)

    scope = _scope(db_session, _principal(lead))
    assert scope.audience is WorkqueueAudience.self_
    seen = {item.item_id for item in _fetch(ticket_provider, db_session, scope)}
    assert theirs.id not in seen

    # Explicitly asking for `team` is clamped back to `self` too.
    requested = _scope(db_session, _principal(lead), requested_audience="team")
    assert requested.audience is WorkqueueAudience.self_


def test_the_rbac_scope_without_a_responsibility_stays_self(db_session):
    """RBAC alone cannot invent operational team ownership."""
    person = uuid4()
    teammate = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    _member(db_session, team, teammate)
    theirs = _ticket(db_session, title="Theirs", team=team, assigned_to=teammate)

    scope = _scope(db_session, _principal(person, scopes=(AUDIENCE_TEAM_SCOPE,)))
    assert scope.audience is WorkqueueAudience.self_
    seen = {item.item_id for item in _fetch(ticket_provider, db_session, scope)}
    assert theirs.id not in seen

    requested = _scope(
        db_session,
        _principal(person, scopes=(AUDIENCE_TEAM_SCOPE,)),
        requested_audience="team",
    )
    assert requested.audience is WorkqueueAudience.self_


def test_org_audience_is_clamped_to_what_the_principal_holds(db_session):
    person = uuid4()
    other_team = _team(db_session, "Other")
    _member(db_session, other_team, uuid4())
    hidden = _ticket(db_session, title="Elsewhere", team=other_team)

    # A plain agent asking for `org` is downscoped to `self`.
    scope = _scope(db_session, _principal(person), requested_audience="org")
    assert scope.audience is WorkqueueAudience.self_
    assert not _fetch(ticket_provider, db_session, scope)

    # An admin genuinely gets org-wide visibility.
    admin_scope = _scope(
        db_session, _principal(uuid4(), roles=("admin",)), requested_audience="org"
    )
    assert admin_scope.audience is WorkqueueAudience.org
    seen = {item.item_id for item in _fetch(ticket_provider, db_session, admin_scope)}
    assert hidden.id in seen


def test_filtering_on_a_team_you_are_not_in_is_refused(db_session):
    person = uuid4()
    mine = _team(db_session, "Mine")
    theirs = _team(db_session, "Theirs")
    _member(db_session, mine, person)

    with pytest.raises(WorkqueuePermissionError):
        _scope(db_session, _principal(person), service_team_id=theirs.id)


def test_viewless_principal_cannot_build_a_queue(db_session):
    with pytest.raises(WorkqueuePermissionError):
        build_workqueue(
            db_session,
            _principal(can_view=False, can_act=False),
            config=CONFIG,
            now=NOW,
        )


# --- permissions: actions ----------------------------------------------------


def test_read_only_principal_loses_the_mutating_actions(db_session):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    _ticket(db_session, team=team, assigned_to=person)

    view = build_workqueue(
        db_session,
        _principal(person, can_act=False),
        config=CONFIG,
        now=NOW,
    )
    [item] = view.right_now
    assert item.can_act is False
    assert ActionKind.claim not in item.actions
    assert ActionKind.complete not in item.actions
    assert ActionKind.snooze in item.actions  # snoozing is a personal view action


def test_a_teammates_item_is_visible_but_not_actionable_at_self_audience():
    principal = _principal()
    assert (
        workqueue.can_act_on_item(
            principal,
            item_assignee_id=uuid4(),
            audience=WorkqueueAudience.self_,
        )
        is False
    )
    assert (
        workqueue.can_act_on_item(
            principal,
            item_assignee_id=None,
            audience=WorkqueueAudience.self_,
        )
        is True
    )
    assert (
        workqueue.can_act_on_item(
            principal,
            item_assignee_id=uuid4(),
            audience=WorkqueueAudience.team,
        )
        is True
    )


# --- work-order mirror -------------------------------------------------------


def test_self_audience_only_surfaces_attributable_work_orders(db_session, subscriber):
    person = uuid4()
    teammate = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    _member(db_session, team, teammate)
    db_session.add_all(
        [
            TechnicianProfile(person_id=person, crm_person_id="crm-me"),
            TechnicianProfile(person_id=teammate, crm_person_id="crm-teammate"),
        ]
    )
    mine = _work_order(db_session, subscriber, assigned_to_crm_person_id="crm-me")
    _work_order(db_session, subscriber, assigned_to_crm_person_id="crm-teammate")
    _work_order(db_session, subscriber)
    _work_order(db_session, subscriber, assigned_to_crm_person_id="crm-unmapped")

    scope = _scope(db_session, _principal(person))
    items = _fetch(work_order_provider, db_session, scope)
    assert [item.item_id for item in items] == [mine.id]
    assert items[0].assigned_person_id == person
    # The mirror is CRM's record — sub only offers open/snooze on it.
    assert set(items[0].actions) == {ActionKind.open, ActionKind.snooze}


def test_a_team_lead_sees_attributable_team_work_orders(db_session, subscriber):
    lead = uuid4()
    teammate = uuid4()
    team = _team(db_session)
    _member(
        db_session,
        team,
        lead,
        responsibility=ServiceTeamResponsibilityKey.queue_lead,
    )
    _member(db_session, team, teammate)
    db_session.add(TechnicianProfile(person_id=teammate, crm_person_id="crm-teammate"))
    work_order = _work_order(
        db_session, subscriber, assigned_to_crm_person_id="crm-teammate"
    )

    scope = _scope(
        db_session,
        _principal(lead, scopes=(AUDIENCE_TEAM_SCOPE,)),
        service_team_id=team.id,
    )
    assert [
        item.item_id for item in _fetch(work_order_provider, db_session, scope)
    ] == [work_order.id]


def test_native_dispatch_assignment_is_the_work_order_scope_fallback(
    db_session, subscriber
):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    profile = TechnicianProfile(person_id=person)
    work_order = _work_order(db_session, subscriber)
    db_session.add(profile)
    db_session.flush()
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=work_order.id,
            assigned_technician_id=profile.id,
            status="assigned",
        )
    )
    db_session.flush()

    scope = _scope(db_session, _principal(person))
    [item] = _fetch(work_order_provider, db_session, scope)
    assert item.item_id == work_order.id
    assert item.assigned_person_id == person


def test_org_audience_surfaces_unassigned_and_unmapped_work_orders(
    db_session, subscriber
):
    unassigned = _work_order(db_session, subscriber)
    unmapped = _work_order(
        db_session, subscriber, assigned_to_crm_person_id="crm-unmapped"
    )

    scope = _scope(
        db_session,
        _principal(uuid4(), roles=("admin",)),
        requested_audience="org",
    )
    assert {
        item.item_id for item in _fetch(work_order_provider, db_session, scope)
    } == {
        unassigned.id,
        unmapped.id,
    }


def test_org_team_filter_does_not_widen_to_unmapped_work_orders(db_session, subscriber):
    teammate = uuid4()
    team = _team(db_session)
    _member(db_session, team, teammate)
    db_session.add(TechnicianProfile(person_id=teammate, crm_person_id="crm-teammate"))
    attributable = _work_order(
        db_session, subscriber, assigned_to_crm_person_id="crm-teammate"
    )
    _work_order(db_session, subscriber)
    _work_order(db_session, subscriber, assigned_to_crm_person_id="crm-unmapped")

    scope = _scope(
        db_session,
        _principal(uuid4(), roles=("admin",)),
        requested_audience="org",
        service_team_id=team.id,
    )
    assert [
        item.item_id for item in _fetch(work_order_provider, db_session, scope)
    ] == [attributable.id]


# --- snooze ------------------------------------------------------------------


def test_snoozing_removes_an_item_until_it_is_cleared(db_session):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    ticket = _ticket(db_session, team=team, assigned_to=person)
    db_session.commit()

    workqueue.snooze_item_committed(
        db_session,
        user_id=person,
        item_kind=ItemKind.ticket.value,
        item_id=ticket.id,
        snooze_until=NOW + timedelta(hours=2),
    )

    view = build_workqueue(db_session, _principal(person), config=CONFIG, now=NOW)
    assert view.total == 0

    with_snoozed = build_workqueue(
        db_session, _principal(person), config=CONFIG, now=NOW, include_snoozed=True
    )
    assert with_snoozed.total == 1

    # An expired snooze stops hiding the item.
    later = build_workqueue(
        db_session, _principal(person), config=CONFIG, now=NOW + timedelta(hours=3)
    )
    assert later.total == 1

    workqueue.clear_snooze_committed(
        db_session,
        user_id=person,
        item_kind=ItemKind.ticket.value,
        item_id=ticket.id,
    )
    assert (
        build_workqueue(db_session, _principal(person), config=CONFIG, now=NOW).total
        == 1
    )


def test_snoozes_are_per_user(db_session):
    person = uuid4()
    colleague = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    _member(db_session, team, colleague)
    ticket = _ticket(db_session, team=team)
    db_session.commit()

    workqueue.snooze_item_committed(
        db_session,
        user_id=person,
        item_kind=ItemKind.ticket.value,
        item_id=ticket.id,
        snooze_until=NOW + timedelta(hours=2),
    )

    assert (
        build_workqueue(db_session, _principal(person), config=CONFIG, now=NOW).total
        == 0
    )
    assert (
        build_workqueue(db_session, _principal(colleague), config=CONFIG, now=NOW).total
        == 1
    )


def test_until_next_reply_snooze_is_released_by_an_inbound_message(db_session):
    person = uuid4()
    team = _team(db_session)
    _member(db_session, team, person)
    conversation = _conversation(db_session, team=team)
    _message(db_session, conversation, created_at=NOW - timedelta(minutes=40))
    db_session.commit()

    workqueue.snooze_item_committed(
        db_session,
        user_id=person,
        item_kind=ItemKind.conversation.value,
        item_id=conversation.id,
        until_next_reply=True,
    )
    assert (
        build_workqueue(db_session, _principal(person), config=CONFIG, now=NOW).total
        == 0
    )

    affected = workqueue.release_until_next_reply(
        db_session, conversation_id=conversation.id
    )
    db_session.commit()
    assert affected == [person]
    assert (
        build_workqueue(db_session, _principal(person), config=CONFIG, now=NOW).total
        == 1
    )
