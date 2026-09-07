"""Sanitized ERP transport evidence and facade compatibility."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from app.services.dotmac_erp.client import (
    DotMacERPAuthError,
    DotMacERPClient,
    DotMacERPError,
    DotMacERPRateLimitError,
    DotMacERPTransientError,
)
from app.services.dotmac_erp.operational_contracts import ErpOperationalSyncCommand
from app.services.integrations.backoffice_contracts import (
    ERP_OPERATIONAL_SYNC_CAPABILITY,
)
from app.services.integrations.connectors.dotmac_erp import DotmacErpRunner
from app.services.integrations.diagnostics import safe_diagnostic
from app.services.integrations.runtime import (
    OperationEnvelope,
    OperationStatus,
    OperationTrigger,
)


@pytest.mark.parametrize(
    "body",
    [
        {
            "detail": {
                "code": "permission_denied",
                "message": "private-token",
                "input": "customer-secret",
            }
        },
        {"error": {"code": "permission_denied", "message": "private-token"}},
        {"code": "permission_denied", "message": "customer-secret"},
        {"detail": [{"msg": "private-token", "input": "customer-secret"}]},
        {"detail": "private-token"},
        "<html>customer-secret</html>",
        None,
    ],
)
def test_safe_error_shapes_never_copy_provider_text(body):
    diagnostic = safe_diagnostic(status=403, body=body)
    assert diagnostic.http_status == 403
    assert diagnostic.code == "permission_denied"
    assert "private-token" not in diagnostic.model_dump_json()
    assert "customer-secret" not in diagnostic.model_dump_json()


@pytest.mark.parametrize(
    "status,error_type",
    [
        (401, DotMacERPAuthError),
        (403, DotMacERPAuthError),
        (422, DotMacERPError),
        (503, DotMacERPTransientError),
    ],
)
def test_http_error_classification_does_not_log_raw_body(status, error_type, caplog):
    client = DotMacERPClient("https://erp.invalid", "test-credential")
    with pytest.raises(error_type) as caught:
        client._handle_response(
            httpx.Response(status, json={"detail": [{"input": "sensitive-value"}]})
        )
    assert caught.value.status_code == status
    assert caught.value.response is None
    assert "sensitive-value" not in str(caught.value)
    assert "sensitive-value" not in caplog.text


@pytest.mark.parametrize(
    "header", ["bad-date", "-3", "999999999999", "Wed, 01 Jan 2020 00:00:00 GMT"]
)
def test_retry_after_is_bounded_and_bad_headers_do_not_change_classification(header):
    client = DotMacERPClient("https://erp.invalid", "test-credential")
    with pytest.raises(DotMacERPRateLimitError) as caught:
        client._handle_response(httpx.Response(429, headers={"Retry-After": header}))
    assert 1 <= caught.value.retry_after <= 86400


def _envelope() -> OperationEnvelope:
    return OperationEnvelope(
        operation_id=uuid4(),
        correlation_id="stable-delivery-key",
        installation_id=uuid4(),
        capability_binding_id=uuid4(),
        capability_id=ERP_OPERATIONAL_SYNC_CAPABILITY,
        connector_key="dotmac.erp",
        connector_version="1.0.0",
        manifest_digest="a" * 64,
        config_revision_id=uuid4(),
        trigger=OperationTrigger.scheduled,
        idempotency_key="stable-delivery-key",
        deadline_at=datetime.now(UTC) + timedelta(seconds=60),
        payload={
            "action": "sync_operational_domains",
            "params": {"payload": ErpOperationalSyncCommand().model_dump(mode="json")},
        },
    )


def test_connector_preserves_status_ids_and_unique_correlations():
    client = DotMacERPClient("https://erp.invalid", "test-credential", retries=0)

    def denied(request):
        return httpx.Response(403, json={"detail": "private-token"})

    client._client = httpx.Client(
        base_url=client.base_url, transport=httpx.MockTransport(denied)
    )
    runner = DotmacErpRunner(client_override=client)
    envelope = _envelope()
    first = runner.execute(envelope, config={}, secret_material={})
    second = runner.execute(envelope, config={}, secret_material={})
    assert first.status == OperationStatus.rejected
    assert first.diagnostic.operation_id == envelope.operation_id
    assert first.diagnostic.operation == "sync_operational_domains"
    assert first.diagnostic.correlation_id != second.diagnostic.correlation_id
    assert first.diagnostic.request_id != second.diagnostic.request_id
    assert first.diagnostic.http_status == 403
    client.close()


def test_success_v2_contract_and_unique_http_request_ids_are_preserved():
    requests = []

    def accepted(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "contract_version": 2,
                "projects_synced": 0,
                "tickets_synced": 0,
                "project_tasks_synced": 0,
                "work_orders_synced": 0,
                "errors": [],
            },
        )

    client = DotMacERPClient("https://erp.invalid", "test-credential", retries=0)
    client._client = httpx.Client(
        base_url=client.base_url, transport=httpx.MockTransport(accepted)
    )
    first = client.sync_operational_domains(ErpOperationalSyncCommand())
    second = client.sync_operational_domains(ErpOperationalSyncCommand())
    assert first == second
    assert first.contract_version == 2
    assert requests[0].headers["X-Request-ID"] != requests[1].headers["X-Request-ID"]
    assert requests[0].url.path == "/api/v1/sync/sub/bulk"
    assert "Idempotency-Key" not in requests[0].headers
    client.close()


def test_rate_limit_returns_immediately_without_sleeping_with_database_lock(
    monkeypatch,
):
    sleep = MagicMock()
    monkeypatch.setattr("dotmac_integration_client.http.time.sleep", sleep)
    client = DotMacERPClient("https://erp.invalid", "test-credential", retries=3)
    client._client = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "3600"})
        ),
    )
    result = DotmacErpRunner(client_override=client).execute(
        _envelope(), config={}, secret_material={}
    )
    assert result.status == OperationStatus.retryable
    assert result.retry_after_seconds == 3600
    assert result.diagnostic.retry_after_seconds == 3600
    assert result.diagnostic.http_status == 429
    sleep.assert_not_called()
    client.close()


def test_http_transient_retries_are_bounded(monkeypatch):
    sleep = MagicMock()
    monkeypatch.setattr("dotmac_integration_client.http.time.sleep", sleep)
    attempts = []

    def unavailable(request):
        attempts.append(request)
        return httpx.Response(503)

    client = DotMacERPClient("https://erp.invalid", "test-credential", retries=99)
    client._client = httpx.Client(
        base_url=client.base_url, transport=httpx.MockTransport(unavailable)
    )
    result = DotmacErpRunner(client_override=client).execute(
        _envelope(), config={}, secret_material={}
    )
    assert result.status == OperationStatus.retryable
    assert len(attempts) == 3
    assert sleep.call_count == 2
    client.close()


def test_invalid_success_contract_preserves_http_evidence_without_validation_input():
    client = DotMacERPClient("https://erp.invalid", "test-credential", retries=0)
    client._client = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"contract_version": "customer-secret"}
            )
        ),
    )
    with pytest.raises(DotMacERPError) as caught:
        client.sync_operational_domains(ErpOperationalSyncCommand())
    assert caught.value.diagnostic.http_status == 200
    assert caught.value.diagnostic.code == "invalid_response"
    assert caught.value.diagnostic.request_id
    assert "customer-secret" not in str(caught.value)
    assert caught.value.__suppress_context__
    client.close()


def test_partial_erp_acceptance_is_rejected_with_request_evidence():
    client = DotMacERPClient("https://erp.invalid", "test-credential", retries=0)
    client._client = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "contract_version": 2,
                    "projects_synced": 0,
                    "tickets_synced": 0,
                    "project_tasks_synced": 0,
                    "work_orders_synced": 0,
                    "errors": [
                        {
                            "entity_type": "project",
                            "source_reference": str(uuid4()),
                            "error": "sensitive-value",
                        }
                    ],
                },
            )
        ),
    )
    with pytest.raises(DotMacERPError) as caught:
        client.sync_operational_domains(ErpOperationalSyncCommand())
    assert caught.value.diagnostic.http_status == 200
    assert caught.value.diagnostic.code == "item_rejected"
    assert caught.value.diagnostic.request_id
    assert "sensitive-value" not in str(caught.value)
    client.close()
