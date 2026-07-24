from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/419_customer_wht_policy_and_direct_targets.py"
    )
    spec = importlib.util.spec_from_file_location("migration_418", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(monkeypatch, migration, connection) -> None:
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))


def _connection_with_references():
    engine = sa.create_engine("sqlite://")
    connection = engine.connect()
    metadata = sa.MetaData()
    for table_name in (
        "subscribers",
        "billing_accounts",
        "resellers",
        "payments",
        "payment_proofs",
        "invoices",
    ):
        sa.Table(table_name, metadata, sa.Column("id", sa.String, primary_key=True))
    metadata.create_all(connection)
    return connection


def _create_legacy_wht_table(connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE withholding_tax_records (
            id TEXT PRIMARY KEY,
            -- billing_account_id is relaxed to nullable by the migration on
            -- Postgres; SQLite cannot ALTER a column to nullable without a
            -- forbidden table recreate, so this fixture reflects the nullable
            -- end-state (the direct-customer target the migration enables).
            billing_account_id TEXT,
            reseller_id TEXT,
            payment_id TEXT,
            payment_proof_id TEXT,
            gross_amount NUMERIC(12, 2) NOT NULL,
            net_amount NUMERIC(12, 2) NOT NULL,
            wht_amount NUMERIC(12, 2) NOT NULL,
            wht_rate NUMERIC(5, 2),
            currency TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            certificate_path TEXT,
            certificate_reference TEXT,
            certified_at DATETIME,
            resolved_at DATETIME,
            notes TEXT,
            created_at DATETIME,
            updated_at DATETIME,
            FOREIGN KEY (billing_account_id) REFERENCES billing_accounts (id),
            FOREIGN KEY (reseller_id) REFERENCES resellers (id),
            FOREIGN KEY (payment_id) REFERENCES payments (id),
            FOREIGN KEY (payment_proof_id) REFERENCES payment_proofs (id)
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX uq_withholding_tax_records_payment_id "
        "ON withholding_tax_records (payment_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX ix_withholding_tax_records_billing_account_id "
        "ON withholding_tax_records (billing_account_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX ix_withholding_tax_records_reseller_id "
        "ON withholding_tax_records (reseller_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX ix_withholding_tax_records_status "
        "ON withholding_tax_records (status)"
    )


def test_customer_wht_policy_revision_is_linear_and_constrained() -> None:
    migration = _load_migration()
    source = Path(migration.__file__).read_text(encoding="utf-8")

    assert migration.revision == "419_customer_wht_policy_and_direct_targets"
    assert migration.down_revision == "418_payment_channel_mapping_sot"
    assert "customer_tax_policies" in source
    assert "ck_withholding_tax_records_exactly_one_target" in source
    assert "cannot downgrade while direct-customer withholding tax records exist" in (
        source
    )


def test_upgrade_creates_policy_table_and_preserves_legacy_consolidated_rows(
    monkeypatch,
) -> None:
    migration = _load_migration()
    connection = _connection_with_references()
    _create_legacy_wht_table(connection)
    connection.exec_driver_sql(
        "INSERT INTO billing_accounts (id) VALUES "
        "('00000000-0000-0000-0000-000000000001')"
    )
    connection.exec_driver_sql(
        "INSERT INTO resellers (id) VALUES ('00000000-0000-0000-0000-000000000002')"
    )
    connection.exec_driver_sql(
        "INSERT INTO payments (id) VALUES ('00000000-0000-0000-0000-000000000003')"
    )
    connection.exec_driver_sql(
        "INSERT INTO payment_proofs (id) VALUES "
        "('00000000-0000-0000-0000-000000000004')"
    )
    connection.exec_driver_sql(
        """
        INSERT INTO withholding_tax_records (
            id, billing_account_id, reseller_id, payment_id, payment_proof_id,
            gross_amount, net_amount, wht_amount, wht_rate, currency, status
        ) VALUES (
            '00000000-0000-0000-0000-000000000010',
            '00000000-0000-0000-0000-000000000001',
            '00000000-0000-0000-0000-000000000002',
            '00000000-0000-0000-0000-000000000003',
            '00000000-0000-0000-0000-000000000004',
            100000.00, 95000.00, 5000.00, 5.00, 'NGN', 'pending'
        )
        """
    )
    _run_migration(monkeypatch, migration, connection)

    migration.upgrade()

    inspector = sa.inspect(connection)
    assert inspector.has_table("customer_tax_policies")
    columns = {
        column["name"] for column in inspector.get_columns("withholding_tax_records")
    }
    assert {
        "account_id",
        "vat_exclusive_amount",
        "vat_amount",
        "source_invoice_id",
        "policy_version",
    } <= columns
    row = (
        connection.execute(
            sa.text(
                "SELECT account_id, billing_account_id, gross_amount, net_amount, "
                "wht_amount FROM withholding_tax_records WHERE id = :record_id"
            ),
            {"record_id": "00000000-0000-0000-0000-000000000010"},
        )
        .mappings()
        .one()
    )
    assert row["account_id"] is None
    assert row["billing_account_id"] == "00000000-0000-0000-0000-000000000001"
    assert str(row["gross_amount"]) == "100000"
    # The exactly-one-target CHECK and the billing_account_id nullable relax are
    # applied only on Postgres (SQLite would need a forbidden table recreate);
    # the model-based squashed schema carries both on fresh SQLite databases.
    # On this hand-built legacy SQLite table the migration adds the columns and
    # indexes via top-level operations, which is what we assert here.
    assert migration._sqlite_table_sql("withholding_tax_records")
    account_indexes = {
        ix["name"]
        for ix in sa.inspect(connection).get_indexes("withholding_tax_records")
    }
    assert "ix_withholding_tax_records_account_id" in account_indexes
    assert "ix_withholding_tax_records_source_invoice_id" in account_indexes


def test_upgrade_accepts_direct_target_and_rejects_dual_or_missing_targets(
    monkeypatch,
) -> None:
    migration = _load_migration()
    connection = _connection_with_references()
    _create_legacy_wht_table(connection)
    connection.exec_driver_sql(
        "INSERT INTO billing_accounts (id) VALUES "
        "('00000000-0000-0000-0000-000000000021')"
    )
    connection.exec_driver_sql(
        "INSERT INTO subscribers (id) VALUES ('00000000-0000-0000-0000-000000000022')"
    )
    connection.exec_driver_sql(
        "INSERT INTO invoices (id) VALUES ('00000000-0000-0000-0000-000000000023')"
    )
    _run_migration(monkeypatch, migration, connection)

    migration.upgrade()

    connection.exec_driver_sql(
        """
        INSERT INTO withholding_tax_records (
            id, account_id, billing_account_id, reseller_id, payment_id,
            payment_proof_id, gross_amount, net_amount, wht_amount, wht_rate,
            vat_exclusive_amount, vat_amount, source_invoice_id, policy_version,
            currency, status
        ) VALUES (
            '00000000-0000-0000-0000-000000000024',
            '00000000-0000-0000-0000-000000000022',
            NULL,
            NULL,
            NULL,
            NULL,
            107500.00,
            102500.00,
            5000.00,
            5.00,
            100000.00,
            7500.00,
            '00000000-0000-0000-0000-000000000023',
            3,
            'NGN',
            'pending'
        )
        """
    )
    count = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM withholding_tax_records WHERE account_id IS NOT NULL"
        )
    ).scalar_one()
    assert count == 1

    # On Postgres the exactly-one-target CHECK rejects these rows at insert.
    # SQLite cannot carry that CHECK without a forbidden table recreate, so we
    # assert the equivalent application-level guard the migration itself runs
    # (_validate_exactly_one_target_rows) rejects the same shapes.
    def _insert_and_expect_rejected(record_id: str, account_id, billing_account_id):
        connection.exec_driver_sql(
            """
            INSERT INTO withholding_tax_records (
                id, account_id, billing_account_id, gross_amount, net_amount,
                wht_amount, currency, status
            ) VALUES (?, ?, ?, 107500.00, 102500.00, 5000.00, 'NGN', 'pending')
            """,
            (record_id, account_id, billing_account_id),
        )
        with pytest.raises(RuntimeError, match="without exactly one target"):
            migration._validate_exactly_one_target_rows(connection)
        connection.exec_driver_sql(
            "DELETE FROM withholding_tax_records WHERE id = ?", (record_id,)
        )

    # Missing target (both NULL).
    _insert_and_expect_rejected("00000000-0000-0000-0000-000000000025", None, None)
    # Dual target (both set).
    _insert_and_expect_rejected(
        "00000000-0000-0000-0000-000000000026",
        "00000000-0000-0000-0000-000000000022",
        "00000000-0000-0000-0000-000000000021",
    )


def test_downgrade_rejects_existing_direct_customer_rows(monkeypatch) -> None:
    migration = _load_migration()
    connection = _connection_with_references()
    _create_legacy_wht_table(connection)
    connection.exec_driver_sql(
        "INSERT INTO subscribers (id) VALUES ('00000000-0000-0000-0000-000000000031')"
    )
    _run_migration(monkeypatch, migration, connection)
    migration.upgrade()
    connection.exec_driver_sql(
        """
        INSERT INTO withholding_tax_records (
            id, account_id, billing_account_id, gross_amount, net_amount,
            wht_amount, currency, status
        ) VALUES (
            '00000000-0000-0000-0000-000000000032',
            '00000000-0000-0000-0000-000000000031',
            NULL,
            107500.00,
            102500.00,
            5000.00,
            'NGN',
            'pending'
        )
        """
    )

    with pytest.raises(
        RuntimeError,
        match="cannot downgrade while direct-customer withholding tax records exist",
    ):
        migration.downgrade()
