from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.audit import AuditEvent
from app.models.dispatch import TechnicianProfile
from app.models.field_map import FieldMapAssetLocationProvenance
from app.models.gis import ServiceBuilding
from app.models.network import FdhCabinet, FiberAccessPoint, FiberSpliceClosure
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.wireless_mast import WirelessMast
from app.models.work_order import WorkOrder
from app.services.auth_dependencies import require_user_auth
from app.services.field.map_assets import (
    SUPPORTED_FIELD_MAP_ASSET_TYPES,
    VENDOR_ROUTE_AUTHORING_POI_FILTERS,
    VENDOR_ROUTE_AUTHORING_POI_TYPES,
    field_map_assets,
)
from app.services.field.map_search import field_map_search


def _user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Map",
        last_name="Tech",
        display_name="Map Tech",
        email=f"map-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _auth(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _seed_assets(db_session):
    fdh = FdhCabinet(
        name="FDH Jabi",
        code="FDH-JB",
        latitude=9.0710,
        longitude=7.4510,
    )
    closure = FiberSpliceClosure(
        name="Closure 14",
        latitude=9.0718,
        longitude=7.4512,
    )
    access_point = FiberAccessPoint(
        name="NAP 4",
        code="NAP-4",
        access_point_type="pole",
        placement="aerial",
        latitude=9.0711,
        longitude=7.4511,
    )
    building = ServiceBuilding(
        name="Jabi Plaza",
        code="BLD-JB",
        clli="JABI01",
        latitude=9.0712,
        longitude=7.4512,
    )
    mast = WirelessMast(
        name="Mast Jabi",
        latitude=9.09,
        longitude=7.49,
        structure_type="monopole",
    )
    inactive = FdhCabinet(
        name="Inactive FDH",
        latitude=9.0710,
        longitude=7.4510,
        is_active=False,
    )
    no_location = FdhCabinet(name="No Location")
    db_session.add_all(
        [fdh, closure, access_point, building, mast, inactive, no_location]
    )
    db_session.commit()
    return fdh, closure, access_point, building, mast


def test_vendor_route_authoring_poi_types_are_supported() -> None:
    assert VENDOR_ROUTE_AUTHORING_POI_TYPES == tuple(
        filter_option.value for filter_option in VENDOR_ROUTE_AUTHORING_POI_FILTERS
    )
    assert tuple(
        filter_option.value
        for filter_option in VENDOR_ROUTE_AUTHORING_POI_FILTERS
        if filter_option.selected_by_default
    ) == ("fdh_cabinet", "splice_closure", "fiber_access_point")
    assert all(
        filter_option.label for filter_option in VENDOR_ROUTE_AUTHORING_POI_FILTERS
    )
    assert set(VENDOR_ROUTE_AUTHORING_POI_TYPES).issubset(
        SUPPORTED_FIELD_MAP_ASSET_TYPES
    )


def test_list_map_assets_filters_active_geocoded_rows(db_session):
    _seed_assets(db_session)

    rows = field_map_assets.list(db_session)

    assert {row["type"] for row in rows} == {
        "fdh_cabinet",
        "fiber_access_point",
        "service_building",
        "splice_closure",
        "wireless_mast",
    }
    assert {row["title"] for row in rows} == {
        "FDH Jabi",
        "NAP 4",
        "Jabi Plaza",
        "Closure 14",
        "Mast Jabi",
    }
    assert all(row["status"] == "active" for row in rows)


def test_list_map_assets_supports_type_and_updated_since(db_session):
    fdh, *_ = _seed_assets(db_session)
    db_session.query(FdhCabinet).filter(FdhCabinet.id == fdh.id).update(
        {"updated_at": datetime.now(UTC) - timedelta(days=2)}
    )
    db_session.commit()

    rows = field_map_assets.list(
        db_session,
        asset_types=["fdh_cabinet", "fiber_access_point"],
        updated_since=datetime.now(UTC) - timedelta(days=1),
    )

    assert [row["type"] for row in rows] == ["fiber_access_point"]


def test_nearby_map_assets_are_sorted_by_distance(db_session):
    _seed_assets(db_session)

    rows = field_map_assets.nearby(
        db_session,
        latitude=9.07105,
        longitude=7.45105,
        radius_m=250,
    )

    assert [row["title"] for row in rows][:3] == ["FDH Jabi", "NAP 4", "Jabi Plaza"]
    assert all(row["distance_m"] is not None for row in rows)
    assert "Mast Jabi" not in {row["title"] for row in rows}


def test_unknown_asset_type_is_rejected(db_session):
    with pytest.raises(HTTPException) as exc:
        field_map_assets.list(db_session, asset_types=["olt_device"])

    assert exc.value.status_code == 400


def test_update_map_asset_location_records_provenance_and_audit(db_session):
    user = _user(db_session)
    fdh, *_ = _seed_assets(db_session)

    payload = field_map_assets.update_location(
        db_session,
        asset_type="fdh_cabinet",
        asset_id=str(fdh.id),
        latitude=9.081,
        longitude=7.462,
        actor_id=str(user.id),
        source="manual",
        accuracy_m=8.5,
        client_ref="client-1",
    )

    assert payload["latitude"] == 9.081
    assert payload["longitude"] == 7.462
    db_session.refresh(fdh)
    assert fdh.latitude == 9.081
    assert fdh.longitude == 7.462
    provenance = db_session.query(FieldMapAssetLocationProvenance).one()
    assert provenance.asset_type == "fdh_cabinet"
    assert provenance.asset_id == fdh.id
    assert provenance.source == "manual"
    audit = db_session.query(AuditEvent).one()
    assert audit.action == "field:map_asset:update_location"
    assert audit.metadata_["from"] == {"latitude": 9.071, "longitude": 7.451}
    assert audit.metadata_["client_ref"] == "client-1"


def test_update_map_asset_location_rejects_lower_confidence_without_force(db_session):
    user = _user(db_session)
    fdh, *_ = _seed_assets(db_session)
    field_map_assets.update_location(
        db_session,
        asset_type="fdh_cabinet",
        asset_id=str(fdh.id),
        latitude=9.081,
        longitude=7.462,
        actor_id=str(user.id),
        source="survey",
    )

    with pytest.raises(HTTPException) as exc:
        field_map_assets.update_location(
            db_session,
            asset_type="fdh_cabinet",
            asset_id=str(fdh.id),
            latitude=9.082,
            longitude=7.463,
            actor_id=str(user.id),
            source="gps",
        )

    assert exc.value.status_code == 409
    assert "higher-confidence" in str(exc.value.detail)


def test_update_map_asset_location_force_and_revert(db_session):
    user = _user(db_session)
    fdh, *_ = _seed_assets(db_session)
    field_map_assets.update_location(
        db_session,
        asset_type="fdh_cabinet",
        asset_id=str(fdh.id),
        latitude=9.081,
        longitude=7.462,
        actor_id=str(user.id),
        source="survey",
    )
    field_map_assets.update_location(
        db_session,
        asset_type="fdh_cabinet",
        asset_id=str(fdh.id),
        latitude=9.082,
        longitude=7.463,
        actor_id=str(user.id),
        source="gps",
        force=True,
    )

    reverted = field_map_assets.revert_location(
        db_session,
        asset_type="fdh_cabinet",
        asset_id=str(fdh.id),
        actor_id=str(user.id),
    )

    assert reverted["latitude"] == 9.081
    assert reverted["longitude"] == 7.462
    assert db_session.query(AuditEvent).count() == 3


def test_map_assets_api(db_session):
    user = _user(db_session)
    # Plant writes are field work: the caller must resolve to a technician.
    db_session.add(
        TechnicianProfile(
            person_id=user.id,
            system_user_id=user.id,
            title="Installer",
            is_active=True,
        )
    )
    db_session.flush()
    fdh, *_ = _seed_assets(db_session)

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)

    resp = TestClient(app).get(
        "/api/v1/field/map-assets",
        params={"asset_type": "fdh_cabinet"},
    )

    assert resp.status_code == 200
    assert resp.json()["items"][0]["title"] == "FDH Jabi"

    nearby = TestClient(app).get(
        "/api/v1/field/map-assets/nearby",
        params={"latitude": 9.07105, "longitude": 7.45105, "radius_m": 250},
    )

    assert nearby.status_code == 200
    assert nearby.json()[0]["distance_m"] >= 0

    updated = TestClient(app).patch(
        f"/api/v1/field/map-assets/fdh_cabinet/{fdh.id}/location",
        json={
            "latitude": 9.081,
            "longitude": 7.462,
            "source": "manual",
            "accuracy_m": 5,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["latitude"] == 9.081

    reverted = TestClient(app).post(
        f"/api/v1/field/map-assets/fdh_cabinet/{fdh.id}/revert-location"
    )
    assert reverted.status_code == 200
    assert reverted.json()["latitude"] == 9.071


def test_plant_relocation_refuses_a_principal_who_is_not_field_staff(db_session):
    """A merely authenticated principal must not move shared plant.

    The caller here has no technician profile and no vendor membership, which
    is what an ordinary portal or reseller session looks like.
    """

    user = _user(db_session)
    fdh, *_ = _seed_assets(db_session)

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    refused = client.patch(
        f"/api/v1/field/map-assets/fdh_cabinet/{fdh.id}/location",
        json={"latitude": 9.081, "longitude": 7.462, "source": "manual"},
    )
    reverted = client.post(
        f"/api/v1/field/map-assets/fdh_cabinet/{fdh.id}/revert-location"
    )

    assert refused.status_code == 403
    assert reverted.status_code == 403
    db_session.refresh(fdh)
    assert float(fdh.latitude) == 9.071


def test_map_search_finds_scoped_jobs_then_assets(db_session):
    user = _user(db_session)
    subscriber = Subscriber(
        first_name="Fiber",
        last_name="Customer",
        email=f"fiber-{uuid4().hex[:8]}@example.com",
    )
    other_subscriber = Subscriber(
        first_name="Other",
        last_name="Customer",
        email=f"other-{uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([subscriber, other_subscriber])
    db_session.flush()
    _seed_assets(db_session)
    db_session.add_all(
        [
            WorkOrder(
                crm_work_order_id="wo-search-visible",
                subscriber_id=subscriber.id,
                title="Fiber Street install",
                status="dispatched",
                assigned_to_crm_person_id="crm-map-tech",
                address="Fiber Street",
                metadata_={"location": {"lat": 9.071, "lng": 7.451}},
            ),
            WorkOrder(
                crm_work_order_id="wo-search-hidden",
                subscriber_id=other_subscriber.id,
                title="Fiber Street hidden",
                status="dispatched",
                assigned_to_crm_person_id="other-tech",
                address="Fiber Street",
                metadata_={"location": {"lat": 9.071, "lng": 7.451}},
            ),
        ]
    )

    db_session.add(
        TechnicianProfile(
            person_id=user.id,
            system_user_id=user.id,
            crm_person_id="crm-map-tech",
        )
    )
    db_session.commit()

    items = field_map_search.search(db_session, _auth(user), "Fiber Street")

    assert items[0]["kind"] == "job"
    assert items[0]["id"] == "wo-search-visible"
    assert items[0]["status_presentation"].value == "dispatched"
    assert {item["id"] for item in items} == {"wo-search-visible"}

    asset_items = field_map_search.search(db_session, _auth(user), "FDH Jabi")
    assert asset_items[0]["kind"] == "asset"
    assert asset_items[0]["asset_type"] == "fdh_cabinet"
