from __future__ import annotations

from pathlib import Path

from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_rule_definition_owner_is_fully_contracted() -> None:
    service = service_relationship("automation.rule_definitions")
    assert service.module == "app.services.automation_rules"
    assert service.is_contracted
    assert service.depends_on == ("automation.capability_registry",)


def test_rule_tables_are_tenant_isolated_and_versions_are_immutable() -> None:
    migration = _source("alembic/versions/613_automation_rule_core.py")
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "app_current_tenant_id()" in migration
    assert "automation_rule_versions_immutable" in migration
    assert "OLD.published_at IS NOT NULL" in migration


def test_permissions_are_assignable_but_not_seeded_to_non_admin_roles() -> None:
    seed = _source("scripts/seed/seed_rbac.py")
    for permission in (
        "automation:rule:read",
        "automation:rule:create",
        "automation:rule:update",
        "automation:rule:publish",
        "automation:rule:operate",
    ):
        assert permission in seed
    role_grants = seed[seed.index('"admin":') :]
    assert '"automation:rule:publish"' not in role_grants


def test_rule_owner_does_not_write_legacy_rule_tables() -> None:
    source = _source("app/services/automation_rules.py")
    for legacy_table in (
        "TicketAssignmentRule",
        "AlertRule",
        "FupRule",
        "InboxAutomationRule",
        "NasConnectionRule",
        "DispatchRule",
    ):
        assert legacy_table not in source
