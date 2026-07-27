"""Cut service-team identity and membership over to native Party-backed rows.

Revision ID: 426_service_team_lifecycle
Revises: 425_vendor_project_intake_evidence
"""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa

from alembic import op

revision = "426_service_team_lifecycle"
down_revision = "425_vendor_project_intake_evidence"
branch_labels = None
depends_on = None

_TEAM_KEY = "support_service_teams"
_MEMBER_KEY = "support_service_team_members"


def _uuid(value: object, *, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {field} UUID in service-team cutover") from exc


def _setting(bind, key: str):
    return bind.execute(
        sa.text(
            "SELECT value_json FROM domain_settings "
            "WHERE domain = CAST('workflow' AS settingdomain) "
            "AND key = :key AND is_active IS TRUE"
        ),
        {"key": key},
    ).scalar_one_or_none()


def _team_type(label: str) -> str:
    normalized = label.casefold()
    if "field" in normalized:
        return "field_service"
    if "operation" in normalized or "ops" in normalized:
        return "operations"
    return "support"


def _backfill_setting_teams(bind) -> None:
    payload = _setting(bind, _TEAM_KEY)
    if payload is None:
        return
    if not isinstance(payload, list):
        raise RuntimeError("support_service_teams must be a JSON array")
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError("support_service_teams contains a non-object")
        team_id = _uuid(item.get("id"), field="team")
        label = " ".join(str(item.get("label") or "").split())
        if not label:
            raise RuntimeError("support_service_teams contains a blank label")
        existing = bind.execute(
            sa.text("SELECT name FROM service_teams WHERE id = :id"),
            {"id": team_id},
        ).scalar_one_or_none()
        if existing is not None and existing != label:
            raise RuntimeError(
                "Workflow and native service-team names disagree; adjudicate before cutover"
            )
        if existing is None:
            bind.execute(
                sa.text(
                    "INSERT INTO service_teams "
                    "(id, name, team_type, is_active, created_at, updated_at) "
                    "VALUES (:id, :name, :team_type, TRUE, now(), now())"
                ),
                {"id": team_id, "name": label, "team_type": _team_type(label)},
            )


def _person_party_for_system_user(
    bind,
    system_user_id: UUID,
    *,
    require_active: bool = True,
) -> UUID:
    person_party_id = bind.execute(
        sa.text(
            "SELECT system_users.person_party_id FROM system_users "
            "JOIN parties ON parties.id = system_users.person_party_id "
            "WHERE system_users.id = :id "
            "AND parties.party_type = 'person' "
            "AND (:require_active IS FALSE OR "
            "(system_users.is_active IS TRUE AND parties.status = 'active'))"
        ),
        {"id": system_user_id, "require_active": require_active},
    ).scalar_one_or_none()
    if person_party_id is None:
        raise RuntimeError(
            "Service-team member has no active reviewed SystemUser → Person Party binding"
        )
    return UUID(str(person_party_id))


def _backfill_setting_members(bind) -> None:
    payload = _setting(bind, _MEMBER_KEY)
    if payload is None:
        return
    if not isinstance(payload, dict):
        raise RuntimeError("support_service_team_members must be a JSON object")
    for raw_team_id, raw_members in payload.items():
        team_id = _uuid(raw_team_id, field="member team")
        if not isinstance(raw_members, list):
            raise RuntimeError("support_service_team_members value must be an array")
        for raw_system_user_id in raw_members:
            person_party_id = _person_party_for_system_user(
                bind,
                _uuid(raw_system_user_id, field="member SystemUser"),
            )
            bind.execute(
                sa.text(
                    "INSERT INTO service_team_members "
                    "(id, team_id, person_id, role, is_active, created_at) "
                    "VALUES (gen_random_uuid(), :team_id, :person_id, "
                    "'member', TRUE, now()) "
                    "ON CONFLICT (team_id, person_id) DO UPDATE SET is_active = TRUE"
                ),
                {"team_id": team_id, "person_id": person_party_id},
            )


def _rewrite_compatibility_person_ids(bind) -> None:
    rows = (
        bind.execute(
            sa.text(
                "SELECT id, team_id, person_id, is_active "
                "FROM service_team_members "
                "ORDER BY team_id, id FOR UPDATE"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        stored = UUID(str(row["person_id"]))
        party_status = bind.execute(
            sa.text(
                "SELECT status FROM parties WHERE id = :id AND party_type = 'person'"
            ),
            {"id": stored},
        ).scalar_one_or_none()
        user_party = bind.execute(
            sa.text("SELECT person_party_id FROM system_users WHERE id = :id"),
            {"id": stored},
        ).scalar_one_or_none()
        if party_status is not None:
            if user_party is not None and UUID(str(user_party)) != stored:
                raise RuntimeError(
                    "Ambiguous service-team identity matches both Party and SystemUser"
                )
            if row["is_active"] and str(party_status) != "active":
                raise RuntimeError(
                    "Active service-team membership references an inactive Person Party"
                )
            if row["is_active"]:
                active_principal = bind.execute(
                    sa.text(
                        "SELECT id FROM system_users "
                        "WHERE person_party_id = :person_party_id "
                        "AND is_active IS TRUE"
                    ),
                    {"person_party_id": stored},
                ).scalar_one_or_none()
                if active_principal is None:
                    raise RuntimeError(
                        "Active service-team membership has no active "
                        "SystemUser principal"
                    )
            continue
        if user_party is None:
            raise RuntimeError(
                "Service-team membership UUID cannot be resolved to a Person Party"
            )
        target = _person_party_for_system_user(
            bind,
            stored,
            require_active=bool(row["is_active"]),
        )
        duplicate = bind.execute(
            sa.text(
                "SELECT id FROM service_team_members "
                "WHERE team_id = :team_id AND person_id = :person_id AND id <> :id"
            ),
            {"team_id": row["team_id"], "person_id": target, "id": row["id"]},
        ).scalar_one_or_none()
        if duplicate is not None:
            raise RuntimeError(
                "Service-team identity rewrite would create duplicate membership"
            )
        bind.execute(
            sa.text(
                "UPDATE service_team_members SET person_id = :target WHERE id = :id"
            ),
            {"target": target, "id": row["id"]},
        )

    managers = (
        bind.execute(
            sa.text(
                "SELECT id, manager_person_id FROM service_teams "
                "WHERE manager_person_id IS NOT NULL FOR UPDATE"
            )
        )
        .mappings()
        .all()
    )
    for row in managers:
        stored = UUID(str(row["manager_person_id"]))
        party_exists = bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM parties "
                "WHERE id = :id AND party_type = 'person' AND status = 'active')"
            ),
            {"id": stored},
        ).scalar_one()
        user_party = bind.execute(
            sa.text("SELECT person_party_id FROM system_users WHERE id = :id"),
            {"id": stored},
        ).scalar_one_or_none()
        if party_exists:
            if user_party is not None and UUID(str(user_party)) != stored:
                raise RuntimeError(
                    "Ambiguous service-team manager matches both Party and SystemUser"
                )
            active_principal = bind.execute(
                sa.text(
                    "SELECT id FROM system_users "
                    "WHERE person_party_id = :person_party_id "
                    "AND is_active IS TRUE"
                ),
                {"person_party_id": stored},
            ).scalar_one_or_none()
            if active_principal is None:
                raise RuntimeError(
                    "Service-team manager has no active SystemUser principal"
                )
            continue
        target = _person_party_for_system_user(bind, stored)
        bind.execute(
            sa.text(
                "UPDATE service_teams SET manager_person_id = :target WHERE id = :id"
            ),
            {"target": target, "id": row["id"]},
        )


def upgrade() -> None:
    bind = op.get_bind()
    duplicate_name = bind.execute(
        sa.text(
            "SELECT lower(name) FROM service_teams GROUP BY lower(name) "
            "HAVING count(*) > 1 LIMIT 1"
        )
    ).scalar_one_or_none()
    if duplicate_name is not None:
        raise RuntimeError(
            "Duplicate case-insensitive service-team names require adjudication"
        )
    _backfill_setting_teams(bind)
    duplicate_name = bind.execute(
        sa.text(
            "SELECT lower(name) FROM service_teams GROUP BY lower(name) "
            "HAVING count(*) > 1 LIMIT 1"
        )
    ).scalar_one_or_none()
    if duplicate_name is not None:
        raise RuntimeError(
            "Workflow backfill creates a duplicate case-insensitive team name"
        )
    _rewrite_compatibility_person_ids(bind)
    _backfill_setting_members(bind)
    op.create_index(
        "ux_service_teams_name_ci",
        "service_teams",
        [sa.text("lower(name)")],
        unique=True,
    )
    op.create_foreign_key(
        "fk_service_teams_manager_person_id_parties",
        "service_teams",
        "parties",
        ["manager_person_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_service_team_members_person_id_parties",
        "service_team_members",
        "parties",
        ["person_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_service_teams_workforce_reference_pair",
        "service_teams",
        "(workforce_system IS NULL) = (workforce_department_reference IS NULL)",
    )
    bind.execute(
        sa.text(
            "DELETE FROM domain_settings "
            "WHERE domain = CAST('workflow' AS settingdomain) "
            "AND key IN (:team_key, :member_key)"
        ),
        {"team_key": _TEAM_KEY, "member_key": _MEMBER_KEY},
    )


def downgrade() -> None:
    raise RuntimeError(
        "Service-team Party identity cutover is irreversible; restore from "
        "reviewed pre-cutover backup instead of reconstructing retired settings."
    )
