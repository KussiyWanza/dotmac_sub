from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from starlette.routing import Match

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.models.network_monitoring import (
    DeviceInterface,
    DeviceMetric,
    DeviceRole,
    DeviceStatus,
    DeviceType,
    MetricType,
    NetworkDevice,
    NetworkWeathermapView,
    PopSite,
    TopologyLinkMedium,
    TopologyLinkRole,
)
from app.models.subscriber import Address
from app.services import network_topology as topology_service
from app.services import web_network_topology as web_topology_service
from app.services.topology.customer_path import GAP_NO_NODE
from app.services.topology.gaps import topology_gaps
from app.timezone import DisplayObject
from app.web.admin import network_weathermap as topology_routes
from app.web.admin import router as admin_router


def _matched_route(router, path: str, method: str = "GET"):
    scope = {
        "type": "http",
        "path": path,
        "method": method,
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    for route in router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route
    return None


def test_topology_link_form_renders_display_object_edit_link() -> None:
    link = DisplayObject(
        SimpleNamespace(
            id="link-1",
            source_device_id="source-device",
            source_interface_id="source-iface",
            target_device_id="target-device",
            target_interface_id="target-iface",
            link_role="uplink",
            medium="fiber",
            capacity_bps=1_000_000_000,
            admin_status="enabled",
            bundle_key="",
            topology_group="",
            notes="",
        )
    )

    html = topology_routes.templates.env.get_template(
        "admin/network/topology/link_form.html"
    ).render(
        {
            "request": SimpleNamespace(
                state=SimpleNamespace(csrf_token="csrf-token"), query_params={}
            ),
            "current_user": {"name": "Admin", "email": "admin@example.test"},
            "sidebar_stats": {},
            "active_page": "topology",
            "active_menu": "network",
            "link": link,
            "action_url": "/admin/network/topology/links/link-1/edit",
            "error": None,
            "devices": [
                {"id": "source-device", "name": "Source Device"},
                {"id": "target-device", "name": "Target Device"},
            ],
            "link_roles": ["unknown", "uplink"],
            "mediums": ["unknown", "fiber"],
            "admin_statuses": ["enabled", "disabled"],
        }
    )

    assert 'value="source-device" selected' in html
    assert 'value="target-device" selected' in html
    assert 'id="sourceInterfaceSelected" value="source-iface"' in html
    assert 'id="targetInterfaceSelected" value="target-iface"' in html


def test_topology_link_update_can_change_endpoints(db_session):
    source_a = NetworkDevice(
        name="Source A",
        role=DeviceRole.core,
        status=DeviceStatus.online,
        is_active=True,
    )
    target_a = NetworkDevice(
        name="Target A",
        role=DeviceRole.edge,
        status=DeviceStatus.online,
        is_active=True,
    )
    source_b = NetworkDevice(
        name="Source B",
        role=DeviceRole.core,
        status=DeviceStatus.online,
        is_active=True,
    )
    target_b = NetworkDevice(
        name="Target B",
        role=DeviceRole.edge,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add_all([source_a, target_a, source_b, target_b])
    db_session.flush()

    iface_a1 = DeviceInterface(device_id=source_a.id, name="xe-0/0/0")
    iface_a2 = DeviceInterface(device_id=target_a.id, name="xe-0/0/1")
    iface_b1 = DeviceInterface(device_id=source_b.id, name="xe-0/0/2")
    iface_b2 = DeviceInterface(device_id=target_b.id, name="xe-0/0/3")
    db_session.add_all([iface_a1, iface_a2, iface_b1, iface_b2])
    db_session.flush()

    link = topology_service.topology_links.create(
        db_session,
        data={
            "source_device_id": str(source_a.id),
            "source_interface_id": str(iface_a1.id),
            "target_device_id": str(target_a.id),
            "target_interface_id": str(iface_a2.id),
            "link_role": "uplink",
            "medium": "fiber",
            "capacity_bps": "1000000000",
        },
    )

    updated = topology_service.topology_links.update(
        db_session,
        str(link.id),
        data={
            "source_device_id": str(source_b.id),
            "source_interface_id": str(iface_b1.id),
            "target_device_id": str(target_b.id),
            "target_interface_id": str(iface_b2.id),
            "link_role": "backhaul",
            "medium": "wireless",
            "capacity_bps": "500000000",
        },
    )

    assert updated.source_device_id == source_b.id
    assert updated.source_interface_id == iface_b1.id
    assert updated.target_device_id == target_b.id
    assert updated.target_interface_id == iface_b2.id
    assert updated.link_role == TopologyLinkRole.backhaul
    assert updated.medium == TopologyLinkMedium.wireless


def test_topology_link_update_allows_same_interfaces_on_same_link(db_session):
    source = NetworkDevice(
        name="Source", role=DeviceRole.core, status=DeviceStatus.online, is_active=True
    )
    target = NetworkDevice(
        name="Target", role=DeviceRole.edge, status=DeviceStatus.online, is_active=True
    )
    db_session.add_all([source, target])
    db_session.flush()

    src_iface = DeviceInterface(device_id=source.id, name="xe-0/0/0")
    tgt_iface = DeviceInterface(device_id=target.id, name="xe-0/0/1")
    db_session.add_all([src_iface, tgt_iface])
    db_session.flush()

    link = topology_service.topology_links.create(
        db_session,
        data={
            "source_device_id": str(source.id),
            "source_interface_id": str(src_iface.id),
            "target_device_id": str(target.id),
            "target_interface_id": str(tgt_iface.id),
            "link_role": "uplink",
            "medium": "fiber",
        },
    )

    updated = topology_service.topology_links.update(
        db_session,
        str(link.id),
        data={
            "source_device_id": str(source.id),
            "source_interface_id": str(src_iface.id),
            "target_device_id": str(target.id),
            "target_interface_id": str(tgt_iface.id),
            "link_role": "uplink",
            "medium": "fiber",
            "bundle_key": "lag-1",
        },
    )

    assert updated.bundle_key == "lag-1"


def test_topology_graph_falls_back_to_active_inventory_when_no_links(db_session):
    active_a = NetworkDevice(
        name="Core A",
        role=DeviceRole.core,
        device_type=DeviceType.router,
        status=DeviceStatus.online,
        is_active=True,
    )
    active_b = NetworkDevice(
        name="Edge B",
        role=DeviceRole.edge,
        device_type=DeviceType.switch,
        status=DeviceStatus.online,
        is_active=True,
    )
    excluded_cpe = NetworkDevice(
        name="Customer CPE",
        role=DeviceRole.cpe,
        device_type=DeviceType.modem,
        status=DeviceStatus.online,
        is_active=True,
    )
    excluded_server = NetworkDevice(
        name="Metrics Server",
        role=DeviceRole.edge,
        device_type=DeviceType.server,
        status=DeviceStatus.online,
        is_active=True,
    )
    inactive = NetworkDevice(
        name="Old Device",
        role=DeviceRole.edge,
        device_type=DeviceType.router,
        status=DeviceStatus.offline,
        is_active=False,
    )
    db_session.add_all([active_a, active_b, excluded_cpe, excluded_server, inactive])
    db_session.flush()

    graph = topology_service.list_nodes_and_edges(db_session)

    node_ids = {node["id"] for node in graph["nodes"]}
    assert str(active_a.id) in node_ids
    assert str(active_b.id) in node_ids
    assert str(excluded_cpe.id) not in node_ids
    assert str(excluded_server.id) not in node_ids
    assert str(inactive.id) not in node_ids
    assert graph["edges"] == []
    assert graph["stats"]["node_count"] == 2


def test_topology_graph_includes_saved_node_position(db_session):
    device = NetworkDevice(
        name="Positioned Core",
        role=DeviceRole.core,
        device_type=DeviceType.router,
        status=DeviceStatus.online,
        is_active=True,
        topology_x=123.45,
        topology_y=678.9,
    )
    db_session.add(device)
    db_session.commit()

    graph = topology_service.list_nodes_and_edges(db_session)

    node = next(item for item in graph["nodes"] if item["id"] == str(device.id))
    assert node["position"] == {"x": 123.45, "y": 678.9}


def test_save_node_positions_updates_devices(db_session):
    device = NetworkDevice(
        name="Dragged Node",
        role=DeviceRole.edge,
        device_type=DeviceType.switch,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(device)
    db_session.commit()

    result = topology_service.save_node_positions(
        db_session,
        [{"id": str(device.id), "x": 42.126, "y": -18.555}],
    )

    db_session.refresh(device)
    assert result == {"saved": 1}
    assert device.topology_x == 42.13
    assert device.topology_y == -18.55


def test_topology_template_exposes_layout_save_controls() -> None:
    template = Path("templates/admin/network/topology/index.html").read_text()

    assert 'id="topologySaveLayout"' in template
    assert "/admin/network/topology/api/node-positions" in template
    assert "'X-CSRF-Token': csrfToken()" in template


def test_topology_link_table_paginates_without_truncating_graph() -> None:
    graph_edges = [
        {
            "id": f"link-{index:02d}",
            "source_device": f"Source {index:02d}",
            "source_interface": "ether1",
            "target_device": f"Target {index:02d}",
            "target_interface": "ether2",
            "status": "up",
            "role": "access",
            "medium": "fiber",
            "capacity_bps": 1_000_000_000,
            "utilization_pct": float(index),
            "util_state": "normal",
        }
        for index in range(26)
    ]
    graph: dict[str, object] = {"nodes": [], "edges": graph_edges}
    list_query = web_topology_service.build_topology_link_list_query(
        group="metro",
        site="pop-1",
        page=2,
        per_page=10,
    )

    table_page = web_topology_service.build_topology_link_table_page(graph, list_query)

    assert len(graph_edges) == 26
    assert len(table_page.rows) == 10
    assert table_page.rows[0].id == "link-10"
    assert table_page.page_meta.start_item == 11
    assert table_page.page_meta.end_item == 20
    assert table_page.page_meta.total_items == 26
    assert table_page.page_meta.total_pages == 3
    assert (
        table_page.list_query.url("/admin/network/topology", page=3)
        == "/admin/network/topology?group=metro&site=pop-1&sort=source_device"
        "&dir=asc&page=3&per_page=10"
    )


def test_topology_link_table_clamps_an_out_of_range_page() -> None:
    graph: dict[str, object] = {
        "edges": [
            {
                "id": "link-1",
                "source_device": "Source",
                "target_device": "Target",
            }
        ]
    }
    list_query = web_topology_service.build_topology_link_list_query(
        page=99, per_page=10
    )

    table_page = web_topology_service.build_topology_link_table_page(graph, list_query)

    assert table_page.page_meta.page == 1
    assert table_page.list_query.page == 1
    assert [row.id for row in table_page.rows] == ["link-1"]


def test_topology_template_uses_paginated_rows_and_keeps_full_graph_for_canvas() -> (
    None
):
    template = Path("templates/admin/network/topology/index.html").read_text()

    assert "{% for edge in topology_links %}" in template
    assert 'aria-label="Topology links pagination"' in template
    assert 'aria-label="Topology links per page"' in template
    assert "Showing {{ page_meta.start_item }} to {{ page_meta.end_item }}" in template
    assert "var graphData = {{ graph | tojson }};" in template


def test_topology_gaps_respects_subscription_service_address(
    db_session,
    subscriber,
    catalog_offer,
):
    address_a = Address(
        subscriber_id=subscriber.id,
        label="Site A",
        address_line1="1 Fiber Way",
    )
    address_b = Address(
        subscriber_id=subscriber.id,
        label="Site B",
        address_line1="2 Fiber Way",
    )
    db_session.add_all([address_a, address_b])
    db_session.flush()

    sub_a = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        service_address_id=address_a.id,
        status=SubscriptionStatus.active,
    )
    sub_b = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        service_address_id=address_b.id,
        status=SubscriptionStatus.active,
    )
    db_session.add_all([sub_a, sub_b])

    pop = PopSite(name="POP A", is_active=True)
    complete_olt = OLTDevice(name="OLT Complete", hostname="complete-olt.local")
    broken_olt = OLTDevice(name="OLT Broken", hostname="broken-olt.local")
    db_session.add_all([pop, complete_olt, broken_olt])
    db_session.flush()

    complete_ont = OntUnit(
        serial_number="TOPO-COMPLETE-001",
        olt_device_id=complete_olt.id,
    )
    broken_ont = OntUnit(
        serial_number="TOPO-BROKEN-001",
        olt_device_id=broken_olt.id,
    )
    db_session.add_all([complete_ont, broken_ont])
    db_session.flush()
    db_session.add_all(
        [
            OntAssignment(
                subscriber_id=subscriber.id,
                service_address_id=address_a.id,
                ont_unit_id=complete_ont.id,
                active=True,
            ),
            OntAssignment(
                subscriber_id=subscriber.id,
                service_address_id=address_b.id,
                ont_unit_id=broken_ont.id,
                active=True,
            ),
            NetworkDevice(
                name="Complete OLT node",
                matched_device_type="olt",
                matched_device_id=complete_olt.id,
                pop_site_id=pop.id,
                role=DeviceRole.edge,
                status=DeviceStatus.online,
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    gaps = topology_gaps(db_session)
    gaps_by_subscription = {row["id"]: row["gap"] for row in gaps.subscription_gaps}

    assert sub_a.id not in gaps_by_subscription
    assert gaps_by_subscription[sub_b.id] == GAP_NO_NODE


def test_node_summary_includes_device_interfaces(db_session):
    device = NetworkDevice(
        name="Core A",
        role=DeviceRole.core,
        device_type=DeviceType.router,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()

    iface_a = DeviceInterface(
        device_id=device.id,
        name="xe-0/0/0",
        status="up",
        speed_mbps=1000,
        monitored=True,
    )
    iface_b = DeviceInterface(
        device_id=device.id,
        name="xe-0/0/1",
        status="down",
        speed_mbps=10000,
        monitored=False,
    )
    db_session.add_all([iface_a, iface_b])
    db_session.flush()

    summary = topology_service.node_summary(db_session, str(device.id))

    assert summary["interface_count"] == 2
    assert {iface["name"] for iface in summary["interfaces"]} == {
        "xe-0/0/0",
        "xe-0/0/1",
    }


def test_topology_graph_aggregates_and_filters_by_pop_site(db_session):
    site_a = PopSite(name="POP A", city="Lagos", region="Ikeja", is_active=True)
    site_b = PopSite(name="POP B", city="Abuja", region="Mabushi", is_active=True)
    db_session.add_all([site_a, site_b])
    db_session.flush()

    dev_a = NetworkDevice(
        name="Core A",
        pop_site_id=site_a.id,
        role=DeviceRole.core,
        device_type=DeviceType.router,
        status=DeviceStatus.online,
        is_active=True,
    )
    dev_b = NetworkDevice(
        name="Edge B",
        pop_site_id=site_b.id,
        role=DeviceRole.edge,
        device_type=DeviceType.switch,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add_all([dev_a, dev_b])
    db_session.flush()

    full_graph = topology_service.list_nodes_and_edges(db_session)
    filtered_graph = topology_service.list_nodes_and_edges(
        db_session, pop_site_id=str(site_a.id)
    )

    assert full_graph["stats"]["site_count"] == 2
    assert {item["pop_site_name"] for item in full_graph["site_summaries"]} == {
        "POP A",
        "POP B",
    }
    assert {node["pop_site_name"] for node in filtered_graph["nodes"]} == {"POP A"}


def test_topology_graph_pop_filter_keeps_boundary_uplink(db_session):
    site_a = PopSite(name="POP A", is_active=True)
    site_b = PopSite(name="POP B", is_active=True)
    db_session.add_all([site_a, site_b])
    db_session.flush()

    local = NetworkDevice(
        name="Access A",
        pop_site_id=site_a.id,
        role=DeviceRole.access,
        device_type=DeviceType.switch,
        status=DeviceStatus.online,
        is_active=True,
    )
    upstream = NetworkDevice(
        name="Core B",
        pop_site_id=site_b.id,
        role=DeviceRole.core,
        device_type=DeviceType.router,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add_all([local, upstream])
    db_session.flush()
    topology_service.topology_links.create(
        db_session,
        data={
            "source_device_id": str(local.id),
            "target_device_id": str(upstream.id),
            "link_role": "uplink",
            "medium": "fiber",
        },
    )

    graph = topology_service.list_nodes_and_edges(
        db_session, pop_site_id=str(site_a.id)
    )

    assert {node["id"] for node in graph["nodes"]} == {str(local.id), str(upstream.id)}
    assert graph["stats"]["edge_count"] == 1
    assert graph["stats"]["boundary_node_count"] == 1
    assert {node["name"] for node in graph["nodes"] if node["is_external"]} == {
        "Core B"
    }


def test_get_device_interfaces_returns_full_inventory(db_session):
    device = NetworkDevice(
        name="Dense Switch",
        role=DeviceRole.access,
        device_type=DeviceType.switch,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()

    db_session.add_all(
        [
            DeviceInterface(
                device_id=device.id, name=f"ge-0/0/{index:03d}", monitored=True
            )
            for index in range(205)
        ]
    )
    db_session.flush()

    interfaces = topology_service.get_device_interfaces(db_session, str(device.id))

    assert len(interfaces) == 205


def test_topology_form_options_include_full_device_inventory(db_session):
    db_session.add_all(
        [
            NetworkDevice(
                name=f"Device {index:03d}",
                role=DeviceRole.edge,
                device_type=DeviceType.router,
                is_active=True,
            )
            for index in range(205)
        ]
    )
    db_session.flush()

    options = topology_service.get_form_options(db_session)

    assert len(options["devices"]) >= 205


def test_weathermap_route_exists():
    route = _matched_route(topology_routes.router, "/network/weathermap")
    assert route is not None
    assert route.endpoint is topology_routes.network_weathermap


def test_admin_router_mounts_topology_and_weathermap_routes():
    assert _matched_route(admin_router, "/admin/network/topology") is not None
    assert _matched_route(admin_router, "/admin/network/weathermap") is not None


def test_topology_routes_include_ajax_endpoints():
    assert (
        _matched_route(topology_routes.router, "/network/topology/api/interfaces/123")
        is not None
    )
    assert (
        _matched_route(topology_routes.router, "/network/topology/api/node/123")
        is not None
    )
    assert (
        _matched_route(topology_routes.router, "/network/topology/api/graph")
        is not None
    )
    assert (
        _matched_route(topology_routes.router, "/network/weathermap/api/graph")
        is not None
    )
    assert (
        _matched_route(topology_routes.router, "/network/weathermap/api/layout", "POST")
        is not None
    )
    assert (
        _matched_route(
            topology_routes.router, "/network/weathermap/api/layout/reset", "POST"
        )
        is not None
    )


def test_topology_link_form_restores_selected_interfaces():
    template = Path("templates/admin/network/topology/link_form.html").read_text()
    assert 'id="sourceInterfaceSelected"' in template
    assert 'id="targetInterfaceSelected"' in template
    assert "if (deviceSelect.value)" in template
    assert 'formaction="/admin/network/topology/links/{{ link.id }}/delete"' in template


def test_legacy_weathermap_assets_removed():
    assert not Path("app/services/web_network_weathermap.py").exists()
    assert not Path("templates/admin/network/weathermap/index.html").exists()


def test_admin_labels_switched_to_topology():
    layout = Path("templates/layouts/admin.html").read_text()
    network_hub = Path("templates/admin/network/index.html").read_text()
    design_system = Path("templates/admin/design_system/modules.html").read_text()
    assert "'weathermap': 'Network Weather Map'" in layout
    assert (
        '"label": "Topology Editor", "href": "/admin/network/topology"' in network_hub
    )
    assert '"label": "Weather Map", "href": "/admin/network/weathermap"' in network_hub
    assert (
        '"label": "Network Topology", "url": "/admin/network/topology"' in design_system
    )
    assert (
        '"label": "Network Weather Map", "url": "/admin/network/weathermap"'
        in design_system
    )


def test_weathermap_canvas_uses_explicit_height():
    template = Path("templates/admin/network/weathermap.html").read_text()

    assert 'id="weatherCanvas" style="height:' in template


def test_weathermap_template_exposes_noc_layout_controls():
    template = Path("templates/admin/network/weathermap.html").read_text()

    assert 'id="viewFilter"' in template
    assert 'id="openNocWindow"' in template
    assert 'id="weatherAutoRefresh"' in template
    assert 'id="saveWeatherLayout"' in template
    assert 'id="resetWeatherLayout"' in template
    assert "/admin/network/weathermap/api/layout" in template


def test_weathermap_context_creates_default_view_and_applies_layout(db_session):
    device = NetworkDevice(
        name="NOC Router",
        role=DeviceRole.core,
        device_type=DeviceType.router,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()

    saved = web_topology_service.save_weathermap_layout(
        db_session,
        view_slug="default",
        payload={"nodes": {str(device.id): {"x": 120.1234, "y": 240.9876}}},
    )
    context = web_topology_service.weathermap_page_context(db_session)
    node = next(n for n in context["graph"]["nodes"] if n["id"] == str(device.id))

    assert saved["slug"] == "default"
    assert context["weathermap_view"]["name"] == "Default NOC Map"
    assert context["selected_view"] == "default"
    assert node["weathermap_positioned"] is True
    assert node["weathermap_position"] == {"x": 120.12, "y": 240.99}


def test_weathermap_layout_reset_clears_saved_positions(db_session):
    device = NetworkDevice(
        name="Distribution Switch",
        role=DeviceRole.distribution,
        device_type=DeviceType.switch,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()

    web_topology_service.save_weathermap_layout(
        db_session,
        view_slug="default",
        payload={"nodes": {str(device.id): {"x": 80, "y": 90}}},
    )
    reset = web_topology_service.reset_weathermap_layout(
        db_session, view_slug="default"
    )
    context = web_topology_service.weathermap_page_context(db_session)
    node = next(n for n in context["graph"]["nodes"] if n["id"] == str(device.id))

    assert reset["layout"] == {"nodes": {}, "viewport": {}}
    assert "weathermap_positioned" not in node


def test_weathermap_context_can_use_named_view_filters(db_session):
    site = PopSite(name="NOC POP")
    db_session.add(site)
    db_session.flush()
    db_session.add(
        NetworkWeathermapView(
            slug="core-noc",
            name="Core NOC",
            topology_group="core",
            pop_site_id=site.id,
            settings={"refresh_seconds": 30},
            layout={"nodes": {}, "viewport": {}},
        )
    )
    db_session.commit()

    context = web_topology_service.weathermap_page_context(
        db_session, view_slug="core-noc"
    )

    assert context["selected_view"] == "core-noc"
    assert context["selected_group"] == "core"
    assert context["selected_site"] == str(site.id)
    assert context["weathermap_view"]["settings"]["refresh_seconds"] == 30


def test_topology_graph_edges_carry_status_and_nodes_carry_live_status(db_session):
    src = NetworkDevice(
        name="Core X",
        role=DeviceRole.core,
        status=DeviceStatus.online,
        live_status="down",
        is_active=True,
    )
    dst = NetworkDevice(
        name="Edge X", role=DeviceRole.edge, status=DeviceStatus.online, is_active=True
    )
    db_session.add_all([src, dst])
    db_session.flush()
    topology_service.topology_links.create(
        db_session,
        data={
            "source_device_id": str(src.id),
            "target_device_id": str(dst.id),
            "link_role": "uplink",
            "medium": "fiber",
        },
    )

    graph = topology_service.list_nodes_and_edges(db_session)

    assert graph["edges"], "expected the created link to appear as an edge"
    assert graph["edges"][0]["status"] == "up"  # enabled, active, no stale signal
    nodes = {node["name"]: node for node in graph["nodes"]}
    assert nodes["Core X"]["status"] == "down"
    assert nodes["Core X"]["inventory_status"] == "online"
    assert nodes["Core X"]["live_status"] == "down"
    assert {node["role"] for node in graph["nodes"]} >= {"core", "edge"}


def test_link_utilization_uses_dominant_direction_not_rx_plus_tx(db_session):
    src = NetworkDevice(name="Source", role=DeviceRole.core, is_active=True)
    dst = NetworkDevice(name="Target", role=DeviceRole.edge, is_active=True)
    db_session.add_all([src, dst])
    db_session.flush()
    src_iface = DeviceInterface(device_id=src.id, name="xe-0/0/0")
    dst_iface = DeviceInterface(device_id=dst.id, name="xe-0/0/1")
    db_session.add_all([src_iface, dst_iface])
    db_session.flush()
    link = topology_service.topology_links.create(
        db_session,
        data={
            "source_device_id": str(src.id),
            "source_interface_id": str(src_iface.id),
            "target_device_id": str(dst.id),
            "target_interface_id": str(dst_iface.id),
            "link_role": "uplink",
            "medium": "fiber",
            "capacity_bps": "1000000000",
        },
    )
    db_session.add_all(
        [
            DeviceMetric(
                device_id=src.id,
                interface_id=src_iface.id,
                metric_type=MetricType.rx_bps,
                value=600_000_000,
                recorded_at=datetime(2026, 7, 7, tzinfo=UTC),
            ),
            DeviceMetric(
                device_id=src.id,
                interface_id=src_iface.id,
                metric_type=MetricType.tx_bps,
                value=600_000_000,
                recorded_at=datetime(2026, 7, 7, tzinfo=UTC),
            ),
        ]
    )
    db_session.flush()

    util = topology_service.compute_link_utilization(db_session, link)

    assert util["utilization_pct"] == 60.0
    assert util["utilization_basis"] == "max_direction"
    assert util["dominant_direction"] == "rx"


def test_derive_link_status_covers_admin_and_staleness():
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from app.models.network_monitoring import TopologyLinkAdminStatus
    from app.services.network_topology import _LINK_STALE_SECONDS, _derive_link_status

    now = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)

    def link(admin, *, active=True, seen=now):
        return SimpleNamespace(admin_status=admin, is_active=active, last_seen_at=seen)

    assert _derive_link_status(link(TopologyLinkAdminStatus.enabled), now) == "up"
    assert _derive_link_status(link(TopologyLinkAdminStatus.disabled), now) == "down"
    assert (
        _derive_link_status(link(TopologyLinkAdminStatus.enabled, active=False), now)
        == "down"
    )
    assert (
        _derive_link_status(link(TopologyLinkAdminStatus.maintenance), now)
        == "degraded"
    )
    stale = now - timedelta(seconds=_LINK_STALE_SECONDS + 60)
    assert (
        _derive_link_status(link(TopologyLinkAdminStatus.enabled, seen=stale), now)
        == "degraded"
    )
    # manual links (no last_seen_at) are never stale-degraded
    assert (
        _derive_link_status(link(TopologyLinkAdminStatus.enabled, seen=None), now)
        == "up"
    )
