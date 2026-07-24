"""Add customer WHT policy and direct-customer WHT targets.

Revision ID: 419_customer_wht_policy_and_direct_targets
Revises: 418_payment_channel_mapping_sot
Create Date: 2026-07-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "419_customer_wht_policy_and_direct_targets"
down_revision = "418_payment_channel_mapping_sot"
branch_labels = None
depends_on = None

_WHT_TARGET_CONSTRAINT = "ck_withholding_tax_records_exactly_one_target"
_CUSTOMER_POLICY_UNIQUE = "uq_customer_tax_policies_account_id"
_ACCOUNT_INDEX = "ix_withholding_tax_records_account_id"
_SOURCE_INVOICE_INDEX = "ix_withholding_tax_records_source_invoice_id"
_CUSTOMER_POLICY_ACCOUNT_INDEX = "ix_customer_tax_policies_account_id"


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _column_exists(table_name: str, column_name: str) -> bool:
    return column_name in {c["name"] for c in _inspector().get_columns(table_name)}


def _index_exists(table_name: str, index_name: str) -> bool:
    return index_name in {i["name"] for i in _inspector().get_indexes(table_name)}


def _unique_exists(table_name: str, constraint_name: str) -> bool:
    return constraint_name in {
        c["name"] for c in _inspector().get_unique_constraints(table_name)
    }


def _check_exists(table_name: str, constraint_name: str) -> bool:
    return constraint_name in {
        c["name"] for c in _inspector().get_check_constraints(table_name)
    }


def _sqlite_table_sql(table_name: str) -> str:
    row = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
        .first()
    )
    return str(row[0] or "") if row else ""


def _validate_exactly_one_target_rows(bind: sa.engine.Connection) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT id
            FROM withholding_tax_records
            WHERE
                (CASE WHEN account_id IS NOT NULL THEN 1 ELSE 0 END) +
                (CASE WHEN billing_account_id IS NOT NULL THEN 1 ELSE 0 END) <> 1
            """
        )
    ).fetchall()
    if rows:
        raise RuntimeError(
            "withholding_tax_records contains rows without exactly one target"
        )


def _validate_downgrade_rows(bind: sa.engine.Connection) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT id
            FROM withholding_tax_records
            WHERE account_id IS NOT NULL
            """
        )
    ).fetchall()
    if rows:
        raise RuntimeError(
            "cannot downgrade while direct-customer withholding tax records exist"
        )


def _create_customer_tax_policies_table() -> None:
    if _table_exists("customer_tax_policies"):
        return
    bind = op.get_bind()
    uuid_type: sa.types.TypeEngine[object]
    created_default: sa.ClauseElement | None
    updated_default: sa.ClauseElement | None
    id_default: sa.ClauseElement | None
    if bind.dialect.name == "sqlite":
        uuid_type = sa.String(length=36)
        created_default = None
        updated_default = None
        id_default = None
    else:
        uuid_type = postgresql.UUID(as_uuid=True)
        created_default = sa.text("now()")
        updated_default = sa.text("now()")
        id_default = sa.text("gen_random_uuid()")
    op.create_table(
        "customer_tax_policies",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            server_default=id_default,
        ),
        sa.Column(
            "account_id",
            uuid_type,
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "withholding_tax_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=created_default,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=updated_default,
        ),
        sa.UniqueConstraint("account_id", name=_CUSTOMER_POLICY_UNIQUE),
    )
    if not _index_exists("customer_tax_policies", _CUSTOMER_POLICY_ACCOUNT_INDEX):
        op.create_index(
            _CUSTOMER_POLICY_ACCOUNT_INDEX,
            "customer_tax_policies",
            ["account_id"],
        )


def _upgrade_withholding_tax_records() -> None:
    bind = op.get_bind()
    if not _table_exists("withholding_tax_records"):
        return

    is_sqlite = bind.dialect.name == "sqlite"
    # UUID columns render as CHAR(32)/String on SQLite and native UUID on
    # Postgres. Top-level op.add_column works on both dialects (SQLite supports
    # ADD COLUMN natively); env.py wraps these ops so re-adding an existing
    # column on a fresh model-based schema is a no-op. Batch recreate is
    # deliberately avoided: it drops and rebuilds the table, which trips global
    # DDL listeners (e.g. geoalchemy2) and is forbidden post-squash.
    uuid_type: sa.types.TypeEngine[object] = (
        sa.String(length=36) if is_sqlite else postgresql.UUID(as_uuid=True)
    )
    # SQLite's ALTER TABLE ADD COLUMN cannot carry a FOREIGN KEY; the model-based
    # squashed schema already declares these FKs, so fresh SQLite test databases
    # have them. Only a real Postgres upgrade adds the columns with their FKs.
    account_fk = [] if is_sqlite else [sa.ForeignKey("subscribers.id")]
    source_invoice_fk = [] if is_sqlite else [sa.ForeignKey("invoices.id")]
    op.add_column(
        "withholding_tax_records",
        sa.Column("account_id", uuid_type, *account_fk, nullable=True),
    )
    op.add_column(
        "withholding_tax_records",
        sa.Column("vat_exclusive_amount", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "withholding_tax_records",
        sa.Column("vat_amount", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "withholding_tax_records",
        sa.Column("source_invoice_id", uuid_type, *source_invoice_fk, nullable=True),
    )
    op.add_column(
        "withholding_tax_records",
        sa.Column("policy_version", sa.Integer(), nullable=True),
    )
    op.create_index(
        _ACCOUNT_INDEX,
        "withholding_tax_records",
        ["account_id"],
    )
    op.create_index(
        _SOURCE_INVOICE_INDEX,
        "withholding_tax_records",
        ["source_invoice_id"],
    )
    _validate_exactly_one_target_rows(bind)
    # Relaxing billing_account_id to nullable and adding the exactly-one-target
    # CHECK both require a table recreate on SQLite, which is forbidden here.
    # The model-based squashed schema already declares both, so fresh SQLite
    # test databases carry them; only a real Postgres upgrade needs to apply
    # them, and Postgres does both as top-level operations.
    if not is_sqlite:
        op.alter_column(
            "withholding_tax_records",
            "billing_account_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=True,
        )
        op.create_check_constraint(
            _WHT_TARGET_CONSTRAINT,
            "withholding_tax_records",
            "(CASE WHEN account_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN billing_account_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
        )


def upgrade() -> None:
    _create_customer_tax_policies_table()
    _upgrade_withholding_tax_records()


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    if _table_exists("withholding_tax_records"):
        _validate_downgrade_rows(bind)
        # SQLite cannot drop a CHECK constraint or restore NOT NULL without a
        # recreate; those were only applied on Postgres in upgrade(), so this
        # mirrors the dialect split. Column and index drops are top-level on
        # both dialects (modern SQLite supports DROP COLUMN).
        if not is_sqlite and _check_exists(
            "withholding_tax_records", _WHT_TARGET_CONSTRAINT
        ):
            op.drop_constraint(
                _WHT_TARGET_CONSTRAINT,
                "withholding_tax_records",
                type_="check",
            )
        if _index_exists("withholding_tax_records", _ACCOUNT_INDEX):
            op.drop_index(_ACCOUNT_INDEX, table_name="withholding_tax_records")
        if _index_exists("withholding_tax_records", _SOURCE_INVOICE_INDEX):
            op.drop_index(
                _SOURCE_INVOICE_INDEX,
                table_name="withholding_tax_records",
            )
        if _column_exists("withholding_tax_records", "source_invoice_id"):
            op.drop_column("withholding_tax_records", "source_invoice_id")
        if _column_exists("withholding_tax_records", "policy_version"):
            op.drop_column("withholding_tax_records", "policy_version")
        if _column_exists("withholding_tax_records", "vat_amount"):
            op.drop_column("withholding_tax_records", "vat_amount")
        if _column_exists("withholding_tax_records", "vat_exclusive_amount"):
            op.drop_column("withholding_tax_records", "vat_exclusive_amount")
        if _column_exists("withholding_tax_records", "account_id"):
            op.drop_column("withholding_tax_records", "account_id")
        if not is_sqlite:
            op.alter_column(
                "withholding_tax_records",
                "billing_account_id",
                existing_type=postgresql.UUID(as_uuid=True),
                nullable=False,
            )

    if _table_exists("customer_tax_policies"):
        if _index_exists("customer_tax_policies", _CUSTOMER_POLICY_ACCOUNT_INDEX):
            op.drop_index(
                _CUSTOMER_POLICY_ACCOUNT_INDEX,
                table_name="customer_tax_policies",
            )
        op.drop_table("customer_tax_policies")
