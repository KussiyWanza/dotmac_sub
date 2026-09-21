from app.services.integrations.backoffice_contracts import (
    ERP_EXPENSE_FORM_CAPABILITY,
    ERP_STATUS_CAPABILITY,
)
from app.services.integrations.connectors.dotmac_erp import DotmacErpRunner


class _Client:
    def get_expense_approvers(self, requested_by_email):
        return [
            {
                "employee_id": "00000000-0000-0000-0000-000000000001",
                "display_name": "Approver",
                "email": requested_by_email,
            }
        ]

    def get_expense_banks(self):
        return [{"bank_code": "058", "bank_name": "Test Bank"}]

    def get_expense_profile_destination(self, requested_by_email):
        return {"available": True, "beneficiary_name": requested_by_email}

    def verify_expense_destination(self, payload):
        return {"destination_token": "enc:opaque", **payload}

    def get_material_request_status(self, source_request_id):
        return {"source_request_id": source_request_id}


def test_expense_form_operations_are_routed_only_through_new_capability() -> None:
    runner = DotmacErpRunner()
    client = _Client()
    output = runner._execute_action(
        client,  # type: ignore[arg-type]
        capability_id=ERP_EXPENSE_FORM_CAPABILITY,
        action="list_expense_approvers",
        params={"requested_by_email": "manager@example.com"},
        idempotency_key="unused",
    )
    assert output["items"][0]["email"] == "manager@example.com"


def test_existing_status_routing_is_unchanged() -> None:
    output = DotmacErpRunner()._execute_action(
        _Client(),  # type: ignore[arg-type]
        capability_id=ERP_STATUS_CAPABILITY,
        action="material_request_status",
        params={"source_request_id": "material-1"},
        idempotency_key="unused",
    )
    assert output == {"item": {"source_request_id": "material-1"}}
