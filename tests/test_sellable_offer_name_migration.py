"""Migration contract for adjudicated legacy sellable-offer collisions."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/489_unique_sellable_offer_name.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "unique_sellable_offer_name", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Rows:
    def __init__(self, rows: list[tuple[str, int]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[str, int]]:
        return self._rows


def test_upgrade_repairs_only_confirmed_legacy_rows_before_index(monkeypatch) -> None:
    migration = _module()
    bind = Mock()
    bind.execute.side_effect = [Mock(), Mock(), _Rows([])]
    create_index = Mock()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "create_index", create_index)

    migration.upgrade()

    first, second, collision_check = bind.execute.call_args_list
    assert first.args[1] == {
        "tariff_id": 71,
        "expected_name": "25 Mbps Fiber",
    }
    assert "available_for_services = false" in str(first.args[0])
    assert "splynx_tariff_id = :tariff_id" in str(first.args[0])
    assert second.args[1] == {
        "tariff_id": 79,
        "expected_name": "Unlimited Pro",
    }
    assert "status = 'archived'" in str(second.args[0])
    assert "is_active = false" in str(second.args[0])
    assert "GROUP BY name HAVING count(*) > 1" in str(collision_check.args[0])
    create_index.assert_called_once()


def test_upgrade_still_fails_closed_for_unadjudicated_collision(monkeypatch) -> None:
    migration = _module()
    bind = Mock()
    bind.execute.side_effect = [Mock(), Mock(), _Rows([("Other Plan", 2)])]
    create_index = Mock()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "create_index", create_index)

    with pytest.raises(RuntimeError, match="'Other Plan' x2"):
        migration.upgrade()

    create_index.assert_not_called()
