"""Bandwidth capacity on the PON port — the one access segment without it.

An earlier draft of this revision created a ``capacity_domains`` table modelling
PON ports, wireless sectors, OLT uplinks and BNGs as one thing. That was wrong:
``device_interfaces.speed_mbps`` (2,611 rows) and
``network_topology_links.capacity_bps`` (334 of 374) already own interface and
link capacity and are populated. A table storing its own copy would have been a
second authority over facts that already have one, drifting from them, and a
caller asking "what capacity does this segment have" would have needed to know
which of three tables to consult before it could ask.

The genuine gap is narrower. The PON port is the first shared segment a fibre
subscriber crosses, and it carried only ``max_ont_capacity`` — a device count,
NULL on all 502 rows — with no bandwidth figure anywhere. So capacity belongs
here, on the thing that has it.

Nullable on purpose: a port must be nameable as survey backlog before it is
measured, and NULL reads as unknown rather than healthy. Zero stays refused —
a bad measurement and a missing one must not look alike.

``target_oversubscription`` is planning policy rather than a hardware fact, but
it is per-port and would need a table of its own for one number, so it rides
here as a nullable override over the network-wide default.

Nothing is seeded. Capacity is recorded, never inferred: split ratio, XGS-PON
upgrades and shared uplinks all move the real number, and a guessed figure
produces a check that quietly passes.

Revision ID: 488_pon_port_capacity
Revises: 487_pon_structural_identity
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "488_pon_port_capacity"
down_revision: str | None = "487_pon_structural_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POSITIVE = "ck_pon_ports_positive_capacity"
_OVERSUB = "ck_pon_ports_oversubscription_floor"


def upgrade() -> None:
    op.add_column(
        "pon_ports",
        sa.Column(
            "downstream_mbps",
            sa.Integer(),
            nullable=True,
            comment="Usable downstream capacity. NULL means unsurveyed.",
        ),
    )
    op.add_column(
        "pon_ports",
        sa.Column(
            "upstream_mbps",
            sa.Integer(),
            nullable=True,
            comment="Usable upstream capacity. Binds before downstream on GPON.",
        ),
    )
    op.add_column(
        "pon_ports",
        sa.Column(
            "capacity_source",
            sa.String(length=200),
            nullable=True,
            comment="Provenance of the capacity figure — survey, vendor sheet, uplink.",
        ),
    )
    op.add_column(
        "pon_ports",
        sa.Column(
            "target_oversubscription",
            sa.Numeric(6, 2),
            nullable=True,
            comment="Sellable multiple of capacity. NULL falls back to the default.",
        ),
    )
    op.create_check_constraint(
        _POSITIVE,
        "pon_ports",
        "(downstream_mbps IS NULL OR downstream_mbps > 0) "
        "AND (upstream_mbps IS NULL OR upstream_mbps > 0)",
    )
    # 1 means 1:1, the strictest a segment can be. Zero or negative would make
    # the headroom arithmetic meaningless rather than merely strict.
    op.create_check_constraint(
        _OVERSUB,
        "pon_ports",
        "target_oversubscription IS NULL OR target_oversubscription >= 1",
    )


def downgrade() -> None:
    op.drop_constraint(_OVERSUB, "pon_ports", type_="check")
    op.drop_constraint(_POSITIVE, "pon_ports", type_="check")
    op.drop_column("pon_ports", "target_oversubscription")
    op.drop_column("pon_ports", "capacity_source")
    op.drop_column("pon_ports", "upstream_mbps")
    op.drop_column("pon_ports", "downstream_mbps")
