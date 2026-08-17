"""Fail-closed contract for migration 541's staff-session Party ratchet."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/541_staff_session_party_ratchet.py"


def _load_migration() -> ModuleType:
    assert MIGRATION.exists(), "write the strict ratchet migration first"
    spec = importlib.util.spec_from_file_location("migration_538_ratchet", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(
        self,
        *,
        missing: int,
        unbound: int,
        disagreement: int,
        wrong_kind: int,
    ) -> None:
        self._row = SimpleNamespace(
            usable_staff_without_party=missing,
            usable_staff_unbound=unbound,
            projection_disagreements=disagreement,
            party_without_staff_context=wrong_kind,
        )

    def one(self) -> SimpleNamespace:
        return self._row


class _Bind:
    def __init__(
        self,
        *,
        missing: int,
        unbound: int,
        disagreement: int,
        wrong_kind: int,
    ) -> None:
        self.missing = missing
        self.unbound = unbound
        self.disagreement = disagreement
        self.wrong_kind = wrong_kind
        self.statements: list[str] = []

    def execute(self, statement: Any) -> _Result:
        self.statements.append(str(statement))
        return _Result(
            missing=self.missing,
            unbound=self.unbound,
            disagreement=self.disagreement,
            wrong_kind=self.wrong_kind,
        )


class _Operations:
    def __init__(
        self,
        *,
        missing: int = 0,
        unbound: int = 0,
        disagreement: int = 0,
        wrong_kind: int = 0,
    ) -> None:
        self.bind = _Bind(
            missing=missing,
            unbound=unbound,
            disagreement=disagreement,
            wrong_kind=wrong_kind,
        )
        self.created: list[tuple[str, str, str]] = []

    def get_bind(self) -> _Bind:
        return self.bind

    def create_check_constraint(self, name: str, table: str, sql: str) -> None:
        self.created.append((name, table, sql))


def test_revision_extends_the_current_head() -> None:
    migration = _load_migration()

    assert migration.revision == "541_staff_session_party_ratchet"
    assert migration.down_revision == "540_ticket_comment_mentions"


@pytest.mark.parametrize(
    ("missing", "unbound", "disagreement", "wrong_kind", "message"),
    [
        (1, 0, 0, 0, "usable_staff_without_party=1"),
        (0, 1, 0, 0, "usable_staff_unbound=1"),
        (0, 0, 1, 0, "projection_disagreements=1"),
        (0, 0, 0, 1, "party_without_staff_context=1"),
    ],
)
def test_preflight_refuses_each_blocking_population_before_ddl(
    monkeypatch: pytest.MonkeyPatch,
    missing: int,
    unbound: int,
    disagreement: int,
    wrong_kind: int,
    message: str,
) -> None:
    migration = _load_migration()
    operations = _Operations(
        missing=missing,
        unbound=unbound,
        disagreement=disagreement,
        wrong_kind=wrong_kind,
    )
    monkeypatch.setattr(migration, "op", operations)

    with pytest.raises(RuntimeError, match=message):
        migration.upgrade()

    assert operations.created == []


def test_zero_blockers_installs_both_specific_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _Operations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert [call[:2] for call in operations.created] == [
        ("ck_sessions_active_staff_requires_party", "sessions"),
        ("ck_sessions_party_requires_staff_context", "sessions"),
    ]
    first_sql = operations.created[0][2]
    second_sql = operations.created[1][2]
    assert "status <> 'active'" in first_sql
    assert "revoked_at IS NOT NULL" in first_sql
    assert "party_id IS NOT NULL" in first_sql
    assert second_sql == "party_id IS NULL OR system_user_id IS NOT NULL"
    preflight_sql = operations.bind.statements[0]
    assert "LEFT JOIN system_users" in preflight_sql
    assert "LEFT JOIN parties" in preflight_sql
    assert "su.is_active IS NOT TRUE" in preflight_sql
    assert "s.party_id IS DISTINCT FROM su.person_party_id" in preflight_sql
    assert "p.party_type IS DISTINCT FROM 'person'" in preflight_sql
