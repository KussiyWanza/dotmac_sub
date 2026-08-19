from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.field_vendor import FieldVendor, FieldVendorDeviceToken, FieldVendorUser
from app.models.network import FdhCabinet
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services.auth_dependencies import require_user_auth
from app.services.field.vendor_auth import vendor_context
from app.services.field.vendor_devices import register_vendor_device


def _user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Vendor",
        last_name="Crew",
        display_name="Vendor Crew",
        email=f"vendor-crew-{uuid4().hex[:8]}@example.com",
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


def _vendor_user(
    db_session, user: SystemUser, *, active: bool = True
) -> FieldVendorUser:
    vendor = FieldVendor(
        name="Install Co", code=f"VC-{uuid4().hex[:6]}", is_active=True
    )
    db_session.add(vendor)
    db_session.flush()
    membership = FieldVendorUser(
        vendor_id=vendor.id,
        system_user_id=user.id,
        role="crew",
        is_active=active,
    )
    db_session.add(membership)
    db_session.flush()
    return membership


def test_vendor_context_resolves_active_membership(db_session):
    user = _user(db_session)
    membership = _vendor_user(db_session, user)
    db_session.commit()

    context = vendor_context(db_session, _auth(user))

    assert context["vendor_user_id"] == str(membership.id)
    assert context["vendor_id"] == str(membership.vendor_id)
    assert context["vendor_role"] == "crew"


def test_vendor_context_rejects_non_vendor_user(db_session):
    user = _user(db_session)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        vendor_context(db_session, _auth(user))

    assert exc.value.status_code == 403


def test_vendor_device_registration_upserts_token(db_session):
    user = _user(db_session)
    membership = _vendor_user(db_session, user)
    db_session.commit()

    first = register_vendor_device(
        db_session,
        vendor_user_id=str(membership.id),
        token="fcm-token",
        platform="android",
        app_version="1.0.0",
    )
    second = register_vendor_device(
        db_session,
        vendor_user_id=str(membership.id),
        token="fcm-token",
        platform="android",
        app_version="1.0.1",
    )

    assert first.id == second.id
    assert second.app_version == "1.0.1"
    assert db_session.query(FieldVendorDeviceToken).count() == 1


def test_vendor_routes_register_device_and_read_nearby_map(db_session):
    user = _user(db_session)
    _vendor_user(db_session, user)
    db_session.add(
        FdhCabinet(
            name="FDH Jabi",
            code="FDH-JBI",
            latitude=9.071,
            longitude=7.451,
            is_active=True,
        )
    )
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    device = client.post(
        "/api/v1/field/vendor/devices",
        json={"platform": "android", "fcm_token": "vendor-fcm", "app_version": "1.2.0"},
    )
    assert device.status_code == 201
    assert device.json()["platform"] == "android"

    nearby = client.get(
        "/api/v1/field/vendor/map-assets/nearby",
        params={"lat": 9.071, "lng": 7.451, "types": "fdh_cabinet"},
    )
    assert nearby.status_code == 200
    assert nearby.json()["items"][0]["title"] == "FDH Jabi"

    unsupported_type = client.get(
        "/api/v1/field/vendor/map-assets/nearby",
        params={"lat": 9.071, "lng": 7.451, "types": "olt_device"},
    )
    assert unsupported_type.status_code == 400
    assert "Unsupported vendor map asset type" in unsupported_type.text

    empty_type = client.get(
        "/api/v1/field/vendor/map-assets/search",
        params={"q": "FDH", "types": ","},
    )
    assert empty_type.status_code == 422
    assert "Select at least one vendor map asset type" in empty_type.text

    invalid_radius = client.get(
        "/api/v1/field/vendor/map-assets/nearby",
        params={"lat": 9.071, "lng": 7.451, "radius_m": 50001},
    )
    assert invalid_radius.status_code == 422
