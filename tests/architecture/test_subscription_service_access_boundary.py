"""Architecture guards for subscription router and primary-IPv4 moves."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/subscription_nas_assignment.py"
IP_OWNER = ROOT / "app/services/ip_assignment_lifecycle.py"
WORKFLOW = ROOT / "app/services/web_catalog_subscription_workflows.py"
BULK = ROOT / "app/services/web_provisioning_migration.py"
FORM = ROOT / "templates/admin/catalog/subscription_form.html"
CATALOG_ROUTES = ROOT / "app/web/admin/catalog.py"
RBAC_SEED = ROOT / "scripts/seed/seed_rbac.py"
ADDITIONAL_IP_MIGRATION = (
    ROOT / "alembic/versions/542_subscription_additional_ip_permission.py"
)


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


def test_pool_link_backfill_excludes_demo_and_uses_active_service_evidence() -> None:
    migration = (
        ROOT / "alembic/versions/521_backfill_nas_radius_pool_links.py"
    ).read_text(encoding="utf-8")

    assert "lower(trim(p.name)) <> 'demo'" in migration
    assert "a.is_active IS TRUE" in migration
    assert "s.provisioning_nas_device_id IS NOT NULL" in migration
    assert 'f"radius_pool:{pool_id}"' in migration


def test_additional_ip_action_has_a_dedicated_ui_assignable_permission() -> None:
    migration = ADDITIONAL_IP_MIGRATION.read_text(encoding="utf-8")
    routes = CATALOG_ROUTES.read_text(encoding="utf-8")
    form = FORM.read_text(encoding="utf-8")
    seed = RBAC_SEED.read_text(encoding="utf-8")

    permission = "subscription:additional_ip:write"
    assert permission in migration
    assert "is_ui_assignable = true" in migration
    assert "lower(trim(r.name)) = 'noc'" in migration
    assert permission in seed
    assert '"/subscriptions/{subscription_id}/additional-ip"' in routes
    assert "require_permission(ADDITIONAL_IP_WRITE_PERMISSION)" in routes
    assert "handle_subscription_additional_ip_form" in routes
    assert "Save additional IPs" in form


def test_additional_ip_permission_does_not_open_generic_subscription_update() -> None:
    routes = CATALOG_ROUTES.read_text(encoding="utf-8")
    marker = '@router.post(\n    "/subscriptions/{subscription_id}/edit"'
    generic_update = routes[routes.index(marker) :]
    generic_update = generic_update[: generic_update.index("\n\n@router.post", 1)]

    assert 'require_permission("catalog:write")' in generic_update
    assert "ADDITIONAL_IP_WRITE_PERMISSION" not in generic_update
