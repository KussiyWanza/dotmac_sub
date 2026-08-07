"""Architecture guards for subscription router and primary-IPv4 moves."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/subscription_nas_assignment.py"
IP_OWNER = ROOT / "app/services/ip_assignment_lifecycle.py"
WORKFLOW = ROOT / "app/services/web_catalog_subscription_workflows.py"
BULK = ROOT / "app/services/web_provisioning_migration.py"
FORM = ROOT / "templates/admin/catalog/subscription_form.html"


def test_ipv4_participants_are_private_to_service_access_coordinator() -> None:
    participant_names = (
        "apply_service_ipv4_assignment_participant",
        "apply_service_ipv4_projection_participant",
    )
    offenders: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        if path in {OWNER, IP_OWNER}:
            continue
        source = path.read_text()
        if any(name in source for name in participant_names):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_generic_edit_cannot_submit_a_router_move() -> None:
    workflow_source = WORKFLOW.read_text()
    form_source = FORM.read_text()
    assert (
        "Use Move service access to change the router and primary IPv4"
        in workflow_source
    )
    assert 'id="provisioning_nas_device_id" readonly' in form_source
    assert "/access/move" in form_source


def test_legacy_bulk_migration_cannot_write_router_or_address_pool() -> None:
    source = BULK.read_text()
    assert "Bulk router and IP-pool migration is retired" in source
    assert "current.provisioning_nas_device_id =" not in source
    assert "assignment.ipv4_address.pool_id =" not in source
    assert "assignment.ipv6_address.pool_id =" not in source
