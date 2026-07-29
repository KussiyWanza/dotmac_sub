from pathlib import Path

from app.services import sot_relationships

ROOT = Path(__file__).resolve().parents[2]


def test_customer_device_compatibility_wrappers_are_retired() -> None:
    flow = (ROOT / "app/services/customer_portal_flow_services.py").read_text()
    routes = (ROOT / "app/web/customer/routes.py").read_text()

    assert "def reboot_customer_subscription_ont(" not in flow
    assert "def update_customer_subscription_wifi(" not in flow
    assert "reboot_subscription_device(" in routes
    assert "update_subscription_wifi(" in routes


def test_operator_reconciliation_is_a_thin_reviewed_adapter() -> None:
    route = (ROOT / "app/web/admin/service_change_reconciliation.py").read_text()
    owner = (ROOT / "app/services/subscription_change_execution.py").read_text()

    assert 'PERMISSION = "provisioning:service_change_reconcile"' in route
    assert "inspect_execution_chain_reconciliation(" in route
    assert "reconcile_execution_chain(" in route
    assert "expected_head: str = Form(" in route
    assert "idempotency_key: str = Form(" in route
    assert "SubscriptionChangeRequest(" not in route
    assert "reconciliation_idempotency_key_hash" in owner
    assert "reconciliation_head_stale" in owner


def test_customer_detail_remote_plan_action_delegates_to_execution_owner() -> None:
    route = (ROOT / "app/web/admin/customers.py").read_text()
    template = (ROOT / "templates/admin/customers/detail.html").read_text()
    owner = (ROOT / "app/services/subscription_change_execution.py").read_text()

    assert "provision_and_verify_remote_change(" in route
    assert 'require_permission("provisioning:service_change_reconcile")' in route
    assert "SubscriptionChangeRequest(" not in route
    assert "reconcile_subscription_connectivity(" in owner
    assert "finalize_verified_remote_reprovision(" in owner
    assert "Provision and verify now" in template
    assert "can_reconcile_service_changes" in template
    assert "remote-plan-change:" in template


def test_operator_reconciliation_permission_is_seeded_admin_only() -> None:
    from scripts.seed.seed_rbac import ADMIN_ONLY_PERMISSION_KEYS, DEFAULT_PERMISSIONS

    seeded = {key for key, _description in DEFAULT_PERMISSIONS}
    permission = "provisioning:service_change_reconcile"
    assert permission in seeded
    assert permission in ADMIN_ONLY_PERMISSION_KEYS


def test_execution_owner_registry_covers_reconciliation() -> None:
    service = sot_relationships.service_relationship(
        "service_intent.subscription_change_execution"
    )
    assert service is not None
    assert "interrupted execution-chain reconciliation" in service.owns


def test_reconciliation_evidence_migration_extends_current_head() -> None:
    migration = (
        ROOT / "alembic/versions/403_service_change_reconciliation_evidence.py"
    ).read_text()
    assert 'down_revision = "402_remote_reprovision_verification"' in migration
    assert "reconciliation_idempotency_key_hash" in migration
    assert "reconciliation_reviewed_head" in migration
    assert "reconciled_at" in migration
