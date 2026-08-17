"""PostgreSQL canaries for invoice provenance and active billing anchors."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from psycopg import errors as pg_errors
from psycopg import sql
from sqlalchemy import inspect
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app import config as app_config
from app.models.billing import Invoice, InvoiceDueDateBasis, InvoiceStatus
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus

ROOT = Path(__file__).resolve().parents[2]
PREDECESSOR = "536_integrator_ingress_scopes"
INVOICE_CONSTRAINT = "ck_invoices_verified_due_date_basis"
SUBSCRIPTION_CONSTRAINT = "ck_subscriptions_active_billing_anchor"


def _render(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def predecessor_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[URL]:
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        raise pytest.UsageError("migration-path test requires TEST_DATABASE_URL")
    base = make_url(configured)
    if not base.drivername.startswith("postgresql"):
        raise pytest.UsageError("migration-path test requires PostgreSQL")

    name = f"dotmac_money_path_{uuid4().hex}"
    maintenance = base.set(database="postgres")
    with psycopg.connect(_render(maintenance), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = base.set(database=name)
    monkeypatch.setattr(
        app_config,
        "settings",
        replace(
            app_config.settings,
            database_url=target.render_as_string(hide_password=False),
        ),
    )
    try:
        yield target
    finally:
        with psycopg.connect(_render(maintenance), autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
            )


def _upgrade(revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, revision)


def test_migrated_schema_contains_money_path_constraints(engine) -> None:
    invoice_constraints = {
        item.get("name") for item in inspect(engine).get_check_constraints("invoices")
    }
    subscription_constraints = {
        item.get("name")
        for item in inspect(engine).get_check_constraints("subscriptions")
    }

    assert INVOICE_CONSTRAINT in invoice_constraints
    assert SUBSCRIPTION_CONSTRAINT in subscription_constraints


def test_verified_invoice_basis_requires_due_date(
    db_session: Session,
    subscriber,
) -> None:
    invalid = Invoice(
        id=uuid4(),
        account_id=subscriber.id,
        invoice_number=f"INV-DUE-BASIS-{uuid4().hex[:10]}",
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("100.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        issued_at=datetime.now(UTC),
        due_at=None,
        due_date_basis=InvoiceDueDateBasis.contract_terms,
        due_date_basis_ref="integration:test-contract",
        due_date_policy_version="test-v1",
    )

    with pytest.raises(IntegrityError) as excinfo:
        with db_session.begin_nested():
            db_session.add(invalid)
            db_session.flush()

    assert INVOICE_CONSTRAINT in str(excinfo.value.orig)


def test_new_active_subscription_requires_billing_anchor(
    db_session: Session,
    subscriber,
    catalog_offer,
) -> None:
    invalid = Subscription(
        id=uuid4(),
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.postpaid,
        start_at=datetime.now(UTC),
        next_billing_at=None,
    )

    with pytest.raises(IntegrityError) as excinfo:
        with db_session.begin_nested():
            db_session.add(invalid)
            db_session.flush()

    assert SUBSCRIPTION_CONSTRAINT in str(excinfo.value.orig)


def test_predecessor_upgrade_preserves_and_quarantines_legacy_rows(
    predecessor_database: URL,
) -> None:
    _upgrade(PREDECESSOR)
    invoice_id = uuid4()
    subscription_id = uuid4()
    with psycopg.connect(_render(predecessor_database)) as connection:
        # The parent records are immaterial to these two invariant migrations.
        # Disabling FK triggers lets this rehearsal seed exactly the legacy row
        # shapes without manufacturing unrelated subscriber/catalog fixtures.
        connection.execute("SET session_replication_role = replica")
        connection.execute(
            "INSERT INTO invoices "
            "(id, account_id, invoice_number, status, currency, subtotal, "
            "discount_amount, discount_revision, tax_total, total, balance_due, "
            "due_at, is_proforma, is_active, created_at, updated_at) "
            "VALUES (%s, %s, 'INV-LEGACY-DUE', 'issued', 'NGN', 100, 0, 0, 0, "
            "100, 100, '2026-08-22 09:00:00+01', false, true, "
            "'2026-08-16 09:00:00+01', '2026-08-16 09:00:00+01')",
            (invoice_id, uuid4()),
        )
        connection.execute(
            "INSERT INTO subscriptions "
            "(id, subscriber_id, offer_id, status, billing_mode, contract_term, discount, "
            "start_at, next_billing_at, created_at, updated_at) "
            "VALUES (%s, %s, %s, 'active', 'postpaid', 'month_to_month', "
            "false, now(), NULL, now(), now())",
            (subscription_id, uuid4(), uuid4()),
        )
        connection.execute("SET session_replication_role = origin")
        connection.commit()

    _upgrade("head")

    with psycopg.connect(_render(predecessor_database)) as connection:
        invoice = connection.execute(
            "SELECT due_date_basis, due_date_basis_ref, due_date_policy_version "
            "FROM invoices WHERE id = %s",
            (invoice_id,),
        ).fetchone()
        assert invoice == ("unknown_unverified", None, None)

        subscription = connection.execute(
            "SELECT status, next_billing_at FROM subscriptions WHERE id = %s",
            (subscription_id,),
        ).fetchone()
        assert subscription == ("active", None)

        with pytest.raises(pg_errors.CheckViolation) as excinfo:
            connection.execute(
                "UPDATE subscriptions SET updated_at = now() WHERE id = %s",
                (subscription_id,),
            )
        assert excinfo.value.diag.constraint_name == SUBSCRIPTION_CONSTRAINT
        connection.rollback()
