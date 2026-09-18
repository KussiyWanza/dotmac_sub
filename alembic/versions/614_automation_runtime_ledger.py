"""Add the Automation Center execution ledger and runtime permissions.

Revision ID: 614_automation_runtime_ledger
Revises: 584_automation_rule_core
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "614_automation_runtime_ledger"
down_revision: str | None = "613_automation_rule_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("automation_runs", "automation_step_runs")
_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("automation:hub:read", "Open the Automation Center"),
    ("automation:run:read", "View Automation Center execution runs"),
    ("automation:run:redrive", "Retry failed Automation Center runs"),
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
                {"id": permission_id, "description": description, "now": now},
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
        "automation_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("rule_id", sa.UUID(), nullable=False),
        sa.Column("rule_version_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("target_type", sa.String(length=120), nullable=False),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="pending", nullable=False
        ),
        sa.Column("matched", sa.Boolean(), nullable=True),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("error_code", sa.String(length=160), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'skipped', 'succeeded', 'failed', 'blocked')",
            name="ck_automation_runs_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "rule_id"],
            ["automation_rules.tenant_id", "automation_rules.id"],
            name="fk_automation_runs_rule_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "rule_version_id"],
            ["automation_rule_versions.tenant_id", "automation_rule_versions.id"],
            name="fk_automation_runs_version_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_automation_runs_tenant_id"),
        sa.UniqueConstraint(
            "rule_version_id", "event_id", name="uq_automation_runs_version_event"
        ),
    )
    op.create_index("ix_automation_runs_tenant_id", "automation_runs", ["tenant_id"])
    op.create_index("ix_automation_runs_rule_id", "automation_runs", ["rule_id"])
    op.create_index("ix_automation_runs_event_id", "automation_runs", ["event_id"])
    op.create_index("ix_automation_runs_status", "automation_runs", ["status"])

    op.create_table(
        "automation_step_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("action_key", sa.String(length=160), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="pending", nullable=False
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("error_code", sa.String(length=160), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("step_index >= 0", name="ck_automation_step_runs_index"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'blocked')",
            name="ck_automation_step_runs_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_automation_step_runs_attempt_count"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["automation_runs.tenant_id", "automation_runs.id"],
            ondelete="CASCADE",
            name="fk_automation_step_runs_run_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "step_index", name="uq_automation_step_runs_run_index"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_automation_step_runs_idempotency_key"
        ),
    )
    op.create_index(
        "ix_automation_step_runs_tenant_id", "automation_step_runs", ["tenant_id"]
    )
    op.create_index(
        "ix_automation_step_runs_run_id", "automation_step_runs", ["run_id"]
    )
    op.create_index(
        "ix_automation_step_runs_status", "automation_step_runs", ["status"]
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
    op.drop_index("ix_automation_step_runs_status", table_name="automation_step_runs")
    op.drop_index("ix_automation_step_runs_run_id", table_name="automation_step_runs")
    op.drop_index(
        "ix_automation_step_runs_tenant_id", table_name="automation_step_runs"
    )
    op.drop_table("automation_step_runs")
    op.drop_index("ix_automation_runs_status", table_name="automation_runs")
    op.drop_index("ix_automation_runs_event_id", table_name="automation_runs")
    op.drop_index("ix_automation_runs_rule_id", table_name="automation_runs")
    op.drop_index("ix_automation_runs_tenant_id", table_name="automation_runs")
    op.drop_table("automation_runs")
