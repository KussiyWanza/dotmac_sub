"""Customer entry point and typed operational network-map contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.services.network.radius_sessions import (
    SubscriptionSessionBinding,
    SubscriptionSessionSnapshot,
    SubscriptionSessionState,
)
from app.services.network_map import (
    build_network_map_projection,
    resolve_customer_connectivity,
)
from app.services.network_map_contracts import (
    NetworkMapCustomerLayer,
    NetworkMapLink,
    NetworkMapPermission,
    NetworkMapProjection,
    NetworkMapStatusOwner,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _snapshot(
    state: SubscriptionSessionState,
    *,
    observed_at: datetime | None,
) -> SubscriptionSessionSnapshot:
    return SubscriptionSessionSnapshot(
        subscription_id=uuid4(),
        state=state,
        binding=SubscriptionSessionBinding.exact_subscription,
        observed_at=observed_at,
        framed_ip_address=None,
        nas_device_id=None,
        acct_session_id=None,
    )


def test_network_map_returns_typed_projection(db_session):
    projection = build_network_map_projection(db=db_session)

    assert isinstance(projection, NetworkMapProjection)
    transport = projection.to_template_context()
    assert transport["map_data"] == {"type": "FeatureCollection", "features": []}
    assert transport["stats"]["customers_connected"] == 0
    assert transport["stats"]["customers_not_connected"] == 0


def test_customer_connectivity_uses_authoritative_session_presentation():
    now = datetime.now(UTC)
    connectivity = resolve_customer_connectivity(
        (
            _snapshot(
                SubscriptionSessionState.stale,
                observed_at=now - timedelta(minutes=20),
            ),
            _snapshot(SubscriptionSessionState.connected, observed_at=now),
        )
    )

    assert connectivity.state is SubscriptionSessionState.connected
    assert connectivity.layer is NetworkMapCustomerLayer.connected
    assert connectivity.presentation.label == "Connected"
    assert connectivity.presentation.tone.value == "positive"
    assert connectivity.source_owner is NetworkMapStatusOwner.radius_sessions


def test_stale_and_offline_sessions_are_not_presented_as_connected():
    now = datetime.now(UTC)
    stale = resolve_customer_connectivity(
        (
            _snapshot(
                SubscriptionSessionState.stale,
                observed_at=now - timedelta(minutes=20),
            ),
        )
    )
    offline = resolve_customer_connectivity(
        (_snapshot(SubscriptionSessionState.offline, observed_at=None),)
    )

    assert stale.layer is NetworkMapCustomerLayer.not_connected
    assert stale.presentation.label == "Last seen"
    assert stale.presentation.tone.value == "warning"
    assert offline.layer is NetworkMapCustomerLayer.not_connected
    assert offline.presentation.label == "Not connected"
    assert offline.presentation.tone.value == "neutral"


def test_customer_navigation_contract_carries_its_permission():
    link = NetworkMapLink(
        href="/admin/customers/person/00000000-0000-0000-0000-000000000001",
        label="View customer and network path",
        permission=NetworkMapPermission.customer_read,
    )

    assert link.to_transport() == {
        "href": "/admin/customers/person/00000000-0000-0000-0000-000000000001",
        "label": "View customer and network path",
        "permission": "customer:read",
    }


def test_customer_network_map_links_and_semantics_are_permission_aware():
    customer_source = (PROJECT_ROOT / "templates/admin/customers/index.html").read_text(
        encoding="utf-8"
    )
    map_source = (PROJECT_ROOT / "templates/admin/network/map.html").read_text(
        encoding="utf-8"
    )

    assert 'can(request, "network:map:read")' in customer_source
    assert 'href="/admin/network/map?focus=customers"' in customer_source
    assert 'can(request, "customer:read")' in map_source
    assert "const canReadCustomers" in map_source
    assert "if (canReadCustomers && p.customer_detail_link)" in map_source
    assert "if (canReadCustomers && p.customer_cohort_link)" in map_source
    assert "p.connectivity.presentation" in map_source
    assert "p.connectivity.source_owner" in map_source
    assert "p.is_online" not in map_source
    assert "initialFocus === 'customers'" in map_source
    assert "map.fitBounds(customerBounds" in map_source
    service_source = (PROJECT_ROOT / "app/services/network_map.py").read_text(
        encoding="utf-8"
    )
    assert '"/admin/customers?infrastructure_type=location"' in service_source
    assert '"/admin/customers?infrastructure_type=cabinet"' in service_source
    assert 'label="Associated customers"' in service_source
