"""Route-level auth pins for the /api/v1/vendor portal surface.

The vendor portal's authorization is MEMBERSHIP, not RBAC: an active
`FieldVendorUser` of an active `FieldVendor` linked to a native `Vendor`
(`require_native_vendor_context`). The former `vendor_auth.
require_scoped_permission` was a bare alias for that context check — it
evaluated no permission claim and existed mainly because its NAME satisfied
the route-guard architecture test. The alias is gone; the router now depends
on `require_native_vendor_context` explicitly, `/api/v1/vendor` is
allowlisted as a self-scoped surface (same rationale as `/api/v1/field` and
`/api/v1/reseller`), and these tests pin the actual auth behavior the
architecture test can only name-check.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.vendor_portal import router
from app.db import get_db
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.services.auth_dependencies import require_user_auth


def _system_user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Vendor",
        last_name="Portal",
        display_name="Vendor Portal",
        email=f"vendor-portal-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _auth(user: SystemUser, principal_type: str = "system_user") -> dict:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": principal_type,
        "roles": [],
        "scopes": [],
    }


def _client(db_session, auth: dict) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: auth
    return TestClient(app)


def test_non_system_user_principal_is_rejected(db_session) -> None:
    user = _system_user(db_session)
    client = _client(db_session, _auth(user, principal_type="subscriber"))
    resp = client.get("/api/v1/vendor/projects/available")
    assert resp.status_code == 403


def test_system_user_without_membership_is_rejected(db_session) -> None:
    user = _system_user(db_session)
    db_session.commit()
    client = _client(db_session, _auth(user))
    resp = client.get("/api/v1/vendor/projects/available")
    assert resp.status_code == 403


def test_member_without_native_vendor_link_gets_409(db_session) -> None:
    user = _system_user(db_session)
    vendor = FieldVendor(
        name="Unlinked Co", code=f"VC-{uuid4().hex[:6]}", is_active=True
    )
    db_session.add(vendor)
    db_session.flush()
    db_session.add(
        FieldVendorUser(
            vendor_id=vendor.id,
            system_user_id=user.id,
            role="crew",
            is_active=True,
        )
    )
    db_session.commit()
    client = _client(db_session, _auth(user))
    resp = client.get("/api/v1/vendor/projects/available")
    assert resp.status_code == 409


def test_linked_member_reaches_the_route(db_session) -> None:
    user = _system_user(db_session)
    native = Vendor(name=f"Native Co {uuid4().hex[:6]}")
    db_session.add(native)
    db_session.flush()
    vendor = FieldVendor(
        name="Linked Co",
        code=f"VC-{uuid4().hex[:6]}",
        is_active=True,
        crm_vendor_id=str(native.id),
    )
    db_session.add(vendor)
    db_session.flush()
    db_session.add(
        FieldVendorUser(
            vendor_id=vendor.id,
            system_user_id=user.id,
            role="crew",
            is_active=True,
        )
    )
    db_session.commit()
    client = _client(db_session, _auth(user))
    resp = client.get("/api/v1/vendor/projects/available")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Capability enforcement
#
# Membership answers "is this a vendor user?". It never answered "which vendor
# user?" — so every authenticated vendor could quote, submit evidence, and
# raise invoices alike. These pin the role split at the route boundary.
# ---------------------------------------------------------------------------


def _linked_member(db_session, role: str) -> SystemUser:
    user = _system_user(db_session)
    native = Vendor(name=f"Native Co {uuid4().hex[:6]}")
    db_session.add(native)
    db_session.flush()
    vendor = FieldVendor(
        name="Linked Co",
        code=f"VC-{uuid4().hex[:6]}",
        is_active=True,
        crm_vendor_id=str(native.id),
    )
    db_session.add(vendor)
    db_session.flush()
    db_session.add(
        FieldVendorUser(
            vendor_id=vendor.id,
            system_user_id=user.id,
            role=role,
            is_active=True,
        )
    )
    db_session.commit()
    return user


def test_field_role_cannot_raise_a_purchase_invoice(db_session) -> None:
    """Money out is the owner's decision. A field technician recording work
    must not be able to invoice for it."""
    user = _linked_member(db_session, "field")
    client = _client(db_session, _auth(user))

    resp = client.post(
        "/api/v1/vendor/purchase-invoices",
        json={"project_id": str(uuid4()), "invoice_number": "INV-1"},
    )

    assert resp.status_code == 403
    assert "vendor:invoice:write" in resp.json()["detail"]


def test_field_role_cannot_create_a_quote(db_session) -> None:
    user = _linked_member(db_session, "field")
    client = _client(db_session, _auth(user))

    resp = client.post(
        "/api/v1/vendor/quotes",
        json={"project_id": str(uuid4()), "currency": "NGN"},
    )

    assert resp.status_code == 403
    assert "vendor:quote:write" in resp.json()["detail"]


def test_supervisor_may_quote_but_not_invoice(db_session) -> None:
    user = _linked_member(db_session, "supervisor")
    client = _client(db_session, _auth(user))

    invoice = client.post(
        "/api/v1/vendor/purchase-invoices",
        json={"project_id": str(uuid4()), "invoice_number": "INV-1"},
    )
    assert invoice.status_code == 403

    # Quoting clears the capability gate; the project simply does not exist,
    # which is the domain owner's answer rather than the capability gate's.
    quote = client.post(
        "/api/v1/vendor/quotes",
        json={"project_id": str(uuid4()), "currency": "NGN"},
    )
    assert quote.status_code != 403


def test_owner_role_clears_every_capability_gate(db_session) -> None:
    user = _linked_member(db_session, "owner")
    client = _client(db_session, _auth(user))

    for path, body in (
        ("/api/v1/vendor/quotes", {"project_id": str(uuid4()), "currency": "NGN"}),
        (
            "/api/v1/vendor/purchase-invoices",
            {"project_id": str(uuid4()), "invoice_number": "INV-1"},
        ),
    ):
        assert client.post(path, json=body).status_code != 403


def test_reads_stay_open_to_every_role(db_session) -> None:
    for role in ("owner", "supervisor", "field"):
        user = _linked_member(db_session, role)
        client = _client(db_session, _auth(user))
        assert client.get("/api/v1/vendor/projects/mine").status_code == 200


def test_field_role_cannot_request_material_or_an_advance(db_session) -> None:
    """A field technician records what happened; drawing stock and asking for
    money are decisions above their role."""
    user = _linked_member(db_session, "field")
    client = _client(db_session, _auth(user))

    material = client.post(
        "/api/v1/vendor/material-releases",
        json={
            "project_id": str(uuid4()),
            "items": [{"description": "Cable", "quantity": 10}],
        },
    )
    advance = client.post(
        "/api/v1/vendor/advances",
        json={"project_id": str(uuid4()), "amount": "1000"},
    )

    assert material.status_code == 403
    assert "vendor:material:request" in material.json()["detail"]
    assert advance.status_code == 403
    assert "vendor:advance:request" in advance.json()["detail"]


def test_supervisor_may_draw_material_but_not_ask_for_money(db_session) -> None:
    """A supervisor runs the site and needs material to keep working;
    committing the organisation to a financial ask stays with the owner."""
    user = _linked_member(db_session, "supervisor")
    client = _client(db_session, _auth(user))

    material = client.post(
        "/api/v1/vendor/material-releases",
        json={
            "project_id": str(uuid4()),
            "items": [{"description": "Cable", "quantity": 10}],
        },
    )
    advance = client.post(
        "/api/v1/vendor/advances",
        json={"project_id": str(uuid4()), "amount": "1000"},
    )

    # Material clears the capability gate; the project simply does not exist,
    # which is the owner's answer rather than the gate's.
    assert material.status_code != 403
    assert advance.status_code == 403
