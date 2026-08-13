"""Every dashboard KPI and attention item must drill into a real route.

The dashboard contract requires each KPI to link to the exact filtered cohort
that produced it. This guards the weaker invariant that is mechanically
checkable: no dead drill-down links — every href rendered by the KPI strip and
the attention-feed owner resolves to a registered route.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute

from app.services.admin_attention import build_attention_items

_STATS = (
    Path(__file__).resolve().parents[2]
    / "templates"
    / "admin"
    / "dashboard"
    / "_stats.html"
)


def _registered_paths() -> set[str]:
    from app.main import _DEFERRED_API_ROUTER_SPECS, _load_router_object, app

    paths: set[str] = set()

    def _collect(routes, prefix: str = "") -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                paths.add(prefix + route.path)
            elif hasattr(route, "routes"):
                _collect(route.routes, prefix + getattr(route, "path", ""))

    _collect(app.routes)
    # Inspect deferred routers directly. Running the production loader here
    # mutates the global app and imports modules through asyncio worker threads;
    # under the parallel architecture suite that can wait indefinitely on
    # Python's import lock. Dependencies do not alter route paths, so applying
    # only the mount prefix gives this guard the same path contract without
    # performing application startup work.
    for (
        module_name,
        attr_name,
        mount_kind,
        _dependency_mode,
    ) in _DEFERRED_API_ROUTER_SPECS:
        router = _load_router_object(module_name, attr_name)
        prefix = "/api/v1" if mount_kind == "api" else ""
        if mount_kind == "admin":
            prefix = "/admin"
        _collect(router.routes, prefix)
    return paths


def _kpi_hrefs() -> list[str]:
    source = _STATS.read_text(encoding="utf-8")
    # kpi_tile(label, value, href, ...) — href is the string literal after the
    # value expression; grab quoted absolute paths inside kpi_tile calls.
    return re.findall(r'kpi_tile\((?:[^()]|\([^()]*\))*?"(/[^"]+)"', source)


def _attention_hrefs() -> list[str]:
    items, _ = build_attention_items(
        net_stats={
            "alarms_critical": 1,
            "alarms_major": 1,
            "alarms_minor": 0,
            "alarms_warning": 0,
            "offline_count": 1,
        },
        overdue_amount=1.0,
        suspended_count=1,
        pending_orders=1,
        ont_summary={"low_signal": 1, "offline": 99},
        unconfigured_ont_count=1,
        pending_location_requests=1,
        pon_outage_count=1,
        infrastructure_alerts={"total": 1, "critical": 1},
    )
    return [item["href"] for item in items]


def test_dashboard_drilldown_links_resolve():
    paths = _registered_paths()
    hrefs = _kpi_hrefs() + _attention_hrefs()
    assert hrefs, "expected drill-down hrefs from the KPI strip and attention feed"
    dead = sorted({href.split("?", 1)[0] for href in hrefs} - {p for p in paths})
    assert not dead, (
        "dashboard drill-down links point at unregistered routes "
        f"(dead cohort links): {dead}"
    )
