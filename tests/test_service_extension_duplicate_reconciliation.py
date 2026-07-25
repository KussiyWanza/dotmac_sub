"""Historical duplicate repair owned by financial.service_extensions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.idempotency import IdempotencyKey
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.service_extensions import (
    ChainedGrantResolution,
    ReconcileServiceExtensionDuplicatesCommand,
    ServiceExtensionDuplicateKind,
    preview_service_extension_duplicate_reconciliation,
    reconcile_service_extension_duplicates,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _legacy_engine() -> sa.Engine:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                CREATE TABLE service_extensions (
                    id TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    window_start DATETIME NOT NULL,
                    window_end DATETIME NOT NULL,
                    days INTEGER NOT NULL,
                    scope_type TEXT NOT NULL,
                    scope_id TEXT,
                    scope_subscriber_ids JSON,
                    status TEXT NOT NULL,
                    affected_count INTEGER NOT NULL,
                    skipped_count INTEGER NOT NULL,
                    resumed_count INTEGER NOT NULL DEFAULT 0,
                    still_suspended_count INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT,
                    applied_by TEXT,
                    applied_at DATETIME,
                    canceled_by TEXT,
                    canceled_at DATETIME,
                    create_idempotency_key_sha256 TEXT,
                    create_fingerprint_sha256 TEXT,
                    create_command_id TEXT,
                    create_correlation_id TEXT,
                    apply_idempotency_key_sha256 TEXT,
                    apply_command_id TEXT,
                    apply_correlation_id TEXT,
                    cancel_idempotency_key_sha256 TEXT,
                    cancel_command_id TEXT,
                    cancel_correlation_id TEXT,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                CREATE TABLE service_extension_entries (
                    id TEXT PRIMARY KEY,
                    extension_id TEXT NOT NULL,
                    subscription_id TEXT NOT NULL,
                    subscriber_id TEXT NOT NULL,
                    previous_next_billing_at DATETIME,
                    grant_starts_at DATETIME,
                    grant_ends_at DATETIME,
                    anchor_basis TEXT,
                    new_next_billing_at DATETIME,
                    policy_version INTEGER,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                CREATE TABLE subscriptions (
                    id TEXT PRIMARY KEY,
                    next_billing_at DATETIME
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                CREATE TABLE prepaid_coverage_reconciliation_items (
                    id TEXT PRIMARY KEY,
                    source_service_extension_entry_id TEXT
                )
                """
            )
        )
    AuditEvent.__table__.create(engine)
    IdempotencyKey.__table__.create(engine)
    return engine


def _iso(day: int) -> str:
    return f"2026-08-{day:02d}T00:00:00+00:00"


def _seed_group(
    engine: sa.Engine,
    *,
    extension_id: str,
    subscription_id: str,
    subscriber_id: str,
    days: int,
    intervals: tuple[tuple[str, str, str], ...],
    current_anchor: str,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO subscriptions (id, next_billing_at)
                VALUES (:subscription_id, :current_anchor)
                """
            ),
            {
                "subscription_id": subscription_id,
                "current_anchor": current_anchor,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO service_extensions (
                    id, reason, window_start, window_end, days, scope_type,
                    status, affected_count, skipped_count, applied_at, created_at
                ) VALUES (
                    :extension_id, 'outage compensation',
                    '2026-07-01T00:00:00+00:00',
                    '2026-07-02T00:00:00+00:00',
                    :days, 'subscribers', 'applied', 1, 0,
                    '2026-07-03T00:00:00+00:00',
                    '2026-07-03T00:00:00+00:00'
                )
                """
            ),
            {"extension_id": extension_id, "days": days},
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO service_extension_entries (
                    id, extension_id, subscription_id, subscriber_id,
                    previous_next_billing_at, new_next_billing_at, created_at
                ) VALUES (
                    :entry_id, :extension_id, :subscription_id, :subscriber_id,
                    :previous_anchor, :new_anchor, :created_at
                )
                """
            ),
            [
                {
                    "entry_id": entry_id,
                    "extension_id": extension_id,
                    "subscription_id": subscription_id,
                    "subscriber_id": subscriber_id,
                    "previous_anchor": previous_anchor,
                    "new_anchor": new_anchor,
                    "created_at": (f"2026-07-03T00:00:{index:02d}+00:00"),
                }
                for index, (entry_id, previous_anchor, new_anchor) in enumerate(
                    intervals,
                    start=1,
                )
            ],
        )


def _command(
    fingerprint: str,
    *,
    key: str = "service-extension-duplicates-2026-07-25",
) -> ReconcileServiceExtensionDuplicatesCommand:
    return ReconcileServiceExtensionDuplicatesCommand(
        context=CommandContext.system(
            actor="system:production-reconciliation",
            scope="service_extension_duplicate_reconciliation",
            reason="preserve approved customer entitlement and remove duplicate evidence",
            idempotency_key=key,
        ),
        preview_fingerprint=fingerprint,
        effective_at=datetime(2026, 7, 25, tzinfo=UTC),
        chained_grant_resolution=(
            ChainedGrantResolution.preserve_as_corrective_extension
        ),
    )


def test_preview_classifies_exact_and_chained_groups_deterministically() -> None:
    engine = _legacy_engine()
    _seed_group(
        engine,
        extension_id="00000000-0000-0000-0000-000000000101",
        subscription_id="00000000-0000-0000-0000-000000000201",
        subscriber_id="00000000-0000-0000-0000-000000000301",
        days=5,
        intervals=(
            ("00000000-0000-0000-0000-000000000401", _iso(4), _iso(9)),
            ("00000000-0000-0000-0000-000000000402", _iso(4), _iso(9)),
        ),
        current_anchor=_iso(9),
    )
    _seed_group(
        engine,
        extension_id="00000000-0000-0000-0000-000000000102",
        subscription_id="00000000-0000-0000-0000-000000000202",
        subscriber_id="00000000-0000-0000-0000-000000000302",
        days=5,
        intervals=(
            ("00000000-0000-0000-0000-000000000403", _iso(4), _iso(9)),
            ("00000000-0000-0000-0000-000000000404", _iso(9), _iso(14)),
        ),
        current_anchor=_iso(14),
    )

    with Session(engine) as db:
        preview = preview_service_extension_duplicate_reconciliation(db)
        repeated = preview_service_extension_duplicate_reconciliation(db)

    assert preview.fingerprint == repeated.fingerprint
    assert len(preview.fingerprint) == 64
    assert preview.exact_duplicate_count == 1
    assert preview.chained_grant_count == 1
    assert preview.manual_review_count == 0
    assert {item.kind for item in preview.groups} == {
        ServiceExtensionDuplicateKind.exact_duplicate,
        ServiceExtensionDuplicateKind.chained_grant,
    }


def test_apply_collapses_copies_and_preserves_chained_entitlement_atomically() -> None:
    engine = _legacy_engine()
    _seed_group(
        engine,
        extension_id="00000000-0000-0000-0000-000000000111",
        subscription_id="00000000-0000-0000-0000-000000000211",
        subscriber_id="00000000-0000-0000-0000-000000000311",
        days=14,
        intervals=(
            ("00000000-0000-0000-0000-000000000411", _iso(1), _iso(15)),
            ("00000000-0000-0000-0000-000000000412", _iso(1), _iso(15)),
        ),
        current_anchor=_iso(15),
    )
    _seed_group(
        engine,
        extension_id="00000000-0000-0000-0000-000000000112",
        subscription_id="00000000-0000-0000-0000-000000000212",
        subscriber_id="00000000-0000-0000-0000-000000000312",
        days=5,
        intervals=(
            ("00000000-0000-0000-0000-000000000413", _iso(4), _iso(9)),
            ("00000000-0000-0000-0000-000000000414", _iso(9), _iso(14)),
        ),
        current_anchor=_iso(14),
    )

    with Session(engine) as db:
        preview = preview_service_extension_duplicate_reconciliation(db)
        db.rollback()
        result = reconcile_service_extension_duplicates(
            db,
            _command(preview.fingerprint),
        )

    assert result.exact_duplicates_collapsed == 1
    assert result.chained_grants_preserved == 1
    assert result.replayed is False

    with Session(engine) as db:
        remaining = preview_service_extension_duplicate_reconciliation(db)
        anchor = db.execute(
            sa.text(
                """
                SELECT next_billing_at
                FROM subscriptions
                WHERE id = '00000000-0000-0000-0000-000000000212'
                """
            )
        ).scalar_one()
        corrective = db.execute(
            sa.text(
                """
                SELECT x.days, x.status, e.previous_next_billing_at,
                       e.new_next_billing_at
                FROM service_extension_entries e
                JOIN service_extensions x ON x.id = e.extension_id
                WHERE e.id = '00000000-0000-0000-0000-000000000414'
                """
            )
        ).one()
        audit_count = db.scalar(sa.select(sa.func.count()).select_from(AuditEvent))

    assert remaining.groups == ()
    assert str(anchor).startswith("2026-08-14")
    assert corrective.days == 5
    assert corrective.status == "applied"
    assert str(corrective.previous_next_billing_at).startswith("2026-08-09")
    assert str(corrective.new_next_billing_at).startswith("2026-08-14")
    assert audit_count == 2

    with Session(engine) as db:
        replay = reconcile_service_extension_duplicates(
            db,
            _command(preview.fingerprint),
        )
    assert replay.replayed is True
    assert replay.exact_duplicates_collapsed == 1
    assert replay.chained_grants_preserved == 1


def test_apply_rejects_stale_fingerprint_without_writes() -> None:
    engine = _legacy_engine()
    _seed_group(
        engine,
        extension_id="00000000-0000-0000-0000-000000000121",
        subscription_id="00000000-0000-0000-0000-000000000221",
        subscriber_id="00000000-0000-0000-0000-000000000321",
        days=5,
        intervals=(
            ("00000000-0000-0000-0000-000000000421", _iso(4), _iso(9)),
            ("00000000-0000-0000-0000-000000000422", _iso(4), _iso(9)),
        ),
        current_anchor=_iso(9),
    )

    with Session(engine) as db:
        try:
            reconcile_service_extension_duplicates(db, _command("0" * 64))
        except DomainError as exc:
            assert exc.code.endswith("duplicate_reconciliation_stale_preview")
        else:
            raise AssertionError("stale preview must fail closed")

    with Session(engine) as db:
        count = db.execute(
            sa.text("SELECT COUNT(*) FROM service_extension_entries")
        ).scalar_one()
    assert count == 2


def test_deploy_runs_candidate_preflight_before_migrations() -> None:
    source = (PROJECT_ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")

    preflight = (
        "python -m scripts.migration.reconcile_service_extension_duplicates --check"
    )
    assert preflight in source
    assert source.index(preflight) < source.index(
        'log "Applying migrations (alembic upgrade heads)"'
    )
