"""Fresh and incremental PostgreSQL proofs for lifecycle evidence authority."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from psycopg import errors as pg_errors
from psycopg import sql
from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from alembic import command
from app import config as app_config
from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Reseller, Subscriber

ROOT = Path(__file__).resolve().parents[2]
PREDECESSOR = "473_lead_reseller_ownership"
CANDIDATE = "474_lifecycle_evidence_authority"
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
SUBSCRIPTION_FK = "subscription_lifecycle_events_subscription_id_fkey"


def _render(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def engine():
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured or not make_url(configured).drivername.startswith("postgresql"):
        pytest.skip("migrated-schema test requires PostgreSQL")

    class _Dialect:
        name = "postgresql"

    class _Stub:
        dialect = _Dialect()

    return _Stub()


@pytest.fixture
def migrated_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[URL]:
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("migrated-schema test requires TEST_DATABASE_URL")
    base = make_url(configured)
    if not base.drivername.startswith("postgresql"):
        pytest.skip("migrated-schema test requires PostgreSQL")

    name = f"dotmac_lifecycle_evidence_{uuid.uuid4().hex}"
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


def _alembic(revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, revision)


def _constraints(url: URL) -> set[str]:
    with psycopg.connect(_render(url)) as conn:
        rows = conn.execute(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'subscription_lifecycle_events'::regclass"
        ).fetchall()
    return {name for (name,) in rows}


def _restore_production_predecessor_shape(url: URL) -> None:
    """Remove revision-470 metadata leaked by the squashed bootstrap.

    Revision 001 calls current ``Base.metadata.create_all()``. When this test
    stops at 469, that bootstrap may already have created columns declared by
    the candidate model even though a real 469 production database cannot have
    them. Remove only the candidate-owned objects so 469 -> 470 exercises the
    actual DDL rather than accepting metadata's future schema.
    """

    with psycopg.connect(_render(url), autocommit=True) as conn:
        conn.execute(
            "ALTER TABLE subscription_lifecycle_events DROP CONSTRAINT IF EXISTS "
            f"{SUBSCRIPTION_FK}"
        )
        conn.execute(
            "ALTER TABLE subscription_lifecycle_events ADD CONSTRAINT "
            f"{SUBSCRIPTION_FK} FOREIGN KEY (subscription_id) "
            "REFERENCES subscriptions(id)"
        )
        conn.execute(
            "ALTER TABLE subscription_lifecycle_events DROP CONSTRAINT IF EXISTS "
            "uq_subscription_lifecycle_events_source_identity"
        )
        conn.execute(
            "DROP INDEX IF EXISTS "
            "ix_subscription_lifecycle_events_subscription_effective"
        )
        for column in (
            "recorded_at",
            "effective_at",
            "evidence_fingerprint",
            "source_id",
            "evidence_source",
        ):
            conn.execute(
                sql.SQL(
                    "ALTER TABLE subscription_lifecycle_events DROP COLUMN IF EXISTS {}"
                ).format(sql.Identifier(column))
            )


def _seed_subscription(url: URL) -> uuid.UUID:
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            reseller = session.scalar(
                select(Reseller).where(Reseller.is_house.is_(True))
            )
            if reseller is None:
                reseller = Reseller(
                    name="Lifecycle migration reseller",
                    is_house=True,
                )
                session.add(reseller)
                session.flush()
            subscriber = Subscriber(
                first_name="Lifecycle",
                last_name="Migration",
                email=f"lifecycle-{uuid.uuid4().hex}@example.com",
                reseller_id=reseller.id,
            )
            offer = CatalogOffer(
                name="Lifecycle migration offer",
                code=f"LIFE-{uuid.uuid4().hex[:8]}",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
                billing_mode=BillingMode.postpaid,
            )
            session.add_all((subscriber, offer))
            session.flush()
            subscription = Subscription(
                subscriber_id=subscriber.id,
                offer_id=offer.id,
                status=SubscriptionStatus.active,
                billing_mode=BillingMode.postpaid,
                start_at=NOW,
                next_billing_at=NOW + timedelta(days=30),
            )
            session.add(subscription)
            session.commit()
            return subscription.id
    finally:
        engine.dispose()


def test_head_has_the_authority_columns_constraints_and_trigger(
    engine, migrated_database
):
    _alembic("head")

    required = {
        "ck_subscription_lifecycle_events_evidence_grade",
        "ck_subscription_lifecycle_events_evidence_source",
        "ck_subscription_lifecycle_events_trusted_shape",
        "uq_subscription_lifecycle_events_source_identity",
    }
    assert required <= _constraints(migrated_database)
    with psycopg.connect(_render(migrated_database)) as conn:
        columns = {
            name
            for (name,) in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'subscription_lifecycle_events'"
            ).fetchall()
        }
        triggers = {
            name
            for (name,) in conn.execute(
                "SELECT tgname FROM pg_trigger WHERE tgrelid = "
                "'subscription_lifecycle_events'::regclass AND NOT tgisinternal"
            ).fetchall()
        }
        delete_rule = conn.execute(
            "SELECT confdeltype FROM pg_constraint WHERE conname = %s",
            (SUBSCRIPTION_FK,),
        ).fetchone()
    assert {
        "evidence_source",
        "source_id",
        "evidence_fingerprint",
        "effective_at",
        "recorded_at",
    } <= columns
    assert "trg_subscription_lifecycle_events_append_only" in triggers
    assert delete_rule == ("r",)


def test_incremental_upgrade_preserves_legacy_rows_and_appends_one_baseline(
    engine, migrated_database
):
    _alembic(PREDECESSOR)
    _restore_production_predecessor_shape(migrated_database)
    subscription_id = _seed_subscription(migrated_database)
    legacy_id = uuid.uuid4()
    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO subscription_lifecycle_events "
            "(id, subscription_id, event_type, to_status, created_at, "
            " evidence_grade) VALUES (%s, %s, 'activate', 'active', %s, "
            " 'unsupported_pre_cutover')",
            (legacy_id, subscription_id, NOW),
        )

    _alembic(CANDIDATE)

    with psycopg.connect(_render(migrated_database)) as conn:
        legacy = conn.execute(
            "SELECT evidence_grade, evidence_source, effective_at, recorded_at "
            "FROM subscription_lifecycle_events WHERE id = %s",
            (legacy_id,),
        ).fetchone()
        baselines = conn.execute(
            "SELECT from_status, to_status, evidence_grade, evidence_source, "
            "effective_at, recorded_at, source_id, evidence_fingerprint "
            "FROM subscription_lifecycle_events "
            "WHERE subscription_id = %s AND evidence_source = 'cutover_baseline'",
            (subscription_id,),
        ).fetchall()

    assert legacy == (
        "unsupported_pre_cutover",
        "legacy_unattributed",
        None,
        None,
    )
    assert len(baselines) == 1
    baseline = baselines[0]
    assert baseline[0] is None
    assert baseline[1:4] == ("active", "state_baseline", "cutover_baseline")
    assert baseline[4] is not None
    assert baseline[5] is not None
    assert baseline[6]
    assert baseline[7].startswith("sha256:")


def test_raw_insert_defaults_to_unsupported_observation(engine, migrated_database):
    _alembic("head")
    subscription_id = _seed_subscription(migrated_database)
    evidence_id = uuid.uuid4()
    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO subscription_lifecycle_events "
            "(id, subscription_id, event_type, to_status, created_at) "
            "VALUES (%s, %s, 'activate', 'active', %s)",
            (evidence_id, subscription_id, NOW),
        )
        found = conn.execute(
            "SELECT evidence_grade, evidence_source FROM "
            "subscription_lifecycle_events WHERE id = %s",
            (evidence_id,),
        ).fetchone()

    assert found == ("unsupported_observation", "untrusted_observation")


def test_database_rejects_an_incomplete_trusted_claim(engine, migrated_database):
    _alembic("head")
    subscription_id = _seed_subscription(migrated_database)
    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        with pytest.raises(pg_errors.CheckViolation) as caught:
            conn.execute(
                "INSERT INTO subscription_lifecycle_events "
                "(id, subscription_id, event_type, to_status, created_at, "
                " evidence_grade, evidence_source) VALUES "
                "(%s, %s, 'activate', 'active', %s, "
                " 'transition_evidence', 'lifecycle_command')",
                (uuid.uuid4(), subscription_id, NOW),
            )

    assert (
        caught.value.diag.constraint_name
        == "ck_subscription_lifecycle_events_trusted_shape"
    )


def test_subscription_identity_cannot_be_deleted_behind_retained_evidence(
    engine, migrated_database
):
    _alembic("head")
    subscription_id = _seed_subscription(migrated_database)
    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO subscription_lifecycle_events "
            "(id, subscription_id, event_type, to_status, created_at) "
            "VALUES (%s, %s, 'activate', 'active', %s)",
            (uuid.uuid4(), subscription_id, NOW),
        )
        with pytest.raises(pg_errors.ForeignKeyViolation) as caught:
            conn.execute("DELETE FROM subscriptions WHERE id = %s", (subscription_id,))

    assert caught.value.diag.constraint_name == SUBSCRIPTION_FK
