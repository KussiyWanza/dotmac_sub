"""Service handoff — how a dedicated circuit reaches the customer edge.

Transit and layer-2 clear channel are delivery variants of a dedicated
circuit, not separate products. Modelling them as plan families would fork the
catalog over a delivery detail; modelling them as an untyped blob on the sales
order would leave provisioning facts with no schema and no owner. The
commercial product stays one offer and the delivery spec lives here, typed and
constrained.

One row per subscription. Each handoff type carries only the fields it needs,
enforced in the database: a BGP handoff without an ASN cannot be provisioned,
and a clear channel carrying an ASN is a contradiction.

Revision ID: 486_service_handoffs
Revises: 485_bandwidth_price_bands
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "486_service_handoffs"
down_revision: str | None = "485_bandwidth_price_bands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIELDS_MATCH_TYPE = (
    "(handoff_type = 'static_ip' AND customer_asn IS NULL "
    " AND announced_prefixes IS NULL AND a_end_description IS NULL "
    " AND b_end_description IS NULL) "
    "OR (handoff_type = 'bgp' AND customer_asn IS NOT NULL "
    " AND a_end_description IS NULL AND b_end_description IS NULL) "
    "OR (handoff_type = 'layer2_clear_channel' AND customer_asn IS NULL "
    " AND announced_prefixes IS NULL "
    " AND a_end_description IS NOT NULL AND b_end_description IS NOT NULL)"
)


def upgrade() -> None:
    handoff_type = postgresql.ENUM(
        "static_ip",
        "bgp",
        "layer2_clear_channel",
        name="servicehandofftype",
        create_type=False,
    )
    handoff_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "service_handoffs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "handoff_type",
            handoff_type,
            nullable=False,
            server_default="static_ip",
        ),
        sa.Column("customer_asn", sa.BigInteger(), nullable=True),
        sa.Column("announced_prefixes", sa.Text(), nullable=True),
        sa.Column("peer_ip", sa.String(length=64), nullable=True),
        sa.Column("a_end_description", sa.String(length=200), nullable=True),
        sa.Column("b_end_description", sa.String(length=200), nullable=True),
        sa.Column("vlan_id", sa.Integer(), nullable=True),
        sa.Column("noc_notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            _FIELDS_MATCH_TYPE, name="ck_service_handoffs_fields_match_type"
        ),
        sa.CheckConstraint(
            "customer_asn IS NULL OR (customer_asn > 0 AND customer_asn < 4294967295)",
            name="ck_service_handoffs_asn_range",
        ),
        sa.CheckConstraint(
            "vlan_id IS NULL OR (vlan_id > 0 AND vlan_id < 4095)",
            name="ck_service_handoffs_vlan_range",
        ),
    )
    op.create_index("ix_service_handoffs_type", "service_handoffs", ["handoff_type"])


def downgrade() -> None:
    op.drop_index("ix_service_handoffs_type", "service_handoffs")
    op.drop_table("service_handoffs")
    postgresql.ENUM(name="servicehandofftype").drop(op.get_bind(), checkfirst=True)
