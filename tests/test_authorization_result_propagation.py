from __future__ import annotations

from uuid import UUID

from app.services.network import olt_api_operations
from app.services.network.ont_authorization_contracts import (
    OntAuthorizationAdmission,
    RequestAssignedOntAuthorization,
)
from app.services.owner_commands import CommandContext


def test_api_authorize_and_provision_returns_durable_command_result(monkeypatch):
    monkeypatch.setattr(
        "app.services.network.olt_api_operations.request_ont_authorization",
        lambda *args, **kwargs: OntAuthorizationAdmission(
            accepted=True,
            waiting=True,
            message="ONT authorization accepted.",
            operation_id=UUID("00000000-0000-0000-0000-000000000001"),
            dispatch_id=UUID("00000000-0000-0000-0000-000000000002"),
        ),
    )

    response = olt_api_operations.authorize_and_provision_ont(
        object(),
        RequestAssignedOntAuthorization.from_transport(
            context=CommandContext.system(
                actor="api",
                scope="network:ont:authorize",
                reason="test API authorization projection",
            ),
            ont_id="00000000-0000-0000-0000-000000000003",
            olt_id="00000000-0000-0000-0000-000000000004",
            fsp="0/1/1",
            serial_number="HWTCWARNQUEUE",
        ),
    )

    assert response.success is True
    assert response.message == "ONT authorization accepted."
    assert response.operation_id == UUID("00000000-0000-0000-0000-000000000001")
    assert response.dispatch_id == UUID("00000000-0000-0000-0000-000000000002")
    assert response.waiting is True
    assert response.duplicate is False
