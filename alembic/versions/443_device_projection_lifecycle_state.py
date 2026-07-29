"""Give the device projection an admission state and gate it against stale 'up'.

Deactivating a device used to erase it: ``collect_devices`` filtered the
``is_active`` flag, and the projection reconciler deletes any row that
derivation stops returning. Inactive devices now stay projected and are marked
with ``lifecycle_state`` instead.

The second constraint is the release gate — an inactive device projecting
``working`` becomes unrepresentable rather than merely improbable.

Revision ID: 443_device_projection_lifecycle_state
Revises: 442_customer_vat_exemption_policy
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "443_device_projection_lifecycle_state"
down_revision = "442_customer_vat_exemption_policy"
branch_labels = None
depends_on = None

_TABLE = "device_projections"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "lifecycle_state",
            sa.String(length=20),
            nullable=False,
            server_default="active",
        ),
    )
    op.create_index(
        "ix_device_projections_lifecycle_state", _TABLE, ["lifecycle_state"]
    )
    op.create_check_constraint(
        "ck_device_projection_lifecycle_state",
        _TABLE,
        "lifecycle_state IN ('active', 'inactive')",
    )
    op.create_check_constraint(
        "ck_device_projection_inactive_never_working",
        _TABLE,
        "lifecycle_state = 'active' OR operational_status = 'not_working'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_device_projection_inactive_never_working", _TABLE, type_="check"
    )
    op.drop_constraint("ck_device_projection_lifecycle_state", _TABLE, type_="check")
    op.drop_index("ix_device_projections_lifecycle_state", table_name=_TABLE)
    op.drop_column(_TABLE, "lifecycle_state")
