"""Migrated-PostgreSQL proofs for immutable SLA period-score evidence.

These are deployed-schema tests.  They exercise both a fresh head and the real
479 -> 480 incremental path, then prove the constraints and triggers by name.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
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
PREDECESSOR = "479_inbox_lifecycle_audit"
CANDIDATE = "482_sla_period_score_revisions"
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
TABLES = (
    "sla_period_score_revisions",
    "sla_score_eligibility_intervals",
    "sla_score_monitoring_intervals",
)


def _render(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def _temporary_database(prefix: str) -> Iterator[URL]:
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("migrated-schema test requires TEST_DATABASE_URL")
    base = make_url(configured)
    if not base.drivername.startswith("postgresql"):
        pytest.skip("migrated-schema test requires PostgreSQL")

    name = f"{prefix}_{uuid.uuid4().hex}"
    maintenance = base.set(database="postgres")
    with psycopg.connect(_render(maintenance), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = base.set(database=name)
    original = app_config.settings
    app_config.settings = replace(
        original,
        database_url=target.render_as_string(hide_password=False),
    )
    try:
        yield target
    finally:
        app_config.settings = original
        with psycopg.connect(_render(maintenance), autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
            )


@pytest.fixture(scope="module")
def incremental_database() -> Iterator[URL]:
    with _temporary_database("dotmac_sla_scores_incremental") as target:
        yield target


@pytest.fixture(scope="module")
def head_database() -> Iterator[URL]:
    with _temporary_database("dotmac_sla_scores_head") as target:
        _alembic("head")
        yield target


def _alembic(revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, revision)


def _candidate_objects(url: URL) -> set[str]:
    with psycopg.connect(_render(url)) as conn:
        return {
            name
            for (name,) in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename = ANY(%s)",
                (list(TABLES),),
            ).fetchall()
        }


def _remove_bootstrap_future_schema(url: URL) -> None:
    """Undo current-Base metadata leaked into revision 001's bootstrap."""

    with psycopg.connect(_render(url), autocommit=True) as conn:
        for table in reversed(TABLES):
            conn.execute(
                sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(table))
            )
        conn.execute("DROP FUNCTION IF EXISTS sla_score_evidence_append_only()")


def _seed_subscription(url: URL) -> uuid.UUID:
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            reseller = session.scalar(
                select(Reseller).where(Reseller.is_house.is_(True))
            )
            if reseller is None:
                reseller = Reseller(
                    name=f"SLA scorer {uuid.uuid4().hex[:8]}",
                    is_house=True,
                )
                session.add(reseller)
                session.flush()
            suffix = uuid.uuid4().hex[:8]
            subscriber = Subscriber(
                first_name="SLA",
                last_name="Scorer",
                email=f"sla-{suffix}@example.test",
                reseller_id=reseller.id,
            )
            offer = CatalogOffer(
                name=f"SLA offer {suffix}",
                code=f"SLA-{suffix}",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
                billing_mode=BillingMode.prepaid,
            )
            session.add_all((subscriber, offer))
            session.flush()
            subscription = Subscription(
                subscriber_id=subscriber.id,
                offer_id=offer.id,
                status=SubscriptionStatus.active,
                billing_mode=BillingMode.prepaid,
                start_at=NOW,
            )
            session.add(subscription)
            session.commit()
            return subscription.id
    finally:
        engine.dispose()


def _insert_score(url: URL, subscription_id: uuid.UUID) -> uuid.UUID:
    score_id = uuid.uuid4()
    with psycopg.connect(_render(url), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO sla_period_score_revisions "
            "(id, subscription_id, period_start, period_end, evaluated_at, "
            " revision, eligible_seconds, unavailable_seconds, excluded_seconds, "
            " unknown_seconds, verdict, evidence_complete, completeness_issues, "
            " policy_segments, policy_version_ids, outage_interval_ids, "
            " lifecycle_evidence_ids, evidence_digest, recorded_by, command_id, "
            " command_idempotency_key, correlation_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 1, 3600, 0, 0, 3600, "
            "'unavailable', false, '[\"monitoring:unknown_eligible_coverage\"]', "
            "'[]', '[]', '[]', '[]', %s, 'test:migration', %s, %s, %s, %s)",
            (
                score_id,
                subscription_id,
                NOW,
                NOW + timedelta(hours=1),
                NOW + timedelta(hours=1),
                f"sha256:{'1' * 64}",
                uuid.uuid4(),
                f"score:{uuid.uuid4()}",
                uuid.uuid4(),
                NOW,
            ),
        )
        conn.execute(
            "INSERT INTO sla_score_eligibility_intervals "
            "(id, score_revision_id, subscription_id, starts_at, ends_at, "
            " evidence_grade, entitlement_source, entitlement_evidence_ids, "
            " lifecycle_evidence_ids, fingerprint, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 'authoritative', "
            "'funded_entitlement', '[]', '[]', %s, %s)",
            (
                uuid.uuid4(),
                score_id,
                subscription_id,
                NOW,
                NOW + timedelta(hours=1),
                f"sha256:{'2' * 64}",
                NOW,
            ),
        )
        conn.execute(
            "INSERT INTO sla_score_monitoring_intervals "
            "(id, score_revision_id, subscription_id, starts_at, ends_at, "
            " source, source_id, fingerprint, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 'radius_accounting_session', %s, %s, %s)",
            (
                uuid.uuid4(),
                score_id,
                subscription_id,
                NOW,
                NOW + timedelta(minutes=30),
                uuid.uuid4(),
                f"sha256:{'3' * 64}",
                NOW,
            ),
        )
    return score_id


def test_incremental_479_to_480_creates_only_the_candidate_schema(
    incremental_database: URL,
):
    _alembic(PREDECESSOR)
    _remove_bootstrap_future_schema(incremental_database)
    assert _candidate_objects(incremental_database) == set()

    _alembic(CANDIDATE)

    assert _candidate_objects(incremental_database) == set(TABLES)


def test_head_has_named_constraints_triggers_and_restrict_retention(
    head_database: URL,
):
    with psycopg.connect(_render(head_database)) as conn:
        constraints = {
            name: (kind, delete_rule)
            for name, kind, delete_rule in conn.execute(
                "SELECT conname, contype, confdeltype FROM pg_constraint "
                "WHERE conrelid = ANY(%s::regclass[])",
                (list(TABLES),),
            ).fetchall()
        }
        triggers = {
            table: name
            for table, name in conn.execute(
                "SELECT c.relname, t.tgname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname = ANY(%s) AND NOT t.tgisinternal",
                (list(TABLES),),
            ).fetchall()
        }

    assert {
        "ck_sla_period_scores_no_incomplete_pass",
        "ck_sla_period_scores_accounted_bounds",
        "ck_sla_period_scores_revision_link",
        "uq_sla_period_scores_period_revision",
        "uq_sla_period_scores_period_evidence",
        "uq_sla_period_scores_id_subscription",
        "uq_sla_period_scores_identity_scope",
    } <= constraints.keys()
    assert constraints["fk_sla_period_scores_subscription"] == ("f", "r")
    assert constraints["fk_sla_eligibility_score"] == ("f", "r")
    assert constraints["fk_sla_monitoring_score"] == ("f", "r")
    assert set(triggers) == set(TABLES)


def test_incomplete_passing_is_rejected_by_the_named_constraint(
    head_database: URL,
):
    subscription_id = _seed_subscription(head_database)
    with psycopg.connect(_render(head_database), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation) as caught:
            conn.execute(
                "INSERT INTO sla_period_score_revisions "
                "(id, subscription_id, period_start, period_end, evaluated_at, "
                " revision, eligible_seconds, unavailable_seconds, excluded_seconds, "
                " unknown_seconds, verdict, evidence_complete, completeness_issues, "
                " policy_segments, policy_version_ids, outage_interval_ids, "
                " lifecycle_evidence_ids, evidence_digest, recorded_by, command_id, "
                " correlation_id, created_at) "
                "VALUES (%s, %s, %s, %s, %s, 1, 3600, 0, 0, 3600, 'passing', "
                "false, '[]', '[]', '[]', '[]', '[]', %s, 'test', %s, %s, %s)",
                (
                    uuid.uuid4(),
                    subscription_id,
                    NOW,
                    NOW + timedelta(hours=1),
                    NOW + timedelta(hours=1),
                    f"sha256:{'4' * 64}",
                    uuid.uuid4(),
                    uuid.uuid4(),
                    NOW,
                ),
            )
    assert (
        caught.value.diag.constraint_name == "ck_sla_period_scores_no_incomplete_pass"
    )


def test_evidence_subscription_must_match_its_score_revision(
    head_database: URL,
):
    score_subscription_id = _seed_subscription(head_database)
    other_subscription_id = _seed_subscription(head_database)
    score_id = _insert_score(head_database, score_subscription_id)
    with psycopg.connect(_render(head_database), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation) as caught:
            conn.execute(
                "INSERT INTO sla_score_eligibility_intervals "
                "(id, score_revision_id, subscription_id, starts_at, ends_at, "
                " evidence_grade, entitlement_source, entitlement_evidence_ids, "
                " lifecycle_evidence_ids, fingerprint, created_at) "
                "VALUES (%s, %s, %s, %s, %s, 'authoritative', "
                "'funded_entitlement', '[]', '[]', %s, %s)",
                (
                    uuid.uuid4(),
                    score_id,
                    other_subscription_id,
                    NOW,
                    NOW + timedelta(hours=1),
                    f"sha256:{'6' * 64}",
                    NOW,
                ),
            )
    assert caught.value.diag.constraint_name == "fk_sla_eligibility_score"


def test_a_revision_cannot_supersede_a_different_period(head_database: URL):
    subscription_id = _seed_subscription(head_database)
    first_id = _insert_score(head_database, subscription_id)
    with psycopg.connect(_render(head_database), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation) as caught:
            conn.execute(
                "INSERT INTO sla_period_score_revisions "
                "(id, subscription_id, period_start, period_end, evaluated_at, "
                " revision, supersedes_id, eligible_seconds, unavailable_seconds, "
                " excluded_seconds, unknown_seconds, verdict, evidence_complete, "
                " completeness_issues, policy_segments, policy_version_ids, "
                " outage_interval_ids, lifecycle_evidence_ids, evidence_digest, "
                " recorded_by, command_id, correlation_id, created_at) "
                "VALUES (%s, %s, %s, %s, %s, 2, %s, 3600, 0, 0, 3600, "
                "'unavailable', false, '[]', '[]', '[]', '[]', '[]', %s, "
                "'test', %s, %s, %s)",
                (
                    uuid.uuid4(),
                    subscription_id,
                    NOW + timedelta(hours=1),
                    NOW + timedelta(hours=2),
                    NOW + timedelta(hours=2),
                    first_id,
                    f"sha256:{'7' * 64}",
                    uuid.uuid4(),
                    uuid.uuid4(),
                    NOW,
                ),
            )
    assert caught.value.diag.constraint_name == "fk_sla_period_scores_supersedes"


@pytest.mark.parametrize("table", TABLES)
def test_each_score_evidence_table_is_append_only(head_database: URL, table: str):
    subscription_id = _seed_subscription(head_database)
    score_id = _insert_score(head_database, subscription_id)
    key = "id" if table == "sla_period_score_revisions" else "score_revision_id"
    value = score_id
    with psycopg.connect(_render(head_database), autocommit=True) as conn:
        with pytest.raises(psycopg.Error) as update_error:
            conn.execute(
                sql.SQL("UPDATE {} SET created_at = created_at WHERE {} = %s").format(
                    sql.Identifier(table), sql.Identifier(key)
                ),
                (value,),
            )
        with pytest.raises(psycopg.Error) as delete_error:
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE {} = %s").format(
                    sql.Identifier(table), sql.Identifier(key)
                ),
                (value,),
            )
    assert "append-only" in str(update_error.value)
    assert "append-only" in str(delete_error.value)


def test_append_only_tables_still_accept_a_new_revision(
    head_database: URL,
):
    subscription_id = _seed_subscription(head_database)
    first_id = _insert_score(head_database, subscription_id)
    second_id = uuid.uuid4()
    with psycopg.connect(_render(head_database), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO sla_period_score_revisions "
            "(id, subscription_id, period_start, period_end, evaluated_at, "
            " revision, supersedes_id, eligible_seconds, unavailable_seconds, "
            " excluded_seconds, unknown_seconds, verdict, evidence_complete, "
            " completeness_issues, policy_segments, policy_version_ids, "
            " outage_interval_ids, lifecycle_evidence_ids, evidence_digest, "
            " recorded_by, command_id, correlation_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 2, %s, 3600, 60, 0, 3540, "
            "'unavailable', false, '[]', '[]', '[]', '[]', '[]', %s, "
            "'test', %s, %s, %s)",
            (
                second_id,
                subscription_id,
                NOW,
                NOW + timedelta(hours=1),
                NOW + timedelta(hours=1),
                first_id,
                f"sha256:{'5' * 64}",
                uuid.uuid4(),
                uuid.uuid4(),
                NOW,
            ),
        )
        found = conn.execute(
            "SELECT revision, supersedes_id FROM sla_period_score_revisions "
            "WHERE id = %s",
            (second_id,),
        ).fetchone()
    assert found == (2, first_id)
