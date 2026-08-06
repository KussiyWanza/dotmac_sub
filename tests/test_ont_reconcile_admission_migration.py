"""Migration-chain contract for positive ONT reconcile admission."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/490_ont_reconcile_positive_admission.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "ont_reconcile_positive_admission", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_positive_admission_is_the_single_linear_head():
    module = _module()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    assert module.revision == "490_ont_reconcile_positive_admission"
    assert module.down_revision == "489_unique_sellable_offer_name"
    # Single-headed with this revision in the head's ancestry. Naming the
    # exact head breaks the test on every later migration.
    heads = script.get_heads()
    assert len(heads) == 1
    assert module.revision in {
        item.revision
        for item in script.iterate_revisions(heads[0], module.revision, inclusive=True)
    }


def test_schema_enforces_expiry_idempotency_and_one_active_admission():
    source = MIGRATION.read_text(encoding="utf-8")

    assert "expires_at > admitted_at" in source
    assert "uq_ont_reconcile_admissions_idempotency_key" in source
    assert "uq_ont_reconcile_admissions_active_per_ont_scope" in source
    assert "postgresql_where=sa.text(\"status = 'active'\")" in source
