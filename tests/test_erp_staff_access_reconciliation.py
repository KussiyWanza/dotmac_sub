from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.schemas.erp_staff_access_webhook import ErpStaffAccessProjectionPage
from app.tasks import dotmac_erp_outbox


def _leave_page() -> ErpStaffAccessProjectionPage:
    return ErpStaffAccessProjectionPage.model_validate(
        {
            "contract_version": "staff.access.projection.v1",
            "entity": "leave_restriction",
            "items": [
                {
                    "entity": "leave_restriction",
                    "restriction_id": "0d3c9255-37c2-49b3-9236-afd48544c244",
                    "organization_id": "a64f60ea-ce11-4609-b2dc-dc35152cdfd5",
                    "employee_id": "dc8148ac-b5d5-43c2-a09d-f342b8204948",
                    "person_id": "40a71f76-77f3-42a5-9721-c4db0db8cc71",
                    "selfcare_user_id": "1a999a5c-4d89-448d-9f68-bc433886529e",
                    "leave_application_id": "707ba800-00b9-4d2a-96a8-4ff5a523c822",
                    "organization_timezone": "Africa/Lagos",
                    "effective_from": "2026-09-01",
                    "effective_until": "2026-09-03",
                    "status": "ACTIVE",
                    "source_leave_status": "APPROVED",
                    "version": 2,
                    "updated_at": "2026-09-03T01:30:00Z",
                }
            ],
        }
    )


def _account_page() -> ErpStaffAccessProjectionPage:
    return ErpStaffAccessProjectionPage.model_validate(
        {
            "contract_version": "staff.access.projection.v1",
            "entity": "account_status",
            "items": [
                {
                    "entity": "account_status",
                    "projection_id": "241c228c-a23e-4914-8f36-b6c4a510a225",
                    "organization_id": "a64f60ea-ce11-4609-b2dc-dc35152cdfd5",
                    "employee_id": "dc8148ac-b5d5-43c2-a09d-f342b8204948",
                    "person_id": "40a71f76-77f3-42a5-9721-c4db0db8cc71",
                    "selfcare_user_id": "1a999a5c-4d89-448d-9f68-bc433886529e",
                    "erp_employee_status": "ON_LEAVE",
                    "state": "ACTIVE",
                    "source_reason": "employee_status",
                    "ownership": "erp_employee_status",
                    "version": 3,
                    "updated_at": "2026-09-03T01:30:00Z",
                }
            ],
        }
    )


def test_reconcile_task_fetches_typed_erp_snapshot_and_enters_owner() -> None:
    db = MagicMock()
    session_context = MagicMock()
    session_context.__enter__.return_value = db
    session_context.__exit__.return_value = False
    pages = {
        "leave_restriction": _leave_page(),
        "account_status": _account_page(),
    }
    captured = []

    class _Client:
        def __init__(self, actual_db) -> None:
            assert actual_db is db

        def get_staff_access_projection(self, *, entity, limit):
            assert limit == 500
            return pages[entity]

    def _reconcile(actual_db, command):
        assert actual_db is db
        captured.append(command)
        return SimpleNamespace(
            leave_restrictions_seen=1,
            account_statuses_seen=1,
            applied=2,
            ignored=0,
        )

    with (
        patch(
            "app.services.db_session_adapter.db_session_adapter.session",
            return_value=session_context,
        ),
        patch(
            "app.services.integrations.erp_capability.ErpCapabilityClient",
            _Client,
        ),
        patch(
            "app.services.erp_staff_access.reconcile_staff_access_snapshot",
            side_effect=_reconcile,
        ),
        patch(
            "app.services.db_session_adapter.db_session_adapter.release_read_transaction"
        ) as release_read,
    ):
        result = dotmac_erp_outbox.reconcile_erp_staff_access.run()

    assert result == {
        "leave_restrictions_seen": 1,
        "account_statuses_seen": 1,
        "unmapped_seen": 0,
        "applied": 2,
        "ignored": 0,
    }
    release_read.assert_called_once_with(db)
    command = captured[0]
    assert command.leave_restrictions[0].effective_from.isoformat() == (
        "2026-08-31T23:00:00+00:00"
    )
    assert command.leave_restrictions[0].effective_until.isoformat() == (
        "2026-09-03T23:00:00+00:00"
    )
    assert command.account_statuses[0].account_status == "active"
