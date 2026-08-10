"""ERP re-home PR 1 — client + outbox + ownership guard + settings substrate.

Everything runs against a MOCKED ERP (a fake client for the outbox; an
``httpx.MockTransport`` for the client's error split), so the money-path
substrate is exercised end-to-end without a live ERP.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401 — registers every model on Base.metadata
from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
    SyncFlowOwner,
    SyncFlowOwnership,
    flow_owned_by_sub,
    get_flow_ownership,
)
from app.services.dotmac_erp import outbox
from app.services.dotmac_erp.client import (
    DotMacERPAuthError,
    DotMacERPClient,
    DotMacERPError,
    DotMacERPNotFoundError,
    DotMacERPTransientError,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _seed_ownership(db, *, sub_flows: set[str] | None = None) -> None:
    """Seed one ownership row per flow (the migration seed, replayed for sqlite)."""
    sub_flows = sub_flows or set()
    for flow in FieldErpSyncFlow:
        owner = (
            SyncFlowOwner.sub.value
            if flow.value in sub_flows
            else SyncFlowOwner.crm.value
        )
        db.add(SyncFlowOwnership(flow=flow.value, owner=owner))
    db.flush()


def _enqueue(db, *, flow=FieldErpSyncFlow.expense_claim, key=None) -> FieldErpSyncEvent:
    return outbox.enqueue(
        db,
        flow=flow,
        entity_type="expense_request",
        entity_id=uuid4(),
        idempotency_key=key or f"exp-{uuid4()}-submit-v1",
        payload={"omni_id": str(uuid4()), "amount": "1000.00"},
    )


class FakeERPClient:
    """Mocked ERP client: canned responses / exceptions per call, records posts."""

    def __init__(self, outcomes):
        # outcomes: list of dict responses or Exception instances, consumed in order.
        self._outcomes = list(outcomes)
        self.posts: list[dict] = []
        self.closed = False

    def post(self, path, payload, idempotency_key=None, expected_status_codes=None):
        self.posts.append(
            {"path": path, "payload": payload, "idempotency_key": idempotency_key}
        )
        outcome = self._outcomes.pop(0) if self._outcomes else {}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# sync_flow_ownership guard + helper + seed
# ---------------------------------------------------------------------------


def test_ownership_helper_defaults_to_crm(db_session):
    _seed_ownership(db_session)
    for flow in FieldErpSyncFlow:
        assert flow_owned_by_sub(db_session, flow) is False


def test_ownership_helper_missing_row_is_not_sub(db_session):
    # No rows seeded at all → helper must not grant sub ownership.
    assert flow_owned_by_sub(db_session, FieldErpSyncFlow.expense_claim) is False


def test_ownership_helper_true_only_when_sub(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    assert flow_owned_by_sub(db_session, FieldErpSyncFlow.expense_claim) is True
    assert flow_owned_by_sub(db_session, FieldErpSyncFlow.material_request) is False


def test_get_flow_ownership_surface(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.material_request.value})
    ownership = get_flow_ownership(db_session)
    assert ownership == {
        "expense_claim": "crm",
        "material_request": "sub",
        "purchase_order": "crm",
        "purchase_invoice": "crm",
    }


def test_seed_covers_all_flows_as_crm_in_migration_source():
    source = (
        REPO_ROOT / "alembic" / "versions" / "249_field_erp_sync_outbox.py"
    ).read_text()
    assert '"owner": "crm"' in source
    for flow in FieldErpSyncFlow:
        assert f'"{flow.value}"' in source


# ---------------------------------------------------------------------------
# Outbox enqueue
# ---------------------------------------------------------------------------


def test_enqueue_is_idempotent_on_key(db_session):
    key = f"exp-{uuid4()}-submit-v1"
    first = _enqueue(db_session, key=key)
    second = _enqueue(db_session, key=key)
    assert first.id == second.id
    rows = db_session.query(FieldErpSyncEvent).filter_by(idempotency_key=key).all()
    assert len(rows) == 1
    assert first.status == FieldErpSyncStatus.pending.value


def test_enqueue_unique_race_preserves_the_source_transaction():
    db = MagicMock()
    winner = MagicMock(spec=FieldErpSyncEvent)
    first = db.query.return_value.filter.return_value.first
    first.side_effect = [None, winner]
    db.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate"))

    result = _enqueue(db)

    assert result is winner
    db.begin_nested.assert_called_once_with()
    db.rollback.assert_not_called()


def test_material_writeback_failure_is_not_swallowed(monkeypatch):
    from app.services.dotmac_erp import material_sync

    row = MagicMock(spec=FieldErpSyncEvent)
    row.flow = FieldErpSyncFlow.material_request.value

    def fail_writeback(*args, **kwargs):
        raise RuntimeError("projection failed")

    monkeypatch.setattr(material_sync, "apply_erp_response", fail_writeback)

    with pytest.raises(RuntimeError, match="projection failed"):
        outbox._dispatch_flow_writeback(MagicMock(), row)


# ---------------------------------------------------------------------------
# Outbox delivery — mocked client
# ---------------------------------------------------------------------------


def test_deliver_accepted_marks_row_and_sends_idempotency_key(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    event = _enqueue(db_session)
    client = FakeERPClient([{"claim_id": "ERP-123", "status": "approved"}])

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(event)
    assert event.status == FieldErpSyncStatus.accepted.value
    assert event.attempts == 1
    assert event.sent_at is not None
    assert event.erp_response == {"claim_id": "ERP-123", "status": "approved"}
    # The stored idempotency key is what gets sent (safe re-delivery).
    assert client.posts[0]["idempotency_key"] == event.idempotency_key
    assert client.posts[0]["path"] == "/api/v1/sync/sub/expense-claims"
    assert result.accepted == 1 and result.processed == 1


def test_deliver_rejected_is_terminal(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    event = _enqueue(db_session)
    client = FakeERPClient([{"status": "rejected", "rejection_reason": "over budget"}])

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(event)
    assert event.status == FieldErpSyncStatus.rejected.value
    assert result.rejected == 1


def test_deliver_2xx_without_decision_marks_sent(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    event = _enqueue(db_session)
    client = FakeERPClient([{"received": True}])  # no id, no terminal status

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(event)
    assert event.status == FieldErpSyncStatus.sent.value
    assert result.sent == 1


def test_deliver_transient_error_stays_pending_for_retry(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    event = _enqueue(db_session)
    client = FakeERPClient([DotMacERPTransientError("ERP 503")])

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(event)
    assert event.status == FieldErpSyncStatus.pending.value
    assert event.attempts == 1
    assert "503" in (event.last_error or "")
    assert result.retried == 1 and result.dead == 0


def test_deliver_transient_dead_letters_at_attempt_budget(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    event = _enqueue(db_session)
    event.attempts = 7  # one below the default budget of 8
    db_session.flush()
    client = FakeERPClient([DotMacERPTransientError("ERP 503 again")])

    result = outbox.deliver_pending(db_session, client=client, max_attempts=8)

    db_session.refresh(event)
    assert event.attempts == 8
    assert event.status == FieldErpSyncStatus.dead.value
    assert result.dead == 1


def test_deliver_permanent_error_dead_letters_immediately(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    event = _enqueue(db_session)
    client = FakeERPClient([DotMacERPError("422 validation", status_code=422)])

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(event)
    assert event.status == FieldErpSyncStatus.dead.value
    assert event.attempts == 1
    assert result.dead == 1


def test_deliver_refuses_flow_sub_does_not_own(db_session):
    # expense_claim still owned by CRM (default) — must NOT be sent.
    _seed_ownership(db_session)
    event = _enqueue(db_session)
    client = FakeERPClient([{"claim_id": "SHOULD-NOT-HAPPEN"}])

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(event)
    assert event.status == FieldErpSyncStatus.pending.value
    assert event.attempts == 0
    assert client.posts == []  # nothing posted
    assert result.skipped_not_owned == 1 and result.processed == 0


def test_deliver_mixed_ownership_only_sends_owned_flow(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    owned = _enqueue(db_session, flow=FieldErpSyncFlow.expense_claim)
    not_owned = _enqueue(db_session, flow=FieldErpSyncFlow.purchase_order)
    client = FakeERPClient([{"claim_id": "ERP-9"}])

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(owned)
    db_session.refresh(not_owned)
    assert owned.status == FieldErpSyncStatus.accepted.value
    assert not_owned.status == FieldErpSyncStatus.pending.value
    assert result.accepted == 1 and result.skipped_not_owned == 1
    assert len(client.posts) == 1


def test_deliver_no_pending_rows_is_noop(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    client = FakeERPClient([])
    result = outbox.deliver_pending(db_session, client=client)
    assert result.processed == 0 and result.skipped_not_owned == 0


# ---------------------------------------------------------------------------
# ERP client — idempotency-key header + transient/permanent error split
# ---------------------------------------------------------------------------


def _client_with_handler(handler) -> DotMacERPClient:
    client = DotMacERPClient(base_url="https://erp.test", token="k", retries=0)
    client._client = httpx.Client(
        base_url="https://erp.test", transport=httpx.MockTransport(handler)
    )
    return client


def test_client_sends_api_key_and_idempotency_key():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={"claim_id": "ERP-1"})

    client = _client_with_handler(handler)
    body = client.post("/sync/crm/expense-claims", {"a": 1}, idempotency_key="exp-1-v1")
    assert body == {"claim_id": "ERP-1"}
    assert captured["idempotency-key"] == "exp-1-v1"


def test_client_5xx_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "unavailable"})

    client = _client_with_handler(handler)
    with pytest.raises(DotMacERPTransientError):
        client.post("/sync/crm/expense-claims", {}, idempotency_key="k")


def test_client_4xx_is_permanent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "bad"})

    client = _client_with_handler(handler)
    with pytest.raises(DotMacERPError) as exc_info:
        client.post("/sync/crm/expense-claims", {})
    assert not isinstance(exc_info.value, DotMacERPTransientError)
    assert exc_info.value.status_code == 422


def test_client_401_is_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "nope"})

    client = _client_with_handler(handler)
    with pytest.raises(DotMacERPAuthError):
        client.post("/sync/crm/expense-claims", {})


def test_client_404_is_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    client = _client_with_handler(handler)
    with pytest.raises(DotMacERPNotFoundError):
        client.get("/sync/crm/expense-claims/x")


# ---------------------------------------------------------------------------
# Capability registration
# ---------------------------------------------------------------------------


def test_erp_settings_are_retired():
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    keys = {
        spec.key
        for spec in settings_spec.SETTINGS_SPECS
        if spec.domain == SettingDomain.integration
    }
    assert (
        not {
            "dotmac_erp_sync_enabled",
            "dotmac_erp_base_url",
            "dotmac_erp_token",
            "dotmac_erp_timeout_seconds",
            "dotmac_erp_max_retries",
        }
        & keys
    )


def test_erp_manifest_owns_config_secrets_and_capabilities():
    from app.services.integrations.registry import require_connector_definition

    definition = require_connector_definition("dotmac.erp")
    assert definition.config_schema["required"] == ["base_url"]
    assert {secret.name for secret in definition.secrets} == {
        "service_credentials",
        "webhook_signing_secret",
    }
    assert {capability.id for capability in definition.capabilities} == {
        "erp.outbox.deliver.v1",
        "erp.status.read.v1",
        "erp.inventory.read.v1",
        "erp.material_status.webhook.v1",
        "erp.operational_context.sync.v1",
        "erp.regulatory.read.v1",
        "workforce.attendance.read.v1",
        "workforce.attendance.punch.v1",
    }
    assert definition.version == "1.2.0"


def test_erp_capability_fails_closed_without_binding(db_session):
    from app.services.integrations import installations
    from app.services.integrations.erp_capability import capability_client

    with pytest.raises(installations.InstallationError, match="no enabled binding"):
        capability_client(db_session).get_ncc_staff_headcount()


# ---------------------------------------------------------------------------
# Task reliability contract + celery registration
# ---------------------------------------------------------------------------


def test_outbox_task_has_reliability_contract():
    from app.services.task_reliability import (
        TASK_RELIABILITY_CONTRACTS,
        Idempotency,
        RetryPolicy,
    )

    contract = TASK_RELIABILITY_CONTRACTS[
        "app.tasks.dotmac_erp_outbox.deliver_erp_sync_events"
    ]
    assert contract.domain == "integration"
    # Money-path outbox must not use blind autoretry.
    assert contract.retry_policy is not RetryPolicy.CELERY_AUTORETRY
    assert contract.idempotency is not Idempotency.NON_IDEMPOTENT


def test_outbox_task_registered_with_celery():
    import app.tasks  # noqa: F401
    from app.celery_app import celery_app

    assert "app.tasks.dotmac_erp_outbox.deliver_erp_sync_events" in celery_app.tasks
    assert (
        "app.tasks.dotmac_erp_outbox.refresh_purchase_invoice_statuses"
        in celery_app.tasks
    )


# ---------------------------------------------------------------------------
# Migration 249 — revision chain + single head
# ---------------------------------------------------------------------------


def _load_migration():
    path = REPO_ROOT / "alembic" / "versions" / "249_field_erp_sync_outbox.py"
    spec = importlib.util.spec_from_file_location("migration_249", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_249_revision_chain():
    module = _load_migration()
    assert module.revision == "249_field_erp_sync_outbox"
    assert module.down_revision == "248_maps_vendor_route_domain"
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_single_alembic_head():
    # PR 3 (material-request ERP fields, revision 250) advances the head off 249.
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"Expected exactly one Alembic head, found {len(heads)}: {heads}. "
        "Two branches almost certainly numbered their migrations off the same "
        "parent -- renumber the later one onto the current head."
    )


def test_migration_249_adds_integration_settingdomain():
    source = (
        REPO_ROOT / "alembic" / "versions" / "249_field_erp_sync_outbox.py"
    ).read_text()
    assert "ADD VALUE IF NOT EXISTS 'integration'" in source


def test_outbox_tables_registered_on_metadata():
    from app.db import Base

    assert "field_erp_sync_events" in Base.metadata.tables
    assert "sync_flow_ownership" in Base.metadata.tables
