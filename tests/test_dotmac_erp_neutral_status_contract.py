from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from app.services.dotmac_erp.client import DotMacERPClient
from app.services.integrations.backoffice_contracts import (
    ERP_STAFF_ACCESS_RECONCILE_CAPABILITY,
    ERP_STATUS_CAPABILITY,
)
from app.services.integrations.connectors.dotmac_erp import DotmacErpRunner
from app.services.integrations.erp_capability import ErpCapabilityClient


@pytest.mark.parametrize(
    ("action", "parameter", "client_method"),
    (
        ("expense_claim_status", "source_claim_id", "get_expense_claim_status"),
        (
            "material_request_status",
            "source_request_id",
            "get_material_request_status",
        ),
    ),
)
def test_connector_status_operations_require_the_neutral_source_role(
    action: str,
    parameter: str,
    client_method: str,
) -> None:
    client = MagicMock(spec=DotMacERPClient)
    getattr(client, client_method).return_value = {"status": "submitted"}

    result = DotmacErpRunner(client_override=client)._execute_action(
        client,
        capability_id=ERP_STATUS_CAPABILITY,
        action=action,
        params={parameter: "source-1"},
        idempotency_key="status-1",
    )

    assert result == {"item": {"status": "submitted"}}
    getattr(client, client_method).assert_called_once_with("source-1")

    with pytest.raises(KeyError):
        DotmacErpRunner(client_override=client)._execute_action(
            client,
            capability_id=ERP_STATUS_CAPABILITY,
            action=action,
            params={"omni_id": "source-1"},
            idempotency_key="status-legacy",
        )


def test_status_capability_emits_only_neutral_parameters(monkeypatch) -> None:
    client = ErpCapabilityClient(MagicMock())
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute(
        _capability_id: str,
        action: str,
        params: dict[str, Any],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append((action, params))
        return {"item": None}

    monkeypatch.setattr(client, "_execute", execute)

    assert client.get_expense_claim_status("claim-1") is None
    assert client.get_material_request_status("request-1") is None
    assert calls == [
        ("expense_claim_status", {"source_claim_id": "claim-1"}),
        ("material_request_status", {"source_request_id": "request-1"}),
    ]


def test_regulatory_client_uses_only_neutral_sub_routes() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={})

    client = DotMacERPClient("https://erp.test", "secret", retries=0)
    client._client = httpx.Client(
        base_url="https://erp.test",
        transport=httpx.MockTransport(handler),
    )

    client.get_ncc_financials(year=2026)
    client.get_ncc_staff_headcount()

    assert paths == [
        "/api/v1/sync/sub/ncc/financials",
        "/api/v1/sync/sub/ncc/staff-headcount",
    ]


def test_staff_access_reconcile_uses_typed_neutral_projection_route() -> None:
    client = MagicMock(spec=DotMacERPClient)
    client.get_staff_access_projection.return_value = {
        "contract_version": "staff.access.projection.v1",
        "entity": "account_status",
        "items": [],
    }

    result = DotmacErpRunner(client_override=client)._execute_action(
        client,
        capability_id=ERP_STAFF_ACCESS_RECONCILE_CAPABILITY,
        action="read_staff_access_projection",
        params={"entity": "account_status", "limit": 500},
        idempotency_key="staff-access-reconcile",
    )

    assert result["contract_version"] == "staff.access.projection.v1"
    client.get_staff_access_projection.assert_called_once_with(
        entity="account_status",
        limit=500,
    )
