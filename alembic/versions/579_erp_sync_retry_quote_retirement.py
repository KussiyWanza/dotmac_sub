"""Add ERP retry evidence and retire persisted quote transport schedules.

Expand-only storage; source rows, cursors and quote history are untouched.
Rollback deliberately retains retry evidence and does not re-enable retired transport.

Revision ID: 579_erp_sync_retry
Revises: 578_project_infrastructure
"""

import sqlalchemy as sa

from alembic import op

revision: str = "579_erp_sync_retry"
down_revision: str | None = "578_project_infrastructure"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("erp_operational_sync_state"):
        op.create_table(
            "erp_operational_sync_state",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("configuration_fingerprint", sa.String(64)),
            sa.Column("status", sa.String(24), nullable=False, server_default="ready"),
            sa.Column(
                "failure_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
            sa.Column("diagnostic", sa.JSON()),
            sa.CheckConstraint("id = 1", name="ck_erp_sync_singleton"),
        )
    op.execute(
        "INSERT INTO erp_operational_sync_state (id, status, failure_count) "
        "VALUES (1, 'ready', 0) ON CONFLICT (id) DO NOTHING"
    )
    op.execute("""
        UPDATE scheduled_tasks SET enabled = false
        WHERE task_name IN (
          'app.tasks.quotes.reconcile_quote_mirror',
          'app.tasks.quotes.refresh_quote_mirror_for_subscriber'
        ) AND enabled = true
    """)


def downgrade() -> None:
    # Forward-only evidence and authority retirement: keep the retry record,
    # historical quote data, and retired schedules. The upgrade is idempotent
    # so migration compatibility rehearsals can move the revision marker back
    # and forward without recreating live transport or losing evidence.
    pass
