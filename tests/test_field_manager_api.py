from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.field_erp_sync import (
    FieldErpSyncFlow,
    SyncFlowOwner,
    SyncFlowOwnership,
)
from app.models.field_location import FieldTechPresence
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.auth_dependencies import require_user_auth
from app.services.field.expense_requests import (
    ApproveFieldExpenseRequest,
    approve_field_expense_request_command,
    field_expense_requests,
)
from app.services.field.jobs import field_jobs
from app.services.field.manager import field_manager
from app.services.owner_commands import CommandContext


def _user(db_session, name: str = "Manager") -> SystemUser:
    user = SystemUser(
        first_name=name,
        last_name="Staff",
        display_name=f"{name} Staff",
        email=f"{name.lower()}-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _auth(user: SystemUser, roles: list[str] | None = None) -> dict:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": roles if roles is not None else ["admin"],
        "scopes": [],
    }


def _approve_expense(db_session, *, request_id, reviewer_id):
    command_id = uuid4()
    db_session.commit()
    return approve_field_expense_request_command(
        db=db_session,
        command=ApproveFieldExpenseRequest(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"user:{reviewer_id}",
                scope="operations:expense_request:write",
                reason=f"approve_expense_request:{request_id}",
                idempotency_key=str(command_id),
            ),
            expense_request_id=request_id,
            reviewer_system_user_id=reviewer_id,
        ),
    )


def _enable_expense_flow(db_session) -> None:
    flow = FieldErpSyncFlow.expense_claim.value
    row = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == flow)
        .one_or_none()
    )
    if row is None:
        db_session.add(SyncFlowOwnership(flow=flow, owner=SyncFlowOwner.sub.value))
    else:
        row.owner = SyncFlowOwner.sub.value
    db_session.flush()


def _profile(db_session, user: SystemUser, **overrides) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=overrides.pop("crm_person_id", f"crm-{uuid4().hex[:8]}"),
        title=overrides.pop("title", "Installer"),
        **overrides,
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Field",
        last_name="Customer",
        email=f"manager-{uuid4().hex[:8]}@example.com",
        account_number=f"AC{uuid4().hex[:6]}",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id=overrides.pop("crm_work_order_id", f"wo-{uuid4().hex[:8]}"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Manager job"),
        status=overrides.pop("status", "scheduled"),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _presence(db_session, profile: TechnicianProfile, **overrides) -> FieldTechPresence:
    presence = FieldTechPresence(
        technician_id=profile.id,
        person_id=profile.person_id,
        status=overrides.pop("status", "on_shift"),
        location_sharing_enabled=overrides.pop("location_sharing_enabled", True),
        last_latitude=overrides.pop("last_latitude", 9.0765),
        last_longitude=overrides.pop("last_longitude", 7.3986),
        last_location_at=overrides.pop("last_location_at", datetime.now(UTC)),
        last_seen_at=overrides.pop("last_seen_at", datetime.now(UTC)),
        **overrides,
    )
    db_session.add(presence)
    db_session.flush()
    return presence


def _expense(db_session, tech_user, profile, work_order, status="submitted") -> dict:
    created = field_expense_requests.create(
        db_session,
        _auth(tech_user, roles=[]),
        crm_work_order_id=work_order.crm_work_order_id,
        purpose="Transport",
        expense_date=date.today(),
        currency="NGN",
        notes=None,
        client_ref=None,
        items=[
            {
                "category_code": "transport",
                "description": "Bike delivery",
                "amount": "2500.00",
            }
        ],
    )
    if status != "draft":
        created = field_expense_requests.submit(
            db_session, _auth(tech_user, roles=[]), str(created["id"])
        )
    return created


def test_manager_me_summary_and_technicians(db_session):
    # Counts are asserted as deltas from a pre-seed baseline: the summary is
    # global by design, and under xdist another worker-local test may have
    # leaked committed rows into the shared engine before this test runs.
    baseline = field_manager.summary(db_session)

    manager = _user(db_session)
    tech_user = _user(db_session, "Tech")
    profile = _profile(db_session, tech_user, crm_person_id="crm-mgr-tech")
    _presence(db_session, profile)
    subscriber = _subscriber(db_session)
    assigned = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-mgr-assigned",
        status="in_progress",
        assigned_to_crm_person_id="crm-mgr-tech",
    )
    _work_order(db_session, subscriber, crm_work_order_id="wo-mgr-unassigned")
    _expense(db_session, tech_user, profile, assigned)
    db_session.commit()

    me = field_manager.me(db_session, _auth(manager))
    assert me["is_manager"] is True
    assert me["name"] == "Manager Staff"
    assert me["roles"] == ["admin"]

    summary = field_manager.summary(db_session)
    assert summary["technicians_total"] == baseline["technicians_total"] + 1
    assert summary["technicians_live"] == baseline["technicians_live"] + 1
    assert summary["technicians_sharing"] == baseline["technicians_sharing"] + 1
    assert summary["open_jobs"] == baseline["open_jobs"] + 2
    assert summary["unassigned_jobs"] == baseline["unassigned_jobs"] + 1
    assert summary["pending_expenses"] == baseline["pending_expenses"] + 1

    technicians = field_manager.list_technicians(db_session)
    item = next(
        entry for entry in technicians if entry["person_id"] == profile.person_id
    )
    assert item["person_label"] == "Tech Staff"
    assert item["status"] == "on_shift"
    assert item["is_live"] is True
    assert item["last_latitude"] == pytest.approx(9.0765)
    assert item["active_work_order"]["id"] == "wo-mgr-assigned"
    assert item["active_work_order"]["status"] == "in_progress"
    assert item["active_work_order"]["status_presentation"].model_dump(mode="json") == {
        "value": "in_progress",
        "label": "In progress",
        "tone": "info",
        "icon": "clock",
    }


def test_manager_jobs_and_assign_flow(db_session):
    tech_user = _user(db_session, "Tech")
    profile = _profile(db_session, tech_user, crm_person_id="crm-assign-tech")
    subscriber = _subscriber(db_session)
    row = _work_order(db_session, subscriber, crm_work_order_id="wo-assign")
    db_session.commit()

    jobs = field_manager.list_jobs(db_session)
    job = next(entry for entry in jobs if entry["id"] == "wo-assign")
    assert job["assigned_to_person_id"] is None
    assert subscriber.account_number in job["subscriber_label"]
    assert job["status_presentation"].value == "scheduled"

    assigned = field_manager.assign_job(
        db_session,
        "wo-assign",
        person_id=str(profile.person_id),
    )
    assert assigned["assigned_to_person_id"] == profile.person_id
    assert assigned["assigned_to_label"] == "Tech Staff"
    assert assigned["status"] == "dispatched"

    db_session.refresh(row)
    assert row.assigned_to_crm_person_id == "crm-assign-tech"
    assert row.technician_name == "Tech Staff"
    assert row.metadata_["native_field_activity"]["assignment"]["technician_id"] == str(
        profile.id
    )
    queue = (
        db_session.query(WorkOrderAssignmentQueue)
        .filter(WorkOrderAssignmentQueue.work_order_mirror_id == row.id)
        .one()
    )
    assert queue.assigned_technician_id == profile.id
    assert queue.status == "assigned"

    # The technician now sees the job in their scoped list.
    mine = field_jobs.list(db_session, _auth(tech_user, roles=[]))
    assert "wo-assign" in [job.id for job in mine]

    # Filtering the manager board by the technician works both ways.
    filtered = field_manager.list_jobs(
        db_session, assigned_to_person_id=str(profile.person_id)
    )
    assert "wo-assign" in [job["id"] for job in filtered]


def test_manager_assign_queue_only_technician(db_session):
    """A technician profile without crm_person_id is scoped via the queue."""
    tech_user = _user(db_session, "Native")
    profile = _profile(db_session, tech_user, crm_person_id=None)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-native-assign")
    db_session.commit()

    assigned = field_manager.assign_job(
        db_session, "wo-native-assign", person_id=str(profile.person_id)
    )
    assert assigned["assigned_to_person_id"] == profile.person_id

    mine = field_jobs.list(db_session, _auth(tech_user, roles=[]))
    assert "wo-native-assign" in [job.id for job in mine]


def test_manager_assign_validation(db_session):
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-assign-bad")
    db_session.commit()

    with pytest.raises(HTTPException) as missing_job:
        field_manager.assign_job(db_session, "wo-nope", person_id=str(uuid4()))
    assert missing_job.value.status_code == 404

    with pytest.raises(HTTPException) as missing_tech:
        field_manager.assign_job(db_session, "wo-assign-bad", person_id=str(uuid4()))
    assert missing_tech.value.status_code == 404

    tech_user = _user(db_session, "Tech")
    profile = _profile(db_session, tech_user)
    db_session.commit()
    with pytest.raises(HTTPException) as bad_status:
        field_manager.assign_job(
            db_session,
            "wo-assign-bad",
            person_id=str(profile.person_id),
            status="completed",
        )
    assert bad_status.value.status_code == 422


def test_manager_expense_approve_and_reject(db_session):
    tech_user = _user(db_session, "Tech")
    profile = _profile(db_session, tech_user, crm_person_id="crm-exp-tech")
    subscriber = _subscriber(db_session)
    work_order = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-exp-mgr",
        status="in_progress",
        assigned_to_crm_person_id="crm-exp-tech",
    )
    first = _expense(db_session, tech_user, profile, work_order)
    second = _expense(db_session, tech_user, profile, work_order)
    _enable_expense_flow(db_session)
    db_session.commit()

    pending = field_expense_requests.list_all(db_session, status="submitted")
    assert {str(item["id"]) for item in pending} >= {
        str(first["id"]),
        str(second["id"]),
    }

    approved = _approve_expense(
        db_session, request_id=first["id"], reviewer_id=tech_user.id
    )
    assert approved.status == "approved"
    assert approved.approved_at is not None
    assert approved.erp_sync_status.value == "pending"

    rejected = field_expense_requests.reject(
        db_session, str(second["id"]), "No receipt provided"
    )
    assert rejected["status"] == "rejected"
    assert rejected["rejection_reason"] == "No receipt provided"

    re_approved = _approve_expense(
        db_session, request_id=first["id"], reviewer_id=tech_user.id
    )
    assert re_approved.status == "approved"
    assert re_approved.erp_sync_event_id == approved.erp_sync_event_id

    still_pending = {
        str(item["id"])
        for item in field_expense_requests.list_all(db_session, status="submitted")
    }
    assert str(first["id"]) not in still_pending
    assert str(second["id"]) not in still_pending


def test_manager_api(db_session):
    manager = _user(db_session)

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(manager)
    client = TestClient(app)

    # Baseline before seeding: global counts must be asserted as deltas (see
    # test_manager_me_summary_and_technicians).
    baseline = client.get("/api/v1/field/manager/summary").json()

    tech_user = _user(db_session, "Tech")
    profile = _profile(db_session, tech_user, crm_person_id="crm-api-tech")
    _presence(db_session, profile)
    subscriber = _subscriber(db_session)
    work_order = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-mgr-api",
        status="in_progress",
        assigned_to_crm_person_id="crm-api-tech",
    )
    expense = _expense(db_session, tech_user, profile, work_order)
    _enable_expense_flow(db_session)
    db_session.commit()

    me = client.get("/api/v1/field/manager/me")
    assert me.status_code == 200
    assert me.json()["is_manager"] is True

    summary = client.get("/api/v1/field/manager/summary")
    assert summary.status_code == 200
    assert summary.json()["technicians_total"] == baseline["technicians_total"] + 1
    assert summary.json()["pending_expenses"] == baseline["pending_expenses"] + 1

    technicians = client.get("/api/v1/field/manager/technicians")
    assert technicians.status_code == 200
    body = technicians.json()
    assert body["count"] == len(body["items"])
    assert body["live_count"] >= 1
    item = next(
        entry for entry in body["items"] if entry["person_id"] == str(profile.person_id)
    )
    assert item["person_label"] == "Tech Staff"
    assert item["active_work_order"]["id"] == "wo-mgr-api"
    assert item["active_work_order"]["status_presentation"] == {
        "value": "in_progress",
        "label": "In progress",
        "tone": "info",
        "icon": "clock",
    }

    jobs = client.get("/api/v1/field/manager/jobs")
    assert jobs.status_code == 200
    assert "wo-mgr-api" in [entry["id"] for entry in jobs.json()["items"]]
    manager_job = next(
        entry for entry in jobs.json()["items"] if entry["id"] == "wo-mgr-api"
    )
    assert manager_job["status_presentation"]["value"] == "in_progress"

    assigned = client.post(
        "/api/v1/field/manager/jobs/wo-mgr-api/assign",
        json={"person_id": str(profile.person_id)},
    )
    assert assigned.status_code == 200
    # Reassignment must not rewind an active field-execution lifecycle.
    assigned_body = assigned.json()
    assert assigned_body["status"] == "in_progress"
    assert assigned_body["assignment_queue_id"] is not None

    unassigned = client.post(
        "/api/v1/field/manager/assignments/"
        f"{assigned_body['assignment_queue_id']}/unassign",
        json={"reason": "Rebalance field workload"},
    )
    assert unassigned.status_code == 200
    assert unassigned.json()["status"] == "skipped"
    refreshed_jobs = client.get("/api/v1/field/manager/jobs").json()["items"]
    refreshed_job = next(
        entry for entry in refreshed_jobs if entry["id"] == "wo-mgr-api"
    )
    assert refreshed_job["assignment_queue_id"] is None
    assert refreshed_job["assigned_to_person_id"] is None
    assert refreshed_job["assigned_to_label"] is None
    assert refreshed_job["status"] == "in_progress"

    expenses = client.get("/api/v1/field/manager/expenses")
    assert expenses.status_code == 200
    assert str(expense["id"]) in [entry["id"] for entry in expenses.json()["items"]]

    approved = client.post(f"/api/v1/field/manager/expenses/{expense['id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["erp_sync_status"] == "pending"

    short_reason = client.post(
        f"/api/v1/field/manager/expenses/{expense['id']}/reject",
        json={"reason": "x"},
    )
    assert short_reason.status_code == 422


def test_manager_api_forbidden_without_permissions(db_session):
    plain = _user(db_session, "Plain")
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(plain, roles=[])
    client = TestClient(app)

    response = client.get("/api/v1/field/manager/me")
    assert response.status_code == 403
