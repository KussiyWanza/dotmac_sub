"""Admin sales web surface tests (Phase 3 §2.6, PR 11): route registration +
RBAC guards, ``web_sales`` context builders, and Jinja compilation of the new
``templates/admin/sales/*`` pages."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.models.party import Party
from app.models.rbac import Role, SystemUserRole
from app.models.sales import LeadStatus, SalesOrder
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.schemas.sales import (
    LeadCreate,
    PipelineCreate,
    PipelineStageCreate,
    QuoteCreate,
    QuoteLineItemCreate,
)
from app.services import sales as sales_service
from app.services import sales_orders as sales_orders_service
from app.services import web_sales, web_sales_dashboard
from app.services.sales import reports as sales_reports
from app.web.admin import sales as admin_sales

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_route(router, path: str, method: str) -> APIRoute:
    for route in router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in route.methods
        ):
            return route
    raise AssertionError(f"Route not found: {method} {path}")


def _contains_value(value, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, (tuple, list, set)):
        return any(_contains_value(item, expected) for item in value)
    if isinstance(value, dict):
        return any(_contains_value(item, expected) for item in value.values())
    return False


def _route_has_permission(router, path: str, method: str, expected: str) -> bool:
    route = _get_route(router, path, method)
    for dependency in route.dependant.dependencies:
        call = dependency.call
        closure = getattr(call, "__closure__", None) or ()
        for cell in closure:
            if _contains_value(cell.cell_contents, expected):
                return True
    return False


def _make_subscriber(db, **overrides) -> Subscriber:
    data = {
        "first_name": "Ada",
        "last_name": "Obi",
        "email": f"ada-{uuid.uuid4().hex}@example.com",
    }
    data.update(overrides)
    party = Party(
        display_name=f"{data['first_name']} {data['last_name']}",
        party_type="person",
        status="active",
    )
    db.add(party)
    db.flush()
    data.update(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Admin sales fixture Party binding",
    )
    subscriber = Subscriber(**data)
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


def _make_pipeline(db, name="Sales"):
    return sales_service.pipelines.create(db, PipelineCreate(name=name))


def _make_stage(db, pipeline, name="New", order_index=0, default_probability=25):
    return sales_service.pipeline_stages.create(
        db,
        PipelineStageCreate(
            pipeline_id=pipeline.id,
            name=name,
            order_index=order_index,
            default_probability=default_probability,
        ),
    )


def _make_lead(db, subscriber, **overrides):
    payload = {"subscriber_id": subscriber.id, "title": "Fiber install"}
    payload.update(overrides)
    requested_status = payload.get("status")
    if requested_status == LeadStatus.won.value:
        payload["status"] = LeadStatus.qualified.value
    lead = sales_service.leads.create(db, LeadCreate(**payload))
    if requested_status == LeadStatus.won.value:
        # Reporting tests need historical Won state; the public transition is
        # exercised separately through Quote acceptance.
        lead.status = LeadStatus.won.value
        lead.closed_at = datetime.now(UTC)
        db.commit()
        db.refresh(lead)
    return lead


# ---------------------------------------------------------------------------
# Route registration + permission guards
# ---------------------------------------------------------------------------


def test_lead_routes_require_lead_permissions():
    router = admin_sales.router
    assert _route_has_permission(router, "/sales/leads", "GET", "crm:lead:read")
    assert _route_has_permission(
        router, "/sales/pipeline-board", "GET", "crm:lead:read"
    )
    assert _route_has_permission(router, "/sales/leads/board", "GET", "crm:lead:read")
    assert _route_has_permission(
        router, "/sales/leads/{lead_id}", "GET", "crm:lead:read"
    )
    for path, method in [
        ("/sales/leads/new", "GET"),
        ("/sales/leads", "POST"),
        ("/sales/leads/{lead_id}/edit", "GET"),
        ("/sales/leads/{lead_id}/edit", "POST"),
        ("/sales/leads/{lead_id}/status", "POST"),
    ]:
        assert _route_has_permission(router, path, method, "crm:lead:write")
    assert _route_has_permission(
        router, "/sales/leads/{lead_id}/delete", "POST", "crm:lead:delete"
    )


def test_sales_dashboard_routes_require_lead_read():
    router = admin_sales.router
    assert _route_has_permission(router, "/sales", "GET", "crm:lead:read")
    assert _route_has_permission(
        router, "/sales/dashboard-data", "GET", "crm:lead:read"
    )


def test_pipeline_settings_routes_ride_lead_write():
    router = admin_sales.router
    for path, method in [
        ("/sales/pipelines-settings", "GET"),
        ("/sales/pipelines-settings/new", "GET"),
        ("/sales/pipelines-settings", "POST"),
        ("/sales/pipelines-settings/{pipeline_id}/edit", "GET"),
        ("/sales/pipelines-settings/{pipeline_id}", "POST"),
        ("/sales/pipelines-settings/{pipeline_id}/status", "POST"),
        ("/sales/pipelines-settings/{pipeline_id}/stages", "POST"),
        ("/sales/pipelines-settings/stages/{stage_id}", "POST"),
        ("/sales/pipelines-settings/stages/{stage_id}/status", "POST"),
        ("/sales/pipelines-settings/{pipeline_id}/stages/reorder", "POST"),
        ("/sales/pipelines-settings/{pipeline_id}/bulk-assign-leads", "POST"),
        ("/sales/pipelines", "GET"),
        ("/sales/pipelines/new", "GET"),
        ("/sales/pipelines", "POST"),
        ("/sales/pipelines/{pipeline_id}/edit", "GET"),
        ("/sales/pipelines/{pipeline_id}", "POST"),
        ("/sales/pipelines/{pipeline_id}/delete", "POST"),
        ("/sales/pipelines/{pipeline_id}/stages", "POST"),
        ("/sales/pipelines/stages/{stage_id}", "POST"),
        ("/sales/pipelines/stages/{stage_id}/delete", "POST"),
        ("/sales/pipelines/{pipeline_id}/bulk-assign-leads", "POST"),
    ]:
        assert _route_has_permission(router, path, method, "crm:lead:write"), (
            f"{method} {path} must require crm:lead:write"
        )


def test_quote_routes_require_quote_read():
    router = admin_sales.router
    assert _route_has_permission(router, "/sales/quotes", "GET", "crm:quote:read")
    assert _route_has_permission(
        router, "/sales/quotes/{quote_id}", "GET", "crm:quote:read"
    )
    assert _route_has_permission(
        router, "/sales/quotes/{quote_id}/pdf", "POST", "crm:quote:read"
    )
    assert _route_has_permission(
        router,
        "/sales/quotes/{quote_id}/send-email",
        "POST",
        "crm:quote:send",
    )


def test_sales_order_routes_require_sales_order_read():
    router = admin_sales.router
    assert _route_has_permission(
        router, "/sales/sales-orders", "GET", "crm:sales_order:read"
    )
    assert _route_has_permission(
        router, "/sales/sales-orders/{order_id}", "GET", "crm:sales_order:read"
    )
    assert _route_has_permission(
        router, "/sales/sales-order", "GET", "crm:sales_order:read"
    )
    assert _route_has_permission(
        router, "/sales/sales-order/{order_id}", "GET", "crm:sales_order:read"
    )


def test_sales_order_mutations_require_sales_order_write():
    router = admin_sales.router
    for path, method in (
        ("/sales/sales-order/new", "GET"),
        ("/sales/sales-order/new", "POST"),
        ("/sales/sales-order/{order_id}/edit", "GET"),
        ("/sales/sales-order/{order_id}/edit", "POST"),
        ("/sales/sales-order/{order_id}/delete", "POST"),
    ):
        assert _route_has_permission(router, path, method, "crm:sales_order:write")


def test_sales_router_is_registered_under_admin():
    from app.web.admin import router as admin_router

    paths = {route.path for route in admin_router.routes if isinstance(route, APIRoute)}
    assert "/admin/sales/leads" in paths
    assert "/admin/sales/pipeline-board" in paths
    assert "/admin/sales" in paths
    assert "/admin/sales/dashboard-data" in paths
    assert "/admin/sales/leads/board" in paths
    assert "/admin/sales/leads/new" in paths
    assert "/admin/sales/leads/{lead_id}/edit" in paths
    assert "/admin/sales/pipelines-settings" in paths
    assert "/admin/sales/pipelines" in paths
    assert "/admin/sales/quotes" in paths
    assert "/admin/sales/sales-orders" in paths
    assert "/admin/sales/sales-order" in paths
    assert "/admin/sales/sales-order/new" in paths


# ---------------------------------------------------------------------------
# Context builders — leads
# ---------------------------------------------------------------------------


def test_leads_list_context_stats_and_filters(db_session):
    pipeline = _make_pipeline(db_session, name=f"P-{uuid.uuid4().hex[:6]}")
    stage = _make_stage(db_session, pipeline)
    open_sub = _make_subscriber(db_session)
    won_sub = _make_subscriber(db_session)
    open_lead = _make_lead(
        db_session,
        open_sub,
        pipeline_id=pipeline.id,
        stage_id=stage.id,
        estimated_value=Decimal("1000.00"),
        currency="NGN",
    )
    won_lead = _make_lead(
        db_session, won_sub, status="won", estimated_value=Decimal("500.00")
    )

    context = web_sales.build_leads_list_context(
        db_session,
        status=None,
        pipeline_id=None,
        stage_id=None,
        lead_source=None,
        search=None,
        page=1,
        per_page=25,
    )
    assert context["total"] >= 2
    ids = {str(lead.id) for lead in context["leads"]}
    assert {str(open_lead.id), str(won_lead.id)} <= ids
    # Won leads never inflate the open pipeline value (CRM BUG-030 carried).
    assert context["lead_stats"]["total_value"] == Decimal("1000.00")
    assert context["lead_stats"]["won"] >= 1
    assert str(pipeline.id) in context["pipeline_map"]
    assert str(open_lead.subscriber_id) in context["subscriber_map"]

    filtered = web_sales.build_leads_list_context(
        db_session,
        status="won",
        pipeline_id=None,
        stage_id=None,
        lead_source=None,
        search=None,
        page=1,
        per_page=25,
    )
    filtered_ids = {str(lead.id) for lead in filtered["leads"]}
    assert str(won_lead.id) in filtered_ids
    assert str(open_lead.id) not in filtered_ids
    assert filtered["total"] == len(filtered_ids) or filtered["total"] >= 1

    # A bogus status is dropped rather than 400ing the page.
    bogus = web_sales.build_leads_list_context(
        db_session,
        status="not-a-status",
        pipeline_id=None,
        stage_id=None,
        lead_source=None,
        search=None,
        page=1,
        per_page=25,
    )
    assert bogus["status"] == ""


def test_leads_list_context_search_scopes_total(db_session):
    needle = uuid.uuid4().hex[:10]
    subscriber = _make_subscriber(db_session, first_name=f"Zed{needle}")
    lead = _make_lead(db_session, subscriber, title=f"Estate build {needle}")
    _make_lead(db_session, _make_subscriber(db_session))

    context = web_sales.build_leads_list_context(
        db_session,
        status=None,
        pipeline_id=None,
        stage_id=None,
        lead_source=None,
        search=needle,
        page=1,
        per_page=25,
    )
    assert context["total"] == 1
    assert [str(item.id) for item in context["leads"]] == [str(lead.id)]


def test_lead_detail_context_includes_quotes(db_session):
    subscriber = _make_subscriber(db_session)
    lead = _make_lead(db_session, subscriber)
    quote = sales_service.quotes.create(
        db_session,
        QuoteCreate(
            subscriber_id=subscriber.id,
            lead_id=lead.id,
            project_type="fiber_optics_installation",
        ),
    )

    context = web_sales.build_lead_detail_context(db_session, lead_id=str(lead.id))
    assert str(context["lead"].id) == str(lead.id)
    assert context["subscriber"].id == subscriber.id
    assert context["subscriber_label"]
    assert [str(item.id) for item in context["quotes"]] == [str(quote.id)]
    assert context["status_val"] == "new"


def test_create_quote_context_preselects_lead_and_subscriber(db_session):
    subscriber = _make_subscriber(db_session)
    lead = _make_lead(db_session, subscriber)

    context = web_sales.build_quote_new_context(db_session, lead_id=str(lead.id))

    assert context["quote_form"]["lead_id"] == str(lead.id)
    assert context["quote_form"]["subscriber_id"] == ""


def test_lead_form_creates_through_native_sales_owner(db_session):
    subscriber = _make_subscriber(db_session)
    pipeline = _make_pipeline(db_session, name=f"Form-{uuid.uuid4().hex[:6]}")
    stage = _make_stage(db_session, pipeline, name="Qualified")

    lead_id, existing = web_sales.create_lead_from_form(
        db_session,
        title="Enterprise fibre opportunity",
        status="qualified",
        party_id=str(subscriber.party_id),
        owner_agent_id=None,
        pipeline_id=str(pipeline.id),
        stage_id=str(stage.id),
        lead_source="Website",
        region="Lagos",
        estimated_value="250000.00",
        currency="NGN",
        address="Victoria Island",
        probability="65",
        expected_close_date="2026-08-31",
        lost_reason=None,
        notes="Customer requested a site survey.",
        is_active=True,
    )

    lead = sales_service.leads.get(db_session, lead_id)
    assert existing is False
    assert lead.party_id == subscriber.party_id
    assert lead.subscriber_id is None
    assert lead.pipeline_id == pipeline.id
    assert lead.stage_id == stage.id
    assert lead.probability == 65
    assert lead.estimated_value == Decimal("250000.00")


def test_pipeline_stage_pair_is_enforced_by_sales_owner(db_session):
    subscriber = _make_subscriber(db_session)
    first = _make_pipeline(db_session, name=f"First-{uuid.uuid4().hex[:6]}")
    second = _make_pipeline(db_session, name=f"Second-{uuid.uuid4().hex[:6]}")
    second_stage = _make_stage(db_session, second)

    with pytest.raises(HTTPException) as exc:
        web_sales.create_lead_from_form(
            db_session,
            title="Mismatched pipeline",
            status="new",
            party_id=str(subscriber.party_id),
            owner_agent_id=None,
            pipeline_id=str(first.id),
            stage_id=str(second_stage.id),
            lead_source="Website",
            region=None,
            estimated_value=None,
            currency="NGN",
            address=None,
            probability="10",
            expected_close_date=None,
            lost_reason=None,
            notes=None,
            is_active=True,
        )

    assert "stage does not belong" in str(exc.value.detail).lower()


def test_leads_board_context_defaults_to_first_pipeline(db_session):
    pipeline = _make_pipeline(db_session, name=f"AA-{uuid.uuid4().hex[:6]}")
    context = web_sales.build_leads_board_context(db_session, pipeline_id=None)
    assert context["selected_pipeline_id"]
    explicit = web_sales.build_leads_board_context(
        db_session, pipeline_id=str(pipeline.id)
    )
    assert explicit["selected_pipeline_id"] == str(pipeline.id)


def test_legacy_pipeline_board_url_redirects_and_preserves_pipeline():
    pipeline_id = str(uuid.uuid4())
    response = admin_sales.legacy_leads_board_redirect(pipeline_id=pipeline_id)
    assert response.status_code == 308
    assert (
        response.headers["location"]
        == f"/admin/sales/pipeline-board?pipeline_id={pipeline_id}"
    )


def test_kanban_cards_link_to_sub_admin_leads(db_session):
    pipeline = _make_pipeline(db_session, name=f"K-{uuid.uuid4().hex[:6]}")
    stage = _make_stage(db_session, pipeline)
    subscriber = _make_subscriber(db_session)
    lead = _make_lead(
        db_session, subscriber, pipeline_id=pipeline.id, stage_id=stage.id
    )

    board = sales_service.leads.kanban_view(db_session, str(pipeline.id))
    column = next(item for item in board["columns"] if item["id"] == str(stage.id))
    record = next(item for item in board["records"] if item["id"] == str(lead.id))
    assert column["stage_type"] == "standard"
    assert column["color"] == "#06B6D4"
    assert column["icon"] is None
    assert record["url"] == f"/admin/sales/leads/{lead.id}"


def test_sales_dashboard_uses_native_currency_safe_reporting(db_session):
    pipeline = _make_pipeline(db_session, name=f"D-{uuid.uuid4().hex[:6]}")
    stage = _make_stage(
        db_session,
        pipeline,
        default_probability=25,
    )
    open_lead = _make_lead(
        db_session,
        _make_subscriber(db_session),
        pipeline_id=pipeline.id,
        stage_id=stage.id,
        estimated_value=Decimal("1000.00"),
        currency="NGN",
        probability=None,
    )
    owner_agent_id = uuid.uuid4()
    won_lead = _make_lead(
        db_session,
        _make_subscriber(db_session),
        pipeline_id=pipeline.id,
        stage_id=stage.id,
        owner_agent_id=owner_agent_id,
        status="won",
        estimated_value=Decimal("600.00"),
        currency="USD",
    )
    now = datetime.now(UTC)

    report = sales_reports.dashboard_report(
        db_session,
        pipeline_id=pipeline.id,
        start_at=now - timedelta(days=30),
        end_at=now + timedelta(seconds=1),
    )
    assert report.summary.pipeline_values == {"NGN": Decimal("1000.00")}
    assert report.summary.weighted_values == {"NGN": Decimal("250.00")}
    assert report.summary.open_deals == 1
    assert report.summary.won_deals == 1
    assert report.summary.average_deal_sizes == {"USD": Decimal("600.00")}
    assert report.agent_performance[0].agent_id == owner_agent_id
    assert report.recent_opportunities[0].id in {open_lead.id, won_lead.id}

    context = web_sales_dashboard.build_dashboard_data_context(
        db_session,
        pipeline_id=str(pipeline.id),
        period_days=30,
    )
    assert context["metrics"]["pipeline_value"] == "NGN 1,000.00"
    assert context["metrics"]["weighted_value"] == "NGN 250.00"
    assert context["metrics"]["average_deal_size"] == "USD 600.00"
    assert context["agent_rows"][0]["name"] == "Unavailable sales agent"


def test_sales_dashboard_resolves_historical_agent_system_user_name(db_session):
    pipeline = _make_pipeline(db_session, name=f"Agent-{uuid.uuid4().hex[:6]}")
    stage = _make_stage(db_session, pipeline, default_probability=100)
    agent = SystemUser(
        first_name="Samuel",
        last_name="Ojo",
        display_name="Samuel Ojo",
        email=f"samuel-{uuid.uuid4().hex[:8]}@example.com",
        is_active=False,
    )
    db_session.add(agent)
    db_session.flush()
    _make_lead(
        db_session,
        _make_subscriber(db_session),
        pipeline_id=pipeline.id,
        stage_id=stage.id,
        owner_agent_id=agent.id,
        status="won",
        estimated_value=Decimal("600.00"),
        currency="NGN",
    )

    context = web_sales_dashboard.build_dashboard_data_context(
        db_session,
        pipeline_id=str(pipeline.id),
        period_days=30,
    )

    assert context["agent_rows"][0]["name"] == "Samuel Ojo"


# ---------------------------------------------------------------------------
# Context builders — pipeline settings
# ---------------------------------------------------------------------------


def test_create_pipeline_from_form_seeds_default_stages(db_session):
    pipeline_id = web_sales.create_pipeline_from_form(
        db_session,
        name=f"Form {uuid.uuid4().hex[:6]}",
        is_active="true",
        create_default_stages="on",
    )
    context = web_sales.build_pipeline_settings_context(
        db_session, bulk_result="", bulk_count=""
    )
    stages = context["stage_map"].get(pipeline_id, [])
    assert len(stages) == len(web_sales.DEFAULT_PIPELINE_STAGES)
    assert stages[0].name == "Lead Identified"
    closed_won = next(stage for stage in stages if stage.name == "Closed Won")
    assert (
        context["stage_presentations"][str(closed_won.id)].stage_type.value
        == "closed_won"
    )
    assert any(str(p.id) == pipeline_id for p in context["pipelines"])


def test_create_pipeline_from_form_requires_name(db_session):
    with pytest.raises(ValueError):
        web_sales.create_pipeline_from_form(
            db_session, name="   ", is_active=None, create_default_stages=None
        )


def test_pipeline_form_contexts(db_session):
    new_ctx = web_sales.build_pipeline_new_context()
    assert new_ctx["action_url"] == "/admin/sales/pipelines-settings"
    assert new_ctx["pipeline"]["create_default_stages"] is True
    assert new_ctx["is_editing"] is False

    pipeline = _make_pipeline(db_session, name=f"Edit-{uuid.uuid4().hex[:6]}")
    edit_ctx = web_sales.build_pipeline_edit_context(
        db_session, pipeline_id=str(pipeline.id)
    )
    assert edit_ctx["action_url"] == f"/admin/sales/pipelines-settings/{pipeline.id}"
    assert edit_ctx["is_editing"] is True

    err_ctx = web_sales.build_pipeline_form_error_context(
        mode="update",
        pipeline_id=str(pipeline.id),
        name="  X  ",
        is_active="false",
        create_default_stages=None,
    )
    assert err_ctx["pipeline"]["name"] == "X"
    assert err_ctx["pipeline"]["is_active"] is False


def test_stage_presentation_and_atomic_reordering(db_session):
    pipeline = _make_pipeline(db_session, name=f"Order-{uuid.uuid4().hex[:6]}")
    first = _make_stage(db_session, pipeline, name="First", order_index=0)
    second = _make_stage(db_session, pipeline, name="Second", order_index=1)

    web_sales.update_stage_from_form(
        db_session,
        stage_id=str(second.id),
        name="Closed Won",
        order_index=1,
        default_probability=100,
        is_active="true",
        stage_type="closed_won",
        color="#10B981",
        icon="check",
    )
    reordered = web_sales.reorder_stages(
        db_session,
        pipeline_id=str(pipeline.id),
        stage_ids=f"{second.id},{first.id}",
    )
    assert reordered == (str(second.id), str(first.id))
    db_session.refresh(first)
    db_session.refresh(second)
    assert (second.order_index, first.order_index) == (0, 1)

    board = sales_service.leads.kanban_view(db_session, str(pipeline.id))
    won_column = next(item for item in board["columns"] if item["id"] == str(second.id))
    assert won_column["stage_type"] == "closed_won"
    assert won_column["color"] == "#10B981"
    assert won_column["icon"] == "check"

    with pytest.raises(HTTPException) as exc_info:
        web_sales.reorder_stages(
            db_session,
            pipeline_id=str(pipeline.id),
            stage_ids=str(first.id),
        )
    assert exc_info.value.status_code == 409

    with pytest.raises(ValueError, match="Unsupported pipeline stage type"):
        web_sales.create_stage_from_form(
            db_session,
            pipeline_id=str(pipeline.id),
            name="Unsupported",
            order_index=2,
            default_probability=50,
            stage_type="custom_json_type",
        )


def test_stage_crud_and_bulk_assign_from_form(db_session):
    pipeline = _make_pipeline(db_session, name=f"S-{uuid.uuid4().hex[:6]}")
    web_sales.create_stage_from_form(
        db_session,
        pipeline_id=str(pipeline.id),
        name="  Survey  ",
        order_index=1,
        default_probability=30,
    )
    context = web_sales.build_pipeline_settings_context(
        db_session, bulk_result="", bulk_count=""
    )
    stages = context["stage_map"][str(pipeline.id)]
    assert stages[0].name == "Survey"

    web_sales.update_stage_from_form(
        db_session,
        stage_id=str(stages[0].id),
        name="Site Survey",
        order_index=2,
        default_probability=40,
        is_active="true",
    )
    db_session.refresh(stages[0])
    assert stages[0].name == "Site Survey"
    assert stages[0].default_probability == 40

    # Unassigned lead gets pulled in by bulk assignment.
    subscriber = _make_subscriber(db_session)
    lead = _make_lead(db_session, subscriber)
    count = web_sales.bulk_assign_leads(
        db_session,
        pipeline_id=str(pipeline.id),
        stage_id=str(stages[0].id),
        scope="unassigned",
    )
    assert count >= 1
    db_session.refresh(lead)
    assert lead.pipeline_id == pipeline.id
    assert lead.stage_id == stages[0].id

    web_sales.deactivate_stage(db_session, stage_id=str(stages[0].id))
    db_session.refresh(stages[0])
    assert stages[0].is_active is False

    web_sales.deactivate_pipeline(db_session, str(pipeline.id))
    db_session.refresh(pipeline)
    assert pipeline.is_active is False


# ---------------------------------------------------------------------------
# Context builders — quotes
# ---------------------------------------------------------------------------


def test_quotes_list_context(db_session):
    subscriber = _make_subscriber(db_session)
    lead = _make_lead(db_session, subscriber)
    quote = sales_service.quotes.create(
        db_session,
        QuoteCreate(
            subscriber_id=subscriber.id,
            lead_id=lead.id,
            project_type="fiber_optics_installation",
        ),
    )

    context = web_sales.build_quotes_list_context(
        db_session,
        status=None,
        lead_id=str(lead.id),
        search=None,
        page=1,
        per_page=25,
    )
    assert context["total"] == 1
    assert [str(item.id) for item in context["quotes"]] == [str(quote.id)]
    assert str(lead.id) in context["lead_map"]
    assert str(subscriber.id) in context["subscriber_map"]
    assert context["stats"]["total"] >= 1

    bogus = web_sales.build_quotes_list_context(
        db_session,
        status="never-a-status",
        lead_id=None,
        search=None,
        page=1,
        per_page=25,
    )
    assert bogus["status"] == ""


def test_quote_detail_context_line_items_deposit_and_accept_state(db_session):
    subscriber = _make_subscriber(db_session)
    lead = sales_service.leads.create(
        db_session, LeadCreate(subscriber_id=subscriber.id)
    )
    quote = sales_service.quotes.create(
        db_session,
        QuoteCreate(
            subscriber_id=subscriber.id,
            lead_id=lead.id,
            project_type="fiber_optics_installation",
            metadata_={
                "source": "portal_self_serve",
                "deposit_percent": 50,
                "feasibility": {"feasible": True, "distance_m": 120},
                "install": {"latitude": 9.05, "longitude": 7.49, "address": "Abuja"},
                "deposit": {
                    "reference": "dep-ref-1",
                    "amount": "500.00",
                    "provider": "paystack",
                    "paid": True,
                },
            },
        ),
    )
    item = sales_service.quote_line_items.create(
        db_session,
        QuoteLineItemCreate(
            quote_id=quote.id,
            description="Installation",
            quantity=Decimal("1.000"),
            unit_price=Decimal("1000.00"),
            metadata_={"sub_offer_id": str(uuid.uuid4())},
        ),
    )

    context = web_sales.build_quote_detail_context(db_session, quote_id=str(quote.id))
    assert [str(row.id) for row in context["items"]] == [str(item.id)]
    assert context["deposit"]["reference"] == "dep-ref-1"
    assert context["deposit"]["paid"] is True
    assert context["deposit_percent"] == 50
    assert context["feasibility"]["feasible"] is True
    assert context["install"]["address"] == "Abuja"
    assert context["is_accepted"] is False
    assert context["subscriber"].id == subscriber.id

    # Accept-state display flips with the stored status (display-only here —
    # the accept pipeline itself is covered by the sales-service tests).
    quote.status = "accepted"
    db_session.commit()
    accepted = web_sales.build_quote_detail_context(db_session, quote_id=str(quote.id))
    assert accepted["is_accepted"] is True
    assert accepted["status_val"] == "accepted"


# ---------------------------------------------------------------------------
# Context builders — sales orders
# ---------------------------------------------------------------------------


def _make_sales_order(db, subscriber, **overrides) -> SalesOrder:
    data = {
        "subscriber_id": subscriber.id,
        "order_number": f"SO-{uuid.uuid4().hex[:8]}",
        "status": "confirmed",
        "payment_status": "partial",
        "currency": "NGN",
        "subtotal": Decimal("100.00"),
        "total": Decimal("100.00"),
        "amount_paid": Decimal("40.00"),
        "balance_due": Decimal("60.00"),
    }
    data.update(overrides)
    order = SalesOrder(**data)
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def test_sales_orders_list_context_stats_and_filters(db_session):
    subscriber = _make_subscriber(db_session)
    order = _make_sales_order(db_session, subscriber)
    paid = _make_sales_order(
        db_session,
        subscriber,
        status="paid",
        payment_status="paid",
        amount_paid=Decimal("100.00"),
        balance_due=Decimal("0.00"),
    )

    context = web_sales.build_sales_orders_list_context(
        db_session,
        status=None,
        payment_status=None,
        source_type=None,
        search=None,
        page=1,
        per_page=25,
    )
    assert context["total"] >= 2
    assert context["stats"]["gross_sales"] >= Decimal("200.00")
    assert context["stats"]["paid"] >= 1
    assert context["stats"]["partial"] >= 1
    assert context["stats"]["manual"] >= 2
    assert str(subscriber.id) in context["subscriber_map"]

    only_paid = web_sales.build_sales_orders_list_context(
        db_session,
        status=None,
        payment_status="paid",
        source_type="manual",
        search=None,
        page=1,
        per_page=25,
    )
    ids = {str(item.id) for item in only_paid["orders"]}
    assert str(paid.id) in ids
    assert str(order.id) not in ids

    # Search by order number narrows to the single row.
    by_number = web_sales.build_sales_orders_list_context(
        db_session,
        status=None,
        payment_status=None,
        source_type=None,
        search=order.order_number,
        page=1,
        per_page=25,
    )
    assert by_number["total"] == 1


def test_sales_order_detail_context(db_session):
    subscriber = _make_subscriber(db_session)
    order = _make_sales_order(db_session, subscriber)

    context = web_sales.build_sales_order_detail_context(
        db_session, sales_order_id=str(order.id)
    )
    assert str(context["order"].id) == str(order.id)
    assert context["subscriber"].id == subscriber.id
    assert context["subscriber_label"]
    assert context["lines"] == []
    assert context["quote"] is None
    assert context["project"] is None


def test_sales_agent_options_use_active_customer_experience_system_users(db_session):
    role = Role(name=f"Customer-Experience-{uuid.uuid4().hex[:6]}", is_active=True)
    # Normalize the test role to the supported mapped spelling after making its
    # database name unique from any fixture seed.
    role.name = "Customer_Experience"
    user = SystemUser(
        first_name="Chidi",
        last_name="Okoro",
        email=f"chidi-{uuid.uuid4().hex}@example.com",
        is_active=True,
    )
    inactive = SystemUser(
        first_name="Retired",
        last_name="Agent",
        email=f"retired-{uuid.uuid4().hex}@example.com",
        is_active=False,
    )
    db_session.add_all([role, user, inactive])
    db_session.flush()
    db_session.add_all(
        [
            SystemUserRole(system_user_id=user.id, role_id=role.id, source="mapped"),
            SystemUserRole(
                system_user_id=inactive.id, role_id=role.id, source="mapped"
            ),
        ]
    )
    db_session.commit()

    options = web_sales.sales_agent_options(db_session)
    option_ids = {item["id"] for item in options}
    assert str(user.id) in option_ids
    assert str(inactive.id) not in option_ids
    assert next(item for item in options if item["id"] == str(user.id))["email"]


def test_manual_sales_order_vat_is_owned_by_sales_orders_service():
    totals = sales_orders_service.calculate_manual_order_totals(
        [(Decimal("2"), Decimal("50.00"))]
    )
    assert totals.subtotal == Decimal("100.00")
    assert totals.tax_total == Decimal("7.50")
    assert totals.total == Decimal("107.50")


# ---------------------------------------------------------------------------
# Templates compile
# ---------------------------------------------------------------------------

_SALES_TEMPLATES = [
    "admin/sales/dashboard.html",
    "admin/sales/_dashboard_data.html",
    "admin/sales/leads/index.html",
    "admin/sales/leads/board.html",
    "admin/sales/leads/detail.html",
    "admin/sales/leads/new_form.html",
    "admin/sales/pipelines/index.html",
    "admin/sales/pipelines/form.html",
    "admin/sales/quotes/index.html",
    "admin/sales/quotes/detail.html",
    "admin/sales/sales_orders/index.html",
    "admin/sales/sales_orders/detail.html",
    "admin/sales/sales_orders/form.html",
]


@pytest.mark.parametrize("template_name", _SALES_TEMPLATES)
def test_sales_templates_compile(template_name):
    templates = Jinja2Templates(directory="templates")
    assert templates.env.get_template(template_name) is not None


def test_complete_lead_form_covers_contact_profile_and_sales_fields():
    source = Path("templates/admin/sales/leads/new_form.html").read_text()
    for field_name in (
        "title",
        "display_name",
        "emails",
        "primary_email",
        "phones",
        "primary_phone",
        "address_line1",
        "address_line2",
        "date_of_birth",
        "gender",
        "nin",
        "city",
        "postal_code",
        "country_code",
        "organization_id",
        "reseller_id",
        "pipeline_id",
        "stage_id",
        "lead_source",
        "region_zone_id",
        "estimated_value",
        "currency",
        "probability",
        "expected_close_date",
        "lost_reason",
        "notes",
        "address",
        "is_active",
    ):
        assert f'name="{field_name}"' in source
    assert 'checkbox.name = "whatsapp_phone_indices"' in source
    assert "Leave blank to keep the stored NIN" in source
    assert "Lead Source is fixed by the original capture" in source


def test_edit_lead_context_composes_complete_form(db_session):
    subscriber = _make_subscriber(db_session)
    lead = _make_lead(db_session, subscriber)
    context = web_sales.build_lead_edit_context(db_session, lead_id=str(lead.id))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/admin/sales/leads/{lead.id}/edit",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
    )
    context.update(
        {
            "request": request,
            "active_page": "sales-leads",
            "active_menu": "sales",
            "current_user": None,
            "sidebar_stats": {},
            "csp_nonce": "test-nonce",
            "inbox_conversation_id": None,
        }
    )

    rendered = admin_sales.templates.env.get_template(
        "admin/sales/leads/new_form.html"
    ).render(context)

    assert "Edit Lead" in rendered
    assert f'action="/admin/sales/leads/{lead.id}/edit"' in rendered
    assert 'name="emails"' in rendered
    assert f'value="{subscriber.email}"' in rendered
    assert 'name="title"' in rendered
    assert "Update Lead" in rendered


def test_board_template_wires_kanban_api_endpoints():
    source = Path("templates/admin/sales/leads/board.html").read_text()
    assert 'action="/admin/sales/pipeline-board"' in source
    assert 'data-kanban-endpoint="/api/v1/leads/kanban?pipeline_id=' in source
    assert 'data-update-endpoint="/api/v1/leads/kanban/move"' in source
    assert "/static/js/kanban.js" in source
    assert "/admin/sales/pipelines-settings" in source


def test_pipeline_settings_template_uses_canonical_routes_and_ux():
    source = Path("templates/admin/sales/pipelines/index.html").read_text()
    assert "data-pipeline-settings" in source
    assert 'id="pipeline-search"' in source
    assert 'id="pipeline-status-filter"' in source
    assert "data-stage-ordering" in source
    assert "data-bulk-assignment-form" in source
    assert "/admin/sales/pipelines-settings/new" in source
    assert "/static/js/pipeline-settings.js" in source
    assert "No pipelines have been created yet." in source
    assert 'action="/admin/sales/pipelines/' not in source

    script = Path("static/js/pipeline-settings.js").read_text()
    assert "requestSubmit" in script
    assert "All Active Leads" not in script


def test_active_sales_ui_no_longer_links_to_legacy_board_url():
    sources = "\n".join(
        Path(path).read_text()
        for path in (
            "templates/admin/sales/leads/index.html",
            "templates/admin/sales/leads/detail.html",
            "templates/admin/sales/leads/board.html",
            "app/web/admin/sales.py",
        )
    )
    assert "/admin/sales/leads/board" not in sources


def test_sidebar_has_sales_entry():
    source = Path("templates/components/navigation/admin_sidebar.html").read_text()
    assert '"/admin/sales"' in source or "'/admin/sales'" in source
    assert "'sales-quotes': 'sales'" in source
    assert "'sales-orders': 'sales'" in source


def test_sales_dashboard_more_menu_links_sales_worklists():
    source = Path("templates/admin/sales/dashboard.html").read_text()
    assert 'href="/admin/sales/leads"' in source
    assert 'href="/admin/sales/quotes"' in source
    assert 'href="/admin/sales/sales-orders"' in source
