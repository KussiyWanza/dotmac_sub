"""ERP re-home PR 3 — material-request ISSUE flow (map + enqueue + write-back +
reconcile).

Everything runs against a MOCKED ERP: the outbox uses a fake client, and the
status reconcile is fed a canned response. The flow is proven end-to-end WITHOUT
a live ERP and, crucially, WITHOUT flipping ownership in prod — tests set
``sync_flow_ownership.material_request = sub`` in-test only. The default (crm)
path is asserted to send nothing (the inert guarantee).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

import app.models  # noqa: F401 — registers every model on Base.metadata
from app.models.dispatch import TechnicianProfile
from app.models.event_store import EventStore
from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
    SyncFlowOwner,
    SyncFlowOwnership,
)
from app.models.field_material import (
    FieldInventoryItem,
    FieldMaterialRequest,
    FieldWorkOrderMaterial,
)
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.dotmac_erp import material_sync, outbox
from app.services.field import material_requests
from app.services.field.material_requests import field_material_requests
from app.services.integrations.backoffice_contracts import ERP_OUTBOX_CAPABILITY
from app.services.owner_commands import CommandContext
from tests.integration_platform_helpers import enable_erp_capability

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _seed_ownership(db, *, sub_flows: set[str] | None = None) -> None:
    sub_flows = sub_flows or set()
    for flow in FieldErpSyncFlow:
        owner = (
            SyncFlowOwner.sub.value
            if flow.value in sub_flows
            else SyncFlowOwner.crm.value
        )
        db.add(SyncFlowOwnership(flow=flow.value, owner=owner))
    db.flush()


def _user(db, name: str = "Material") -> SystemUser:
    user = SystemUser(
        first_name=name,
        last_name="Tech",
        display_name=f"{name} Tech",
        email=f"{name.lower()}-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db.add(user)
    db.flush()
    return user


def _profile(
    db, user: SystemUser, crm_person_id: str = "crm-material-tech"
) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=crm_person_id,
        title="Installer",
    )
    db.add(profile)
    db.flush()
    return profile


def _subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Material",
        last_name="Customer",
        email=f"material-{uuid4().hex[:8]}@example.com",
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def _work_order(db, subscriber: Subscriber, **overrides) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id=overrides.pop("crm_work_order_id", "wo-material"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Field material"),
        status=overrides.pop("status", "in_progress"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-material-tech"
        ),
        crm_ticket_id=overrides.pop("crm_ticket_id", "crm-ticket-77"),
        crm_project_id=overrides.pop("crm_project_id", "crm-project-88"),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        **overrides,
    )
    db.add(row)
    db.flush()
    return row


def _inventory_item(db, *, sku="RJ45-CAT6", name="RJ45 Connector", unit="PCS"):
    item = FieldInventoryItem(sku=sku, name=name, unit=unit)
    db.add(item)
    db.flush()
    return item


def _auth(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _make_approved_request(db, *, crm_work_order_id="wo-mat") -> FieldMaterialRequest:
    """Create → submit → approve a request via the real service.

    Enqueue behavior is determined by recorded ownership and an enabled typed
    capability. A missing capability is recorded for reconciliation and does
    not reverse the Sub-owned approval.
    """
    crm_person_id = f"crm-tech-{uuid4().hex[:8]}"
    user = _user(db)
    _profile(db, user, crm_person_id=crm_person_id)
    subscriber = _subscriber(db)
    _work_order(
        db,
        subscriber,
        crm_work_order_id=crm_work_order_id,
        assigned_to_crm_person_id=crm_person_id,
    )
    item = _inventory_item(db)
    db.commit()
    created = field_material_requests.create(
        db,
        _auth(user),
        crm_work_order_id=crm_work_order_id,
        priority="high",
        notes="Need connectors for the drop",
        source_warehouse_code="WH-LAGOS",
        items=[{"item_id": str(item.id), "quantity": 5, "notes": "cat6"}],
    )
    request = db.get(FieldMaterialRequest, created["id"])
    request.fulfillment_channel = "erp"
    field_material_requests.submit(db, _auth(user), str(created["id"]))
    field_material_requests.approve(db, str(created["id"]))
    return db.get(FieldMaterialRequest, created["id"])


class _FakeERPClient:
    """Mocked ERP client for outbox + reconcile: canned responses in order."""

    def __init__(self, post_outcomes=None, status_outcomes=None):
        self._post = list(post_outcomes or [])
        self._status = list(status_outcomes or [])
        self.posts: list[dict] = []
        self.status_calls: list[str] = []
        self.closed = False

    def post(self, path, payload, idempotency_key=None, expected_status_codes=None):
        self.posts.append(
            {"path": path, "payload": payload, "idempotency_key": idempotency_key}
        )
        outcome = self._post.pop(0) if self._post else {}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get_material_request_status(self, omni_id):
        self.status_calls.append(omni_id)
        outcome = self._status.pop(0) if self._status else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Payload mapping fidelity (vs CRM's _map_material_request shape)
# ---------------------------------------------------------------------------


def test_payload_mapping_matches_crm_shape(db_session):
    request = _make_approved_request(db_session)
    payload = material_sync.build_material_request_payload(request)

    assert payload["omni_id"] == str(request.id)
    assert payload["request_type"] == "ISSUE"
    assert payload["status"] == "submitted"
    assert payload["requested_by_email"] == request.requested_by_system_user.email
    # ticket id comes off the work-order mirror (sub has no direct FK).
    assert payload["ticket_crm_id"] == "crm-ticket-77"
    assert payload["remarks"] == "Need connectors for the drop"
    assert payload["schedule_date"] == request.approved_at.date().isoformat()

    assert len(payload["items"]) == 1
    line = payload["items"][0]
    assert line["item_code"] == "RJ45-CAT6"
    assert line["quantity"] == 5
    assert line["uom"] == "PCS"
    assert line["from_warehouse_code"] == "WH-LAGOS"
    # No serials tracked → key omitted.
    assert "serial_numbers" not in line


def test_payload_supports_legacy_serials_and_warehouse_metadata(db_session):
    request = _make_approved_request(db_session)
    request.source_warehouse_code = None
    request.metadata_ = {"from_warehouse_code": "WH-LAGOS"}
    request.items[0].metadata_ = {"serial_numbers": ["SN-1", " SN-2 ", ""]}
    db_session.flush()

    line = material_sync.build_material_request_payload(request)["items"][0]
    assert line["from_warehouse_code"] == "WH-LAGOS"
    assert line["serial_numbers"] == ["SN-1", "SN-2"]


def test_idempotency_key_is_stable_across_reapprove(db_session):
    request = _make_approved_request(db_session)
    key1 = material_sync.material_request_idempotency_key(request)
    key2 = material_sync.material_request_idempotency_key(request)
    assert key1 == key2 == f"mr-{request.id}-approve-v1"


def test_eligibility_requires_approved_items_and_email(db_session):
    request = _make_approved_request(db_session)
    assert material_sync.material_request_eligibility_error(request) is None

    request.status = "fulfilled"
    assert "cannot be synced" in material_sync.material_request_eligibility_error(
        request
    )


# ---------------------------------------------------------------------------
# Enqueue on approve — gated by ownership and the typed capability
# ---------------------------------------------------------------------------


def _outbox_rows(db, request) -> list[FieldErpSyncEvent]:
    return (
        db.query(FieldErpSyncEvent)
        .filter(FieldErpSyncEvent.entity_id == request.id)
        .all()
    )


def test_approve_does_not_enqueue_before_ownership_cutover(db_session):
    request = _make_approved_request(db_session)
    assert _outbox_rows(db_session, request) == []
    assert not any(
        event.get("event") == "backoffice_delivery_pending"
        for event in (request.metadata_ or {}).get("manager_events", [])
    )


def test_approve_enqueues_with_owner_and_enabled_capability(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.material_request.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_approved_request(db_session)

    rows = _outbox_rows(db_session, request)
    assert len(rows) == 1
    row = rows[0]
    assert row.flow == FieldErpSyncFlow.material_request.value
    assert row.idempotency_key == f"mr-{request.id}-approve-v1"
    assert row.status == FieldErpSyncStatus.pending.value
    assert row.payload["omni_id"] == str(request.id)
    assert row.payload["request_type"] == "ISSUE"


def test_adapter_failure_does_not_reverse_sub_approval(db_session, monkeypatch):
    request = _make_approved_request(db_session)
    request.status = "submitted"
    request.approved_at = None
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.material_request.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    db_session.commit()

    def fail_enqueue(*args, **kwargs):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(material_sync, "enqueue_material_request", fail_enqueue)
    field_material_requests.approve(db_session, str(request.id))
    db_session.expire_all()
    persisted = db_session.get(FieldMaterialRequest, request.id)
    # The Sub approval is never reversed by an ERP-transport failure; the
    # consequence is a durably failed, retryable event delivery instead of
    # a metadata breadcrumb.
    assert persisted.status == "approved"
    assert persisted.approved_at is not None
    assert _outbox_rows(db_session, persisted) == []
    from app.models.event_store import EventStatus, EventStore

    delivery = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "field_material_request.approved")
        .order_by(EventStore.created_at.desc())
        .first()
    )
    assert delivery is not None
    assert delivery.status == EventStatus.failed


def test_approval_survives_when_provider_capability_is_disabled(db_session):
    request = _make_approved_request(db_session)
    request.status = "submitted"
    request.approved_at = None
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.material_request.value})
    db_session.commit()

    result = field_material_requests.approve(db_session, str(request.id))
    assert result["status"] == "approved"
    # The outbox intent is created regardless of provider capability; the
    # capability gates delivery, so the row waits as a durable pending
    # delivery instead of a lost breadcrumb.
    rows = _outbox_rows(db_session, request)
    assert len(rows) == 1
    assert rows[0].status == FieldErpSyncStatus.pending.value


def test_reenqueue_reuses_the_same_outbox_row(db_session):
    request = _make_approved_request(db_session)
    # Re-enqueue directly with the same (stable) key → idempotent, no duplicate.
    first = material_sync.enqueue_material_request(db_session, request)
    second = material_sync.enqueue_material_request(db_session, request)
    assert first.id == second.id
    assert len(_outbox_rows(db_session, request)) == 1


# ---------------------------------------------------------------------------
# Outbox delivery → write-back onto the source row
# ---------------------------------------------------------------------------


def test_delivery_accepted_writes_erp_fields_back(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.material_request.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_approved_request(db_session)
    client = _FakeERPClient(
        post_outcomes=[
            {
                "request_id": "ERP-MR-1",
                "status": "issued",
                "omni_id": str(request.id),
            }
        ]
    )

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    assert result.accepted == 1
    assert request.support_reference == "ERP-MR-1"
    assert request.support_status == "issued"
    assert request.status == "issued"
    assert request.issued_at is not None
    allocation = db_session.query(FieldWorkOrderMaterial).one()
    assert allocation.allocated_quantity == 5
    assert client.posts[0]["path"] == "/api/v1/sync/sub/material-requests"


def test_delivery_fulfilled_maps_terminal_status(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.material_request.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_approved_request(db_session)
    client = _FakeERPClient(
        post_outcomes=[{"request_id": "ERP-MR-2", "status": "fulfilled"}]
    )

    outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    assert request.support_status == "fulfilled"
    assert request.status == "issued"
    assert request.issued_at is not None


def test_delivery_rejected_records_erp_status(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.material_request.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_approved_request(db_session)
    client = _FakeERPClient(post_outcomes=[{"status": "rejected"}])

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    assert result.rejected == 1
    assert row.status == FieldErpSyncStatus.rejected.value
    assert request.support_status == "rejected"
    assert request.status == "canceled"


# ---------------------------------------------------------------------------
# Ownership guard — the inert guarantee
# ---------------------------------------------------------------------------


def test_delivery_refused_when_flow_owned_by_crm(db_session):
    # material_request left at the seeded default (crm) — must NOT be sent.
    _seed_ownership(db_session)
    request = _make_approved_request(db_session)
    material_sync.enqueue_material_request(db_session, request)
    db_session.commit()
    client = _FakeERPClient(post_outcomes=[{"request_id": "SHOULD-NOT-HAPPEN"}])

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    assert result.skipped_not_owned == 1
    assert client.posts == []
    assert request.support_reference is None
    row = _outbox_rows(db_session, request)[0]
    assert row.status == FieldErpSyncStatus.pending.value
    assert row.attempts == 0


def test_local_issue_is_blocked_after_material_flow_cutover(db_session):
    request = _make_approved_request(db_session)
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.material_request.value})
    db_session.commit()

    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        field_material_requests.issue(db_session, str(request.id))

    assert exc_info.value.status_code == 409
    assert "back-office system owns material issue" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# Status reconcile
# ---------------------------------------------------------------------------


def test_refresh_updates_status_for_in_flight_request(db_session):
    request = _make_approved_request(db_session)
    request.support_system = "dotmac_erp"
    request.support_reference = "ERP-MR-9"
    request.support_status = "issued"
    request.status = "accepted_by_erp"
    db_session.commit()

    client = _FakeERPClient(
        status_outcomes=[{"request_id": "ERP-MR-9", "status": "fulfilled"}]
    )
    result = material_sync.refresh_material_request_statuses(db_session, client=client)

    db_session.refresh(request)
    assert result["processed"] == 1
    assert result["updated"] == 1
    assert client.status_calls == [str(request.id)]
    assert request.support_status == "fulfilled"
    assert request.status == "issued"


def test_refresh_skips_unsynced_and_terminal_requests(db_session):
    # Not synced yet (no erp id) → excluded.
    unsynced = _make_approved_request(db_session, crm_work_order_id="wo-a")
    # Synced but already fulfilled (terminal) → excluded from the in-flight poll.
    fulfilled = _make_approved_request(db_session, crm_work_order_id="wo-b")
    fulfilled.support_system = "dotmac_erp"
    fulfilled.support_reference = "ERP-DONE"
    fulfilled.status = "fulfilled"
    db_session.commit()

    client = _FakeERPClient(status_outcomes=[])
    result = material_sync.refresh_material_request_statuses(db_session, client=client)

    assert result["processed"] == 0
    assert client.status_calls == []
    assert unsynced.support_reference is None


def _erp_observation(request_id, *, command_id, observed_at):
    return material_requests.ObserveErpMaterialStatus(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="integration:test-dotmac-erp",
            scope="erp.material_status.webhook.v1",
            reason="Test ERP material status observation",
            idempotency_key="stable-test-delivery",
        ),
        request_id=request_id,
        provider_request_id="MR-TEST-1",
        provider_status="ISSUED",
        observed_at=observed_at,
    )


def test_signed_observation_path_is_idempotent_after_issuance(db_session):
    request = _make_approved_request(db_session)
    request_id = request.id
    observed_at = datetime.now(UTC)
    db_session.commit()

    first = material_requests.observe_erp_material_status(
        db_session,
        _erp_observation(request_id, command_id=uuid4(), observed_at=observed_at),
    )
    first_row = db_session.get(FieldMaterialRequest, request_id)
    first_issued_at = first_row.issued_at
    db_session.commit()
    second = material_requests.observe_erp_material_status(
        db_session,
        _erp_observation(request_id, command_id=uuid4(), observed_at=observed_at),
    )

    assert first.status is material_requests.MaterialRequestStatus.ISSUED
    assert second.status is material_requests.MaterialRequestStatus.ISSUED
    assert second.support_reference == "MR-TEST-1"
    second_row = db_session.get(FieldMaterialRequest, request_id)
    assert second_row.issued_at == first_issued_at
    assert db_session.query(FieldWorkOrderMaterial).count() == 1
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "field_material_request.fulfilled")
        .count()
        == 1
    )
    manager_events = list((second_row.metadata_ or {}).get("manager_events") or [])
    assert (
        sum(
            event.get("event") == "backoffice_material_issued"
            for event in manager_events
        )
        == 1
    )


def test_signed_observation_rejects_manual_request(db_session):
    request = _make_approved_request(db_session)
    request_id = request.id
    request.fulfillment_channel = "manual"
    db_session.commit()

    with pytest.raises(material_requests.MaterialRequestError) as exc_info:
        material_requests.observe_erp_material_status(
            db_session,
            _erp_observation(
                request_id,
                command_id=uuid4(),
                observed_at=datetime.now(UTC),
            ),
        )

    assert exc_info.value.code.endswith("manual_delivery_conflict")
