from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/420_billing_run_launch_evidence.py"


def test_billing_run_evidence_is_the_single_migration_head() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["420_billing_run_launch_evidence"]
    assert (
        script.get_revision("420_billing_run_launch_evidence").down_revision
        == "419_customer_wht_policy_and_direct_targets"
    )


def test_migration_retires_the_dead_schedule_and_adds_launch_evidence() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'op.drop_table("billing_run_schedules")' in source
    assert "billing_run_schedule_config" in source
    for column in (
        "launch_kind",
        "requested_by",
        "preview_fingerprint",
        "source_run_id",
    ):
        assert f'"{column}"' in source
    assert "fk_billing_runs_source_run_id" in source
    assert "ix_billing_runs_source_run_id" in source
