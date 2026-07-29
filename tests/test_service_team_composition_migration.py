from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/440_composable_service_teams.py"
)
MIGRATION_426 = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/426_service_team_lifecycle.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_440_composable_service_teams",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_is_forward_from_current_head_and_keeps_426_immutable():
    migration = _load_migration()
    source = MIGRATION.read_text(encoding="utf-8")
    source_426 = MIGRATION_426.read_text(encoding="utf-8")

    assert migration.revision == "440_composable_service_teams"
    assert migration.down_revision == "439_billing_obligation_rating_provenance"
    assert 'revision = "426_service_team_lifecycle"' in source_426
    assert 'down_revision = "425_vendor_project_intake_evidence"' in source_426
    assert "op.drop_column" not in source
    assert 'op.alter_column("service_teams", "team_type", nullable=True)' in source
    assert '"service_team_members",\n        "role",\n        nullable=True,' in source
    assert "server_default=None" in source
    assert '"ux_service_team_global_scope"' in source
    assert '"ux_service_team_unscoped_route_priority"' in source


def test_migration_adds_every_composable_relation_and_only_seeds_vocabulary():
    migration = _load_migration()
    source = MIGRATION.read_text(encoding="utf-8")

    for table in (
        "service_team_capability_definitions",
        "service_team_capabilities",
        "service_team_member_responsibilities",
        "service_team_relationships",
        "service_team_scope_bindings",
        "service_team_external_references",
        "service_team_routing_policies",
    ):
        assert f'"{table}"' in source

    backfilled_capabilities = {
        capability
        for capabilities in migration.LEGACY_TYPE_CAPABILITIES.values()
        for capability in capabilities
    }
    assert backfilled_capabilities <= {
        item[0] for item in migration.CAPABILITY_DEFINITIONS
    }
    assert "INSERT INTO service_teams" not in source
    assert "INSERT INTO system_users" not in source
    assert "INSERT INTO parties" not in source


def test_routing_backfill_is_legacy_derived_continuity_only():
    """Routing rows come only from legacy scalar authority, never invented.

    The continuity backfill replicates the retired oldest-active-team-of-type
    outage selection so incidents keep an owner at cutover. It must be guarded
    so operator-configured routes are never overridden.
    """

    migration = _load_migration()
    source = MIGRATION.read_text(encoding="utf-8")

    routing_inserts = source.count("INSERT INTO service_team_routing_policies")
    assert routing_inserts == 1
    assert "NOT EXISTS" in source
    assert "WHERE teams.team_type = :legacy_type" in source
    assert {route_key for route_key, _ in migration.LEGACY_OUTAGE_ROUTES} == {
        "incident.primary",
        "incident.support_watcher",
        "incident.field_watcher",
    }


def test_migration_is_forward_fix_only():
    migration = _load_migration()

    with pytest.raises(RuntimeError, match="forward-fix"):
        migration.downgrade()
