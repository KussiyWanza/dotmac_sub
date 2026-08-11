"""Ticket dynamic-filter engine + list-param coverage.

Mirrors tests/test_dynamic_filters_users.py for the shared engine, plus the
support-ticket field whitelist and the new simple list params
(priority/channel/created_by_person_id/is_active) on Tickets.list.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from app.models.support import Ticket, TicketAssignee, TicketChannel
from app.services import support as support_service
from app.services.domain_errors import DomainError
from app.services.dynamic_filters import (
    FilterValidationError,
    build_filter_expression,
    parse_filter_payload,
)
from app.services.support_ticket_filters import (
    TICKET_DOCTYPE,
    TICKET_FILTER_SPECS,
    build_ticket_filter_clause,
    serialize_ticket_filter_schema,
)


def _ticket(**overrides) -> Ticket:
    defaults = {
        "title": f"Ticket {uuid.uuid4().hex[:8]}",
        "status": "open",
        "priority": "normal",
        "channel": TicketChannel.web,
        "is_active": True,
    }
    defaults.update(overrides)
    return Ticket(**defaults)


# ── Engine: CRM-contract payload shapes ──────────────────────────────────────


def test_parser_accepts_crm_inline_or_groups():
    payload = [
        ["Ticket", "status", "=", "open"],
        {
            "or": [
                ["Ticket", "priority", "=", "high"],
                ["Ticket", "priority", "=", "urgent"],
            ]
        },
    ]

    query = parse_filter_payload(payload, default_doctype=TICKET_DOCTYPE)

    assert len(query.and_filters) == 1
    assert len(query.or_groups) == 1
    assert len(query.or_groups[0]) == 2
    assert not query.or_filters


def test_parser_rejects_malformed_inline_group():
    with pytest.raises(FilterValidationError):
        parse_filter_payload(
            [{"or": [["Ticket", "status", "=", "open"]], "and": []}],
            default_doctype=TICKET_DOCTYPE,
        )
    with pytest.raises(FilterValidationError):
        parse_filter_payload([{"or": []}], default_doctype=TICKET_DOCTYPE)


def test_builder_rejects_non_whitelisted_field():
    query = parse_filter_payload(
        [["Ticket", "description; DROP TABLE", "=", "x"]],
        default_doctype=TICKET_DOCTYPE,
    )
    with pytest.raises(FilterValidationError):
        build_filter_expression(
            query, doctype=TICKET_DOCTYPE, field_specs=TICKET_FILTER_SPECS
        )


def test_builder_rejects_wrong_doctype():
    query = parse_filter_payload(
        [["User", "status", "=", "open"]], default_doctype=TICKET_DOCTYPE
    )
    with pytest.raises(FilterValidationError):
        build_filter_expression(
            query, doctype=TICKET_DOCTYPE, field_specs=TICKET_FILTER_SPECS
        )


def test_build_ticket_filter_clause_none_for_empty_payload():
    assert build_ticket_filter_clause(None) is None
    assert build_ticket_filter_clause("") is None
    assert build_ticket_filter_clause('{"and": [], "or": []}') is None


# ── Service: filters param on Tickets.list ───────────────────────────────────


def test_list_applies_and_or_filter_groups(db_session):
    open_high = _ticket(status="open", priority="high", region="abuja")
    open_urgent = _ticket(status="open", priority="urgent", region="lagos")
    open_normal = _ticket(status="open", priority="normal", region="abuja")
    closed_high = _ticket(status="closed", priority="high", region="abuja")
    db_session.add_all([open_high, open_urgent, open_normal, closed_high])
    db_session.commit()

    filters = json.dumps(
        {
            "and": [["Ticket", "status", "=", "open"]],
            "or": [
                ["Ticket", "priority", "=", "high"],
                ["Ticket", "priority", "=", "urgent"],
            ],
        }
    )
    rows = support_service.tickets.list(db_session, filters=filters, limit=50)
    ids = {ticket.id for ticket in rows}

    assert open_high.id in ids
    assert open_urgent.id in ids
    assert open_normal.id not in ids
    assert closed_high.id not in ids


def test_legacy_resolved_filters_select_canonical_closed_tickets(db_session):
    closed = _ticket(status="closed")
    open_ticket = _ticket(status="open")
    db_session.add_all([closed, open_ticket])
    db_session.commit()

    simple_rows = support_service.tickets.list(db_session, status="resolved", limit=50)
    advanced_rows = support_service.tickets.list(
        db_session,
        filters=json.dumps([["Ticket", "status", "=", "resolved"]]),
        limit=50,
    )

    assert {ticket.id for ticket in simple_rows} == {closed.id}
    assert {ticket.id for ticket in advanced_rows} == {closed.id}
    assert open_ticket.id not in {ticket.id for ticket in simple_rows}


def test_status_totals_never_return_legacy_resolved_key(db_session):
    db_session.add_all([_ticket(status="closed"), _ticket(status="resolved")])
    db_session.commit()

    totals = support_service.status_totals(db_session)

    assert "resolved" not in totals
    assert totals["closed"] >= 2


def test_list_applies_crm_shape_inline_or_group(db_session):
    abuja_high = _ticket(priority="high", region="abuja")
    abuja_urgent = _ticket(priority="urgent", region="abuja")
    abuja_low = _ticket(priority="low", region="abuja")
    lagos_high = _ticket(priority="high", region="lagos")
    db_session.add_all([abuja_high, abuja_urgent, abuja_low, lagos_high])
    db_session.commit()

    filters = json.dumps(
        [
            ["Ticket", "region", "=", "abuja"],
            {
                "or": [
                    ["Ticket", "priority", "=", "high"],
                    ["Ticket", "priority", "=", "urgent"],
                ]
            },
        ]
    )
    rows = support_service.tickets.list(db_session, filters=filters, limit=50)
    ids = {ticket.id for ticket in rows}

    assert ids >= {abuja_high.id, abuja_urgent.id}
    assert abuja_low.id not in ids
    assert lagos_high.id not in ids


def test_list_filters_created_at_between(db_session):
    inside = _ticket()
    inside.created_at = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    outside = _ticket()
    outside.created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    db_session.add_all([inside, outside])
    db_session.commit()

    filters = json.dumps(
        [["Ticket", "created_at", "between", ["2026-06-01", "2026-07-01"]]]
    )
    rows = support_service.tickets.list(db_session, filters=filters, limit=50)
    ids = {ticket.id for ticket in rows}

    assert inside.id in ids
    assert outside.id not in ids


def test_list_filters_tags_membership(db_session):
    tagged = _ticket(tags=["field_visit", "outage"])
    other = _ticket(tags=["billing"])
    db_session.add_all([tagged, other])
    db_session.commit()

    filters = json.dumps([["Ticket", "tags", "=", "field_visit"]])
    rows = support_service.tickets.list(db_session, filters=filters, limit=50)
    ids = {ticket.id for ticket in rows}

    assert tagged.id in ids
    assert other.id not in ids


def test_list_filters_assignee_via_assignees_table(db_session):
    person_id = uuid.uuid4()
    legacy = _ticket(assigned_to_person_id=person_id)
    multi = _ticket()
    unrelated = _ticket()
    db_session.add_all([legacy, multi, unrelated])
    db_session.flush()
    db_session.add(TicketAssignee(ticket_id=multi.id, person_id=person_id))
    db_session.commit()

    filters = json.dumps([["Ticket", "assigned_to_person_id", "=", str(person_id)]])
    rows = support_service.tickets.list(db_session, filters=filters, limit=50)
    ids = {ticket.id for ticket in rows}

    assert legacy.id in ids
    assert multi.id in ids
    assert unrelated.id not in ids


def test_list_invalid_filters_raise_400(db_session):
    with pytest.raises(DomainError) as exc_info:
        support_service.tickets.list(db_session, filters="{not json")
    assert exc_info.value.code == "invalid_ticket_filter"

    with pytest.raises(DomainError) as exc_info:
        support_service.tickets.list(
            db_session,
            filters=json.dumps([["Ticket", "no_such_field", "=", "x"]]),
        )
    assert exc_info.value.code == "invalid_ticket_filter"


# ── Service: new simple list params ──────────────────────────────────────────


def test_list_simple_params_priority_channel_creator(db_session):
    creator = uuid.uuid4()
    match = _ticket(
        priority="high",
        channel=TicketChannel.email,
        created_by_person_id=creator,
    )
    wrong_priority = _ticket(
        priority="low", channel=TicketChannel.email, created_by_person_id=creator
    )
    wrong_channel = _ticket(
        priority="high", channel=TicketChannel.phone, created_by_person_id=creator
    )
    wrong_creator = _ticket(priority="high", channel=TicketChannel.email)
    db_session.add_all([match, wrong_priority, wrong_channel, wrong_creator])
    db_session.commit()

    rows = support_service.tickets.list(
        db_session,
        priority="high",
        channel="email",
        created_by_person_id=str(creator),
        limit=50,
    )
    ids = {ticket.id for ticket in rows}

    assert ids == {match.id}


def test_subscriber_filter_matches_customer_person_link(db_session, subscriber):
    match = _ticket(customer_person_id=subscriber.id)
    other = _ticket()
    db_session.add_all([match, other])
    db_session.commit()

    rows = support_service.tickets.list(
        db_session,
        subscriber_id=str(subscriber.id),
        limit=50,
    )
    ids = {ticket.id for ticket in rows}

    assert ids == {match.id}


def test_list_is_active_param(db_session):
    active = _ticket()
    archived = _ticket(is_active=False)
    db_session.add_all([active, archived])
    db_session.commit()

    default_ids = {t.id for t in support_service.tickets.list(db_session, limit=200)}
    assert active.id in default_ids
    assert archived.id not in default_ids

    inactive_ids = {
        t.id
        for t in support_service.tickets.list(db_session, is_active=False, limit=200)
    }
    assert archived.id in inactive_ids
    assert active.id not in inactive_ids


# ── Filter schema for the admin UI ───────────────────────────────────────────


def test_serialize_ticket_filter_schema_shape():
    schema = serialize_ticket_filter_schema(
        status_options=["open", "closed"],
        priority_options=["normal", "high"],
        ticket_type_options=["outage"],
        staff_options=[{"id": "abc", "label": "Tech One"}],
        service_team_options=[{"id": "team1", "label": "Field Ops"}],
    )
    by_field = {entry["field"]: entry for entry in schema}

    assert set(by_field) == set(TICKET_FILTER_SPECS)
    assert {"value": "open", "label": "Open"} in by_field["status"]["options"]
    assert {"value": "abc", "label": "Tech One"} in by_field["assigned_to_person_id"][
        "options"
    ]
    assert {"value": "team1", "label": "Field Ops"} in by_field["service_team_id"][
        "options"
    ]
    operator_values = {op["value"] for op in by_field["created_at"]["operators"]}
    assert "between" in operator_values
    # Custom-builder fields expose only their whitelisted operators.
    tags_ops = {op["value"] for op in by_field["tags"]["operators"]}
    assert tags_ops == {"=", "!=", "like", "not like"}


# ── Admin list context (web queue) ───────────────────────────────────────────


def test_admin_list_context_applies_filters_and_exposes_schema(db_session):
    from app.services import web_support_tickets as web_support_tickets_service

    high = _ticket(priority="high")
    low = _ticket(priority="low")
    db_session.add_all([high, low])
    db_session.commit()

    filters = json.dumps([["Ticket", "priority", "=", "high"]])
    context = web_support_tickets_service.build_tickets_list_context(
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
        page=1,
        per_page=25,
        visible_columns_cookie=None,
        filters=filters,
    )

    ids = {ticket.id for ticket in context["tickets"]}
    assert high.id in ids
    assert low.id not in ids
    assert context["filters"] == '[["Ticket","priority","=","high"]]'
    schema_fields = {entry["field"] for entry in context["ticket_filter_schema"]}
    assert schema_fields == set(TICKET_FILTER_SPECS)


# ── API endpoint plumbing (GET /support/tickets) ─────────────────────────────


def test_api_list_tickets_accepts_filters_and_new_params(db_session):
    from app.api import support as support_api

    match = _ticket(priority="high", channel=TicketChannel.email, status="open")
    other = _ticket(priority="low", channel=TicketChannel.web, status="open")
    db_session.add_all([match, other])
    db_session.commit()

    response = support_api.list_tickets(
        search=None,
        status=None,
        ticket_type=None,
        priority="high",
        channel="email",
        assigned_to_person_id=None,
        created_by_person_id=None,
        project_manager_person_id=None,
        site_coordinator_person_id=None,
        subscriber_id=None,
        is_active=None,
        filters=json.dumps([["Ticket", "status", "=", "open"]]),
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
        db=db_session,
    )

    ids = {ticket.id for ticket in response["items"]}
    assert ids == {match.id}

    with pytest.raises(DomainError) as exc_info:
        support_api.list_tickets(
            search=None,
            status=None,
            ticket_type=None,
            priority=None,
            channel=None,
            assigned_to_person_id=None,
            created_by_person_id=None,
            project_manager_person_id=None,
            site_coordinator_person_id=None,
            subscriber_id=None,
            is_active=None,
            filters='[["Ticket", "definitely_not_a_field", "=", "x"]]',
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
            db=db_session,
        )
    assert exc_info.value.code == "invalid_ticket_filter"
