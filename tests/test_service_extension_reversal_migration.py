"""Migration contract for append-only service-extension reversals."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/472_service_extension_reversals.py"


def test_service_extension_reversal_is_the_single_additive_head() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["474_lifecycle_evidence_authority"]
    assert (
        script.get_revision("472_service_extension_reversals").down_revision
        == "471_quote_documents_and_delivery"
    )


def test_service_extension_reversal_migration_preserves_original_evidence() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert (
        "ALTER TYPE serviceextensionstatus ADD VALUE IF NOT EXISTS 'reversed'" in source
    )
    assert '"service_extension_reversals"' in source
    assert '"service_extension_reversal_entries"' in source
    assert "uq_service_extension_reversals_extension" in source
    assert "uq_service_extension_reversal_entries_extension_entry" in source
    assert 'ondelete="RESTRICT"' in source
    assert '"billing:extension:reverse"' in source
    assert "role_permissions.insert()" in source
    assert "DELETE FROM service_extension_entries" not in source
    assert "UPDATE service_extension_entries" not in source
