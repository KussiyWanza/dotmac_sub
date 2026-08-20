"""Deployed-schema contract for carried-source identity adjudication."""

from sqlalchemy import inspect, text

TABLE = "carried_source_identity_adjudications"


def test_carried_source_identity_adjudication_schema_is_migration_owned(
    db_session,
) -> None:
    inspector = inspect(db_session.get_bind())
    columns = {item["name"]: item for item in inspector.get_columns(TABLE)}
    assert {
        "id",
        "account_id",
        "disposition",
        "source_system",
        "financial_handoff_at",
        "account_created_at",
        "preview_fingerprint",
        "evidence_ref",
        "evidence_sha256",
        "reviewed_by_id",
        "approved_by_id",
        "reason",
        "idempotency_key",
        "command_fingerprint",
        "command_id",
        "correlation_id",
        "created_at",
    } == set(columns)
    assert all(not item["nullable"] for item in columns.values())

    checks = {item["name"] for item in inspector.get_check_constraints(TABLE)}
    assert {
        "ck_carried_source_identity_distinct_reviewers",
        "ck_carried_source_identity_digest_lengths",
        "ck_carried_source_identity_review_evidence",
    } <= checks
    unique_constraints = {
        item["name"] for item in inspector.get_unique_constraints(TABLE)
    }
    assert {
        "uq_carried_source_identity_adjudications_account",
        "uq_carried_source_identity_adjudications_idempotency",
        "uq_carried_source_identity_adjudications_command",
    } <= unique_constraints
    foreign_keys = {
        tuple(item["constrained_columns"]): (
            item["referred_table"],
            tuple(item["referred_columns"]),
            item["options"].get("ondelete"),
        )
        for item in inspector.get_foreign_keys(TABLE)
    }
    assert foreign_keys[("account_id",)] == (
        "subscribers",
        ("id",),
        "RESTRICT",
    )
    assert foreign_keys[("reviewed_by_id",)] == (
        "system_users",
        ("id",),
        "RESTRICT",
    )
    assert foreign_keys[("approved_by_id",)] == (
        "system_users",
        ("id",),
        "RESTRICT",
    )
    append_only_trigger = db_session.scalar(
        text(
            "SELECT count(*) FROM pg_trigger "
            "WHERE tgrelid = "
            "'carried_source_identity_adjudications'::regclass "
            "AND tgname = "
            "'trg_carried_source_identity_adjudications_append_only' "
            "AND NOT tgisinternal"
        )
    )
    assert append_only_trigger == 1
