"""Customer entry point and operational network-map UI contracts."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_customer_list_links_to_the_network_operations_map():
    source = (PROJECT_ROOT / "templates/admin/customers/index.html").read_text(
        encoding="utf-8"
    )

    assert 'can(request, "network:map:read")' in source
    assert 'href="/admin/network/map?focus=customers"' in source
    assert "Customer Network Map" in source


def test_network_map_exposes_status_and_topology_drill_downs():
    source = (PROJECT_ROOT / "templates/admin/network/map.html").read_text(
        encoding="utf-8"
    )

    assert 'id="layer-customers-online"' in source
    assert 'id="layer-customers-offline"' in source
    assert "initialFocus === 'customers'" in source
    assert "map.fitBounds(customerBounds" in source
    assert "p.is_online ? 'Online' : 'Offline'" in source
    assert "View customer and network path" in source
    assert "infrastructure_type=location" in source
    assert "infrastructure_type=cabinet" in source
    assert "Connected customers" in source
