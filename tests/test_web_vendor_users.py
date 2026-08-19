from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.models.field_vendor import FieldVendorUser
from app.models.system_user import SystemUser
from app.services import vendor_admin
from app.services import web_vendors as web_vendors_service
from app.services.operator_tenant import provision_operator_tenant
from app.web.admin.vendors import router


@pytest.fixture(autouse=True)
def _operator_tenant(db_session):
    provision_operator_tenant(db_session)


def _client(db_session) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    app.dependency_overrides[get_db] = lambda: db_session
    for route in router.routes:
        for dependency in route.dependant.dependencies:
            if dependency.call is not None and dependency.call is not get_db:
                app.dependency_overrides[dependency.call] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def test_add_portal_user_with_unused_email_succeeds(db_session):
    vendor = vendor_admin.create_committed(
        db_session,
        name="Admin Portal Vendor",
        code=f"APV-{uuid4().hex[:8]}",
    )
    vendor_id = str(vendor.id)
    email = f"portal-{uuid4().hex[:8]}@vendor.example"

    response = _client(db_session).post(
        f"/admin/vendors/{vendor_id}/users",
        data={
            "first_name": "Ada",
            "last_name": "Obi",
            "email": email,
            "role": "field",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/vendors/{vendor_id}"
    membership = db_session.query(FieldVendorUser).one()
    principal = db_session.get(SystemUser, membership.system_user_id)
    assert principal is not None
    assert principal.email == email


def test_vendor_detail_exposes_login_enablement_actions(db_session):
    vendor = vendor_admin.create_committed(
        db_session,
        name="Vendor Detail Actions",
        code=f"VDA-{uuid4().hex[:8]}",
    )
    vendor_id = str(vendor.id)
    _client(db_session).post(
        f"/admin/vendors/{vendor_id}/users",
        data={
            "first_name": "Ada",
            "last_name": "Obi",
            "email": f"detail-{uuid4().hex[:8]}@vendor.example",
            "role": "field",
        },
        follow_redirects=False,
    )

    response = _client(db_session).get(f"/admin/vendors/{vendor_id}")

    assert response.status_code == 200
    html = response.text
    assert "/enable" in html
    assert "Enable" in html
    assert "/setup-link" in html
    assert "Send setup link" in html


def test_setup_link_route_delegates_to_vendor_service(db_session, monkeypatch):
    vendor = vendor_admin.create_committed(
        db_session,
        name="Setup Link Vendor",
        code=f"SLV-{uuid4().hex[:8]}",
    )
    vendor_id = str(vendor.id)
    _client(db_session).post(
        f"/admin/vendors/{vendor_id}/users",
        data={
            "first_name": "Ada",
            "last_name": "Obi",
            "email": f"setup-{uuid4().hex[:8]}@vendor.example",
            "role": "field",
        },
        follow_redirects=False,
    )
    membership = db_session.query(FieldVendorUser).one()
    captured = {}

    def fake_send(db, *, membership_id, actor_id=None):
        captured["membership_id"] = membership_id
        captured["actor_id"] = actor_id

    monkeypatch.setattr(
        web_vendors_service,
        "send_vendor_user_setup_link",
        fake_send,
    )

    response = _client(db_session).post(
        f"/admin/vendors/{vendor_id}/users/{membership.id}/setup-link",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/vendors/{vendor_id}"
    assert captured["membership_id"] == str(membership.id)
