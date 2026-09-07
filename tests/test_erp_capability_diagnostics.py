"""Application-boundary ERP diagnostic and heartbeat propagation."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.dotmac_erp.client import DotMacERPError
from app.services.dotmac_erp.operational_contracts import ErpOperationalSyncCommand
from app.services.integrations.diagnostics import safe_diagnostic
from app.services.integrations.erp_capability import ErpCapabilityClient
from app.services.integrations.runtime import OperationResult, OperationStatus


def test_facade_preserves_typed_failure_evidence(monkeypatch):
    first = OperationResult(
        operation_id=uuid4(),
        status=OperationStatus.rejected,
        diagnostic=safe_diagnostic(status=403),
        error_code="permission_denied",
    )
    monkeypatch.setattr(
        "app.services.integrations.erp_capability.installations.require_enabled_capability_binding",
        lambda *a, **k: MagicMock(),
    )
    monkeypatch.setattr(
        "app.services.integrations.erp_capability.build_execution_context",
        lambda *a, **k: MagicMock(),
    )
    monkeypatch.setattr(
        "app.services.integrations.erp_capability.make_operation_executor",
        lambda *a, **k: lambda *a, **k: first,
    )
    with pytest.raises(DotMacERPError) as caught:
        ErpCapabilityClient(MagicMock()).sync_operational_domains(
            ErpOperationalSyncCommand()
        )
    assert caught.value.diagnostic == first.diagnostic
    assert caught.value.status_code == 403


def test_blocked_task_completion_is_not_a_success_heartbeat(monkeypatch):
    from app.services.dotmac_erp.operational_contracts import OperationalSyncRunOutcome
    from app.services.observability import record_celery_task_success

    record = MagicMock()
    monkeypatch.setattr("app.services.job_heartbeat.record_success", record)
    result = OperationalSyncRunOutcome(
        projects=0, tickets=0, project_tasks=0, work_orders=0, status="blocked"
    )
    record_celery_task_success(
        "app.tasks.dotmac_erp_outbox.sync_erp_operational_domains",
        result=result.model_dump(mode="json"),
    )
    record.assert_not_called()
