"""Fiber drop-cost components become rows, and lose their invented prices

The estimator on the fiber map priced an install from four hardcoded
components, each baked into three layers: a `SettingSpec`, a reader in
`web_network_fiber`, and the arithmetic in the map template's JavaScript.
Adding a splice closure, a pole or a permit fee meant editing all three.

This creates the table those components move into, and seeds them as STRUCTURE
— code, label, unit — with no amounts.

## Why no amounts

The settings they replace defaulted to `2.50` per metre of drop cable, `1.50`
per metre of labour, `85.00` for an ONT and `50.00` installation base, rendered
with `billing/default_currency`. Against NGN that quotes ₦85 for an ONT and ₦50
to install a service — off by roughly three orders of magnitude, and invisible
because an amount carries no currency of its own and therefore looks correct in
any of them.

Carrying those numbers forward would move the defect rather than remove it, and
inventing replacements would be worse: nobody in this change knows what a metre
of drop cable costs. So the rows arrive unpriced, `amount IS NULL`, and the
estimator reports itself unconfigured until an operator prices them. That is
true, loud, and fixable from a screen.

`amount` is nullable rather than defaulted to zero because "not priced yet" and
"free" are different answers, and only one of them should suppress a warning.

Revision ID: 519_fiber_cost_items
Revises: 518_retire_splynx_staging_schema
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "519_fiber_cost_items"
down_revision: str | None = "518_retire_splynx_staging_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "fiber_cost_items"

#: The four the estimator already knew about, in the order an installer reads
#: them. Codes match the settings keys they replace so the change is traceable.
SEED = (
    ("drop_cable_per_meter", "Drop cable", "per_meter", 10),
    ("labor_per_meter", "Labour", "per_meter", 20),
    ("ont_device", "ONT device", "flat", 30),
    ("installation_base", "Installation base fee", "flat", 40),
)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(length=60), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column(
            "unit",
            sa.Enum("per_meter", "flat", name="fibercostunit", native_enum=False),
            nullable=False,
        ),
        # Nullable on purpose — see the docstring. Not priced is not free.
        sa.Column("amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
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
        sa.UniqueConstraint("code", name="uq_fiber_cost_items_code"),
        sa.CheckConstraint(
            "amount IS NULL OR amount >= 0",
            name="ck_fiber_cost_items_amount_nonnegative",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_fiber_cost_items_version_positive",
        ),
    )

    bind = op.get_bind()
    for code, label, unit, sort_order in SEED:
        bind.execute(
            sa.text(
                f"""
                INSERT INTO {TABLE}
                    (id, code, label, unit, amount, is_active, sort_order, version,
                     created_at, updated_at)
                SELECT CAST(:id AS uuid), CAST(:code AS varchar),
                       CAST(:label AS varchar), CAST(:unit AS varchar),
                       CAST(NULL AS numeric), true,
                       CAST(:sort_order AS integer), 1, NOW(), NOW()
                WHERE NOT EXISTS (
                    SELECT 1 FROM {TABLE} WHERE code = CAST(:code AS varchar)
                )
                """
            )
            if bind.dialect.name == "postgresql"
            else sa.text(
                f"""
                INSERT INTO {TABLE}
                    (id, code, label, unit, amount, is_active, sort_order, version,
                     created_at, updated_at)
                SELECT :id, :code, :label, :unit, NULL, 1, :sort_order, 1,
                       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                WHERE NOT EXISTS (
                    SELECT 1 FROM {TABLE} WHERE code = :code
                )
                """
            ),
            {
                # Deterministic per code so a re-run cannot double-seed even if
                # the WHERE NOT EXISTS were removed, and so a fixture can name
                # a row without querying for it.
                "id": _seed_id(code),
                "code": code,
                "label": label,
                "unit": unit,
                "sort_order": sort_order,
            },
        )


def _seed_id(code: str) -> str:
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"fiber-cost-item.{code}.dotmac"))


def downgrade() -> None:
    """Drops the table, and with it any prices an operator has set.

    Deliberately not reinstating the four settings: their defaults were the
    defect this removed, and recreating them would restore a screen that quotes
    a number nobody chose.
    """

    op.drop_table(TABLE)
