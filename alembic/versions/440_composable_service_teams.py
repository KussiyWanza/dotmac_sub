"""Add composable service-team capabilities, scopes, and responsibilities.

Revision ID: 440_composable_service_teams
Revises: 437_add_pon_port_admin_enabled

This is the expand/backfill half of the service-team authority migration.
Migration 426 remains unchanged. Legacy scalar columns stay in place as
shadow-comparison inputs and are made nullable so new native records do not
invent a type or role. A later reviewed contract migration may remove them
only after the verification gate in the service-team design is satisfied.

Expected production volume is small (hundreds of teams and memberships).
DDL uses a five-second lock budget and a sixty-second statement budget. A lock
timeout is safe to retry from the beginning because every backfill is
constraint-arbitrated and idempotent. Failures are forward-fixed; downgrade
must not reconstruct scalar authority.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "440_composable_service_teams"
down_revision = "439_billing_obligation_rating_provenance"
branch_labels = None
depends_on = None


CAPABILITY_DEFINITIONS = (
    (
        "customer_support",
        "Customer support",
        "support.ticket_lifecycle",
        "Customer-facing support intake, triage, and case handling.",
    ),
    (
        "billing_operations",
        "Billing operations",
        "billing.account_lifecycle",
        "Billing review and customer-account financial operations.",
    ),
    (
        "field_service",
        "Field service",
        "operations.work_orders",
        "On-site installation, repair, and field execution.",
    ),
    (
        "project_delivery",
        "Project delivery",
        "operations.project_lifecycle",
        "Operational project planning and delivery.",
    ),
    (
        "network_operations",
        "Network operations",
        "network.topology",
        "Network operations and infrastructure coordination.",
    ),
    (
        "outage_response",
        "Outage response",
        "network.outage_lifecycle",
        "Incident ownership and outage-response coordination.",
    ),
)

LEGACY_TYPE_CAPABILITIES = {
    "support": ("customer_support",),
    "billing": ("billing_operations",),
    "field_service": ("field_service",),
    "project_management": ("project_delivery",),
    # Legacy operations teams owned outages, so they carry both capabilities.
    "operations": ("network_operations", "outage_response"),
}

# Continuity routes replicating the retired oldest-active-team-of-type
# selection, so outages keep an owner and watchers at cutover.
LEGACY_OUTAGE_ROUTES = (
    ("incident.primary", "operations"),
    ("incident.support_watcher", "support"),
    ("incident.field_watcher", "field_service"),
)

LEGACY_ROLE_RESPONSIBILITIES = {
    "member": ("agent",),
    "lead": ("agent", "queue_lead"),
    "manager": ("agent", "accountable_manager"),
}


def _create_capability_tables() -> None:
    op.create_table(
        "service_team_capability_definitions",
        sa.Column("key", sa.String(length=80), primary_key=True),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("contract_owner", sa.String(length=160), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    definition_table = sa.table(
        "service_team_capability_definitions",
        sa.column("key", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("contract_owner", sa.String()),
        sa.column("contract_version", sa.Integer()),
        sa.column("description", sa.Text()),
        sa.column("is_active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    # Timestamps are explicit: on squash-path databases this table is created
    # from model metadata without server defaults, so relying on the
    # server_default declared above raises NotNullViolation.
    seeded_at = datetime.now(UTC)
    op.bulk_insert(
        definition_table,
        [
            {
                "key": key,
                "display_name": display_name,
                "contract_owner": owner,
                "contract_version": 1,
                "description": description,
                "is_active": True,
                "created_at": seeded_at,
                "updated_at": seeded_at,
            }
            for key, display_name, owner, description in CAPABILITY_DEFINITIONS
        ],
    )
    op.create_table(
        "service_team_capabilities",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_teams.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "capability_key",
            sa.String(length=80),
            sa.ForeignKey(
                "service_team_capability_definitions.key",
                name="fk_service_team_capabilities_definition",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "team_id", "capability_key", name="uq_service_team_capability"
        ),
    )
    op.create_index(
        "ix_service_team_capabilities_key_active",
        "service_team_capabilities",
        ["capability_key", "is_active"],
    )


def _create_responsibility_and_topology_tables() -> None:
    op.create_table(
        "service_team_member_responsibilities",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column(
            "membership_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_team_members.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("responsibility_key", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "membership_id",
            "responsibility_key",
            name="uq_service_team_member_responsibility",
        ),
    )
    op.create_index(
        "ix_service_team_responsibilities_key_active",
        "service_team_member_responsibilities",
        ["responsibility_key", "is_active"],
    )
    op.create_table(
        "service_team_relationships",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column(
            "parent_team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_teams.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "child_team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_teams.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("relationship_kind", sa.String(length=40), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "parent_team_id <> child_team_id",
            name="ck_service_team_relationship_not_self",
        ),
        sa.UniqueConstraint(
            "parent_team_id",
            "child_team_id",
            "relationship_kind",
            name="uq_service_team_relationship",
        ),
    )


def _create_scope_reference_and_routing_tables() -> None:
    op.create_table(
        "service_team_scope_bindings",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_teams.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("scope_type", sa.String(length=40), nullable=False),
        sa.Column(
            "geo_area_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("geo_areas.id", ondelete="RESTRICT"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "(scope_type = 'global' AND geo_area_id IS NULL) OR "
            "(scope_type = 'geo_area' AND geo_area_id IS NOT NULL)",
            name="ck_service_team_scope_binding_target",
        ),
        sa.UniqueConstraint(
            "team_id",
            "scope_type",
            "geo_area_id",
            name="uq_service_team_scope_binding",
        ),
    )
    op.create_index(
        "ix_service_team_scope_geo_area_active",
        "service_team_scope_bindings",
        ["geo_area_id", "is_active"],
    )
    op.create_index(
        "ux_service_team_global_scope",
        "service_team_scope_bindings",
        ["team_id"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'global' AND is_active IS TRUE"),
    )
    op.create_table(
        "service_team_external_references",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_teams.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("account_scope", sa.String(length=120), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("provenance", sa.String(length=160), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "provider",
            "account_scope",
            "external_id",
            name="uq_service_team_external_provider_reference",
        ),
        sa.UniqueConstraint(
            "team_id",
            "provider",
            "account_scope",
            "external_id",
            name="uq_service_team_external_team_reference",
        ),
    )
    op.create_index(
        "ix_service_team_external_team_active",
        "service_team_external_references",
        ["team_id", "is_active"],
    )
    op.create_table(
        "service_team_routing_policies",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("domain", sa.String(length=80), nullable=False),
        sa.Column("route_key", sa.String(length=120), nullable=False),
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_teams.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "scope_binding_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_team_scope_bindings.id", ondelete="RESTRICT"),
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("priority >= 0", name="ck_service_team_routing_priority"),
        sa.UniqueConstraint(
            "domain",
            "route_key",
            "scope_binding_id",
            "priority",
            name="uq_service_team_routing_policy",
        ),
    )
    op.create_index(
        "ix_service_team_routing_domain_key_active",
        "service_team_routing_policies",
        ["domain", "route_key", "is_active"],
    )
    op.create_index(
        "ux_service_team_unscoped_route_priority",
        "service_team_routing_policies",
        ["domain", "route_key", "priority"],
        unique=True,
        postgresql_where=sa.text("scope_binding_id IS NULL AND is_active IS TRUE"),
    )


def _backfill_shadow_bindings(bind) -> None:
    for legacy_type, capability_keys in LEGACY_TYPE_CAPABILITIES.items():
        for capability_key in capability_keys:
            bind.execute(
                sa.text(
                    "INSERT INTO service_team_capabilities "
                    "(id, team_id, capability_key, is_active, created_at, updated_at) "
                    "SELECT gen_random_uuid(), id, :capability_key, TRUE, now(), now() "
                    "FROM service_teams WHERE team_type = :legacy_type "
                    "ON CONFLICT (team_id, capability_key) DO NOTHING"
                ),
                {"legacy_type": legacy_type, "capability_key": capability_key},
            )

    for legacy_role, responsibility_keys in LEGACY_ROLE_RESPONSIBILITIES.items():
        for responsibility_key in responsibility_keys:
            bind.execute(
                sa.text(
                    "INSERT INTO service_team_member_responsibilities "
                    "(id, membership_id, responsibility_key, is_active, "
                    "created_at, updated_at) "
                    "SELECT gen_random_uuid(), id, :responsibility_key, TRUE, "
                    "now(), now() FROM service_team_members "
                    "WHERE role = :legacy_role AND is_active IS TRUE "
                    "ON CONFLICT (membership_id, responsibility_key) DO NOTHING"
                ),
                {
                    "legacy_role": legacy_role,
                    "responsibility_key": responsibility_key,
                },
            )

    # manager_person_id is a legacy pointer, not a second manager authority.
    # Preserve its current meaning only as a responsibility on a membership.
    # DO NOTHING: a deliberately deactivated membership must stay deactivated
    # even when a stale manager pointer still references it.
    bind.execute(
        sa.text(
            "INSERT INTO service_team_members "
            "(id, team_id, person_id, role, is_active, created_at) "
            "SELECT gen_random_uuid(), id, manager_person_id, NULL, TRUE, now() "
            "FROM service_teams WHERE manager_person_id IS NOT NULL "
            "ON CONFLICT (team_id, person_id) DO NOTHING"
        )
    )
    bind.execute(
        sa.text(
            "INSERT INTO service_team_member_responsibilities "
            "(id, membership_id, responsibility_key, is_active, "
            "created_at, updated_at) "
            "SELECT gen_random_uuid(), members.id, 'accountable_manager', TRUE, "
            "now(), now() FROM service_team_members AS members "
            "JOIN service_teams AS teams "
            "ON teams.id = members.team_id "
            "AND teams.manager_person_id = members.person_id "
            "WHERE teams.manager_person_id IS NOT NULL "
            "AND members.is_active IS TRUE "
            "ON CONFLICT (membership_id, responsibility_key) DO NOTHING"
        )
    )
    bind.execute(
        sa.text(
            "INSERT INTO service_team_external_references "
            "(id, team_id, provider, account_scope, external_id, provenance, "
            "observed_at, is_active, created_at, updated_at) "
            "SELECT gen_random_uuid(), id, lower(workforce_system), 'default', "
            "workforce_department_reference, 'legacy_service_team_observation', "
            "COALESCE(updated_at, created_at, now()), TRUE, now(), now() "
            "FROM service_teams "
            "WHERE workforce_system IS NOT NULL "
            "AND workforce_department_reference IS NOT NULL "
            "ON CONFLICT (provider, account_scope, external_id) DO NOTHING"
        )
    )


def _seed_outage_continuity_routes(bind) -> None:
    """Replicate the retired oldest-active-team-of-type outage selection.

    Explicit routing replaces implicit selection; without these rows every
    outage would activate with no operational owner or watchers until an
    operator configures routes by hand. Idempotent: each route is seeded only
    when no policy (active or not) exists for it, so operator changes are
    never overridden on re-run.
    """

    for route_key, legacy_type in LEGACY_OUTAGE_ROUTES:
        bind.execute(
            sa.text(
                "INSERT INTO service_team_routing_policies "
                "(id, domain, route_key, team_id, scope_binding_id, priority, "
                "is_active, created_at, updated_at) "
                "SELECT gen_random_uuid(), 'network.outage', "
                "CAST(:route_key AS VARCHAR), teams.id, "
                "NULL, 100, TRUE, now(), now() "
                "FROM service_teams AS teams "
                "WHERE teams.team_type = :legacy_type "
                "AND teams.is_active IS TRUE "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM service_team_routing_policies AS existing "
                "  WHERE existing.domain = 'network.outage' "
                "  AND existing.route_key = CAST(:route_key AS VARCHAR)"
                ") "
                "ORDER BY teams.created_at ASC "
                "LIMIT 1"
            ),
            {"route_key": route_key, "legacy_type": legacy_type},
        )


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    bind.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    _create_capability_tables()
    _create_responsibility_and_topology_tables()
    _create_scope_reference_and_routing_tables()
    op.alter_column("service_teams", "team_type", nullable=True)
    op.alter_column(
        "service_team_members",
        "role",
        nullable=True,
        server_default=None,
    )
    _backfill_shadow_bindings(bind)
    _seed_outage_continuity_routes(bind)


def downgrade() -> None:
    raise RuntimeError(
        "Composable service-team authority is forward-fix only; do not recreate "
        "scalar type, region, manager, role, or workforce authority."
    )
