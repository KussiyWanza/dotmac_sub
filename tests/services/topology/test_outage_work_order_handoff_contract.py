"""Focused contract checks for shared-outage field work."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.dispatch import WorkOrderHeaderCreate
from app.schemas.network import InfrastructureWorkOrderIssueRequest
from app.services.field.work_order_kind import WorkOrderKind

ROOT = Path(__file__).resolve().parents[3]


def test_customer_work_order_still_requires_a_subscriber():
    with pytest.raises(ValidationError, match="subscriber_id"):
        WorkOrderHeaderCreate(title="Customer repair")


def test_infrastructure_request_has_no_customer_target():
    request = InfrastructureWorkOrderIssueRequest(
        title="Repair feeder",
        reason="Shared outage confirmed",
    )
    assert request.expected_scope_revision_sequence is None
    assert WorkOrderKind.INFRASTRUCTURE.value == "infrastructure"


def test_infrastructure_flow_has_one_typed_owner_and_no_adapter_commit():
    source = (ROOT / "app/web/admin/network_monitoring.py").read_text()
    route = source.split("def outages_issue_work_order", 1)[1].split("@router.", 1)[0]
    assert "issue_work_order(" in route
    assert "db.commit()" not in route


def test_migration_is_on_current_trunk_head():
    migration = (ROOT / "alembic/versions/612_shared_outage_work_orders.py").read_text()
    assert (
        'down_revision: str | None = "611_offer_versions_unique_version_number"'
        in migration
    )
    assert "outage_incident_work_order_links" in migration
