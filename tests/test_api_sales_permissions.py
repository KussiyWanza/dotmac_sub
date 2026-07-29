"""Route registration + permission guards for the Phase 3 sales-vertical
API port (§2.4): leads keep ``crm:lead:*``; pipelines ride ``crm:lead:*``;
quotes and sales orders are tightened from the CRM's auth-only surface onto
``crm:quote:*`` / ``crm:sales_order:*`` (keys seeded by the RBAC PR)."""

from fastapi.routing import APIRoute

from app.api import crm_sales as crm_sales_api
from app.api import sales as sales_api
from app.api import sales_orders as sales_orders_api


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


def test_pipeline_routes_ride_lead_permissions():
    router = crm_sales_api.router
    assert _route_has_permission(router, "/crm/pipelines", "GET", "crm:lead:read")
    assert _route_has_permission(router, "/crm/pipelines", "POST", "crm:lead:write")
    assert _route_has_permission(
        router, "/crm/pipelines/{pipeline_id}", "GET", "crm:lead:read"
    )
    assert _route_has_permission(
        router, "/crm/pipelines/{pipeline_id}", "PATCH", "crm:lead:write"
    )
    assert _route_has_permission(
        router, "/crm/pipelines/{pipeline_id}", "DELETE", "crm:lead:write"
    )
    assert _route_has_permission(
        router, "/crm/pipelines/{pipeline_id}/stages", "POST", "crm:lead:write"
    )
    assert _route_has_permission(
        router, "/crm/pipelines/{pipeline_id}/stages", "GET", "crm:lead:read"
    )
    assert _route_has_permission(
        router, "/crm/pipeline-stages/{stage_id}", "PATCH", "crm:lead:write"
    )


def test_lead_routes_keep_crm_lead_permissions():
    router = crm_sales_api.router
    assert _route_has_permission(router, "/crm/leads", "GET", "crm:lead:read")
    assert _route_has_permission(router, "/crm/leads", "POST", "crm:lead:write")
    assert _route_has_permission(router, "/crm/leads/{lead_id}", "GET", "crm:lead:read")
    assert _route_has_permission(
        router, "/crm/leads/{lead_id}", "PATCH", "crm:lead:write"
    )
    assert _route_has_permission(
        router, "/crm/leads/{lead_id}", "DELETE", "crm:lead:delete"
    )


def test_lead_list_exposes_contact_search_and_owner_filters():
    route = _get_route(crm_sales_api.router, "/crm/leads", "GET")
    param_names = {param.name for param in route.dependant.query_params}
    assert {
        "search",
        "status",
        "pipeline_id",
        "stage_id",
        "owner_agent_id",
        "lead_source",
    } <= param_names


def test_quote_routes_tightened_onto_crm_quote_permissions():
    router = crm_sales_api.router
    assert _route_has_permission(router, "/crm/quotes", "GET", "crm:quote:read")
    assert _route_has_permission(router, "/crm/quotes", "POST", "crm:quote:write")
    assert _route_has_permission(
        router, "/crm/quotes/{quote_id}", "GET", "crm:quote:read"
    )
    assert _route_has_permission(
        router, "/crm/quotes/{quote_id}", "PATCH", "crm:quote:write"
    )
    assert _route_has_permission(
        router, "/crm/quotes/{quote_id}", "DELETE", "crm:quote:write"
    )
    assert _route_has_permission(
        router, "/crm/quotes/{quote_id}/line-items", "POST", "crm:quote:write"
    )
    assert _route_has_permission(
        router, "/crm/quotes/{quote_id}/line-items", "GET", "crm:quote:read"
    )
    assert _route_has_permission(
        router, "/crm/quote-line-items/{item_id}", "PATCH", "crm:quote:write"
    )


def test_kanban_routes_ride_lead_permissions():
    router = sales_api.router
    assert _route_has_permission(router, "/leads/kanban", "GET", "crm:lead:read")
    assert _route_has_permission(router, "/leads/kanban/move", "POST", "crm:lead:write")


def test_sales_order_routes_tightened_onto_sales_order_permissions():
    router = sales_orders_api.router
    assert _route_has_permission(
        router, "/sales-orders", "POST", "crm:sales_order:write"
    )
    assert _route_has_permission(router, "/sales-orders", "GET", "crm:sales_order:read")
    assert _route_has_permission(
        router, "/sales-orders/{sales_order_id}", "GET", "crm:sales_order:read"
    )
    assert _route_has_permission(
        router, "/sales-orders/{sales_order_id}", "PATCH", "crm:sales_order:write"
    )
    assert _route_has_permission(
        router, "/sales-orders/{sales_order_id}", "DELETE", "crm:sales_order:write"
    )
    assert _route_has_permission(
        router, "/sales-orders/{sales_order_id}/lines", "POST", "crm:sales_order:write"
    )
    assert _route_has_permission(
        router, "/sales-orders/{sales_order_id}/lines", "GET", "crm:sales_order:read"
    )
    assert _route_has_permission(
        router, "/sales-orders/lines/{line_id}", "PATCH", "crm:sales_order:write"
    )


def test_sales_order_list_has_no_account_id_param():
    """The crm#233 fix: the legacy account_id query param (which the CRM
    passed positionally into the service's quote_id slot) is gone."""
    route = _get_route(sales_orders_api.router, "/sales-orders", "GET")
    param_names = {p.name for p in route.dependant.query_params}
    assert "account_id" not in param_names
    assert {"subscriber_id", "quote_id", "status", "payment_status"} <= param_names


def test_sales_routers_registered_in_main():
    from app import main

    registered = {
        (module, attr) for module, attr, _kind, _mode in main._DEFERRED_API_ROUTER_SPECS
    }
    assert ("app.api.crm_sales", "router") in registered
    assert ("app.api.sales", "router") in registered
    assert ("app.api.sales_orders", "router") in registered
