"""Record what a setting was before it changed, and who changed it

`AuditEvent` records that a settings change happened; it does not record the
value. "Who turned this off, and what was it before" has therefore been
unanswerable — the question that gets asked during an incident.

The shape is `dotmac_kernel.settings_models.DomainSettingHistory` exactly. Sub's
settings writes are moving onto the kernel's writers, whose `_record_history`
writes this table; declaring it now means that cutover needs no schema change
and no backfill.

No backfill here either, and none is possible: the transitions that already
happened were never recorded anywhere. The table starts empty and fills from the
next change onward.

Revision ID: 520_domain_setting_history
Revises: 519_fiber_cost_items
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "520_domain_setting_history"
down_revision: str | None = "519_fiber_cost_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "domain_setting_history"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        # Denormalised from the parent so a row survives the setting's deletion.
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("domain", sa.String(length=120), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column(
            "setting_id",
            sa.Uuid(),
            sa.ForeignKey("domain_settings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "action",
            sa.Enum(
                "create",
                "update",
                "delete",
                name="ck_domain_setting_history_action",
                native_enum=False,
            ),
            nullable=False,
        ),
        # Text for both scalar and JSON settings: history is read by a human
        # comparing two states, not by code re-parsing them. NULL for a secret.
        sa.Column("value_before", sa.Text(), nullable=True),
        sa.Column("value_after", sa.Text(), nullable=True),
        sa.Column(
            "secret_changed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # SET NULL rather than CASCADE: deleting a person must not erase the
        # record that a setting changed, only who is still linked to it.
        sa.Column(
            "changed_by_party_id",
            sa.Uuid(),
            sa.ForeignKey("parties.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_domain_setting_history_lookup", TABLE, ["tenant_id", "domain", "key"]
    )
    op.create_index("ix_domain_setting_history_changed_at", TABLE, ["changed_at"])
    op.create_index("ix_domain_setting_history_actor", TABLE, ["changed_by_party_id"])
    op.create_index("ix_domain_setting_history_setting_id", TABLE, ["setting_id"])
    op.create_index("ix_domain_setting_history_tenant_id", TABLE, ["tenant_id"])


def downgrade() -> None:
    """Drops the table, and every transition recorded in it.

    There is nowhere else those transitions exist, so this is lossy by nature
    rather than by oversight.
    """

    op.drop_table(TABLE)
