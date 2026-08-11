from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.field_material import (
    FieldInventoryItem,
    FieldMaterialRequest,
    FieldWorkOrderMaterial,
)
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.auth_dependencies import require_user_auth
from app.services.field.jobs import field_jobs
from app.services.field.material_requests import field_material_requests


def _user(db_session, name: str = "MaterialReq") -> SystemUser:
    user = SystemUser(
        first_name=name,
        last_name="Tech",
        display_name=f"{name} Tech",
        email=f"{name.lower()}-{uuid4().hex[:8]}@example.com",
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


def _profile(
    db_session, user: SystemUser, crm_person_id: str = "crm-material-request-tech"
) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=crm_person_id,
        title="Installer",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="MaterialReq",
        last_name="Customer",
        email=f"material-request-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id=overrides.pop("crm_work_order_id", "wo-material-request"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Request materials"),
        status=overrides.pop("status", "in_progress"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-material-request-tech"
        ),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _item(db_session, **overrides) -> FieldInventoryItem:
    item = FieldInventoryItem(
        sku=overrides.pop("sku", f"SKU-{uuid4().hex[:6]}"),
        name=overrides.pop("name", "Drop cable"),
        unit=overrides.pop("unit", "m"),
        **overrides,
    )
    db_session.add(item)
    db_session.flush()
    return item


def test_create_submit_and_surface_material_request_in_job_detail(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(
        db_session, subscriber, crm_work_order_id="wo-material-request-flow"
    )
    item = _item(db_session, name="Connector", unit="pcs")
    db_session.commit()

    created = field_material_requests.create(
        db_session,
        _auth(user),
        crm_work_order_id="wo-material-request-flow",
        priority="urgent",
        notes="Need more connectors",
        items=[{"item_id": item.id, "quantity": 4, "notes": "SC/APC"}],
    )

    assert created["status"] == "draft"
    assert created["priority"] == "urgent"
    assert created["items"][0]["name"] == "Connector"
    db_session.refresh(work_order)
    assert work_order.metadata_["native_field_source"] == "sub"
    assert "material_requests" in work_order.metadata_["native_field_activity"]

    listed = field_material_requests.list_mine(db_session, _auth(user))
    assert [request["id"] for request in listed] == [created["id"]]

    submitted = field_material_requests.submit(
        db_session, _auth(user), str(created["id"])
    )
    assert submitted["status"] == "submitted"
    assert submitted["submitted_at"] is not None

    detail = field_jobs.get_detail(db_session, _auth(user), "wo-material-request-flow")
    assert len(detail.material_requests) == 1
    assert detail.material_requests[0].status == "submitted"


def test_material_request_validation_and_scope(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    other = _user(db_session, "Other")
    _profile(db_session, other, crm_person_id="other-material-request-tech")
    subscriber = _subscriber(db_session)
    hidden = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-material-request-hidden",
        assigned_to_crm_person_id="other-material-request-tech",
    )
    visible = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-material-request-visible",
    )
    item = _item(db_session)
    db_session.commit()

    with pytest.raises(HTTPException) as hidden_exc:
        field_material_requests.create(
            db_session,
            _auth(user),
            crm_work_order_id=hidden.crm_work_order_id,
            priority="medium",
            notes=None,
            items=[{"item_id": item.id, "quantity": 1}],
        )
    assert hidden_exc.value.status_code == 404

    with pytest.raises(HTTPException) as duplicate_exc:
        field_material_requests.create(
            db_session,
            _auth(user),
            crm_work_order_id=visible.crm_work_order_id,
            priority="medium",
            notes=None,
            items=[
                {"item_id": item.id, "quantity": 1},
                {"item_id": item.id, "quantity": 2},
            ],
        )
    assert duplicate_exc.value.status_code == 422


def test_only_requesting_technician_can_submit(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    other = _user(db_session, "Other")
    _profile(db_session, other, crm_person_id="other-submit-material-request-tech")
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-material-request-submit")
    item = _item(db_session)
    db_session.commit()
    created = field_material_requests.create(
        db_session,
        _auth(user),
        crm_work_order_id="wo-material-request-submit",
        priority="low",
        notes=None,
        items=[{"item_id": item.id, "quantity": 1}],
    )

    with pytest.raises(HTTPException) as exc:
        field_material_requests.submit(db_session, _auth(other), str(created["id"]))

    assert exc.value.status_code == 404


def test_manager_material_lifecycle_allocates_work_order_stock(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-material-manager")
    item = _item(db_session, sku="DROP-250", name="Drop cable")
    db_session.commit()
    created = field_material_requests.create(
        db_session,
        _auth(user),
        crm_work_order_id="wo-material-manager",
        priority="medium",
        notes="Need cable",
        items=[{"item_id": item.id, "quantity": 10}],
    )
    field_material_requests.submit(db_session, _auth(user), str(created["id"]))

    approved = field_material_requests.approve(db_session, str(created["id"]))
    assert approved["status"] == "approved"
    issued = field_material_requests.issue(db_session, str(created["id"]))
    assert issued["status"] == "issued"
    fulfilled = field_material_requests.fulfill(db_session, str(created["id"]))
    assert fulfilled["status"] == "fulfilled"

    allocation = db_session.query(FieldWorkOrderMaterial).one()
    assert allocation.item_id == item.id
    assert allocation.allocated_quantity == 10


def test_material_request_api(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-material-request-api")
    item = _item(db_session, name="Drop cable")
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    created = client.post(
        "/api/v1/field/material-requests",
        json={
            "work_order_id": "wo-material-request-api",
            "priority": "high",
            "source_warehouse_code": "WH-LAGOS",
            "items": [{"item_id": str(item.id), "quantity": 3}],
        },
    )
    assert created.status_code == 201
    assert created.json()["work_order_id"] == "wo-material-request-api"
    request_id = created.json()["id"]

    listed = client.get("/api/v1/field/material-requests?status=draft")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == request_id

    submitted = client.post(f"/api/v1/field/material-requests/{request_id}/submit")
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "submitted"
    assert db_session.query(FieldMaterialRequest).count() == 1
