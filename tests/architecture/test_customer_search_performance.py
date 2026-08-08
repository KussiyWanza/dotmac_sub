"""Guards for the canonical admin customer-search read path."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = PROJECT_ROOT / "alembic/versions/503_customer_search_trigram_indexes.py"


def test_customer_search_indexes_every_previously_unindexed_or_branch() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")

    assert (
        'down_revision: str | None = "502_open_setting_domain_vocabulary"' in migration
    )
    assert '("ix_trgm_subscribers_display_name", "display_name")' in migration
    assert '("ix_trgm_subscribers_phone", "phone")' in migration
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in migration
    assert "gin_trgm_ops" in migration
