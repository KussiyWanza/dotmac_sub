from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/423_prepaid_opening_funding_reconciliation.py"
    )
    spec = importlib.util.spec_from_file_location("migration_423", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prepaid_opening_funding_revision_is_linear_additive_and_structural():
    migration = _load_migration()

    assert migration.revision == "423_prepaid_opening_funding_reconciliation"
    assert migration.down_revision == "422_conversation_ticket_handoff"
    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert "prepaid_opening_funding_consumptions" in source
    assert "prepaid_draft_reconciliation_exceptions" in source
    assert "prepaid_funding_baselines.id" in source
    assert "uq_prepaid_opening_consumption_invoice" in source
    assert "uq_prepaid_opening_consumption_idempotency" in source
    assert "uq_prepaid_draft_exception_invoice" in source
    assert "op.execute" not in source
