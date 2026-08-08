"""Guards for the canonical admin customer-search read path."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = PROJECT_ROOT / "alembic/versions/504_customer_search_trigram_indexes.py"


def test_customer_search_indexes_every_previously_unindexed_or_branch() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")

    # Re-parented onto 503_reconcile_ticket_portal_visibility: both migrations
    # were numbered 503 off 502 in parallel branches and merged within seconds
    # of each other, leaving two Alembic heads. The trigram work is independent
    # of the ticket-visibility change, so it simply follows it.
    assert (
        'down_revision: str | None = "503_reconcile_ticket_portal_visibility"'
        in migration
    )
    assert '("ix_trgm_subscribers_display_name", "display_name")' in migration
    assert '("ix_trgm_subscribers_phone", "phone")' in migration
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in migration
    assert "gin_trgm_ops" in migration
