"""Exercise the PostgreSQL migration boundary that introduces revisions 430-440."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine, make_url

from alembic import command
from app import config as app_config

ROOT = Path(__file__).resolve().parents[2]
REVISION_423 = "423_prepaid_opening_funding_reconciliation"
REVISION_437 = "437_add_pon_port_admin_enabled"

TABLES_430_TO_440 = (
    "billing_contracts",
    "billing_contract_versions",
    "billing_contract_lines",
    "billing_obligations",
    "customer_posting_groups",
    "customer_position_effects",
    "owner_output_receipts",
    "durable_timers",
    "collections_cases",
    "sales_order_funding_gates",
    "sales_order_funding_obligations",
    "erp_billing_exports",
    "billing_shadow_delivery_evidence",
    "billing_cutover_verification_runs",
    "service_team_capability_definitions",
    "service_team_capabilities",
    "service_team_member_responsibilities",
    "service_team_relationships",
    "service_team_scope_bindings",
    "service_team_external_references",
    "service_team_routing_policies",
)

ENUMS_430_TO_434 = (
    "billingrecordauthority",
    "billingcontractsourcekind",
    "billingcontractversionstatus",
    "ratebasis",
    "intervalunit",
    "collectiontiming",
    "cadencealignment",
    "endofmonthrule",
    "prorationpolicy",
    "chargecomponent",
    "accountingtreatment",
    "obligationstate",
    "obligationresolutionkind",
    "postingcommandkind",
    "positioneffectkind",
    "receiptoutcome",
    "timerstatus",
    "collectionsreason",
    "collectionscasestate",
    "fundinggatestate",
    "erpbillingflow",
    "erpexportstatus",
)


def _render_url(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _psycopg_url(url: URL) -> str:
    return _render_url(url.set(drivername="postgresql"))


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Satisfy the integration-package guard without creating current schema.

    This module owns a separate disposable database and drives it exclusively
    through Alembic. The package engine fixture calls ``create_all()``, which
    would bypass the migration path under test and require PostGIS before the
    isolated database exists.
    """

    configured_url = os.getenv("TEST_DATABASE_URL")
    if not configured_url:
        pytest.skip("migration-path test requires TEST_DATABASE_URL")
    database_url = make_url(configured_url)
    if not database_url.drivername.startswith("postgresql"):
        pytest.skip("migration-path test requires PostgreSQL")
    test_engine = create_engine(database_url)
    try:
        yield test_engine
    finally:
        test_engine.dispose()


@pytest.fixture
def isolated_migration_database() -> Iterator[URL]:
    configured_url = os.getenv("TEST_DATABASE_URL")
    if not configured_url:
        pytest.skip("migration-path test requires TEST_DATABASE_URL")
    base_url = make_url(configured_url)
    if not base_url.drivername.startswith("postgresql"):
        pytest.skip("migration-path test requires PostgreSQL")

    database_name = f"dotmac_migration_{uuid4().hex}"
    maintenance_url = base_url.set(database="postgres")
    with psycopg.connect(_psycopg_url(maintenance_url), autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )

    try:
        yield base_url.set(database=database_name)
    finally:
        with psycopg.connect(
            _psycopg_url(maintenance_url),
            autocommit=True,
        ) as admin:
            admin.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s
                  AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )


def _alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _revision_rows(database_url: URL) -> set[str]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return set(
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalars()
            )
    finally:
        engine.dispose()


def _restore_pre_430_shape(database_url: URL) -> None:
    """Undo current-model objects created by the squashed initial migration.

    Revision 001 builds ``Base.metadata`` from the current checkout, so even an
    upgrade stopping at 423 contains later model tables. Removing only the
    objects introduced by 430-434 recreates the production boundary while
    preserving the real revision-423 stamp and all earlier schema.
    """

    with psycopg.connect(_psycopg_url(database_url), autocommit=True) as connection:
        for table_name in reversed(TABLES_430_TO_440):
            connection.execute(
                sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                    sql.Identifier(table_name)
                )
            )
        for enum_name in reversed(ENUMS_430_TO_434):
            connection.execute(
                sql.SQL("DROP TYPE IF EXISTS {} CASCADE").format(
                    sql.Identifier(enum_name)
                )
            )


@dataclass(frozen=True)
class _LegacyServiceTeamRows:
    """Identifiers of the representative pre-440 rows staged at revision 437."""

    operations_team_id: UUID = field(default_factory=uuid4)
    support_team_id: UUID = field(default_factory=uuid4)
    #: Party referenced by ``manager_person_id`` whose membership is inactive.
    stale_manager_person_id: UUID = field(default_factory=uuid4)
    lead_person_id: UUID = field(default_factory=uuid4)
    manager_person_id: UUID = field(default_factory=uuid4)
    inactive_membership_id: UUID = field(default_factory=uuid4)
    lead_membership_id: UUID = field(default_factory=uuid4)
    manager_membership_id: UUID = field(default_factory=uuid4)


def _stage_legacy_service_teams_at_437(database_url: URL) -> _LegacyServiceTeamRows:
    """Insert legacy scalar-authority rows for revision 440's backfill to read."""

    rows = _LegacyServiceTeamRows()
    params = {
        "operations_team_id": rows.operations_team_id,
        "support_team_id": rows.support_team_id,
        "stale_manager_person_id": rows.stale_manager_person_id,
        "lead_person_id": rows.lead_person_id,
        "manager_person_id": rows.manager_person_id,
        "inactive_membership_id": rows.inactive_membership_id,
        "lead_membership_id": rows.lead_membership_id,
        "manager_membership_id": rows.manager_membership_id,
    }
    with psycopg.connect(_psycopg_url(database_url), autocommit=True) as connection:
        # The continuity-route seed picks the oldest active team per legacy
        # type and skips routes with no matching team; these two teams (and no
        # field_service team) must be the only teams present.
        assert connection.execute("SELECT count(*) FROM service_teams").fetchone() == (
            0,
        )
        connection.execute(
            """
            INSERT INTO parties
                (id, party_type, display_name, status, data_classification,
                 created_at, updated_at)
            VALUES
                (%(stale_manager_person_id)s, 'person', 'Stale manager party',
                 'active', 'test', now(), now()),
                (%(lead_person_id)s, 'person', 'Lead party',
                 'active', 'test', now(), now()),
                (%(manager_person_id)s, 'person', 'Manager party',
                 'active', 'test', now(), now())
            """,
            params,
        )
        connection.execute(
            """
            INSERT INTO service_teams
                (id, name, team_type, region, manager_person_id,
                 workforce_system, workforce_department_reference,
                 is_active, created_at, updated_at)
            VALUES
                (%(operations_team_id)s, 'Legacy operations Abuja', 'operations',
                 'Abuja', %(stale_manager_person_id)s, 'MixedCase', 'dept-7',
                 TRUE, TIMESTAMPTZ '2001-01-01 00:00:00+00',
                 TIMESTAMPTZ '2001-01-03 00:00:00+00'),
                (%(support_team_id)s, 'Legacy support Lagos', 'support',
                 NULL, NULL, NULL, NULL,
                 TRUE, TIMESTAMPTZ '2001-01-02 00:00:00+00',
                 TIMESTAMPTZ '2001-01-03 00:00:00+00')
            """,
            params,
        )
        connection.execute(
            """
            INSERT INTO service_team_members
                (id, team_id, person_id, role, is_active, created_at)
            VALUES
                (%(inactive_membership_id)s, %(operations_team_id)s,
                 %(stale_manager_person_id)s, 'member', FALSE, now()),
                (%(lead_membership_id)s, %(support_team_id)s,
                 %(lead_person_id)s, 'lead', TRUE, now()),
                (%(manager_membership_id)s, %(operations_team_id)s,
                 %(manager_person_id)s, 'manager', TRUE, now())
            """,
            params,
        )
    return rows


def _assert_440_backfill_translated_legacy_rows(
    engine: Engine, rows: _LegacyServiceTeamRows
) -> None:
    with engine.connect() as connection:
        # Legacy `operations` teams owned outages, so both capabilities bind.
        capabilities = set(
            connection.execute(
                text(
                    "SELECT capability_key FROM service_team_capabilities "
                    "WHERE team_id = :team_id AND is_active IS TRUE"
                ),
                {"team_id": rows.operations_team_id},
            ).scalars()
        )
        assert capabilities == {"network_operations", "outage_response"}

        def active_responsibilities(membership_id: UUID) -> set[str]:
            return set(
                connection.execute(
                    text(
                        "SELECT responsibility_key "
                        "FROM service_team_member_responsibilities "
                        "WHERE membership_id = :membership_id "
                        "AND is_active IS TRUE"
                    ),
                    {"membership_id": membership_id},
                ).scalars()
            )

        assert active_responsibilities(rows.lead_membership_id) == {
            "agent",
            "queue_lead",
        }
        assert active_responsibilities(rows.manager_membership_id) == {
            "agent",
            "accountable_manager",
        }

        # The stale manager pointer must not resurrect the deactivated
        # membership: no reactivation, no duplicate row, no responsibilities.
        memberships = connection.execute(
            text(
                "SELECT id, is_active FROM service_team_members "
                "WHERE team_id = :team_id AND person_id = :person_id"
            ),
            {
                "team_id": rows.operations_team_id,
                "person_id": rows.stale_manager_person_id,
            },
        ).all()
        assert memberships == [(rows.inactive_membership_id, False)]
        responsibility_count = connection.execute(
            text(
                "SELECT count(*) FROM service_team_member_responsibilities "
                "WHERE membership_id = :membership_id"
            ),
            {"membership_id": rows.inactive_membership_id},
        ).scalar_one()
        assert responsibility_count == 0

        # Workforce identifiers become provider-neutral observations with a
        # casefolded provider name.
        references = connection.execute(
            text(
                "SELECT provider, account_scope, external_id, is_active "
                "FROM service_team_external_references "
                "WHERE team_id = :team_id"
            ),
            {"team_id": rows.operations_team_id},
        ).all()
        assert references == [("mixedcase", "default", "dept-7", True)]

        # Continuity routes are seeded only where a matching legacy team
        # exists: primary + support watcher, and no field watcher because no
        # field_service team was staged.
        routes = dict(
            connection.execute(
                text(
                    "SELECT route_key, team_id FROM service_team_routing_policies "
                    "WHERE domain = 'network.outage' AND is_active IS TRUE"
                )
            ).all()
        )
        assert routes == {
            "incident.primary": rows.operations_team_id,
            "incident.support_watcher": rows.support_team_id,
        }


def test_postgres_upgrades_revision_423_through_current_head(
    isolated_migration_database: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = isolated_migration_database
    monkeypatch.setattr(
        app_config,
        "settings",
        replace(app_config.settings, database_url=_render_url(database_url)),
    )
    config = _alembic_config()

    command.upgrade(config, REVISION_423)
    assert _revision_rows(database_url) == {REVISION_423}
    _restore_pre_430_shape(database_url)
    assert _revision_rows(database_url) == {REVISION_423}

    # Stop at the revision-437 boundary and stage legacy scalar-authority rows
    # so revision 440's backfill runs against representative production shapes.
    command.upgrade(config, REVISION_437)
    assert _revision_rows(database_url) == {REVISION_437}
    legacy_rows = _stage_legacy_service_teams_at_437(database_url)

    command.upgrade(config, "head")

    expected_heads = set(ScriptDirectory.from_config(config).get_heads())
    assert _revision_rows(database_url) == expected_heads
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert set(TABLES_430_TO_440) <= table_names
        verification_columns = {
            column["name"]
            for column in inspector.get_columns("billing_cutover_verification_runs")
        }
        assert {
            "expected_difference_count",
            "gap_count",
            "overlap_count",
        } <= verification_columns
        obligation_columns = {
            column["name"] for column in inspector.get_columns("billing_obligations")
        }
        assert {
            "rating_provenance_complete",
            "rating_policy_version",
            "rating_coverage_start",
            "rating_coverage_end",
            "rating_unit_price",
            "rating_quantity",
            "rating_rate_basis",
            "rating_rate_unit",
            "rating_rate_quantity",
            "rating_timezone_name",
            "rating_proration_policy",
            "rating_rate_units",
            "rating_proration_factor",
            "rating_tax_treatment_code",
            "rating_tax_rate_id",
            "rating_tax_rate_percent",
            "rating_tax_inclusive",
            "rating_input_fingerprint",
        } <= obligation_columns
        obligation_foreign_keys = {
            foreign_key["name"]
            for foreign_key in inspector.get_foreign_keys("billing_obligations")
        }
        assert "fk_billing_obligation_rating_tax_rate" in obligation_foreign_keys

        with engine.connect() as connection:
            enum_names = list(
                connection.execute(
                    text(
                        """
                        SELECT type.typname
                        FROM pg_type AS type
                        JOIN pg_namespace AS namespace
                          ON namespace.oid = type.typnamespace
                        WHERE namespace.nspname = current_schema()
                          AND type.typtype = 'e'
                        """
                    )
                ).scalars()
            )
        for enum_name in ENUMS_430_TO_434:
            assert enum_names.count(enum_name) == 1

        _assert_440_backfill_translated_legacy_rows(engine, legacy_rows)
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    assert _revision_rows(database_url) == expected_heads
