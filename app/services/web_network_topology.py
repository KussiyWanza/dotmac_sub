"""Web helpers for admin network topology routes."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network_monitoring import NetworkWeathermapView
from app.services import network_topology as topology_service
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
)

DEFAULT_WEATHERMAP_SLUG = "default"
DEFAULT_WEATHERMAP_SETTINGS = {
    "refresh_seconds": 60,
    "warning_threshold_pct": 70,
    "critical_threshold_pct": 90,
    "show_link_labels": True,
}

TOPOLOGY_LINK_LIST_DEFINITION = ListDefinition(
    key="topology_links",
    fields=(
        ListFieldDefinition("group", "Group", filterable=True),
        ListFieldDefinition("site", "Site", filterable=True),
        ListFieldDefinition("source_device", "Source device", sortable=True),
    ),
    default_sort="source_device",
    default_sort_dir="asc",
    default_per_page=25,
)


@dataclass(frozen=True, slots=True)
class TopologyLinkRow:
    """Typed table projection for one observed or manually recorded link."""

    id: str
    source_device: str
    source_interface: str
    target_device: str
    target_interface: str
    status: str
    role: str
    medium: str
    capacity_bps: int | None
    utilization_pct: float | None
    util_state: str

    @classmethod
    def from_graph_edge(cls, edge: Mapping[str, object]) -> TopologyLinkRow:
        capacity = edge.get("capacity_bps")
        utilization = edge.get("utilization_pct")
        return cls(
            id=str(edge.get("id") or ""),
            source_device=str(edge.get("source_device") or ""),
            source_interface=str(edge.get("source_interface") or ""),
            target_device=str(edge.get("target_device") or ""),
            target_interface=str(edge.get("target_interface") or ""),
            status=str(edge.get("status") or "unknown"),
            role=str(edge.get("role") or "unknown"),
            medium=str(edge.get("medium") or "unknown"),
            capacity_bps=int(cast(Any, capacity)) if capacity is not None else None,
            utilization_pct=float(cast(Any, utilization))
            if utilization is not None
            else None,
            util_state=str(edge.get("util_state") or "unknown"),
        )


@dataclass(frozen=True, slots=True)
class TopologyLinkTablePage:
    """Typed, paginated table result; the topology graph remains complete."""

    rows: tuple[TopologyLinkRow, ...]
    list_query: ListQuery
    page_meta: PageMeta


def build_topology_link_list_query(
    *,
    group: str | None = None,
    site: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = 1,
    per_page: int | None = None,
) -> ListQuery:
    """Normalize topology-link table controls through the list contract."""
    return TOPOLOGY_LINK_LIST_DEFINITION.build_query(
        search=None,
        filters={"group": group, "site": site},
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    )


def build_topology_link_table_page(
    graph: Mapping[str, object], list_query: ListQuery
) -> TopologyLinkTablePage:
    """Page the table projection without truncating the topology canvas graph."""
    graph_edges = cast(list[Mapping[str, object]], graph.get("edges") or [])
    rows = [TopologyLinkRow.from_graph_edge(edge) for edge in graph_edges]
    rows.sort(
        key=lambda row: (
            row.source_device.casefold(),
            row.target_device.casefold(),
            row.id,
        ),
        reverse=list_query.sort_dir == "desc",
    )

    page_meta = PageMeta.from_query(list_query, total_items=len(rows))
    canonical_query = list_query.with_page(page_meta.page)
    start = (page_meta.page - 1) * page_meta.per_page
    return TopologyLinkTablePage(
        rows=tuple(rows[start : start + page_meta.per_page]),
        list_query=canonical_query,
        page_meta=page_meta,
    )


def topology_page_context(
    db: Session,
    *,
    group: str | None = None,
    site: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = 1,
    per_page: int | None = None,
) -> dict[str, object]:
    graph = topology_service.list_nodes_and_edges(
        db,
        topology_group=group,
        pop_site_id=site,
        include_utilization=True,
    )
    link_table_page = build_topology_link_table_page(
        graph,
        build_topology_link_list_query(
            group=group,
            site=site,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            per_page=per_page,
        ),
    )
    return {
        "graph": graph,
        "form_options": topology_service.get_form_options(db),
        "selected_group": group or "",
        "selected_site": site or "",
        "topology_links": link_table_page.rows,
        "list_query": link_table_page.list_query,
        "page_meta": link_table_page.page_meta,
    }


def _weather_summary(graph: dict[str, object]) -> dict[str, object]:
    nodes = cast(list[dict[str, Any]], graph.get("nodes") or [])
    edges = cast(list[dict[str, Any]], graph.get("edges") or [])
    node_counts = {"up": 0, "down": 0, "problem": 0, "unknown": 0}
    edge_counts = {"up": 0, "degraded": 0, "down": 0, "unknown": 0}
    for node in nodes:
        status = str(node.get("status") or "unknown").lower()
        if status in ("online", "up"):
            node_counts["up"] += 1
        elif status in ("offline", "down"):
            node_counts["down"] += 1
        elif status in ("degraded", "maintenance", "problem"):
            node_counts["problem"] += 1
        else:
            node_counts["unknown"] += 1
    for edge in edges:
        status = str(edge.get("status") or "unknown").lower()
        edge_counts[status if status in edge_counts else "unknown"] += 1
    hot_edges = sorted(
        [
            edge
            for edge in edges
            if edge.get("utilization_pct") is not None
            and float(edge["utilization_pct"]) >= 70
        ],
        key=lambda edge: float(edge.get("utilization_pct") or 0),
        reverse=True,
    )[:10]
    dark_nodes = [
        node
        for node in nodes
        if str(node.get("status") or "").lower() in ("down", "offline")
    ][:10]
    return {
        "node_counts": node_counts,
        "edge_counts": edge_counts,
        "hot_edges": hot_edges,
        "dark_nodes": dark_nodes,
    }


def _view_payload(view: NetworkWeathermapView) -> dict[str, object]:
    settings = dict(DEFAULT_WEATHERMAP_SETTINGS)
    settings.update(view.settings or {})
    layout = view.layout or {}
    return {
        "id": str(view.id),
        "slug": view.slug,
        "name": view.name,
        "description": view.description or "",
        "topology_group": view.topology_group or "",
        "pop_site_id": str(view.pop_site_id) if view.pop_site_id else "",
        "is_default": bool(view.is_default),
        "settings": settings,
        "layout": layout,
    }


def _ensure_default_weathermap_view(db: Session) -> NetworkWeathermapView:
    view = db.scalar(
        select(NetworkWeathermapView).where(
            NetworkWeathermapView.slug == DEFAULT_WEATHERMAP_SLUG
        )
    )
    if view:
        return view

    view = NetworkWeathermapView(
        slug=DEFAULT_WEATHERMAP_SLUG,
        name="Default NOC Map",
        description="Operational weather map layout for the NOC view.",
        settings=dict(DEFAULT_WEATHERMAP_SETTINGS),
        layout={"nodes": {}, "viewport": {}},
        is_default=True,
    )
    db.add(view)
    db.commit()
    db.refresh(view)
    return view


def list_weathermap_views(db: Session) -> list[dict[str, object]]:
    views = list(
        db.scalars(
            select(NetworkWeathermapView).order_by(
                NetworkWeathermapView.is_default.desc(),
                NetworkWeathermapView.name.asc(),
            )
        ).all()
    )
    if not views:
        views = [_ensure_default_weathermap_view(db)]
    return [_view_payload(view) for view in views]


def get_weathermap_view(db: Session, slug: str | None = None) -> NetworkWeathermapView:
    normalized = (slug or DEFAULT_WEATHERMAP_SLUG).strip() or DEFAULT_WEATHERMAP_SLUG
    view = db.scalar(
        select(NetworkWeathermapView).where(NetworkWeathermapView.slug == normalized)
    )
    if view:
        return view
    if normalized == DEFAULT_WEATHERMAP_SLUG:
        return _ensure_default_weathermap_view(db)
    raise ValueError("Weather map view not found")


def _finite_number(value: object) -> float | None:
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalized_layout(payload: dict[str, Any]) -> dict[str, object]:
    raw_nodes = payload.get("nodes") or {}
    if isinstance(raw_nodes, list):
        node_items = [
            (str(item.get("id") or ""), item)
            for item in raw_nodes
            if isinstance(item, dict)
        ]
    elif isinstance(raw_nodes, dict):
        node_items = [
            (str(node_id), item)
            for node_id, item in raw_nodes.items()
            if isinstance(item, dict)
        ]
    else:
        node_items = []

    nodes: dict[str, dict[str, float]] = {}
    for node_id, item in node_items:
        if not node_id:
            continue
        x = _finite_number(item.get("x"))
        y = _finite_number(item.get("y"))
        if x is None or y is None:
            continue
        nodes[node_id] = {"x": round(x, 2), "y": round(y, 2)}

    viewport_payload = payload.get("viewport") or {}
    viewport: dict[str, float] = {}
    if isinstance(viewport_payload, dict):
        zoom = _finite_number(viewport_payload.get("zoom"))
        pan_x = _finite_number(viewport_payload.get("pan_x"))
        pan_y = _finite_number(viewport_payload.get("pan_y"))
        if zoom is not None:
            viewport["zoom"] = round(max(0.05, min(4.0, zoom)), 3)
        if pan_x is not None:
            viewport["pan_x"] = round(pan_x, 2)
        if pan_y is not None:
            viewport["pan_y"] = round(pan_y, 2)

    return {"nodes": nodes, "viewport": viewport}


def _apply_weathermap_layout(
    graph: dict[str, object], view: NetworkWeathermapView
) -> dict[str, object]:
    view_data = _view_payload(view)
    layout = cast(dict[str, Any], view_data["layout"] or {})
    positions = layout.get("nodes") or {}
    if isinstance(positions, dict):
        for node in cast(list[dict[str, Any]], graph.get("nodes") or []):
            position = positions.get(str(node.get("id")))
            if isinstance(position, dict):
                x = _finite_number(position.get("x"))
                y = _finite_number(position.get("y"))
                if x is not None and y is not None:
                    node["weathermap_position"] = {"x": x, "y": y}
                    node["weathermap_positioned"] = True
    graph["weathermap"] = view_data
    return graph


def save_weathermap_layout(
    db: Session, *, view_slug: str | None, payload: dict[str, Any]
) -> dict[str, object]:
    view = get_weathermap_view(db, view_slug)
    view.layout = _normalized_layout(payload)
    db.commit()
    db.refresh(view)
    return _view_payload(view)


def reset_weathermap_layout(db: Session, *, view_slug: str | None) -> dict[str, object]:
    view = get_weathermap_view(db, view_slug)
    view.layout = {"nodes": {}, "viewport": {}}
    db.commit()
    db.refresh(view)
    return _view_payload(view)


def weathermap_page_context(
    db: Session,
    *,
    group: str | None = None,
    site: str | None = None,
    view_slug: str | None = None,
    noc: bool = False,
) -> dict[str, object]:
    view = get_weathermap_view(db, view_slug)
    effective_group = group if group is not None else view.topology_group
    effective_site = (
        site
        if site is not None
        else (str(view.pop_site_id) if view.pop_site_id else None)
    )
    graph = topology_service.list_nodes_and_edges(
        db,
        topology_group=effective_group,
        pop_site_id=effective_site,
        include_utilization=True,
    )
    graph = _apply_weathermap_layout(graph, view)
    return {
        "graph": graph,
        "weather": _weather_summary(graph),
        "weathermap_view": _view_payload(view),
        "weathermap_views": list_weathermap_views(db),
        "form_options": topology_service.get_form_options(db),
        "selected_group": effective_group or "",
        "selected_site": effective_site or "",
        "selected_view": view.slug,
        "noc_mode": noc,
    }


def weathermap_graph_data(
    db: Session,
    *,
    group: str | None = None,
    site: str | None = None,
    view_slug: str | None = None,
) -> dict[str, object]:
    context = weathermap_page_context(db, group=group, site=site, view_slug=view_slug)
    return {
        "graph": context["graph"],
        "weather": context["weather"],
        "weathermap_view": context["weathermap_view"],
    }


def link_form_context(
    db: Session,
    *,
    action_url: str,
    link: object | None = None,
    error: str | None = None,
) -> dict[str, object]:
    return {
        "link": link,
        "action_url": action_url,
        "error": error,
        **topology_service.get_form_options(db),
    }


def link_edit_context(db: Session, link_id: str) -> dict[str, object]:
    return link_form_context(
        db,
        link=topology_service.topology_links.get(db, link_id),
        action_url=f"/admin/network/topology/links/{link_id}/edit",
    )


def parse_link_form(form) -> dict[str, object]:
    return {
        "source_device_id": str(form.get("source_device_id") or "").strip(),
        "source_interface_id": str(form.get("source_interface_id") or "").strip()
        or None,
        "target_device_id": str(form.get("target_device_id") or "").strip(),
        "target_interface_id": str(form.get("target_interface_id") or "").strip()
        or None,
        "link_role": str(form.get("link_role") or "unknown").strip(),
        "medium": str(form.get("medium") or "unknown").strip(),
        "capacity_bps": str(form.get("capacity_bps") or "").strip() or None,
        "bundle_key": str(form.get("bundle_key") or "").strip() or None,
        "topology_group": str(form.get("topology_group") or "").strip() or None,
        "admin_status": str(form.get("admin_status") or "enabled").strip(),
        "notes": str(form.get("notes") or "").strip() or None,
    }


def create_link(db: Session, data: dict[str, object]) -> None:
    topology_service.topology_links.create(db, data=data)


def update_link(db: Session, link_id: str, data: dict[str, object]) -> None:
    topology_service.topology_links.update(db, link_id, data=data)


def delete_link(db: Session, link_id: str) -> None:
    topology_service.topology_links.delete(db, link_id)


def get_device_interfaces(db: Session, device_id: str) -> list[dict[str, object]]:
    return topology_service.get_device_interfaces(db, device_id)


def node_summary(db: Session, device_id: str) -> dict[str, object]:
    return topology_service.node_summary(db, device_id)


def graph_data(
    db: Session,
    *,
    group: str | None = None,
    site: str | None = None,
) -> dict[str, object]:
    return topology_service.list_nodes_and_edges(
        db, topology_group=group, pop_site_id=site
    )


def save_node_positions(db: Session, positions: list[dict]) -> dict:
    return topology_service.save_node_positions(db, positions)
