from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.models.field_vendor import FieldVendorUser
from app.models.system_user import SystemUser
from app.services import vendor_admin
from app.web.admin.vendors import router


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
