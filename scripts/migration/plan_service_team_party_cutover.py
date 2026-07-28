#!/usr/bin/env python3
"""Build a private, non-executable service-team Party cutover plan.

The command reads CRM and Sub in read-only transactions, verifies that the CRM
team rows already copied into Sub still agree, and combines the exact CRM
membership snapshot with an explicit reviewed decision CSV.  It never writes a
database.  The output contains internal identity data, is created mode 0600,
and still requires a separate expiring approval before execution.

Environment:
  CRM_DATABASE_URL  source dotmac_crm PostgreSQL URL
  SUB_DATABASE_URL  target dotmac_sub PostgreSQL URL

Decision CSV columns:
  crm_person_id,decision,system_user_id,decision_id,reason

``decision`` is ``bind`` for an explicit SystemUser binding or
``identity_only`` for an inactive historical member with no Sub principal.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from app.models.service_team import ServiceTeamMemberRole
from app.services.service_team_party_cutover import (
    IdentityDecisionKind,
    PlannedServiceTeamMembership,
    PlannedStaffIdentity,
    ServiceTeamPartyCutoverError,
    ServiceTeamPartyCutoverPlan,
)

_DECISION_COLUMNS = {
    "crm_person_id",
    "decision",
    "system_user_id",
    "decision_id",
    "reason",
}


@dataclass(frozen=True)
class ReviewedDecision:
    crm_person_id: UUID
    decision: IdentityDecisionKind
    system_user_id: UUID | None
    decision_id: UUID
    reason_sha256: str


class PlanBuildError(ValueError):
    """Raised when the source snapshot or reviewed decisions are incomplete."""


def _engine(name: str) -> Engine:
    url = os.environ.get(name)
    if not url:
        raise PlanBuildError(f"{name} is required")
    return create_engine(url, pool_pre_ping=True)


def _rows(
    connection: Connection,
    statement: str,
    parameters: dict[str, object] | None = None,
) -> list[dict[str, Any]]:
    return [
        dict(row._mapping)
        for row in connection.execute(text(statement), parameters or {})
    ]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _private_file_bytes(path: Path, *, label: str) -> bytes:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise PlanBuildError(f"{label} cannot be read") from exc
    if mode & 0o077:
        raise PlanBuildError(f"{label} must not be accessible by group or others")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PlanBuildError(f"{label} cannot be read") from exc


def _uuid(value: object, *, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise PlanBuildError(f"{field} is not a UUID") from exc


def _required(value: object, *, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise PlanBuildError(f"{field} is required")
    return cleaned


def _load_decisions(path: Path) -> tuple[dict[UUID, ReviewedDecision], str]:
    raw = _private_file_bytes(path, label="decision file")
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PlanBuildError("decision file must be UTF-8") from exc
    reader = csv.DictReader(decoded.splitlines())
    if reader.fieldnames is None or set(reader.fieldnames) != _DECISION_COLUMNS:
        raise PlanBuildError(
            "decision file columns must be exactly: "
            + ",".join(sorted(_DECISION_COLUMNS))
        )
    decisions: dict[UUID, ReviewedDecision] = {}
    system_user_ids: set[UUID] = set()
    for row_number, row in enumerate(reader, start=2):
        person_id = _uuid(row.get("crm_person_id"), field=f"row {row_number} person")
        if person_id in decisions:
            raise PlanBuildError("decision file repeats a CRM Person")
        try:
            decision = IdentityDecisionKind(
                _required(row.get("decision"), field=f"row {row_number} decision")
            )
        except ValueError as exc:
            raise PlanBuildError(
                f"row {row_number} decision must be bind or identity_only"
            ) from exc
        raw_system_user = str(row.get("system_user_id") or "").strip()
        system_user_id = (
            _uuid(raw_system_user, field=f"row {row_number} SystemUser")
            if raw_system_user
            else None
        )
        if decision is IdentityDecisionKind.bind and system_user_id is None:
            raise PlanBuildError(f"row {row_number} bind decision needs a SystemUser")
        if (
            decision is IdentityDecisionKind.identity_only
            and system_user_id is not None
        ):
            raise PlanBuildError(
                f"row {row_number} identity_only decision cannot name a SystemUser"
            )
        if system_user_id is not None:
            if system_user_id in system_user_ids:
                raise PlanBuildError(
                    "decision file maps one SystemUser to multiple CRM People"
                )
            system_user_ids.add(system_user_id)
        reason = _required(row.get("reason"), field=f"row {row_number} reason")
        decisions[person_id] = ReviewedDecision(
            crm_person_id=person_id,
            decision=decision,
            system_user_id=system_user_id,
            decision_id=_uuid(
                row.get("decision_id"),
                field=f"row {row_number} decision_id",
            ),
            reason_sha256=_sha256_bytes(reason.encode()),
        )
    if not decisions:
        raise PlanBuildError("decision file is empty")
    return decisions, _sha256_bytes(raw)


def _crm_snapshot(
    connection: Connection,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[UUID, dict[str, Any]],
]:
    teams = _rows(
        connection,
        """
        SELECT id, name, team_type::text AS team_type, region,
               manager_person_id, is_active, created_at, updated_at
        FROM service_teams
        ORDER BY id
        """,
    )
    memberships = _rows(
        connection,
        """
        SELECT id, team_id, person_id, role::text AS role, is_active, created_at
        FROM service_team_members
        ORDER BY id
        """,
    )
    referenced = {
        _uuid(row["manager_person_id"], field="CRM manager")
        for row in teams
        if row.get("manager_person_id") is not None
    } | {_uuid(row["person_id"], field="CRM membership person") for row in memberships}
    if not referenced:
        return teams, memberships, {}
    people = _rows(
        connection,
        """
        SELECT id, first_name, last_name, display_name, email, is_active
        FROM people
        WHERE id = ANY(:person_ids)
        ORDER BY id
        """,
        {"person_ids": list(referenced)},
    )
    by_id = {_uuid(row["id"], field="CRM Person"): row for row in people}
    if referenced - set(by_id):
        raise PlanBuildError("CRM service-team state references missing People")
    return teams, memberships, by_id


def _sub_snapshot(
    connection: Connection,
) -> tuple[list[dict[str, Any]], dict[UUID, dict[str, Any]]]:
    teams = _rows(
        connection,
        """
        SELECT id, name, team_type::text AS team_type, region,
               manager_person_id, is_active, created_at, updated_at
        FROM service_teams
        ORDER BY id
        """,
    )
    users = _rows(
        connection,
        """
        SELECT id, is_active, person_party_id
        FROM system_users
        ORDER BY id
        """,
    )
    return teams, {_uuid(row["id"], field="Sub SystemUser"): row for row in users}


def _display_name(person: dict[str, Any]) -> str:
    display = " ".join(str(person.get("display_name") or "").split())
    if display:
        return display
    names = " ".join(
        value
        for value in (
            str(person.get("first_name") or "").strip(),
            str(person.get("last_name") or "").strip(),
        )
        if value
    )
    if names:
        return names
    email = str(person.get("email") or "").strip()
    if email:
        return email
    raise PlanBuildError("a referenced CRM Person has no usable display name")


def _instant(value: object) -> str:
    if not isinstance(value, datetime):
        raise PlanBuildError("source timestamp is missing")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _member_role(value: object) -> ServiceTeamMemberRole:
    try:
        return ServiceTeamMemberRole(str(value))
    except ValueError as exc:
        raise PlanBuildError("CRM membership has an unsupported role") from exc


def _team_contract(row: dict[str, Any]) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "team_type": str(row["team_type"]),
        "region": row.get("region"),
        "manager_person_id": (
            str(row["manager_person_id"])
            if row.get("manager_person_id") is not None
            else None
        ),
        "is_active": bool(row["is_active"]),
    }


def _verify_team_copy(
    crm_teams: list[dict[str, Any]],
    sub_teams: list[dict[str, Any]],
) -> None:
    sub_by_id = {_uuid(row["id"], field="Sub team"): row for row in sub_teams}
    missing = 0
    mismatched = 0
    duplicate_names = 0
    names: set[str] = set()
    for crm in crm_teams:
        team_id = _uuid(crm["id"], field="CRM team")
        target = sub_by_id.get(team_id)
        if target is None:
            missing += 1
            continue
        if _team_contract(crm) != _team_contract(target):
            mismatched += 1
        name = str(target["name"]).casefold()
        if name in names:
            duplicate_names += 1
        names.add(name)
    if missing or mismatched or duplicate_names:
        raise PlanBuildError(
            "CRM-to-Sub service-team copy is not cutover-ready "
            f"(missing={missing}, mismatched={mismatched}, "
            f"duplicate_names={duplicate_names})"
        )


def _source_snapshot_digest(
    *,
    teams: list[dict[str, Any]],
    memberships: list[dict[str, Any]],
    people: dict[UUID, dict[str, Any]],
) -> str:
    payload = {
        "source_system": "dotmac_crm",
        "teams": [
            {
                **_team_contract(row),
                "created_at": _instant(row["created_at"]),
                "updated_at": _instant(row["updated_at"]),
            }
            for row in teams
        ],
        "memberships": [
            {
                "id": str(row["id"]),
                "team_id": str(row["team_id"]),
                "person_id": str(row["person_id"]),
                "role": str(row["role"]),
                "is_active": bool(row["is_active"]),
                "created_at": _instant(row["created_at"]),
            }
            for row in memberships
        ],
        "people": [
            {
                "id": str(person_id),
                "display_name": _display_name(row),
                "is_active": bool(row["is_active"]),
            }
            for person_id, row in sorted(people.items(), key=lambda item: str(item[0]))
        ],
    }
    return _sha256_bytes(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    )


def build_plan(
    *,
    crm_teams: list[dict[str, Any]],
    crm_memberships: list[dict[str, Any]],
    crm_people: dict[UUID, dict[str, Any]],
    sub_teams: list[dict[str, Any]],
    sub_users: dict[UUID, dict[str, Any]],
    decisions: dict[UUID, ReviewedDecision],
    decision_file_sha256: str,
    planned_at: datetime,
) -> ServiceTeamPartyCutoverPlan:
    """Build a validated plan from already-read source rows."""

    _verify_team_copy(crm_teams, sub_teams)
    manager_ids = {
        _uuid(row["manager_person_id"], field="CRM manager")
        for row in crm_teams
        if row.get("manager_person_id") is not None
    }
    active_member_ids = {
        _uuid(row["person_id"], field="CRM member")
        for row in crm_memberships
        if bool(row["is_active"])
    }
    referenced_ids = set(crm_people)
    if set(decisions) != referenced_ids:
        raise PlanBuildError(
            "decision file must contain exactly every referenced CRM Person "
            f"(missing={len(referenced_ids - set(decisions))}, "
            f"extra={len(set(decisions) - referenced_ids)})"
        )
    require_active_user = manager_ids | active_member_ids
    identities: list[PlannedStaffIdentity] = []
    for person_id in sorted(referenced_ids, key=str):
        decision = decisions[person_id]
        if person_id in require_active_user and decision.system_user_id is None:
            raise PlanBuildError(
                "active members and all managers require a bind decision"
            )
        if decision.system_user_id is not None:
            user = sub_users.get(decision.system_user_id)
            if user is None:
                raise PlanBuildError("a reviewed SystemUser is absent from Sub")
            existing_party = user.get("person_party_id")
            if (
                existing_party is not None
                and _uuid(existing_party, field="existing Person Party") != person_id
            ):
                raise PlanBuildError(
                    "a reviewed SystemUser is already bound to another Party"
                )
            if person_id in require_active_user and not bool(user["is_active"]):
                raise PlanBuildError(
                    "active members and all managers require active SystemUsers"
                )
        identities.append(
            PlannedStaffIdentity(
                legacy_person_id=person_id,
                display_name=_display_name(crm_people[person_id]),
                decision=decision.decision,
                decision_id=decision.decision_id,
                reason_sha256=decision.reason_sha256,
                system_user_id=decision.system_user_id,
            )
        )
    memberships = tuple(
        PlannedServiceTeamMembership(
            membership_id=_uuid(row["id"], field="CRM membership"),
            team_id=_uuid(row["team_id"], field="CRM membership team"),
            legacy_person_id=_uuid(
                row["person_id"],
                field="CRM membership person",
            ),
            role=_member_role(row["role"]),
            is_active=bool(row["is_active"]),
            created_at=datetime.fromisoformat(_instant(row["created_at"])),
        )
        for row in crm_memberships
    )
    plan = ServiceTeamPartyCutoverPlan(
        source_snapshot_sha256=_source_snapshot_digest(
            teams=crm_teams,
            memberships=crm_memberships,
            people=crm_people,
        ),
        decision_file_sha256=decision_file_sha256,
        planned_at=planned_at,
        identities=tuple(identities),
        memberships=memberships,
    )
    return ServiceTeamPartyCutoverPlan.from_payload(plan.file_payload())


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise PlanBuildError("refusing to overwrite an existing plan file") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        decisions, decision_digest = _load_decisions(args.decisions)
        crm_engine = _engine("CRM_DATABASE_URL")
        sub_engine = _engine("SUB_DATABASE_URL")
        with crm_engine.connect() as crm, sub_engine.connect() as sub:
            crm.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            crm.execute(text("SET TRANSACTION READ ONLY"))
            crm_teams, crm_memberships, crm_people = _crm_snapshot(crm)
            crm.rollback()
            sub.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            sub.execute(text("SET TRANSACTION READ ONLY"))
            sub_teams, sub_users = _sub_snapshot(sub)
            sub.rollback()
        plan = build_plan(
            crm_teams=crm_teams,
            crm_memberships=crm_memberships,
            crm_people=crm_people,
            sub_teams=sub_teams,
            sub_users=sub_users,
            decisions=decisions,
            decision_file_sha256=decision_digest,
            planned_at=datetime.now(UTC),
        )
        _write_private_json(args.out, plan.file_payload())
    except (OSError, PlanBuildError, ServiceTeamPartyCutoverError) as exc:
        print(f"REFUSED: {exc}")
        return 2
    except SQLAlchemyError:
        print("FAILED: database snapshot could not be read")
        return 1
    print(
        json.dumps(
            {
                "status": "planned",
                "plan_digest": plan.plan_digest,
                "identity_count": len(plan.identities),
                "membership_count": len(plan.memberships),
                "source_snapshot_sha256": plan.source_snapshot_sha256,
                "output": str(args.out),
                "database_writes": 0,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
