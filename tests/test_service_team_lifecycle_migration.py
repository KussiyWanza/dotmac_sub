from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/426_service_team_lifecycle.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_426_service_team_lifecycle",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(
        self,
        *,
        scalar: object | None = None,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self) -> object | None:
        return self._scalar

    def scalar_one(self) -> object:
        assert self._scalar is not None
        return self._scalar

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, object]]:
        return self._rows


def _statement_text(call) -> str:
    return str(call.args[0])


def test_revision_is_linear_forward_only_party_identity_cutover() -> None:
    migration = _load_migration()
    source = MIGRATION.read_text(encoding="utf-8")

    assert migration.revision == "426_service_team_lifecycle"
    assert migration.down_revision == "425_vendor_project_intake_evidence"
    assert "fk_service_teams_manager_person_id_parties" in source
    assert "fk_service_team_members_person_id_parties" in source
    assert "ux_service_teams_name_ci" in source
    assert "ck_service_teams_workforce_reference_pair" in source
    assert "support_service_teams" in source
    assert "support_service_team_members" in source

    with pytest.raises(RuntimeError, match="irreversible"):
        migration.downgrade()


def test_setting_member_backfill_translates_principal_to_person_party(
    monkeypatch,
) -> None:
    migration = _load_migration()
    team_id = UUID("00000000-0000-0000-0000-000000000101")
    system_user_id = UUID("00000000-0000-0000-0000-000000000102")
    person_party_id = UUID("00000000-0000-0000-0000-000000000103")
    bind = Mock()
    bind.execute.return_value = _Result()

    monkeypatch.setattr(
        migration,
        "_setting",
        lambda _bind, key: (
            {str(team_id): [str(system_user_id)]}
            if key == migration._MEMBER_KEY
            else None
        ),
    )
    translated: list[tuple[UUID, bool]] = []

    def translate(_bind, value: UUID, *, require_active: bool = True) -> UUID:
        translated.append((value, require_active))
        return person_party_id

    monkeypatch.setattr(migration, "_person_party_for_system_user", translate)

    migration._backfill_setting_members(bind)

    assert translated == [(system_user_id, True)]
    insert = next(
        call
        for call in bind.execute.call_args_list
        if "INSERT INTO service_team_members" in _statement_text(call)
    )
    assert insert.args[1] == {
        "team_id": team_id,
        "person_id": person_party_id,
    }
    assert "ON CONFLICT (team_id, person_id) DO UPDATE" in _statement_text(insert)


def test_compatibility_member_uuid_is_rewritten_to_person_party(monkeypatch) -> None:
    migration = _load_migration()
    member_id = UUID("00000000-0000-0000-0000-000000000201")
    team_id = UUID("00000000-0000-0000-0000-000000000202")
    system_user_id = UUID("00000000-0000-0000-0000-000000000203")
    person_party_id = UUID("00000000-0000-0000-0000-000000000204")
    bind = Mock()
    bind.execute.side_effect = [
        _Result(
            rows=[
                {
                    "id": member_id,
                    "team_id": team_id,
                    "person_id": system_user_id,
                    "is_active": True,
                }
            ]
        ),
        _Result(scalar=None),
        _Result(scalar=person_party_id),
        _Result(scalar=None),
        _Result(),
        _Result(rows=[]),
    ]
    monkeypatch.setattr(
        migration,
        "_person_party_for_system_user",
        lambda _bind, value, *, require_active=True: (
            person_party_id
            if value == system_user_id and require_active
            else pytest.fail("unexpected identity translation")
        ),
    )

    migration._rewrite_compatibility_person_ids(bind)

    update = next(
        call
        for call in bind.execute.call_args_list
        if "UPDATE service_team_members SET person_id" in _statement_text(call)
    )
    assert update.args[1] == {"target": person_party_id, "id": member_id}


def test_compatibility_rewrite_rejects_ambiguous_party_and_principal_uuid() -> None:
    migration = _load_migration()
    member_id = UUID("00000000-0000-0000-0000-000000000301")
    team_id = UUID("00000000-0000-0000-0000-000000000302")
    stored_id = UUID("00000000-0000-0000-0000-000000000303")
    different_party_id = UUID("00000000-0000-0000-0000-000000000304")
    bind = Mock()
    bind.execute.side_effect = [
        _Result(
            rows=[
                {
                    "id": member_id,
                    "team_id": team_id,
                    "person_id": stored_id,
                    "is_active": True,
                }
            ]
        ),
        _Result(scalar="active"),
        _Result(scalar=different_party_id),
    ]

    with pytest.raises(RuntimeError, match="Ambiguous service-team identity"):
        migration._rewrite_compatibility_person_ids(bind)


def test_setting_backfill_rejects_malformed_payloads(monkeypatch) -> None:
    migration = _load_migration()
    bind = Mock()
    monkeypatch.setattr(migration, "_setting", lambda *_args: {"not": "an array"})

    with pytest.raises(RuntimeError, match="must be a JSON array"):
        migration._backfill_setting_teams(bind)

    monkeypatch.setattr(migration, "_setting", lambda *_args: ["not an object"])
    with pytest.raises(RuntimeError, match="contains a non-object"):
        migration._backfill_setting_teams(bind)


def test_upgrade_retires_settings_only_after_backfill_and_constraints(
    monkeypatch,
) -> None:
    migration = _load_migration()
    calls: list[str] = []
    bind = Mock()
    bind.execute.side_effect = [
        _Result(scalar=None),
        _Result(scalar=None),
        _Result(),
        _Result(),
    ]
    operations = SimpleNamespace(
        get_bind=lambda: bind,
        add_column=lambda table, column: calls.append(f"column:{table}.{column.name}"),
        create_index=lambda *_args, **_kwargs: calls.append("index"),
        create_foreign_key=lambda name, *_args, **_kwargs: calls.append(name),
        create_check_constraint=lambda name, *_args, **_kwargs: calls.append(name),
        create_unique_constraint=lambda name, *_args, **_kwargs: calls.append(name),
    )
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(
        migration,
        "_backfill_setting_teams",
        lambda _bind: calls.append("teams"),
    )
    monkeypatch.setattr(
        migration,
        "_rewrite_compatibility_person_ids",
        lambda _bind: calls.append("identities"),
    )
    monkeypatch.setattr(
        migration,
        "_backfill_setting_members",
        lambda _bind: calls.append("members"),
    )

    migration.upgrade()

    assert calls == [
        "teams",
        "identities",
        "members",
        "index",
        "fk_service_teams_manager_person_id_parties",
        "fk_service_team_members_person_id_parties",
        "ck_service_teams_workforce_reference_pair",
    ]
    delete = bind.execute.call_args_list[-1]
    assert "DELETE FROM domain_settings" in _statement_text(delete)
    assert delete.args[1] == {
        "team_key": migration._TEAM_KEY,
        "member_key": migration._MEMBER_KEY,
    }
