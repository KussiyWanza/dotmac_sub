from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services import support as support_service
from app.services import support_ticket_settings, web_support_tickets
from app.services import typeahead as typeahead_service
from app.web.admin import support_tickets as admin_support_tickets
from tests.staff_identity_fixtures import add_bound_staff_user

REPO_ROOT = Path(__file__).resolve().parents[1]


def _system_user(**overrides) -> SystemUser:
    return SystemUser(
        first_name=overrides.pop("first_name", "Support"),
        last_name=overrides.pop("last_name", "Agent"),
        display_name=overrides.pop("display_name", "Support Agent"),
        email=overrides.pop("email", f"{uuid4().hex}@example.com"),
        phone=overrides.pop("phone", "+15551110000"),
        **overrides,
    )


def test_list_people_limit_excludes_records_after_first_500(db_session) -> None:
    for index in range(500):
        db_session.add(
            Subscriber(
                first_name=f"A{index:03d}",
                last_name="Customer",
                email=f"a{index:03d}@example.com",
            )
        )
    target = Subscriber(
        first_name="Zed",
        last_name="Customer",
        email="zed-customer@example.com",
        account_number="ACCT-ZED",
    )
    db_session.add(target)
    db_session.commit()

    people = support_service.list_people(db_session)

    assert len(people) == 500
    assert str(target.id) not in {item["id"] for item in people}


def test_subscriber_typeahead_finds_records_beyond_preloaded_people_limit(
    db_session,
) -> None:
    for index in range(500):
        db_session.add(
            Subscriber(
                first_name=f"A{index:03d}",
                last_name="Customer",
                email=f"a{index:03d}@example.com",
            )
        )
    target = Subscriber(
        first_name="Zed",
        last_name="Customer",
        email="zed-search@example.com",
        account_number="ACCT-ZSEARCH",
        subscriber_number="SUB-ZSEARCH",
        phone="+2348000000000",
        company_name="Zed Corp",
        address_line1="1 Search Way",
        city="Lagos",
        region="LA",
    )
    db_session.add(target)
    db_session.commit()

    results = typeahead_service.subscribers(db_session, "zed-search@example.com", 8)

    assert len(results) == 1
    assert str(results[0]["id"]) == str(target.id)
    assert results[0]["email"] == "zed-search@example.com"
    assert results[0]["account_number"] == "ACCT-ZSEARCH"
    assert results[0]["service_address"] == "1 Search Way, Lagos, LA"


def test_ticket_form_context_prefills_selected_person_labels(db_session) -> None:
    subscriber = Subscriber(
        first_name="Typeahead",
        last_name="Target",
        email=f"{uuid4().hex}@example.com",
        account_number="ACCT-123",
    )
    db_session.add(subscriber)
    db_session.commit()

    context = web_support_tickets.build_ticket_form_context(
        db_session,
        query_params={
            "subscriber_id": str(subscriber.id),
            "customer_person_id": str(subscriber.id),
        },
    )

    assert context["prefill"]["subscriber_label"]
    assert context["prefill"]["customer_person_label"]
    assert context["selected_person"]["id"] == str(subscriber.id)


def test_ticket_form_context_uses_staff_options_for_assignments(db_session) -> None:
    subscriber = Subscriber(
        first_name="Customer",
        last_name="User",
        email=f"{uuid4().hex}@example.com",
    )
    staff = _system_user(display_name="Internal Technician")
    db_session.add_all([subscriber, staff])
    db_session.commit()

    context = web_support_tickets.build_ticket_form_context(
        db_session,
        query_params={"subscriber_id": str(subscriber.id)},
    )

    staff_ids = {item["id"] for item in context["staff_options"]}
    subscriber_ids = {item["id"] for item in context["subscriber_options"]}

    assert str(staff.id) in staff_ids
    assert str(subscriber.id) not in staff_ids
    assert str(subscriber.id) in subscriber_ids


def test_ticket_form_context_includes_authoritative_region_manager_preview(
    db_session,
) -> None:
    manager, _person = add_bound_staff_user(db_session)
    support_ticket_settings.update_ticket_configuration(
        db_session,
        support_ticket_settings.TicketConfigurationUpdate(
            statuses=("open",),
            priorities=("normal",),
            ticket_types=("incident",),
            regions=("test-region",),
            routing_rules=(
                support_ticket_settings.RegionRoutingRuleUpdate(
                    region="test-region",
                    ticket_manager_person_id=manager.id,
                ),
            ),
        ),
    )

    context = web_support_tickets.build_ticket_form_context(db_session)

    assert context["region_manager_routing"] == {"test-region": str(manager.id)}

    request = SimpleNamespace(
        state=SimpleNamespace(csrf_token="test-csrf-token"),
        query_params={},
        headers={},
        cookies={},
        url=SimpleNamespace(path="/admin/support/tickets/new"),
        session={},
        client=None,
        scope={},
        url_for=lambda *_args, **_kwargs: "/",
    )
    html = admin_support_tickets.templates.env.get_template(
        "admin/support/tickets/new.html"
    ).render(
        request=request,
        csrf_token="test-csrf-token",
        current_user={"name": "Test Admin", "email": "admin@example.com"},
        sidebar_stats={},
        active_menu="support",
        active_page="support-tickets",
        page_title="New Ticket",
        form_mode="create",
        ticket=None,
        error=None,
        duplicate_warning=None,
        **context,
    )
    assert f'"test-region": "{manager.id}"' in html
    assert 'x-model="selectedManager"' in html
    assert 'x-model="selectedRegion"' in html


def test_list_assignment_people_keeps_legacy_subscriber_assignments_visible(
    db_session,
) -> None:
    legacy_subscriber = Subscriber(
        first_name="Legacy",
        last_name="Assignee",
        email=f"{uuid4().hex}@example.com",
    )
    staff = _system_user(display_name="Current Staff")
    db_session.add_all([legacy_subscriber, staff])
    db_session.commit()

    options = support_service.list_assignment_people(
        db_session,
        include_ids=[legacy_subscriber.id],
    )

    labels = {item["id"]: item["label"] for item in options}

    assert str(staff.id) in labels
    assert labels[str(legacy_subscriber.id)] == "Legacy Assignee"


def test_support_ticket_form_uses_live_typeahead_endpoints() -> None:
    template = (REPO_ROOT / "templates/admin/support/tickets/new.html").read_text()

    assert 'data-typeahead-url="/api/v1/search/people"' in template
    assert 'data-typeahead-url="/api/v1/search/subscribers"' in template
    assert "window.initTypeaheadFields(this.$root)" in template
    assert 'list="people-options"' not in template


def test_support_ticket_templates_use_staff_data_for_assignment_controls() -> None:
    form_template = (REPO_ROOT / "templates/admin/support/tickets/new.html").read_text()
    index_template = (
        REPO_ROOT / "templates/admin/support/tickets/index.html"
    ).read_text()
    detail_template = (
        REPO_ROOT / "templates/admin/support/tickets/detail.html"
    ).read_text()

    assert "staff_options" in form_template
    assert "staff_options" in index_template
    # Subscriber filtering on the ticket index converged into the code-native
    # filter builder (ListDefinition subscriber_id is filterable); the standalone
    # ``subscriber_options`` select was retired with the list convergence (#1305).
    assert "data-filter-schema" in index_template
    assert "{% for person in staff_options %}" in detail_template
    assert "Type at least 2 characters to search technicians" in form_template
    assert "if (search.length < 2) return []" in form_template
    assert 'x-show="shouldShowAssigneeResults"' in form_template


def test_ticket_detail_mentions_track_person_and_group_tokens() -> None:
    detail_template = (
        REPO_ROOT / "templates/admin/support/tickets/detail.html"
    ).read_text()

    assert "mention.token" in detail_template
    assert "item.token === person.id" in detail_template
    assert "selectedMentions.push({id: person.id, token: person.id, label})" in (
        detail_template
    )
