from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/408_radius_session_latest_projection.py"
VALIDATION_MIGRATION = (
    ROOT / "alembic/versions/410_validate_radius_session_latest_index.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "radius_session_latest_projection", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_radius_session_projection_remains_in_the_single_migration_chain() -> None:
    module = _module()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    assert module.revision == "408_radius_session_latest_projection"
    assert module.down_revision == "407_retire_parallel_radius_refresh"
    assert script.get_heads() == ["418_payment_channel_mapping_sot"]
    assert (
        script.get_revision("418_payment_channel_mapping_sot").down_revision
        == "417_service_extension_grant_intervals"
    )


def test_radius_session_projection_uses_nonblocking_postgres_ddl() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "ensure_postgres_index(bind, op.execute)" in source
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" not in source
    assert "autocommit_block()" in source


def test_forward_revision_validates_databases_already_stamped_408() -> None:
    source = VALIDATION_MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "410_validate_radius_session_latest_index"' in source
    assert 'down_revision = "409_tr069_operation_lifecycle"' in source
    assert "ensure_postgres_index(bind, op.execute)" in source
    assert "autocommit_block()" in source
