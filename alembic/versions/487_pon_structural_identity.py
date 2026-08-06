"""store PON port structural identity and constrain it per platform shape

``PonPort.name`` has been carrying two jobs -- display text and identity -- and
the only uniqueness the database enforced was ``UNIQUE(olt_id, name)``, which is
uniqueness of a *display string*. Two rows naming one physical port in different
forms (``pon5`` and ``5``, or ``0/1/13`` and ``pon-0/1/13``) were both accepted.
The application guard refuses such a pair at decision time, but nothing stopped
them being written, and a guard in application code cannot constrain a race or a
path that does not call it.

This migration moves identity out of the name and into columns the database can
constrain.

Shape differs by platform, so one universal constraint was never correct:

* chassis OLTs (Huawei MA5600/MA5608T/MA5800, ZTE, Nokia) -- identity is
  ``(olt_id, frame, slot, port)``;
* single-box OLTs (Ubiquiti UF-OLT) -- the box *is* the OLT, there is no frame
  and no slot, and identity is ``(olt_id, port)``.

Hence two partial unique indexes rather than one constraint. ``identity_frame``
being NULL is not "unknown" -- it is the positive statement that this platform
has no frame, and it is what separates the two indexes.

Identity is deliberately NOT backfilled here. Establishing it requires reading
the platform shape from the OLT vendor and the identity owner's refusals, which
is application logic, not SQL. ``network.pon_port_identity.materialize_identity``
does that and is idempotent; run it after this migration. Until then every row
has NULL identity columns, both partial indexes match nothing, and behaviour is
unchanged -- so this migration is safe to deploy ahead of the backfill.

Measured on production 2026-08-06 before writing this: 502 PON rows, of which
187 chassis-named and 121 single-box-named already carry a ``port_number`` that
agrees with their name, so 308 rows are backfillable with no device I/O. The
remaining 194 are Huawei rows with no derivable identity and no ONTs attached;
they stay NULL and unconstrained until the topology import or their deletion.

Revision ID: 487_pon_structural_identity
Revises: 486_service_handoffs
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "487_pon_structural_identity"
down_revision = "486_service_handoffs"
branch_labels = None
depends_on = None


_CHASSIS_INDEX = "uq_pon_ports_chassis_identity"
_SINGLE_BOX_INDEX = "uq_pon_ports_single_box_identity"


def upgrade() -> None:
    op.add_column(
        "pon_ports",
        sa.Column(
            "identity_frame",
            sa.Integer(),
            nullable=True,
            comment=(
                "Chassis frame. NULL states that this platform has no frame "
                "(single-box OLT), or that identity is not yet established."
            ),
        ),
    )
    op.add_column(
        "pon_ports",
        sa.Column(
            "identity_slot",
            sa.Integer(),
            nullable=True,
            comment="Chassis slot. NULL on single-box platforms.",
        ),
    )
    op.add_column(
        "pon_ports",
        sa.Column(
            "identity_port",
            sa.Integer(),
            nullable=True,
            comment=(
                "Structural port. Non-NULL exactly when identity is "
                "established; this is the flag the partial indexes key on."
            ),
        ),
    )

    # Chassis identity: frame present means the platform has a chassis.
    op.create_index(
        _CHASSIS_INDEX,
        "pon_ports",
        ["olt_id", "identity_frame", "identity_slot", "identity_port"],
        unique=True,
        postgresql_where=sa.text("identity_frame IS NOT NULL"),
    )
    # Single-box identity: no frame by nature, so the port alone identifies it.
    op.create_index(
        _SINGLE_BOX_INDEX,
        "pon_ports",
        ["olt_id", "identity_port"],
        unique=True,
        postgresql_where=sa.text(
            "identity_frame IS NULL AND identity_port IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(_SINGLE_BOX_INDEX, table_name="pon_ports")
    op.drop_index(_CHASSIS_INDEX, table_name="pon_ports")
    op.drop_column("pon_ports", "identity_port")
    op.drop_column("pon_ports", "identity_slot")
    op.drop_column("pon_ports", "identity_frame")
