from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.support import Ticket, TicketStatus
from app.models.system_user import SystemUser
from app.models.ticket_workflow import (
    SlaBreach,
    SlaBreachStatus,
    SlaClock,
    SlaClockStatus,
    SlaPolicy,
    WorkflowEntityType,
)
from app.services import ticket_sla_reports
from app.services.dynamic_filters import FilterCondition, parse_filter_payload
from app.web.admin import reports as reports_web


def test_ticket_sla_queue_is_compact_without_horizontal_scrolling() -> None:
    template = Path("templates/admin/reports/ticket_sla.html").read_text(
        encoding="utf-8"
    )

    queue = template[template.index('{% call card("SLA Breach Queue"') :]
    assert 'class="overflow-x-auto"' not in queue
    assert 'class="w-full table-fixed' in queue
    assert 'class="hidden lg:block"' in queue
    assert "lg:hidden" in queue
    assert "Over target:" in queue
    assert 'aria-label="SLA breach queue pages"' in queue
    assert "violation_page.page + 1" in queue
    assert "violation_page.per_page" in queue


def test_ticket_sla_dashboard_names_current_metric_scope() -> None:
    template = Path("templates/admin/reports/ticket_sla.html").read_text(
        encoding="utf-8"
    )

    assert "currently breaching / currently open" in template
    assert "summary.total_open_tickets" in template
    assert "summary.total_currently_breaching" in template
    assert "summary.current_breach_rate" in template
    assert "item.currently_breaching }} / {{ item.open_tickets" in template
    assert "closed_breached" not in template


def test_ticket_sla_drilldowns_use_the_ticket_list_filter_contract() -> None:
    team_id = str(uuid4())
    team_url = reports_web._ticket_sla_drilldown_url(
        key=team_id,
        field="service_team_id",
        date_from="2026-09-01",
        date_to="2026-09-14",
    )
    team_query = parse_qs(urlsplit(team_url).query)

    assert team_query["status"] == ["not_closed"]
    assert parse_filter_payload(
        team_query["filters"][0], default_doctype="Ticket"
    ).and_filters == [
        FilterCondition("Ticket", "service_team_id", "=", team_id),
        FilterCondition("Ticket", "created_at", ">=", "2026-09-01T00:00:00+00:00"),
        FilterCondition(
            "Ticket", "created_at", "<=", "2026-09-14T23:59:59.999999+00:00"
        ),
    ]

    unassigned_url = reports_web._ticket_sla_drilldown_url(
        key="unassigned_region",
        field="region",
        date_from=None,
        date_to=None,
    )
    unassigned_query = parse_qs(urlsplit(unassigned_url).query)
    assert parse_filter_payload(
        unassigned_query["filters"][0], default_doctype="Ticket"
    ).and_filters == [FilterCondition("Ticket", "region", "is", None)]


def _policy(db_session) -> SlaPolicy:
    policy = SlaPolicy(
        name="Ticket Resolution SLA",
        entity_type=WorkflowEntityType.ticket.value,
        is_active=True,
    )
    db_session.add(policy)
    db_session.flush()
    return policy


def test_ticket_sla_report_summary_aggregates_breakdowns(db_session):
    team = ServiceTeam(name="SLA Team", team_type=ServiceTeamType.support.value)
    assignee = SystemUser(
        first_name="Sla",
        last_name="Agent",
        display_name="SLA Agent",
        email="sla-agent@example.com",
    )
    db_session.add_all([team, assignee])
    db_session.flush()

    ticket_ok = Ticket(
        title="Ticket OK",
        status=TicketStatus.open.value,
        region="gudu",
        service_team_id=team.id,
        assigned_to_person_id=assignee.id,
    )
    ticket_bad = Ticket(
        title="Ticket Bad",
        status=TicketStatus.waiting_on_customer.value,
        region="gudu",
        service_team_id=team.id,
        assigned_to_person_id=assignee.id,
    )
    ticket_closed = Ticket(
        title="Historical closed breach",
        status=TicketStatus.closed.value,
        region="gudu",
        service_team_id=team.id,
        assigned_to_person_id=assignee.id,
    )
    ticket_canceled = Ticket(
        title="Canceled ticket",
        status=TicketStatus.canceled.value,
        region="gudu",
        service_team_id=team.id,
        assigned_to_person_id=assignee.id,
    )
    db_session.add_all([ticket_ok, ticket_bad, ticket_closed, ticket_canceled])
    db_session.flush()

    policy = _policy(db_session)
    now = datetime.now(UTC)
    db_session.add_all(
        [
            SlaClock(
                policy_id=policy.id,
                entity_type=WorkflowEntityType.ticket.value,
                entity_id=ticket_ok.id,
                status=SlaClockStatus.running.value,
                started_at=now - timedelta(hours=2),
                due_at=now + timedelta(hours=1),
            ),
            SlaClock(
                policy_id=policy.id,
                entity_type=WorkflowEntityType.ticket.value,
                entity_id=ticket_bad.id,
                status=SlaClockStatus.breached.value,
                started_at=now - timedelta(hours=4),
                due_at=now - timedelta(hours=1),
                breached_at=now - timedelta(minutes=30),
            ),
            SlaClock(
                policy_id=policy.id,
                entity_type=WorkflowEntityType.ticket.value,
                entity_id=ticket_closed.id,
                status=SlaClockStatus.completed.value,
                started_at=now - timedelta(days=2),
                due_at=now - timedelta(days=1),
                breached_at=now - timedelta(days=1),
                completed_at=now - timedelta(hours=12),
            ),
        ]
    )
    db_session.commit()

    summary = ticket_sla_reports.summary(
        db_session,
        query=ticket_sla_reports.TicketSlaSummaryQuery(),
    )

    assert summary.total_open_tickets == 2
    assert summary.total_currently_breaching == 1
    assert summary.current_breach_rate == 0.5
    by_status = {item.key: item for item in summary.by_status}
    assert by_status[TicketStatus.open.value].open_tickets == 1
    assert by_status[TicketStatus.open.value].currently_breaching == 0
    waiting = by_status[TicketStatus.waiting_on_customer.value]
    assert waiting.open_tickets == 1
    assert waiting.currently_breaching == 1
    by_team = {item.key: item for item in summary.by_service_team}
    assert by_team[str(team.id)].open_tickets == 2
    assert by_team[str(team.id)].currently_breaching == 1
    assert by_team[str(team.id)].label == "SLA Team"
    by_region = {item.key: item for item in summary.by_region}
    assert by_region["gudu"].open_tickets == 2
    assert by_region["gudu"].currently_breaching == 1
    assert by_region["gudu"].breach_rate == 0.5
    by_assignee = {item.key: item for item in summary.by_assignee}
    assert by_assignee[str(assignee.id)].open_tickets == 2
    assert by_assignee[str(assignee.id)].currently_breaching == 1
    assert by_assignee[str(assignee.id)].label == "SLA Agent"
    serialized = summary.as_serializable()
    assert serialized["total_open_tickets"] == 2
    assert serialized["total_currently_breaching"] == 1
    assert "total_breaches" not in serialized


def test_ticket_sla_report_trend_daily_honors_date_window(db_session):
    policy = _policy(db_session)
    ticket = Ticket(title="Trend Ticket")
    db_session.add(ticket)
    db_session.flush()

    now = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0)
    older = now - timedelta(days=4)
    in_window = now - timedelta(days=1)
    db_session.add_all(
        [
            SlaClock(
                policy_id=policy.id,
                entity_type=WorkflowEntityType.ticket.value,
                entity_id=ticket.id,
                status=SlaClockStatus.breached.value,
                started_at=older,
                due_at=older + timedelta(hours=2),
                breached_at=older + timedelta(hours=3),
            ),
            SlaClock(
                policy_id=policy.id,
                entity_type=WorkflowEntityType.ticket.value,
                entity_id=ticket.id,
                status=SlaClockStatus.running.value,
                started_at=in_window,
                due_at=in_window + timedelta(hours=2),
            ),
        ]
    )
    db_session.commit()

    trend = ticket_sla_reports.trend_daily(
        db_session,
        start_at=now - timedelta(days=2),
        end_at=now,
    )

    assert trend == [
        {
            "date": str((now - timedelta(days=1)).date()),
            "total": 1,
            "breached": 0,
            "breach_rate": 0.0,
        }
    ]


def test_ticket_sla_report_violation_records(db_session):
    policy = _policy(db_session)
    team = ServiceTeam(name="Field Ops", team_type=ServiceTeamType.support.value)
    assignee = SystemUser(
        first_name="Field",
        last_name="Tech",
        display_name="Field Tech",
        email="field-tech@example.com",
    )
    db_session.add_all([team, assignee])
    db_session.flush()
    ticket = Ticket(
        title="Late ticket",
        number="T-100",
        region="north",
        priority="urgent",
        service_team_id=team.id,
        assigned_to_person_id=assignee.id,
    )
    db_session.add(ticket)
    db_session.flush()
    now = datetime.now(UTC).replace(microsecond=0)
    clock = SlaClock(
        policy_id=policy.id,
        entity_type=WorkflowEntityType.ticket.value,
        entity_id=ticket.id,
        status=SlaClockStatus.breached.value,
        started_at=now - timedelta(hours=3),
        due_at=now - timedelta(hours=1),
        breached_at=now - timedelta(hours=1),
    )
    db_session.add(clock)
    db_session.flush()
    db_session.add(
        SlaBreach(
            clock_id=clock.id,
            status=SlaBreachStatus.open.value,
            breached_at=now - timedelta(hours=1),
        )
    )
    db_session.commit()

    records = ticket_sla_reports.violation_records(db_session)

    assert len(records) == 1
    record = records[0]
    assert record["ticket_reference"] == "T-100"
    assert record["ticket_url"] == "/admin/support/tickets/T-100"
    assert record["service_team"] == "Field Ops"
    assert record["assignee"] == "Field Tech"
    assert record["region"] == "north"
    assert record["breach_minutes"] >= 60
    assert record["sla_status"] == SlaBreachStatus.open.value


def test_ticket_sla_violation_page_fetches_fifteen_rows_at_a_time(db_session):
    policy = _policy(db_session)
    ticket = Ticket(title="Paginated SLA breach", number="T-PAGE")
    db_session.add(ticket)
    db_session.flush()
    now = datetime.now(UTC).replace(microsecond=0)
    for index in range(16):
        clock = SlaClock(
            policy_id=policy.id,
            entity_type=WorkflowEntityType.ticket.value,
            entity_id=ticket.id,
            status=SlaClockStatus.breached.value,
            started_at=now - timedelta(hours=index + 3),
            due_at=now - timedelta(hours=index + 2),
            breached_at=now - timedelta(minutes=index + 1),
        )
        db_session.add(clock)
        db_session.flush()
        db_session.add(
            SlaBreach(
                clock_id=clock.id,
                status=SlaBreachStatus.open.value,
                breached_at=now - timedelta(minutes=index + 1),
            )
        )
    db_session.commit()

    first = ticket_sla_reports.violation_page(
        db_session,
        query=ticket_sla_reports.TicketSlaViolationPageQuery(page=1),
    )
    second = ticket_sla_reports.violation_page(
        db_session,
        query=ticket_sla_reports.TicketSlaViolationPageQuery(page=2),
    )

    assert len(first.rows) == 15
    assert first.total_count == 16
    assert first.total_pages == 2
    assert first.has_previous is False
    assert first.has_next is True
    assert len(second.rows) == 1
    assert second.has_previous is True
    assert second.has_next is False


def test_ticket_sla_report_violation_records_open_only(db_session):
    policy = _policy(db_session)
    ticket = Ticket(title="Resolved breach")
    db_session.add(ticket)
    db_session.flush()
    now = datetime.now(UTC)
    clock = SlaClock(
        policy_id=policy.id,
        entity_type=WorkflowEntityType.ticket.value,
        entity_id=ticket.id,
        status=SlaClockStatus.completed.value,
        started_at=now - timedelta(hours=3),
        due_at=now - timedelta(hours=1),
        completed_at=now,
    )
    db_session.add(clock)
    db_session.flush()
    db_session.add(
        SlaBreach(
            clock_id=clock.id,
            status=SlaBreachStatus.resolved.value,
            breached_at=now - timedelta(hours=1),
        )
    )
    db_session.commit()

    assert ticket_sla_reports.violation_records(db_session, open_only=True) == []
    assert len(ticket_sla_reports.violation_records(db_session, open_only=False)) == 1
