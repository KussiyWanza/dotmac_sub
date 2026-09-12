from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
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
from app.models.vendor_routes import Vendor
from app.models.work_order import WorkOrder
from app.services.auth_dependencies import require_user_auth
from app.services.field import attachments as attachments_module
from app.services.field import expense_categories as expense_categories_module
from app.services.field.attachments import field_attachments
from app.services.field.expense_requests import (
    CancelFieldExpenseRequest,
    ExpenseCategoryRule,
    ExpenseRequestLineInput,
    ExpenseRequestStatus,
    ExpenseWorkOrderIdentity,
    FieldExpenseRequestError,
    ListFieldExpenseVendors,
    RequesterExpenseDetailQuery,
    RequesterExpenseHistoryQuery,
    ResolvedFieldExpenseSubmissionContext,
    SelectedExpenseApprover,
    SubmitFieldExpenseRequest,
    VerifiedExpenseDestinationInput,
    cancel_field_expense_request_command,
    field_expense_requests,
    get_requester_expense_request,
    list_expense_vendors,
    list_requester_expense_requests,
    submit_field_expense_request_command,
)
from app.services.field.jobs import field_jobs
from app.services.owner_commands import CommandContext


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


@pytest.fixture(autouse=True)
def _authoritative_expense_rules(monkeypatch):
    monkeypatch.setattr(
        expense_categories_module,
        "list_expense_categories",
        lambda _db, _query: (
            ExpenseCategoryRule(
                category_code="transport",
                category_name="Transport",
                requires_receipt=False,
                max_amount_per_claim=Decimal("10000.00"),
            ),
        ),
    )


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


def _vendor(db_session, name: str, *, is_active: bool = True) -> Vendor:
    row = Vendor(name=name, is_active=is_active)
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


def _command_context(user: SystemUser, command_id=None) -> CommandContext:
    resolved_id = command_id or uuid4()
    return CommandContext(
        command_id=resolved_id,
        correlation_id=resolved_id,
        actor=f"user:{user.id}",
        scope="field:expense_requests:write",
        reason="test expense submission",
        idempotency_key=str(resolved_id),
    )


def _submit_expense(
    db_session,
    user: SystemUser,
    work_order: WorkOrder,
    *,
    request_id=None,
    purpose="Transport",
    notes=None,
    items=None,
):
    resolved_id = request_id or uuid4()
    user_id = user.id
    work_order_public_id = work_order.public_id
    db_session.commit()
    outcome = submit_field_expense_request_command(
        db_session,
        SubmitFieldExpenseRequest(
            context=CommandContext(
                command_id=resolved_id,
                correlation_id=resolved_id,
                actor=f"user:{user_id}",
                scope="field:expense_requests:write",
                reason="test expense submission",
                idempotency_key=str(resolved_id),
            ),
            requester_person_id=user_id,
            work_order=ExpenseWorkOrderIdentity(public_id=work_order_public_id),
            request_id=resolved_id,
            purpose=purpose,
            expense_date=date.today(),
            currency="NGN",
            notes=notes,
            items=tuple(
                ExpenseRequestLineInput(
                    category_code=item["category_code"],
                    category_name=item.get("category_name"),
                    description=item["description"],
                    amount=Decimal(str(item["amount"])),
                    expense_date=item.get("expense_date"),
                    vendor_name=item.get("vendor_name"),
                    receipt_url=item.get("receipt_url"),
                    receipt_attachment_id=item.get("receipt_attachment_id"),
                    notes=item.get("notes"),
                )
                for item in (items or _expense_items())
            ),
        ),
    )
    return field_expense_requests.get(db_session, _auth(user), str(outcome.id))


def _cancel_expense(db_session, user: SystemUser, request_id):
    user_id = user.id
    db_session.commit()
    outcome = cancel_field_expense_request_command(
        db_session,
        command=CancelFieldExpenseRequest(
            context=CommandContext.system(
                actor=f"user:{user_id}",
                scope="field:expense_requests:write",
                reason="test expense cancellation",
            ),
            expense_request_id=request_id,
            requester_person_id=user_id,
        ),
    )
    return field_expense_requests.get(db_session, _auth(user), str(outcome.id))


def test_create_submit_cancel_and_surface_expense_in_job_detail(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(
        db_session, subscriber, crm_work_order_id="wo-expense-flow"
    )
    client_ref = uuid4()
    db_session.commit()

    created = _submit_expense(
        db_session,
        user,
        work_order,
        request_id=client_ref,
        purpose="Transport for extra drop cable",
        notes="Customer site was missing materials",
    )
    replayed = _submit_expense(
        db_session,
        user,
        work_order,
        request_id=client_ref,
        purpose="Transport for extra drop cable",
        notes="Customer site was missing materials",
    )

    assert replayed["id"] == created["id"]
    assert created["status"] == "submitted"
    assert str(created["total_amount"]) == "2500.00"
    db_session.refresh(work_order)
    assert work_order.metadata_["native_field_source"] == "sub"
    assert "expense_requests" in work_order.metadata_["native_field_activity"]

    detail = field_jobs.get_detail(db_session, _auth(user), "wo-expense-flow")
    assert len(detail.expense_requests) == 1
    assert detail.expense_requests[0].status == "submitted"

    canceled = _cancel_expense(db_session, user, created["id"])
    assert canceled["status"] == "canceled"


def test_expense_history_survives_completion_and_reassignment(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    other = _user(db_session, "Other")
    _profile(db_session, other, crm_person_id="other-expense-history-tech")
    subscriber = _subscriber(db_session)
    work_order = _work_order(
        db_session, subscriber, crm_work_order_id="wo-expense-history"
    )
    db_session.commit()

    created = _submit_expense(
        db_session, user, work_order, purpose="Completion history"
    )
    work_order.status = "completed"
    work_order.assigned_to_crm_person_id = "other-expense-history-tech"
    db_session.commit()

    own_history = field_expense_requests.list_mine(db_session, _auth(user))
    assert [row["id"] for row in own_history] == [created["id"]]
    assert field_expense_requests.list_mine(db_session, _auth(other)) == []
    stored = db_session.get(FieldExpenseRequest, created["id"])
    assert stored is not None
    assert stored.requested_by_technician_id == profile.id
    assert (
        field_expense_requests.get(db_session, _auth(user), str(created["id"]))["id"]
        == created["id"]
    )
    with pytest.raises(HTTPException) as other_detail:
        field_expense_requests.get(db_session, _auth(other), str(created["id"]))
    assert other_detail.value.status_code == 404
    assert _cancel_expense(db_session, user, created["id"])["status"] == "canceled"


def test_expense_history_supports_person_and_legacy_user_ownership(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    other = _user(db_session, "OtherIdentity")
    _profile(db_session, other, crm_person_id="other-expense-identity-tech")
    decoy = _user(db_session, "DecoyIdentity")
    decoy_profile = _profile(
        db_session, decoy, crm_person_id="decoy-expense-identity-tech"
    )
    subscriber = _subscriber(db_session)
    work_order = _work_order(
        db_session, subscriber, crm_work_order_id="wo-expense-identity"
    )
    db_session.commit()

    created = _submit_expense(db_session, user, work_order, purpose="Identity history")
    row = db_session.get(FieldExpenseRequest, created["id"])
    assert row is not None

    permanent_person_id = uuid4()
    profile.person_id = permanent_person_id
    row.requested_by_person_id = permanent_person_id
    row.requested_by_technician_id = decoy_profile.id
    row.requested_by_system_user_id = None
    db_session.commit()
    assert [
        item["id"]
        for item in field_expense_requests.list_mine(
            db_session, {**_auth(user), "person_id": str(permanent_person_id)}
        )
    ] == [created["id"]]

    row.requested_by_person_id = uuid4()
    row.requested_by_system_user_id = user.id
    db_session.commit()
    assert [
        item["id"] for item in field_expense_requests.list_mine(db_session, _auth(user))
    ] == [created["id"]]
    assert field_expense_requests.list_mine(db_session, _auth(other)) == []


def test_requester_expense_history_survives_inactive_profile_and_counts_total(
    db_session,
):
    user = _user(db_session, "InactiveHistory")
    profile = _profile(db_session, user, crm_person_id="inactive-expense-history-tech")
    other = _user(db_session, "OtherHistory")
    _profile(db_session, other, crm_person_id="other-inactive-expense-history-tech")
    subscriber = _subscriber(db_session)
    work_order = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-inactive-expense-history",
        assigned_to_crm_person_id="inactive-expense-history-tech",
    )
    db_session.commit()

    first = _submit_expense(db_session, user, work_order, purpose="First history")
    second = _submit_expense(db_session, user, work_order, purpose="Second history")
    for request_id in (first["id"], second["id"]):
        row = db_session.get(FieldExpenseRequest, request_id)
        assert row is not None
        row.requested_by_person_id = uuid4()
        row.requested_by_system_user_id = None
    profile.is_active = False
    db_session.commit()

    page = list_requester_expense_requests(
        db_session,
        RequesterExpenseHistoryQuery(
            system_user_id=user.id,
            status=ExpenseRequestStatus.SUBMITTED,
            limit=1,
        ),
    )

    assert page.total == 2
    assert len(page.items) == 1
    assert page.items[0].id == second["id"]
    assert (
        get_requester_expense_request(
            db_session,
            RequesterExpenseDetailQuery(
                system_user_id=user.id,
                request_id=first["id"],
            ),
        ).id
        == first["id"]
    )
    assert (
        list_requester_expense_requests(
            db_session,
            RequesterExpenseHistoryQuery(system_user_id=other.id),
        ).items
        == ()
    )


def test_requester_expense_history_uses_system_user_without_profile(db_session):
    user = _user(db_session, "ProfilelessHistory")
    profile = _profile(
        db_session, user, crm_person_id="profileless-expense-history-tech"
    )
    subscriber = _subscriber(db_session)
    work_order = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-profileless-expense-history",
        assigned_to_crm_person_id="profileless-expense-history-tech",
    )
    db_session.commit()

    created = _submit_expense(
        db_session, user, work_order, purpose="Profileless history"
    )
    row = db_session.get(FieldExpenseRequest, created["id"])
    assert row is not None
    row.requested_by_technician_id = None
    db_session.delete(profile)
    db_session.commit()

    page = list_requester_expense_requests(
        db_session,
        RequesterExpenseHistoryQuery(system_user_id=user.id),
    )

    assert page.total == 1
    assert [item.id for item in page.items] == [created["id"]]

    with pytest.raises(FieldExpenseRequestError) as invalid_page:
        list_requester_expense_requests(
            db_session,
            RequesterExpenseHistoryQuery(system_user_id=user.id, limit=0),
        )
    assert invalid_page.value.code == "operations.expense_requests.invalid_request"


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

    with pytest.raises(FieldExpenseRequestError) as hidden_exc:
        _submit_expense(db_session, user, hidden, purpose="Hidden")
    assert hidden_exc.value.code == "operations.expense_requests.work_order_not_found"

    created = _submit_expense(
        db_session,
        user,
        visible,
        purpose="Receipt linked",
        items=_expense_items(receipt_attachment_id=receipt["id"]),
    )
    assert created["items"][0]["receipt_attachment_id"] == receipt["id"]


def test_expense_vendor_picker_lists_active_vendors(db_session):
    _vendor(db_session, "Zed Supplies")
    alpha = _vendor(db_session, "Alpha Logistics")
    _vendor(db_session, "Inactive Vendor", is_active=False)
    db_session.commit()

    items = list_expense_vendors(
        db=db_session,
        query=ListFieldExpenseVendors(search="alpha", limit=25),
    )

    assert [(item.id, item.label) for item in items] == [
        (alpha.id, "Alpha Logistics"),
    ]


def test_expense_request_api(db_session, fake_uploads, monkeypatch):
    user = _user(db_session)
    approver = _user(db_session, "Approver")
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-expense-api")
    alpha = _vendor(db_session, "Alpha Logistics")
    zed = _vendor(db_session, "Zed Supplies")
    _vendor(db_session, "Inactive Vendor", is_active=False)
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)
    client_ref = uuid4()
    approver_erp_id = uuid4()
    now = datetime.now(UTC)
    monkeypatch.setattr(
        "app.api.field.expense_requests.resolve_field_expense_submission_context",
        lambda **_kwargs: ResolvedFieldExpenseSubmissionContext(
            selected_approver=SelectedExpenseApprover(
                erp_employee_id=approver_erp_id,
                system_user_id=approver.id,
                display_name=approver.display_name,
                email=approver.email,
            ),
            payment_destination=VerifiedExpenseDestinationInput(
                mode="erp_profile",
                destination_token="verified-destination-token",
                bank_code="058",
                bank_name="Example Bank",
                masked_account_number="******6789",
                verified_beneficiary_name=user.display_name,
                verified_at=now,
                expires_at=now + timedelta(minutes=10),
            ),
        ),
    )

    vendors = client.get("/api/v1/field/expense-requests/vendors?limit=25")
    assert vendors.status_code == 200
    assert vendors.json()["items"] == [
        {"id": str(alpha.id), "label": "Alpha Logistics"},
        {"id": str(zed.id), "label": "Zed Supplies"},
    ]

    filtered_vendors = client.get("/api/v1/field/expense-requests/vendors?q=zed")
    assert filtered_vendors.status_code == 200
    assert filtered_vendors.json()["items"] == [
        {"id": str(zed.id), "label": "Zed Supplies"},
    ]

    bad_id = client.get("/api/v1/field/expense-requests/not-a-uuid")
    assert bad_id.status_code == 422

    receipt = client.post(
        "/api/v1/field/expense-requests/receipts",
        data={"work_order_id": "wo-expense-api"},
        files={"file": ("taxi.jpg", b"receipt-bytes", "image/jpeg")},
    )
    assert receipt.status_code == 201
    assert receipt.json()["work_order_id"] == "wo-expense-api"

    retired = client.post(
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
    assert retired.status_code == 410

    created = client.post(
        "/api/v1/field/expense-requests/submit",
        json={
            "client_ref": str(client_ref),
            "work_order_id": "wo-expense-api",
            "purpose": "Transport",
            "currency": "NGN",
            "selected_approver": {
                "erp_employee_id": str(approver_erp_id),
                "system_user_id": str(approver.id),
                "display_name": approver.display_name,
                "email": approver.email,
            },
            "payment_destination": {
                "destination_token": "verified-destination-token",
                "mode": "erp_profile",
                "bank_code": "058",
                "bank_name": "Example Bank",
                "masked_account_number": "******6789",
                "verified_beneficiary_name": user.display_name,
                "verified_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=10)).isoformat(),
            },
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

    second = _submit_expense(
        db_session, user, work_order, purpose="Second paginated expense"
    )
    listed = client.get("/api/v1/field/expense-requests?status=submitted&limit=1")
    assert listed.status_code == 200
    assert listed.json()["count"] == 2
    assert len(listed.json()["items"]) == 1
    assert listed.json()["items"][0]["id"] == str(second["id"])

    detail = client.get(f"/api/v1/field/expense-requests/{request_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == request_id

    legacy_submit = client.post(f"/api/v1/field/expense-requests/{request_id}/submit")
    assert legacy_submit.status_code == 410
    assert db_session.query(FieldExpenseRequest).count() == 2


def test_atomic_expense_submission_replays_and_rejects_changed_payload(
    db_session, monkeypatch
):
    user = _user(db_session)
    approver = _user(db_session, "Approver")
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
    approver_erp_id = uuid4()
    now = datetime.now(UTC)
    monkeypatch.setattr(
        "app.api.field.expense_requests.resolve_field_expense_submission_context",
        lambda **_kwargs: ResolvedFieldExpenseSubmissionContext(
            selected_approver=SelectedExpenseApprover(
                erp_employee_id=approver_erp_id,
                system_user_id=approver.id,
                display_name=approver.display_name,
                email=approver.email,
            ),
            payment_destination=VerifiedExpenseDestinationInput(
                mode="erp_profile",
                destination_token="verified-destination-token",
                bank_code="058",
                bank_name="Example Bank",
                masked_account_number="******6789",
                verified_beneficiary_name=user.display_name,
                verified_at=now,
                expires_at=now + timedelta(minutes=10),
            ),
        ),
    )
    payload = {
        "client_ref": client_ref,
        "work_order_id": "wo-expense-atomic",
        "purpose": "Transport",
        "currency": "NGN",
        "selected_approver": {
            "erp_employee_id": str(approver_erp_id),
            "system_user_id": str(approver.id),
            "display_name": approver.display_name,
            "email": approver.email,
        },
        "payment_destination": {
            "destination_token": "verified-destination-token",
            "mode": "erp_profile",
            "bank_code": "058",
            "bank_name": "Example Bank",
            "masked_account_number": "******6789",
            "verified_beneficiary_name": user.display_name,
            "verified_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=10)).isoformat(),
        },
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
    listed = client.get("/api/v1/field/expense-requests")
    changed = client.post(
        "/api/v1/field/expense-requests/submit",
        json={**payload, "purpose": "Different purpose"},
    )

    assert created.status_code == 201
    assert created.json()["status"] == "submitted"
    assert replayed.status_code == 201
    assert replayed.json()["id"] == created.json()["id"]
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [created.json()["id"]]
    assert changed.status_code == 409
    assert db_session.query(FieldExpenseRequest).count() == 1
