"""Tests for network catalog API endpoints (Phase 3)."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.models.gis import GeoArea
from app.schemas.network_catalog import (
    NetworkZoneCreate,
    NetworkZoneUpdate,
    OnuTypeCreate,
    OnuTypeRead,
    OnuTypeUpdate,
    SpeedProfileCreate,
    Tr069ParameterMapCreate,
    VendorCapabilityCreate,
    VendorCapabilityUpdate,
)


class TestOnuTypeSchemas:
    """ONU type schema tests."""

    def test_create_valid(self):
        req = OnuTypeCreate(
            name="EG8145V5",
            pon_type="gpon",
            gpon_channel="veip",
            ethernet_ports=4,
            wifi_ports=2,
            capability="bridge_route",
        )
        assert req.name == "EG8145V5"
        assert req.catv_ports == 0  # default

    def test_create_empty_name(self):
        with pytest.raises(Exception):
            OnuTypeCreate(
                name="",
                pon_type="gpon",
                gpon_channel="veip",
                capability="bridge",
            )

    def test_update_partial(self):
        req = OnuTypeUpdate(name="Updated Name")
        data = req.model_dump(exclude_unset=True)
        assert data == {"name": "Updated Name"}

    def test_read_from_dict(self):
        read = OnuTypeRead(
            id="00000000-0000-0000-0000-000000000001",
            name="Test",
            pon_type="gpon",
            gpon_channel="veip",
            ethernet_ports=4,
            wifi_ports=2,
            voip_ports=0,
            catv_ports=0,
            allow_custom_profiles=True,
            capability="bridge",
            is_active=True,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        assert read.is_active is True


class TestSpeedProfileSchemas:
    """Speed profile schema tests."""

    def test_create_valid(self):
        req = SpeedProfileCreate(
            name="100M Download", direction="download", speed_kbps=100_000
        )
        assert req.speed_type == "internet"

    def test_create_zero_speed(self):
        req = SpeedProfileCreate(name="Unlimited", direction="download", speed_kbps=0)
        assert req.speed_kbps == 0

    def test_create_negative_speed(self):
        with pytest.raises(Exception):
            SpeedProfileCreate(name="Bad", direction="download", speed_kbps=-1)


class TestNetworkZoneSchemas:
    """Network zone schema tests."""

    def test_create_minimal(self):
        req = NetworkZoneCreate(name="Zone A")
        assert req.is_active is True
        assert req.parent_id is None

    def test_create_with_coordinates(self):
        req = NetworkZoneCreate(name="Zone B", latitude=6.5244, longitude=3.3792)
        assert req.latitude == 6.5244

    def test_update_clear_parent(self):
        req = NetworkZoneUpdate(clear_parent=True)
        assert req.clear_parent is True


class TestNetworkZoneGeoAreaBinding:
    """NetworkZones owns the zone -> GeoArea binding and its resolution."""

    @staticmethod
    def _area(db_session, *, name: str, is_active: bool = True) -> GeoArea:
        area = GeoArea(name=name, is_active=is_active)
        db_session.add(area)
        db_session.flush()
        return area

    def test_create_persists_active_geo_area_binding(self, db_session):
        from app.services.network.zones import NetworkZones

        area = self._area(db_session, name="Abuja Coverage")
        zone = NetworkZones.create(
            db_session, name="Garki Zone", geo_area_id=str(area.id)
        )

        assert zone.geo_area_id == area.id

    def test_update_persists_and_clear_geo_area_clears(self, db_session):
        from app.services.network.zones import NetworkZones

        area = self._area(db_session, name="Lagos Coverage")
        zone = NetworkZones.create(db_session, name="Lekki Zone")

        updated = NetworkZones.update(
            db_session, str(zone.id), geo_area_id=str(area.id)
        )
        assert updated.geo_area_id == area.id

        cleared = NetworkZones.update(db_session, str(zone.id), clear_geo_area=True)
        assert cleared.geo_area_id is None

    def test_create_rejects_missing_geo_area(self, db_session):
        from app.services.network.zones import NetworkZones

        with pytest.raises(HTTPException) as excinfo:
            NetworkZones.create(
                db_session, name="Orphan Zone", geo_area_id=str(uuid.uuid4())
            )

        assert excinfo.value.status_code == 400

    def test_create_rejects_inactive_geo_area(self, db_session):
        from app.services.network.zones import NetworkZones

        retired = self._area(db_session, name="Retired Coverage", is_active=False)
        with pytest.raises(HTTPException) as excinfo:
            NetworkZones.create(
                db_session, name="Stale Zone", geo_area_id=str(retired.id)
            )

        assert excinfo.value.status_code == 400

    def test_update_rejects_inactive_geo_area(self, db_session):
        from app.services.network.zones import NetworkZones

        retired = self._area(db_session, name="Retired Update Area", is_active=False)
        zone = NetworkZones.create(db_session, name="Update Guard Zone")
        with pytest.raises(HTTPException) as excinfo:
            NetworkZones.update(db_session, str(zone.id), geo_area_id=str(retired.id))

        assert excinfo.value.status_code == 400
        assert zone.geo_area_id is None

    def test_resolve_geo_area_returns_own_binding(self, db_session):
        from app.services.network.zones import NetworkZones

        area = self._area(db_session, name="Own Binding Area")
        zone = NetworkZones.create(
            db_session, name="Bound Zone", geo_area_id=str(area.id)
        )

        assert NetworkZones.resolve_geo_area(db_session, zone.id) == area.id

    def test_resolve_geo_area_inherits_through_parent_chain(self, db_session):
        from app.services.network.zones import NetworkZones

        area = self._area(db_session, name="Inherited Area")
        grandparent = NetworkZones.create(
            db_session, name="Region Zone", geo_area_id=str(area.id)
        )
        parent = NetworkZones.create(
            db_session, name="District Zone", parent_id=str(grandparent.id)
        )
        child = NetworkZones.create(
            db_session, name="Street Zone", parent_id=str(parent.id)
        )

        assert NetworkZones.resolve_geo_area(db_session, child.id) == area.id

    def test_resolve_geo_area_returns_none_for_unbound_chain(self, db_session):
        from app.services.network.zones import NetworkZones

        parent = NetworkZones.create(db_session, name="Unbound Parent")
        child = NetworkZones.create(
            db_session, name="Unbound Child", parent_id=str(parent.id)
        )

        assert NetworkZones.resolve_geo_area(db_session, child.id) is None
        assert NetworkZones.resolve_geo_area(db_session, None) is None

    def test_resolve_geo_area_degrades_when_nearest_binding_is_inactive(
        self, db_session
    ):
        """A retired GeoArea on the nearest bound zone must not rebind wider."""
        from app.services.network.zones import NetworkZones

        wide_area = self._area(db_session, name="Wide Active Area")
        near_area = self._area(db_session, name="Near Area")
        grandparent = NetworkZones.create(
            db_session, name="Wide Zone", geo_area_id=str(wide_area.id)
        )
        parent = NetworkZones.create(
            db_session,
            name="Near Zone",
            parent_id=str(grandparent.id),
            geo_area_id=str(near_area.id),
        )
        child = NetworkZones.create(
            db_session, name="Leaf Zone", parent_id=str(parent.id)
        )
        near_area.is_active = False
        db_session.flush()

        assert NetworkZones.resolve_geo_area(db_session, child.id) is None

    def test_resolve_geo_area_tolerates_parent_cycle(self, db_session):
        from app.services.network.zones import NetworkZones

        first = NetworkZones.create(db_session, name="Cycle A")
        second = NetworkZones.create(
            db_session, name="Cycle B", parent_id=str(first.id)
        )
        NetworkZones.update(db_session, str(first.id), parent_id=str(second.id))

        assert NetworkZones.resolve_geo_area(db_session, second.id) is None


class TestVendorCapabilitySchemas:
    """Vendor capability schema tests."""

    def test_create_valid(self):
        req = VendorCapabilityCreate(
            vendor="Huawei",
            model="EG8145V5",
            supported_features={"wifi": True, "voip": True, "catv": False},
        )
        assert req.max_lan_ports == 4
        assert req.supported_features["wifi"] is True

    def test_update_partial(self):
        req = VendorCapabilityUpdate(max_lan_ports=8)
        data = req.model_dump(exclude_unset=True)
        assert data == {"max_lan_ports": 8}


class TestTr069ParameterMapSchemas:
    """TR-069 parameter map schema tests."""

    def test_create_valid(self):
        req = Tr069ParameterMapCreate(
            canonical_name="wifi_ssid",
            tr069_path="InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID",
        )
        assert req.writable is True

    def test_create_empty_name(self):
        with pytest.raises(Exception):
            Tr069ParameterMapCreate(canonical_name="", tr069_path="some.path")


class TestRouterRegistration:
    """Test that catalog router is properly registered."""

    def test_catalog_routes_exist(self):
        from app.api.network_catalog import router

        paths = [r.path for r in router.routes]
        assert any("/onu-types" in p for p in paths)
        assert any("/speed-profiles" in p for p in paths)
        assert any("/network-zones" in p for p in paths)
        assert any("/vendor-capabilities" in p for p in paths)
