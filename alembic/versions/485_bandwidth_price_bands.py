"""Bandwidth price bands — rule-driven quoting for arbitrary circuit speeds.

Dedicated circuits are sold at any speed, so pricing them from one
``catalog_offers`` row per speed produced duplicate speeds at incompatible
prices and a 500 Mbps circuit priced below a 300 Mbps one. A band set replaces
those rows with a rule sales quotes from.

Bands are half-open ``[speed_from_mbps, speed_to_mbps)``; the top band is left
open. Rates accumulate progressively, so a band set cannot price more
bandwidth cheaper than less. See ``app/services/bandwidth_pricing.py``.

No seed data: the rates are a commercial decision, not a schema concern.

Revision ID: 485_bandwidth_price_bands
Revises: 484_sla_policy_plan_family_scope
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "485_bandwidth_price_bands"
down_revision: str | None = "484_sla_policy_plan_family_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bandwidth_price_bands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("plan_family", sa.String(length=40), nullable=False),
        sa.Column("speed_from_mbps", sa.Integer(), nullable=False),
        sa.Column("speed_to_mbps", sa.Integer(), nullable=True),
        sa.Column("rate_per_mbps", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "currency", sa.String(length=3), nullable=False, server_default="NGN"
        ),
        sa.Column("description", sa.String(length=200), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
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
            "plan_family IN ('unlimited', 'dedicated', 'home_flex')",
            name="ck_bandwidth_price_bands_family_vocab",
        ),
        sa.CheckConstraint(
            "speed_from_mbps >= 0", name="ck_bandwidth_price_bands_from"
        ),
        sa.CheckConstraint(
            "speed_to_mbps IS NULL OR speed_to_mbps > speed_from_mbps",
            name="ck_bandwidth_price_bands_range",
        ),
        sa.CheckConstraint(
            "rate_per_mbps >= 0", name="ck_bandwidth_price_bands_rate_sign"
        ),
    )
    # Partial: two LIVE bands must not start at the same speed, but retiring a
    # band and re-cutting the ladder from the same boundary is ordinary
    # repricing and must stay possible.
    op.create_index(
        "uq_bandwidth_price_bands_family_from",
        "bandwidth_price_bands",
        ["plan_family", "speed_from_mbps"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "ix_bandwidth_price_bands_family",
        "bandwidth_price_bands",
        ["plan_family", "speed_from_mbps"],
    )


def downgrade() -> None:
    op.drop_index("ix_bandwidth_price_bands_family", "bandwidth_price_bands")
    op.drop_index("uq_bandwidth_price_bands_family_from", "bandwidth_price_bands")
    op.drop_table("bandwidth_price_bands")
