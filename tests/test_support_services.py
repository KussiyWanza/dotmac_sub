from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.subscriber import Subscriber, SubscriberContact
from app.models.subscription_engine import SettingValueType
from app.models.support import (
    AutomationActionType,
    AutomationTrigger,
    Ticket,
    TicketAccessToken,
    TicketAssignee,
    TicketChannel,
    TicketComment,
    TicketCommentAuthorType,
    TicketSlaEvent,
    TicketStatus,
)
from app.models.system_user import SystemUser
from app.models.ticket_workflow import TicketAssignmentRule, TicketAssignmentStrategy
from app.schemas.support import (
    TicketCommentCreate,
    TicketCreate,
    TicketMergeRequest,
    TicketUpdate,
)
from app.services import support as support_service
from app.services import support_automation_rules
from app.services import support_ticket_settings as support_ticket_settings_service
from app.services import web_support_tickets as web_support_tickets_service
from app.services.customer_identity_resolution import (
    rebuild_identity_index_for_subscriber,
)
from app.services.customer_support_links import ticket_customer_any_link_filter
from app.services.domain_errors import DomainError
from tests.staff_identity_fixtures import add_bound_staff_user


def _ticket_payload(subscriber_id):
    return TicketCreate(
        title="Internet unstable",
        description="Packet loss observed",
        subscriber_id=subscriber_id,
        customer_account_id=subscriber_id,
        channel="web",
        priority="normal",
    )


def _system_user(**overrides) -> SystemUser:
    return SystemUser(
        first_name=overrides.pop("first_name", "Support"),
        last_name=overrides.pop("last_name", "Tech"),
        display_name=overrides.pop("display_name", "Support Tech"),
        email=overrides.pop("email", f"{uuid4().hex}@example.com"),
        phone=overrides.pop("phone", "+15550000000"),
        **overrides,
    )


def _enable_support_ticket_notifications(db_session) -> None:
    db_session.add(
        DomainSetting(
            domain=SettingDomain.notification,
            key=support_service.SUPPORT_NOTIFICATION_TOGGLE_KEY,
            value_type=SettingValueType.boolean,
            value_text="true",
            is_active=True,
        )
    )
    db_session.commit()


def test_ticket_customer_any_link_filter_matches_all_customer_link_fields(db_session):
    account = Subscriber(
        first_name="Support",
        last_name="Account",
        email=f"support-account-{uuid4().hex}@example.com",
    )
    person = Subscriber(
        first_name="Support",
        last_name="Person",
        email=f"support-person-{uuid4().hex}@example.com",
    )
    other = Subscriber(
        first_name="Support",
        last_name="Other",
        email=f"support-other-{uuid4().hex}@example.com",
    )
    db_session.add_all([account, person, other])
    db_session.commit()

    linked_by_subscriber = Ticket(title="Subscriber link", subscriber_id=account.id)
    linked_by_account = Ticket(title="Account link", customer_account_id=account.id)
    linked_by_person = Ticket(title="Person link", customer_person_id=person.id)
    unlinked = Ticket(title="Other link", customer_account_id=other.id)
    db_session.add_all(
        [linked_by_subscriber, linked_by_account, linked_by_person, unlinked]
    )
    db_session.commit()

    rows = (
        db_session.query(Ticket)
        .filter(ticket_customer_any_link_filter(Ticket, [account.id, person.id]))
        .order_by(Ticket.title.asc())
        .all()
    )

    assert [ticket.title for ticket in rows] == [
        "Account link",
        "Person link",
        "Subscriber link",
    ]


def test_ticket_create_defaults_to_open_and_generates_number(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session,
        _ticket_payload(subscriber.id),
        actor_id=str(subscriber.id),
    )

    assert ticket.status == "open"
    assert ticket.number is not None
    assert ticket.number != ""


def test_ticket_create_uses_configured_routing_and_sla_policy(db_session, subscriber):
    team_id = uuid4()
    technician_id = uuid4()
    member_id = uuid4()
    team = ServiceTeam(
        id=team_id,
        name="Field Operations",
        team_type=ServiceTeamType.field_service.value,
    )
    member, person = add_bound_staff_user(
        db_session,
        system_user_id=member_id,
    )
    db_session.add_all(
        [
            team,
            ServiceTeamMember(
                team_id=team_id,
                person_id=person.id,
                is_active=True,
            ),
        ]
    )
    db_session.commit()
    assert member.id == member_id
    support_ticket_settings_service.update_options(
        db_session,
        statuses=["open", "closed", "merged"],
        priorities=["normal"],
        ticket_types=["incident"],
        regions=["north"],
        routing_regions=["north"],
        routing_technician_person_ids=[str(technician_id)],
        routing_service_team_ids=[str(team_id)],
        sla_priorities=["normal"],
        sla_response_hours=["1"],
        sla_resolution_hours=["8"],
        sla_aging_hours=["4"],
    )

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="North outage",
            description="Needs routing",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            region="north",
            priority="normal",
        ),
        actor_id=str(subscriber.id),
    )

    assert ticket.technician_person_id == technician_id
    assert ticket.service_team_id == team_id
    assert ticket.due_at is not None
    assert (
        db_session.query(TicketAssignee)
        .filter(
            TicketAssignee.ticket_id == ticket.id,
            TicketAssignee.person_id == member_id,
        )
        .count()
        == 1
    )
    assert (
        db_session.query(TicketSlaEvent)
        .filter(
            TicketSlaEvent.ticket_id == ticket.id,
            TicketSlaEvent.event_type == "resolution_due",
        )
        .count()
        == 1
    )


def test_ticket_create_preserves_requested_team_or_unassigned_routing(
    db_session, subscriber
):
    requested_team = ServiceTeam(
        name="Requested Portal Team",
        team_type=ServiceTeamType.support.value,
    )
    rule_team = ServiceTeam(
        name="Assignment Rule Team",
        team_type=ServiceTeamType.support.value,
    )
    automation_team = ServiceTeam(
        name="Automation Team",
        team_type=ServiceTeamType.support.value,
    )
    db_session.add_all([requested_team, rule_team, automation_team])
    db_session.flush()
    db_session.add(
        TicketAssignmentRule(
            name="Route all north tickets",
            priority=100,
            is_active=True,
            strategy=TicketAssignmentStrategy.round_robin.value,
            team_id=rule_team.id,
            match_config={"regions": ["north"]},
        )
    )
    db_session.commit()
    support_automation_rules.create_rule(
        db_session,
        name="Replace created ticket team",
        trigger=AutomationTrigger.ticket_created,
        action_type=AutomationActionType.assign_team,
        action_value=support_automation_rules.TicketAutomationAction(
            service_team_id=automation_team.id
        ),
    )
    support_automation_rules.create_rule(
        db_session,
        name="Tag portal-created tickets",
        trigger=AutomationTrigger.ticket_created,
        action_type=AutomationActionType.add_tag,
        action_value=support_automation_rules.TicketAutomationAction(
            tag="creation_automation_ran"
        ),
    )

    explicitly_routed = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Preserve requested team",
            subscriber_id=subscriber.id,
            region="north",
            service_team_id=requested_team.id,
        ),
        routing_mode=support_service.TicketCreationRoutingMode.preserve_requested_team,
    )
    intentionally_unassigned = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Preserve intentional unassignment",
            subscriber_id=subscriber.id,
            region="north",
            service_team_id=None,
        ),
        routing_mode=support_service.TicketCreationRoutingMode.preserve_requested_team,
    )

    assert explicitly_routed.service_team_id == requested_team.id
    assert intentionally_unassigned.service_team_id is None
    assert "creation_automation_ran" in explicitly_routed.tags
    assert "creation_automation_ran" in intentionally_unassigned.tags


def test_ticket_auto_assignment_respects_configured_open_limit(db_session, subscriber):
    team = ServiceTeam(name="Support queue", team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    loaded_person_id = uuid4()
    free_person_id = uuid4()
    _loaded_user, loaded_party = add_bound_staff_user(
        db_session,
        system_user_id=loaded_person_id,
    )
    _free_user, free_party = add_bound_staff_user(
        db_session,
        system_user_id=free_person_id,
    )
    db_session.add_all(
        [
            ServiceTeamMember(
                team_id=team.id, person_id=loaded_party.id, is_active=True
            ),
            ServiceTeamMember(team_id=team.id, person_id=free_party.id, is_active=True),
            TicketAssignmentRule(
                name="Support default",
                priority=100,
                is_active=True,
                strategy=TicketAssignmentStrategy.least_loaded.value,
                team_id=team.id,
            ),
            Ticket(
                title="Existing open ticket",
                assigned_to_person_id=loaded_person_id,
                status=TicketStatus.open.value,
            ),
        ]
    )
    db_session.commit()
    support_ticket_settings_service.update_options(
        db_session,
        statuses=["open", "closed", "merged"],
        priorities=["normal"],
        ticket_types=["incident"],
        auto_assign=True,
        auto_assign_max_open_tickets=0,
    )
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="New support ticket",
            description="Needs dispatch",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            priority="normal",
        ),
        actor_id=str(subscriber.id),
    )

    assert ticket.assigned_to_person_id == free_person_id
    assert ticket.assigned_to_person_id != loaded_person_id


def test_link_and_merge_form_reject_invalid_target_uuid(db_session):
    with pytest.raises(ValueError, match="valid ticket UUID"):
        web_support_tickets_service.link_ticket_from_form(
            db_session,
            request=None,
            ticket_id=str(uuid4()),
            to_ticket_id="not-a-uuid",
            link_type="related_outage",
            actor_id=None,
        )

    with pytest.raises(ValueError, match="valid ticket UUID"):
        web_support_tickets_service.merge_ticket_from_form(
            db_session,
            request=None,
            ticket_id=str(uuid4()),
            target_ticket_id="",
            reason=None,
            actor_id=None,
        )


def test_ticket_resolved_and_closed_set_timestamps(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )

    resolved = support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(status="resolved"),
        actor_id=str(subscriber.id),
    )
    assert resolved.resolved_at is not None

    closed = support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(status="closed"),
        actor_id=str(subscriber.id),
    )
    assert closed.closed_at is not None


def test_crm_provenance_does_not_create_a_second_write_owner(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )
    ticket.metadata_ = {"crm_ticket_id": str(uuid4())}
    db_session.add(ticket)
    db_session.commit()

    updated = support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(status="closed"),
        actor_id=str(subscriber.id),
    )
    assert updated.status == "closed"
    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        TicketCommentCreate(
            body="Handled by Sub",
            is_internal=False,
            author_person_id=subscriber.id,
        ),
        actor_id=str(subscriber.id),
    )
    assert comment.body == "Handled by Sub"


def test_resolution_confirmation_request_mints_token(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )

    updated, token_row = support_service.tickets.request_resolution_confirmation(
        db_session,
        str(ticket.id),
        actor_id=str(subscriber.id),
        grace_hours=24,
    )

    assert updated.status == TicketStatus.pending_confirmation.value
    assert updated.resolved_at is not None
    assert token_row.is_active is True
    assert token_row.ticket_id == updated.id
    assert (
        db_session.query(TicketAccessToken)
        .filter(TicketAccessToken.ticket_id == updated.id)
        .count()
        == 1
    )


def test_resolution_confirmation_notifications_disabled_by_default(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setenv("APP_URL", "https://selfcare.example.test")
    subscriber.phone = "+2348012345678"
    db_session.add(subscriber)
    db_session.commit()
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )

    support_service.tickets.request_resolution_confirmation(
        db_session,
        str(ticket.id),
        actor_id=str(subscriber.id),
        grace_hours=24,
    )

    assert db_session.query(Notification).count() == 0


def test_resolution_confirmation_queues_customer_notifications(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setenv("APP_URL", "https://selfcare.example.test")
    _enable_support_ticket_notifications(db_session)
    subscriber.phone = "+2348012345678"
    db_session.add(
        SubscriberContact(
            subscriber_id=subscriber.id,
            full_name="Authorized Contact",
            email="authorized@example.com",
            whatsapp="+2348099999999",
            receives_notifications=True,
        )
    )
    db_session.add(
        SubscriberContact(
            subscriber_id=subscriber.id,
            full_name="Silent Contact",
            email="silent@example.com",
            phone="+2348077777777",
            receives_notifications=False,
        )
    )
    db_session.add(subscriber)
    db_session.commit()
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )

    support_service.tickets.request_resolution_confirmation(
        db_session,
        str(ticket.id),
        actor_id=str(subscriber.id),
        grace_hours=24,
    )

    rows = (
        db_session.query(Notification)
        .filter(
            Notification.event_type == "support_ticket_resolution_confirmation",
        )
        .all()
    )
    recipients = {row.recipient for row in rows}
    assert recipients == {
        subscriber.email,
        "+2348012345678",
        "authorized@example.com",
        "+2348099999999",
        str(subscriber.id),
    }
    assert all(row.status == NotificationStatus.queued for row in rows)
    assert all("/ticket-confirm/" in (row.body or "") for row in rows)
    assert {
        row.channel
        for row in rows
        if row.recipient in {subscriber.email, "authorized@example.com"}
    } == {NotificationChannel.email}
    assert {
        row.channel
        for row in rows
        if row.recipient in {"+2348012345678", "+2348099999999"}
    } == {NotificationChannel.whatsapp}
    assert {row.channel for row in rows if row.recipient == str(subscriber.id)} == {
        NotificationChannel.push
    }


def test_resolution_confirmation_confirm_closes_ticket(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )
    _, token_row = support_service.tickets.request_resolution_confirmation(
        db_session, str(ticket.id), actor_id=str(subscriber.id)
    )

    confirmed = support_service.tickets.confirm_resolution(db_session, token_row)

    assert confirmed.status == TicketStatus.closed.value
    assert confirmed.closed_at is not None
    db_session.refresh(token_row)
    assert token_row.is_active is False
    assert token_row.responded_at is not None


def test_resolution_confirmation_dispute_reopens_ticket(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )
    _, token_row = support_service.tickets.request_resolution_confirmation(
        db_session, str(ticket.id), actor_id=str(subscriber.id)
    )

    disputed = support_service.tickets.dispute_resolution(
        db_session,
        token_row,
        reason="Still offline",
    )

    assert disputed.status == TicketStatus.open.value
    assert disputed.resolved_at is None
    confirmation = disputed.metadata_["resolution_confirmation"]
    assert confirmation["customer_dispute_reason"] == "Still offline"


def test_auto_confirm_pending_closes_after_grace_window(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )
    updated, _ = support_service.tickets.request_resolution_confirmation(
        db_session,
        str(ticket.id),
        actor_id=str(subscriber.id),
        grace_hours=24,
    )
    updated.resolved_at = datetime.now(UTC) - timedelta(hours=25)
    db_session.add(updated)
    db_session.commit()

    count = support_service.tickets.auto_confirm_pending(db_session)

    assert count == 1
    db_session.refresh(updated)
    assert updated.status == TicketStatus.closed.value


def test_auto_confirm_pending_ignores_crm_provenance(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )
    ticket.status = TicketStatus.pending_confirmation.value
    ticket.resolved_at = datetime.now(UTC) - timedelta(hours=25)
    ticket.metadata_ = {
        "crm_ticket_id": str(uuid4()),
        "resolution_confirmation": {"grace_hours": 24},
    }
    token_row = support_service.ticket_access_tokens.mint(db_session, ticket)
    db_session.commit()

    count = support_service.tickets.auto_confirm_pending(db_session)

    assert count == 1
    db_session.refresh(ticket)
    db_session.refresh(token_row)
    assert ticket.status == TicketStatus.closed.value
    assert token_row.is_active is False
    assert token_row.responded_at is not None


def test_authenticated_selfcare_resolution_uses_same_confirmation_owner(
    db_session, subscriber
):
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )
    support_service.tickets.request_resolution_confirmation(
        db_session, str(ticket.id), actor_id=str(subscriber.id)
    )

    confirmed = support_service.tickets.respond_to_resolution_for_customer(
        db_session, ticket, confirm=True
    )

    assert confirmed.status == TicketStatus.closed.value
    assert confirmed.closed_at is not None


def test_authenticated_selfcare_dispute_reopens_with_reason(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )
    support_service.tickets.request_resolution_confirmation(
        db_session, str(ticket.id), actor_id=str(subscriber.id)
    )

    reopened = support_service.tickets.respond_to_resolution_for_customer(
        db_session,
        ticket,
        confirm=False,
        reason="Optical light is still red",
    )

    assert reopened.status == TicketStatus.open.value
    assert reopened.metadata_["resolution_confirmation"]["customer_dispute_reason"] == (
        "Optical light is still red"
    )


def test_authenticated_selfcare_rejects_expired_confirmation(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )
    _, token = support_service.tickets.request_resolution_confirmation(
        db_session, str(ticket.id), actor_id=str(subscriber.id)
    )
    token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()

    with pytest.raises(DomainError) as expired:
        support_service.tickets.respond_to_resolution_for_customer(
            db_session, ticket, confirm=True
        )
    assert expired.value.code == "resolution_confirmation_inactive"


def test_resolution_confirmation_rejects_closed_ticket(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )
    support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(status=TicketStatus.closed.value),
        actor_id=str(subscriber.id),
    )

    with pytest.raises(DomainError) as exc:
        support_service.tickets.request_resolution_confirmation(
            db_session, str(ticket.id), actor_id=str(subscriber.id)
        )

    assert exc.value.code == "resolution_confirmation_not_eligible"


def test_resolution_confirmation_accepts_crm_provenance(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )
    ticket.metadata_ = {"crm_ticket_id": str(uuid4())}
    token_row = support_service.ticket_access_tokens.mint(db_session, ticket)
    db_session.commit()

    confirmed = support_service.tickets.confirm_resolution(db_session, token_row)
    assert confirmed.status == TicketStatus.closed.value


def test_native_ticket_writes_remain_enabled(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )

    updated = support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(status="closed"),
        actor_id=str(subscriber.id),
    )

    assert updated.status == "closed"


def test_field_visit_tag_is_descriptive_and_does_not_issue_work(db_session, subscriber):
    """A tag may describe triage but cannot cross the field-work boundary."""
    from app.models.work_order import WorkOrder

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Fiber issue",
            description="Needs onsite check",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            tags=["field_visit"],
        ),
        actor_id=str(subscriber.id),
    )

    assert "work_order_id" not in (ticket.metadata_ or {})
    assert db_session.query(WorkOrder).count() == 0

    # Updating with field_visit again should not duplicate the work order.
    support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(tags=["field_visit"]),
        actor_id=str(subscriber.id),
    )
    assert db_session.query(WorkOrder).count() == 0


def test_automation_added_field_visit_tag_does_not_issue_work(db_session, subscriber):
    """Automation may tag triage; explicit assigned-team issuance is separate."""
    from app.models.support import AutomationActionType, AutomationTrigger
    from app.models.work_order import WorkOrder

    support_automation_rules.create_rule(
        db_session,
        name="Site visits get a work order",
        trigger=AutomationTrigger.ticket_created,
        conditions=support_automation_rules.TicketAutomationConditions(
            ticket_type="site_visit"
        ),
        action_type=AutomationActionType.add_tag,
        action_value=support_automation_rules.TicketAutomationAction(tag="field_visit"),
    )

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Router down at site",
            description="Needs onsite check",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            ticket_type="site_visit",
        ),
        actor_id=str(subscriber.id),
    )

    db_session.refresh(ticket)
    assert "field_visit" in (ticket.tags or [])
    assert "work_order_id" not in (ticket.metadata_ or {})
    assert db_session.query(WorkOrder).count() == 0


def test_merge_moves_comments_assignees_and_blocks_source_mutations(
    db_session, subscriber
):
    source = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Source",
            description="source",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            assignee_person_ids=[subscriber.id],
        ),
        actor_id=str(subscriber.id),
    )
    target = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Target",
            description="target",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
        ),
        actor_id=str(subscriber.id),
    )

    support_service.tickets.create_comment(
        db_session,
        str(source.id),
        TicketCommentCreate(
            body="Please fix", is_internal=False, author_person_id=subscriber.id
        ),
        actor_id=str(subscriber.id),
    )

    merged = support_service.tickets.merge(
        db_session,
        str(source.id),
        TicketMergeRequest(target_ticket_id=target.id, reason="duplicate"),
        actor_id=str(subscriber.id),
    )

    assert merged.id == target.id
    db_session.refresh(source)
    assert source.status == "merged"
    assert source.merged_into_ticket_id == target.id

    target_comments = (
        db_session.query(TicketComment)
        .filter(TicketComment.ticket_id == target.id)
        .all()
    )
    assert any("Please fix" in item.body for item in target_comments)

    assignee_rows = (
        db_session.query(TicketAssignee)
        .filter(TicketAssignee.ticket_id == target.id)
        .all()
    )
    assert any(str(row.person_id) == str(subscriber.id) for row in assignee_rows)

    with pytest.raises(DomainError) as exc:
        support_service.tickets.update(
            db_session,
            str(source.id),
            TicketUpdate(title="forbidden"),
            actor_id=str(subscriber.id),
        )
    assert exc.value.code == "ticket_merged_source"


def test_assignment_notifications_wired_but_disabled(db_session, subscriber):
    technician = _system_user(display_name="Technician")
    manager = _system_user(display_name="Manager")
    coordinator = _system_user(display_name="Coordinator")
    db_session.add_all([technician, manager, coordinator])
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Notify test",
            description="",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            technician_person_id=technician.id,
            ticket_manager_person_id=manager.id,
            site_coordinator_person_id=coordinator.id,
            service_team_id=uuid4(),
        ),
        actor_id=str(subscriber.id),
    )

    support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(priority="high"),
        actor_id=str(subscriber.id),
    )

    assert db_session.query(Notification).count() == 0


def test_ticket_assignments_accept_system_user_ids(db_session, subscriber):
    technician = _system_user(display_name="Field Tech")
    manager = _system_user(display_name="Project Manager")
    coordinator = _system_user(display_name="Site Coordinator")
    assignee = _system_user(display_name="Queue Assignee")
    db_session.add_all([technician, manager, coordinator, assignee])
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Staff assignment",
            description="Assign to internal users",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            technician_person_id=technician.id,
            ticket_manager_person_id=manager.id,
            site_coordinator_person_id=coordinator.id,
            assignee_person_ids=[assignee.id],
        ),
        actor_id=str(subscriber.id),
    )

    db_session.refresh(ticket)
    assignee_rows = (
        db_session.query(TicketAssignee)
        .filter(TicketAssignee.ticket_id == ticket.id)
        .all()
    )

    assert ticket.technician_person_id == technician.id
    assert ticket.ticket_manager_person_id == manager.id
    assert ticket.site_coordinator_person_id == coordinator.id
    assert any(row.person_id == assignee.id for row in assignee_rows)


def test_ticket_create_ignores_system_user_created_by_subscriber_fk(
    db_session, subscriber
):
    system_user = _system_user(display_name="Support Admin")
    db_session.add(system_user)
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Created by admin",
            description="",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            created_by_person_id=system_user.id,
        ),
        actor_id=str(system_user.id),
    )

    assert ticket.created_by_person_id is None


def test_ticket_comment_stores_system_user_author_identity(db_session, subscriber):
    system_user = _system_user(display_name="Comment Admin")
    db_session.add(system_user)
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Comment target",
            description="",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
        ),
        actor_id=str(subscriber.id),
    )

    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        TicketCommentCreate(
            body="Internal admin note",
            is_internal=True,
            author_type=TicketCommentAuthorType.staff,
            author_system_user_id=system_user.id,
        ),
        actor_id=str(system_user.id),
    )

    assert comment.author_type == TicketCommentAuthorType.staff.value
    assert comment.author_person_id is None
    assert comment.author_system_user_id == system_user.id


def test_ticket_comment_stores_customer_author_identity(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Comment target",
            description="",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
        ),
        actor_id=str(subscriber.id),
    )

    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        TicketCommentCreate(
            body="Customer reply",
            is_internal=False,
            author_type=TicketCommentAuthorType.customer,
            author_person_id=subscriber.id,
        ),
        actor_id=str(subscriber.id),
    )

    assert comment.author_type == TicketCommentAuthorType.customer.value
    assert comment.author_person_id == subscriber.id
    assert comment.author_system_user_id is None


def test_ticket_create_auto_links_inbound_sender_from_subscriber_contact(
    db_session, subscriber
):
    contact = SubscriberContact(
        subscriber_id=subscriber.id,
        email="linked-contact@example.com",
        contact_type="general",
    )
    db_session.add(contact)
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, subscriber.id)

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Inbound email",
            description="Created from inbound email",
            channel=TicketChannel.email,
            inbound_sender=" LINKED-CONTACT@example.com ",
        ),
        actor_id=None,
    )

    assert ticket.subscriber_id == subscriber.id
    assert ticket.customer_account_id == subscriber.id
    assert ticket.metadata_ is not None
    assert ticket.metadata_["identity_resolution"]["status"] == "matched"
    assert (
        ticket.metadata_["identity_resolution"]["matched_via"] == "subscriber_contact"
    )
    assert ticket.metadata_["identity_resolution"]["matched_contact_id"] == str(
        contact.id
    )
    assert ticket.metadata_["identity_resolution"]["match_confidence"] == "MEDIUM"
    assert ticket.metadata_["account_sensitive_automation_allowed"] is True


def test_ticket_create_marks_ambiguous_inbound_sender_for_manual_review(
    db_session, subscriber
):
    other = Subscriber(
        first_name="Other",
        last_name="Subscriber",
        email="other@example.com",
    )
    db_session.add(other)
    db_session.flush()
    db_session.add_all(
        [
            SubscriberContact(
                subscriber_id=subscriber.id,
                phone="08012345678",
                contact_type="general",
            ),
            SubscriberContact(
                subscriber_id=other.id,
                phone="+2348012345678",
                contact_type="general",
            ),
        ]
    )
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, subscriber.id)
    rebuild_identity_index_for_subscriber(db_session, other.id)

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Inbound SMS",
            description="Needs manual review",
            channel=TicketChannel.phone,
            inbound_sender="0801 234 5678",
        ),
        actor_id=None,
    )

    assert ticket.subscriber_id is None
    assert ticket.customer_account_id is None
    assert ticket.metadata_ is not None
    assert ticket.metadata_["identity_resolution"]["status"] == "ambiguous"
    assert ticket.metadata_["identity_resolution"]["manual_review_required"] is True
    assert ticket.metadata_["manual_review_required"] is True
    assert ticket.metadata_["automation_paused"] is True
    assert ticket.metadata_["account_sensitive_automation_allowed"] is False


def test_ticket_create_marks_historical_match_low_confidence_for_manual_review(
    db_session, subscriber
):
    from app.models.comms import CustomerNotificationEvent

    db_session.add(
        CustomerNotificationEvent(
            entity_type="support_ticket",
            entity_id=subscriber.id,
            subscriber_id=subscriber.id,
            channel="sms",
            recipient="+2348091111111",
            message="Previous notification",
        )
    )
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Inbound SMS",
            description="Historical only",
            channel=TicketChannel.phone,
            inbound_sender="08091111111",
        ),
        actor_id=None,
    )

    assert ticket.subscriber_id == subscriber.id
    assert ticket.customer_account_id == subscriber.id
    assert (
        ticket.metadata_["identity_resolution"]["matched_via"]
        == "historical_participant"
    )
    assert ticket.metadata_["identity_resolution"]["match_confidence"] == "LOW"
    assert ticket.metadata_["manual_review_required"] is True
    assert ticket.metadata_["automation_paused"] is True
    assert ticket.metadata_["account_sensitive_automation_allowed"] is False


def test_ticket_automation_is_suppressed_for_ambiguous_identity(db_session, subscriber):
    other = Subscriber(
        first_name="Other",
        last_name="Subscriber",
        email="other-automation@example.com",
    )
    db_session.add(other)
    db_session.flush()
    db_session.add_all(
        [
            SubscriberContact(
                subscriber_id=subscriber.id,
                phone="08077777777",
                contact_type="general",
            ),
            SubscriberContact(
                subscriber_id=other.id,
                phone="+2348077777777",
                contact_type="general",
            ),
        ]
    )
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, subscriber.id)
    rebuild_identity_index_for_subscriber(db_session, other.id)
    db_session.commit()
    support_automation_rules.create_rule(
        db_session,
        name="Auto high priority",
        trigger=AutomationTrigger.ticket_created,
        action_type=AutomationActionType.set_priority,
        action_value=support_automation_rules.TicketAutomationAction(priority="high"),
    )
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Inbound SMS",
            description="Ambiguous identity",
            channel=TicketChannel.phone,
            priority="normal",
            inbound_sender="08077777777",
        ),
        actor_id=None,
    )

    assert ticket.priority == "normal"
    assert ticket.metadata_["automation_paused"] is True
    assert ticket.metadata_["automation_suppressed_reason"] == (
        "identity_manual_review_required"
    )


def test_ticket_automation_is_suppressed_for_low_confidence_identity(
    db_session, subscriber
):
    from app.models.comms import CustomerNotificationEvent

    db_session.add(
        CustomerNotificationEvent(
            entity_type="support_ticket",
            entity_id=subscriber.id,
            subscriber_id=subscriber.id,
            channel="sms",
            recipient="+2348066666666",
            message="Previous notification",
        )
    )
    db_session.flush()
    db_session.commit()
    support_automation_rules.create_rule(
        db_session,
        name="Auto open status",
        trigger=AutomationTrigger.ticket_created,
        action_type=AutomationActionType.set_status,
        action_value=support_automation_rules.TicketAutomationAction(
            status="pending_customer"
        ),
    )
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Inbound SMS",
            description="Historical identity",
            channel=TicketChannel.phone,
            status="open",
            inbound_sender="08066666666",
        ),
        actor_id=None,
    )

    assert ticket.status == "open"
    assert ticket.metadata_["identity_resolution"]["match_confidence"] == "LOW"
    assert ticket.metadata_["automation_paused"] is True


def test_render_tickets_csv_honors_filters_and_columns(db_session, subscriber):
    exported = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Exported ticket",
            description="",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            ticket_type="billing",
        ),
        actor_id=str(subscriber.id),
    )
    support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Filtered out",
            description="",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            ticket_type="installation",
        ),
        actor_id=str(subscriber.id),
    )

    content = web_support_tickets_service.render_tickets_csv(
        db_session,
        search=None,
        status=None,
        ticket_type="billing",
        assigned_to_me=False,
        actor_id=None,
        project_manager_person_id=None,
        site_coordinator_person_id=None,
        subscriber_id=None,
        order_by="created_at",
        order_dir="desc",
        visible_columns_cookie="number,ticket_type,status",
    )

    lines = content.strip().splitlines()
    assert lines[0] == "Ticket ID,Ticket Type,Status"
    assert lines[1] == f"{exported.number},billing,open"
    assert len(lines) == 2


def test_render_tickets_csv_falls_back_to_default_columns(db_session, subscriber):
    support_service.tickets.create(
        db_session,
        _ticket_payload(subscriber.id),
        actor_id=str(subscriber.id),
    )

    content = web_support_tickets_service.render_tickets_csv(
        db_session,
        search=None,
        status=None,
        ticket_type=None,
        assigned_to_me=False,
        actor_id=None,
        project_manager_person_id=None,
        site_coordinator_person_id=None,
        subscriber_id=None,
        order_by="created_at",
        order_dir="desc",
        visible_columns_cookie="bogus,columns",
    )

    header = content.strip().splitlines()[0]
    assert header == (
        "Ticket ID,Ticket Type,Priority,Status,Customer Name,"
        "Assigned Technician,Due Date,Opening Date"
    )


def test_web_comment_edit_updates_body_and_marks_edited(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session,
        _ticket_payload(subscriber.id),
        actor_id=str(subscriber.id),
    )
    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        TicketCommentCreate(
            body="Original body",
            is_internal=False,
            author_type=TicketCommentAuthorType.staff,
        ),
        actor_id=None,
    )
    assert not (comment.metadata_ or {}).get("edited_at")

    updated = web_support_tickets_service.update_ticket_comment_from_form(
        db_session,
        request=None,
        ticket_id=str(ticket.id),
        comment_id=str(comment.id),
        actor_id=None,
        body="Corrected body",
    )

    assert updated.body == "Corrected body"
    assert (updated.metadata_ or {}).get("edited_at")


def test_web_comment_edit_rejects_comment_from_other_ticket(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session,
        _ticket_payload(subscriber.id),
        actor_id=str(subscriber.id),
    )
    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        TicketCommentCreate(
            body="Original body",
            is_internal=False,
            author_type=TicketCommentAuthorType.staff,
        ),
        actor_id=None,
    )

    with pytest.raises(DomainError) as exc:
        web_support_tickets_service.update_ticket_comment_from_form(
            db_session,
            request=None,
            ticket_id=str(uuid4()),
            comment_id=str(comment.id),
            actor_id=None,
            body="Should not apply",
        )
    assert exc.value.code == "ticket_comment_not_found"
