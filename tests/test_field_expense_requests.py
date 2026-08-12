from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.field_expense import FieldExpenseRequest
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.auth_dependencies import require_user_auth
from app.services.field import attachments as attachments_module
from app.services.field.attachments import field_attachments
from app.services.field.expense_requests import field_expense_requests
from app.services.field.jobs import field_jobs


@dataclass
class _Stream:
    chunks: Iterator[bytes]
    content_type: str
    content_length: int


class _FakeUploads:
    def __init__(self):
        self.contents: dict[str, bytes] = {}

    def upload(self, **kwargs):
        record = StoredFile(
            entity_type=kwargs["entity_type"],
            entity_id=kwargs["entity_id"],
            original_filename=kwargs["original_filename"],
            storage_key_or_relative_path=f"attachments/{uuid4().hex}",
            file_size=len(kwargs["data"]),
            content_type=kwargs["content_type"],
            storage_provider="s3",
            uploaded_by=kwargs["uploaded_by"],
            owner_subscriber_id=kwargs["owner_subscriber_id"],
        )
        kwargs["db"].add(record)
        kwargs["db"].commit()
        kwargs["db"].refresh(record)
        self.contents[str(record.id)] = kwargs["data"]
        return record

    def stream_file(self, record):
        data = self.contents[str(record.id)]
        return _Stream(iter([data]), record.content_type, len(data))

    def soft_delete(self, *, db, file, hard_delete_object=True):
        file.is_deleted = True
        db.commit()
        return file


@pytest.fixture()
def fake_uploads(monkeypatch):
    fake = _FakeUploads()
    monkeypatch.setattr(attachments_module, "file_uploads", fake)
    return fake


def _user(db_session, name: str = "Expense") -> SystemUser:
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
    db_session, user: SystemUser, crm_person_id: str = "crm-expense-tech"
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
        first_name="Expense",
        last_name="Customer",
        email=f"expense-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id=overrides.pop("crm_work_order_id", "wo-expense"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Field expense"),
        status=overrides.pop("status", "in_progress"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-expense-tech"
        ),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _expense_items(**overrides):
    item = {
        "category_code": "transport",
        "category_name": "Transport",
        "description": "Bike delivery",
        "amount": "2500.00",
        "expense_date": date.today(),
        "vendor_name": "Rider",
        "notes": "Urgent part pickup",
    }
    item.update(overrides)
    return [item]


def test_create_submit_cancel_and_surface_expense_in_job_detail(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(
        db_session, subscriber, crm_work_order_id="wo-expense-flow"
    )
    client_ref = uuid4()
    db_session.commit()

    created = field_expense_requests.create(
        db_session,
        _auth(user),
        crm_work_order_id="wo-expense-flow",
        purpose="Transport for extra drop cable",
        expense_date=date.today(),
        currency="ngn",
        notes="Customer site was missing materials",
        client_ref=client_ref,
        items=_expense_items(),
    )
    replayed = field_expense_requests.create(
        db_session,
        _auth(user),
        crm_work_order_id="wo-expense-flow",
        purpose="Transport for extra drop cable",
        expense_date=date.today(),
        currency="NGN",
        notes=None,
        client_ref=client_ref,
        items=_expense_items(),
    )

    assert replayed["id"] == created["id"]
    assert created["status"] == "draft"
    assert str(created["total_amount"]) == "2500.00"
    db_session.refresh(work_order)
    assert work_order.metadata_["native_field_source"] == "sub"
    assert "expense_requests" in work_order.metadata_["native_field_activity"]

    submitted = field_expense_requests.submit(
        db_session, _auth(user), str(created["id"])
    )
    assert submitted["status"] == "submitted"
    assert submitted["submitted_at"] is not None

    detail = field_jobs.get_detail(db_session, _auth(user), "wo-expense-flow")
    assert len(detail.expense_requests) == 1
    assert detail.expense_requests[0].status == "submitted"

    canceled = field_expense_requests.cancel(
        db_session, _auth(user), str(created["id"])
    )
    assert canceled["status"] == "canceled"


def test_expense_request_scope_and_receipt_attachment_validation(
    db_session, fake_uploads
):
    user = _user(db_session)
    _profile(db_session, user)
    other = _user(db_session, "Other")
    _profile(db_session, other, crm_person_id="other-expense-tech")
    subscriber = _subscriber(db_session)
    visible = _work_order(
        db_session, subscriber, crm_work_order_id="wo-expense-visible"
    )
    hidden = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-expense-hidden",
        assigned_to_crm_person_id="other-expense-tech",
    )
    db_session.commit()
    receipt = field_attachments.create(
        db_session,
        _auth(user),
        kind="document",
        file_name="receipt.pdf",
        mime_type="application/pdf",
        content=b"%PDF",
        crm_work_order_id=visible.crm_work_order_id,
    )

    with pytest.raises(HTTPException) as hidden_exc:
        field_expense_requests.create(
            db_session,
            _auth(user),
            crm_work_order_id=hidden.crm_work_order_id,
            purpose="Hidden",
            expense_date=None,
            currency="NGN",
            notes=None,
            client_ref=None,
            items=_expense_items(),
        )
    assert hidden_exc.value.status_code == 404

    created = field_expense_requests.create(
        db_session,
        _auth(user),
        crm_work_order_id=visible.crm_work_order_id,
        purpose="Receipt linked",
        expense_date=None,
        currency="NGN",
        notes=None,
        client_ref=None,
        items=_expense_items(receipt_attachment_id=receipt["id"]),
    )
    assert created["items"][0]["receipt_attachment_id"] == receipt["id"]


def test_expense_request_api(db_session, fake_uploads):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-expense-api")
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    receipt = client.post(
        "/api/v1/field/expense-requests/receipts",
        data={"work_order_id": "wo-expense-api"},
        files={"file": ("taxi.jpg", b"receipt-bytes", "image/jpeg")},
    )
    assert receipt.status_code == 201
    assert receipt.json()["work_order_id"] == "wo-expense-api"

    created = client.post(
        "/api/v1/field/expense-requests",
        json={
            "work_order_id": "wo-expense-api",
            "purpose": "Transport",
            "currency": "NGN",
            "items": [
                {
                    "category_code": "transport",
                    "description": "Bike delivery",
                    "amount": "1800.00",
                }
            ],
        },
    )
    assert created.status_code == 201
    assert created.json()["work_order_id"] == "wo-expense-api"
    request_id = created.json()["id"]

    listed = client.get("/api/v1/field/expense-requests?status=draft")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == request_id

    submitted = client.post(f"/api/v1/field/expense-requests/{request_id}/submit")
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "submitted"
    assert db_session.query(FieldExpenseRequest).count() == 1


def test_atomic_expense_submission_replays_and_rejects_changed_payload(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-expense-atomic")
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)
    client_ref = str(uuid4())
    payload = {
        "client_ref": client_ref,
        "work_order_id": "wo-expense-atomic",
        "purpose": "Transport",
        "currency": "NGN",
        "items": [
            {
                "category_code": "transport",
                "description": "Bike delivery",
                "amount": "1800.00",
            }
        ],
    }

    created = client.post("/api/v1/field/expense-requests/submit", json=payload)
    replayed = client.post("/api/v1/field/expense-requests/submit", json=payload)
    changed = client.post(
        "/api/v1/field/expense-requests/submit",
        json={**payload, "purpose": "Different purpose"},
    )

    assert created.status_code == 201
    assert created.json()["status"] == "submitted"
    assert replayed.status_code == 201
    assert replayed.json()["id"] == created.json()["id"]
    assert changed.status_code == 409
    assert db_session.query(FieldExpenseRequest).count() == 1
