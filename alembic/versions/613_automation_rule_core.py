"""Add Automation Center rule definitions and immutable versions.

Revision ID: 613_automation_rule_core
Revises: 583_staff_expense_requesters
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "613_automation_rule_core"
down_revision: str | None = "612_shared_outage_work_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("automation_rules", "automation_rule_versions")
_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("automation:rule:read", "View Automation Center rules and versions"),
    ("automation:rule:create", "Create Automation Center rule drafts"),
    ("automation:rule:update", "Update Automation Center rule drafts"),
    ("automation:rule:publish", "Publish Automation Center rule versions"),
    ("automation:rule:operate", "Pause, resume, and retire automation rules"),
)
_GRANT_TABLES = (
    "role_permissions",
    "subscriber_permissions",
    "system_user_permissions",
)


def _permission_id(bind, key: str) -> str | None:
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
    ).scalar()


def _seed_permissions(bind) -> None:
    if "permissions" not in set(sa.inspect(bind).get_table_names()):
        return
    now = datetime.now(UTC)
    for key, description in _PERMISSIONS:
        permission_id = _permission_id(bind, key)
        if permission_id is None:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO permissions (
                        id, key, description, is_active, is_ui_assignable,
                        created_at, updated_at
                    ) VALUES (:id, :key, :description, true, true, :now, :now)
                    """
                ),
                {
                    "id": str(uuid4()),
                    "key": key,
                    "description": description,
                    "now": now,
                },
            )
        else:
            bind.execute(
                sa.text(
                    """
                    UPDATE permissions
                    SET description = :description,
                        is_active = true,
                        is_ui_assignable = true,
                        updated_at = :now
                    WHERE id = :id
                    """
                ),
                {
                    "id": permission_id,
                    "description": description,
                    "now": now,
                },
            )


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
          USING (tenant_id = app_current_tenant_id())
          WITH CHECK (tenant_id = app_current_tenant_id())
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_user")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO platform_api")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_table(
        "automation_rules",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("trigger_key", sa.String(length=160), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="draft", nullable=False
        ),
        sa.Column("active_version_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("length(trim(key)) > 0", name="ck_automation_rules_key"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_automation_rules_name"),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'paused', 'retired')",
            name="ck_automation_rules_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_automation_rules_tenant_key"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_automation_rules_tenant_id"),
    )
    op.create_index("ix_automation_rules_tenant_id", "automation_rules", ["tenant_id"])
    op.create_index(
        "ix_automation_rules_trigger_key", "automation_rules", ["trigger_key"]
    )

    op.create_table(
        "automation_rule_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("rule_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("trigger_schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "actions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_by", sa.String(length=255), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version >= 1", name="ck_automation_rule_versions_version"),
        sa.CheckConstraint(
            "trigger_schema_version >= 1",
            name="ck_automation_rule_versions_trigger_schema",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "rule_id"],
            ["automation_rules.tenant_id", "automation_rules.id"],
            ondelete="CASCADE",
            name="fk_automation_rule_versions_rule_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "rule_id", "version", name="uq_automation_rule_versions_rule_version"
        ),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_automation_rule_versions_tenant_id"
        ),
    )
    op.create_index(
        "ix_automation_rule_versions_rule_id",
        "automation_rule_versions",
        ["rule_id"],
    )
    op.create_index(
        "ix_automation_rule_versions_tenant_id",
        "automation_rule_versions",
        ["tenant_id"],
    )
    op.create_index(
        "uq_automation_rule_versions_draft",
        "automation_rule_versions",
        ["rule_id"],
        unique=True,
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_foreign_key(
        "fk_automation_rules_active_version_tenant",
        "automation_rules",
        "automation_rule_versions",
        ["tenant_id", "active_version_id"],
        ["tenant_id", "id"],
    )
    op.execute(
        """
        CREATE FUNCTION reject_published_automation_rule_version_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.published_at IS NOT NULL THEN
            RAISE EXCEPTION 'published automation rule versions are immutable';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER automation_rule_versions_immutable
        BEFORE UPDATE OR DELETE ON automation_rule_versions
        FOR EACH ROW EXECUTE FUNCTION
        reject_published_automation_rule_version_mutation()
        """
    )
    for table in _TABLES:
        _enable_rls(table)
    _seed_permissions(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    if "permissions" in set(sa.inspect(bind).get_table_names()):
        for key, _description in _PERMISSIONS:
            permission_id = _permission_id(bind, key)
            if permission_id is None:
                continue
            for table in _GRANT_TABLES:
                if table in set(sa.inspect(bind).get_table_names()):
                    bind.execute(
                        sa.text(f"DELETE FROM {table} WHERE permission_id = :id"),
                        {"id": permission_id},
                    )
            bind.execute(
                sa.text("DELETE FROM permissions WHERE id = :id"),
                {"id": permission_id},
            )
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.execute(
        "DROP TRIGGER IF EXISTS automation_rule_versions_immutable "
        "ON automation_rule_versions"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS reject_published_automation_rule_version_mutation()"
    )
    op.drop_constraint(
        "fk_automation_rules_active_version_tenant",
        "automation_rules",
        type_="foreignkey",
    )
    op.drop_index(
        "uq_automation_rule_versions_draft", table_name="automation_rule_versions"
    )
    op.drop_index(
        "ix_automation_rule_versions_tenant_id",
        table_name="automation_rule_versions",
    )
    op.drop_index(
        "ix_automation_rule_versions_rule_id", table_name="automation_rule_versions"
    )
    op.drop_table("automation_rule_versions")
    op.drop_index("ix_automation_rules_trigger_key", table_name="automation_rules")
    op.drop_index("ix_automation_rules_tenant_id", table_name="automation_rules")
    op.drop_table("automation_rules")
