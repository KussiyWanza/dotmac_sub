#!/usr/bin/env python3
"""Plan, report and execute approved staff-session Party projections.

Plans contain UUIDs and evidence digests only. Identity is derived exclusively
from the deployed ``SystemUser.person_party_id`` foreign-key binding; names,
email addresses, usernames and token material are neither read nor accepted.
Every mutation delegates to the canonical owner in its own transaction, so an
interrupted execution resumes by exact replay.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import secrets
import stat
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, cast, get_args, get_type_hints
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import SessionLocal, read_only_snapshot_session
from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.party import Party, PartyType
from app.models.system_user import SystemUser
from app.services import staff_session_party_adoption as owner
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.staff_session_party_adoption import (
    ProjectStaffSessionPartyCommand,
    StaffSessionPartyProjectionOutcome,
)

CONTRACT_VERSION = 1
MAX_ITEMS_PER_EXECUTION = 1_000
MAX_APPROVAL_WINDOW = timedelta(hours=24)
MAX_FUTURE_APPROVAL_SKEW = timedelta(minutes=5)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
BoundedCount = Annotated[int, Field(ge=0, le=MAX_ITEMS_PER_EXECUTION)]
NonnegativeCount = Annotated[int, Field(ge=0)]


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StaffSessionProjectionRefusalCode(StrEnum):
    """Stable, PII-free operator refusal vocabulary."""

    invalid_file = "invalid_file"
    invalid_plan = "invalid_plan"
    invalid_approval = "invalid_approval"
    approval_mismatch = "approval_mismatch"
    expired_approval = "expired_approval"
    changed_input = "changed_input"
    owner_refused = "owner_refused"
    database_failure = "database_failure"


class StaffSessionProjectionRefused(Exception):
    """Typed operator refusal that never contains identity display fields."""

    code: StaffSessionProjectionRefusalCode
    message: str
    decision_id: UUID | None
    owner_code: str | None

    def __init__(
        self,
        code: StaffSessionProjectionRefusalCode,
        message: str,
        *,
        decision_id: UUID | None = None,
        owner_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.decision_id = decision_id
        self.owner_code = owner_code


class StaffSessionPartyProjectionItem(_StrictContract):
    """One exact legacy session and its reviewed bound identity/context pair."""

    decision_id: UUID
    session_id: UUID
    system_user_id: UUID
    person_party_id: UUID
    evidence_sha256: Sha256

    @property
    def key(self) -> tuple[str, str]:
        return str(self.session_id), str(self.decision_id)


class StaffSessionPartyProjectionPlan(_StrictContract):
    """Deterministic, bounded UUID-only projection plan."""

    contract_version: Literal[1] = 1
    planned_at: AwareDatetime
    items: tuple[StaffSessionPartyProjectionItem, ...]
    plan_digest: Sha256


class StaffSessionPartyProjectionApproval(_StrictContract):
    """Separate attributable approval for one exact private plan file."""

    contract_version: Literal[1] = 1
    approval_id: UUID
    plan_digest: Sha256
    plan_file_sha256: Sha256
    approved_by_user_id: UUID
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    reason_sha256: Sha256
    maximum_session_projections: BoundedCount


class StaffSessionPartyProjectionExecutionOutcome(_StrictContract):
    """PII-free execution counts for one approved plan."""

    plan_digest: Sha256
    projections_applied: BoundedCount
    projection_replays: BoundedCount


class StaffSessionPartyProjectionReport(_StrictContract):
    """Aggregate readiness evidence with no identity values."""

    staff_sessions: NonnegativeCount
    projected_staff_sessions: NonnegativeCount
    historical_unprojected: NonnegativeCount
    active_unrevoked_staff_sessions: NonnegativeCount
    active_unrevoked_projected: NonnegativeCount
    active_unrevoked_remaining: NonnegativeCount
    active_unrevoked_unbound: NonnegativeCount
    projection_disagreements: NonnegativeCount
    is_ratchet_ready: bool


def _refuse(
    code: StaffSessionProjectionRefusalCode,
    message: str,
    *,
    decision_id: UUID | None = None,
    owner_code: str | None = None,
) -> typing.NoReturn:
    raise StaffSessionProjectionRefused(
        code,
        message,
        decision_id=decision_id,
        owner_code=owner_code,
    )


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _payload_digest(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _item_payload(item: StaffSessionPartyProjectionItem) -> dict[str, str]:
    return {
        "decision_id": str(item.decision_id),
        "session_id": str(item.session_id),
        "system_user_id": str(item.system_user_id),
        "person_party_id": str(item.person_party_id),
        "evidence_sha256": item.evidence_sha256,
    }


def _plan_payload(
    *,
    planned_at: datetime,
    items: tuple[StaffSessionPartyProjectionItem, ...],
) -> dict[str, object]:
    return {
        "contract_version": CONTRACT_VERSION,
        "planned_at": _iso_utc(planned_at),
        "items": [_item_payload(item) for item in items],
    }


def _approval_digest(approval: StaffSessionPartyProjectionApproval) -> str:
    return _payload_digest(cast(dict[str, object], approval.model_dump(mode="json")))


def build_plan(
    *,
    items: Sequence[StaffSessionPartyProjectionItem],
    planned_at: datetime,
) -> StaffSessionPartyProjectionPlan:
    """Normalize and digest one bounded set of exact projection decisions."""

    if planned_at.tzinfo is None:
        _refuse(
            StaffSessionProjectionRefusalCode.invalid_plan,
            "planned_at must be timezone-aware",
        )
    normalized_at = planned_at.astimezone(UTC)
    normalized_items = tuple(sorted(items, key=lambda item: item.key))
    if not normalized_items:
        _refuse(
            StaffSessionProjectionRefusalCode.invalid_plan,
            "The staff session projection plan has no exact decisions",
        )
    if len(normalized_items) > MAX_ITEMS_PER_EXECUTION:
        _refuse(
            StaffSessionProjectionRefusalCode.invalid_plan,
            "The staff session projection plan exceeds the execution limit",
        )
    for field_name, values in (
        ("decision_id", [item.decision_id for item in normalized_items]),
        ("session_id", [item.session_id for item in normalized_items]),
    ):
        if len(values) != len(set(values)):
            _refuse(
                StaffSessionProjectionRefusalCode.invalid_plan,
                f"The staff session projection plan repeats {field_name}",
            )
    payload = _plan_payload(planned_at=normalized_at, items=normalized_items)
    return StaffSessionPartyProjectionPlan(
        planned_at=normalized_at,
        items=normalized_items,
        plan_digest=_payload_digest(payload),
    )


def parse_plan_payload(
    payload: Mapping[str, object],
) -> StaffSessionPartyProjectionPlan:
    """Parse and independently re-digest an untrusted plan payload."""

    if payload.get("contract_version") != CONTRACT_VERSION:
        _refuse(
            StaffSessionProjectionRefusalCode.invalid_plan,
            "plan.contract_version is not supported",
        )
    try:
        parsed = StaffSessionPartyProjectionPlan.model_validate(payload)
    except ValidationError as exc:
        raise StaffSessionProjectionRefused(
            StaffSessionProjectionRefusalCode.invalid_plan,
            "The plan does not match the fully typed UUID-only contract",
        ) from exc
    recomputed = build_plan(items=parsed.items, planned_at=parsed.planned_at)
    if not secrets.compare_digest(recomputed.plan_digest, parsed.plan_digest):
        _refuse(
            StaffSessionProjectionRefusalCode.invalid_plan,
            "plan.plan_digest does not match the exact typed decisions",
        )
    return recomputed


def parse_approval_payload(
    payload: Mapping[str, object],
) -> StaffSessionPartyProjectionApproval:
    """Parse a separate, strict approval envelope."""

    if payload.get("contract_version") != CONTRACT_VERSION:
        _refuse(
            StaffSessionProjectionRefusalCode.invalid_approval,
            "approval.contract_version is not supported",
        )
    try:
        return StaffSessionPartyProjectionApproval.model_validate(payload)
    except ValidationError as exc:
        raise StaffSessionProjectionRefused(
            StaffSessionProjectionRefusalCode.invalid_approval,
            "The approval does not match the fully typed contract",
        ) from exc


def validate_approval(
    *,
    plan: StaffSessionPartyProjectionPlan,
    approval: StaffSessionPartyProjectionApproval,
    plan_file_sha256: str,
    executed_at: datetime,
) -> None:
    """Require exact digest, file, count, attribution and expiry binding."""

    if executed_at.tzinfo is None:
        _refuse(
            StaffSessionProjectionRefusalCode.invalid_approval,
            "executed_at must be timezone-aware",
        )
    now = executed_at.astimezone(UTC)
    approved_at = approval.approved_at.astimezone(UTC)
    expires_at = approval.expires_at.astimezone(UTC)
    if (
        expires_at < approved_at
        or expires_at - approved_at > MAX_APPROVAL_WINDOW
        or approved_at < plan.planned_at
        or approved_at > now + MAX_FUTURE_APPROVAL_SKEW
    ):
        _refuse(
            StaffSessionProjectionRefusalCode.invalid_approval,
            "The approval timing does not satisfy the execution contract",
        )
    if now > expires_at:
        _refuse(
            StaffSessionProjectionRefusalCode.expired_approval,
            "The staff session projection approval has expired",
        )
    normalized_file_digest = plan_file_sha256.strip().lower()
    if (
        len(normalized_file_digest) != 64
        or any(
            character not in "0123456789abcdef" for character in normalized_file_digest
        )
        or normalized_file_digest != plan_file_sha256.strip()
    ):
        _refuse(
            StaffSessionProjectionRefusalCode.approval_mismatch,
            "plan_file_sha256 is not a lowercase SHA-256 digest",
        )
    if not secrets.compare_digest(plan.plan_digest, approval.plan_digest) or not (
        secrets.compare_digest(normalized_file_digest, approval.plan_file_sha256)
    ):
        _refuse(
            StaffSessionProjectionRefusalCode.approval_mismatch,
            "The exact plan is not the approved plan",
        )
    if approval.maximum_session_projections != len(plan.items):
        _refuse(
            StaffSessionProjectionRefusalCode.approval_mismatch,
            "The approved count does not exactly match the plan",
        )


@dataclass(frozen=True, slots=True)
class _SessionProjectionRow:
    party_id: UUID | None
    status: SessionStatus
    revoked_at: datetime | None
    principal_id: UUID | None
    person_party_id: UUID | None
    principal_active: bool | None
    bound_party_id: UUID | None
    party_type: str | None


def _session_rows(db: Session) -> tuple[_SessionProjectionRow, ...]:
    rows = db.execute(
        select(
            AuthSession.party_id,
            AuthSession.status,
            AuthSession.revoked_at,
            SystemUser.id.label("principal_id"),
            SystemUser.person_party_id,
            SystemUser.is_active.label("principal_active"),
            Party.id.label("bound_party_id"),
            Party.party_type,
        )
        .select_from(AuthSession)
        .outerjoin(SystemUser, SystemUser.id == AuthSession.system_user_id)
        .outerjoin(Party, Party.id == SystemUser.person_party_id)
        .where(AuthSession.system_user_id.is_not(None))
    ).all()
    return tuple(
        _SessionProjectionRow(
            party_id=row.party_id,
            status=row.status,
            revoked_at=row.revoked_at,
            principal_id=row.principal_id,
            person_party_id=row.person_party_id,
            principal_active=row.principal_active,
            bound_party_id=row.bound_party_id,
            party_type=row.party_type,
        )
        for row in rows
    )


def build_projection_report(db: Session) -> StaffSessionPartyProjectionReport:
    """Measure cutover readiness without returning any row identity."""

    staff_sessions = 0
    projected_staff_sessions = 0
    historical_unprojected = 0
    active_unrevoked_staff_sessions = 0
    active_unrevoked_projected = 0
    active_unrevoked_remaining = 0
    active_unrevoked_unbound = 0
    projection_disagreements = 0

    for row in _session_rows(db):
        staff_sessions += 1
        projected = row.party_id is not None
        active_unrevoked = row.status is SessionStatus.active and row.revoked_at is None
        exact_person_binding = (
            row.principal_id is not None
            and bool(row.principal_active)
            and row.person_party_id is not None
            and row.bound_party_id == row.person_party_id
            and row.party_type == PartyType.person.value
        )
        if projected:
            projected_staff_sessions += 1
        if projected and row.party_id != row.person_party_id:
            projection_disagreements += 1
        if active_unrevoked:
            active_unrevoked_staff_sessions += 1
            if projected:
                active_unrevoked_projected += 1
            else:
                active_unrevoked_remaining += 1
            if not exact_person_binding:
                active_unrevoked_unbound += 1
        elif not projected:
            historical_unprojected += 1

    ready = (
        active_unrevoked_remaining == 0
        and active_unrevoked_unbound == 0
        and projection_disagreements == 0
    )
    return StaffSessionPartyProjectionReport(
        staff_sessions=staff_sessions,
        projected_staff_sessions=projected_staff_sessions,
        historical_unprojected=historical_unprojected,
        active_unrevoked_staff_sessions=active_unrevoked_staff_sessions,
        active_unrevoked_projected=active_unrevoked_projected,
        active_unrevoked_remaining=active_unrevoked_remaining,
        active_unrevoked_unbound=active_unrevoked_unbound,
        projection_disagreements=projection_disagreements,
        is_ratchet_ready=ready,
    )


def _item_evidence_sha256(
    *,
    session_id: UUID,
    system_user_id: UUID,
    person_party_id: UUID,
) -> str:
    return _payload_digest(
        {
            "session_id": str(session_id),
            "system_user_id": str(system_user_id),
            "person_party_id": str(person_party_id),
            "expected_status": SessionStatus.active.value,
            "expected_revoked_at": None,
            "identity_source": "system_users.person_party_id",
        }
    )


def build_plan_from_database(
    db: Session,
    *,
    planned_at: datetime,
    maximum_items: int = MAX_ITEMS_PER_EXECUTION,
) -> StaffSessionPartyProjectionPlan:
    """Derive one deterministic batch from exact deployed foreign-key evidence."""

    if maximum_items < 1 or maximum_items > MAX_ITEMS_PER_EXECUTION:
        _refuse(
            StaffSessionProjectionRefusalCode.invalid_plan,
            "maximum_items is outside the bounded execution contract",
        )
    report = build_projection_report(db)
    if report.active_unrevoked_unbound or report.projection_disagreements:
        _refuse(
            StaffSessionProjectionRefusalCode.changed_input,
            "The database contains blocking session identity drift",
        )
    rows = db.execute(
        select(
            AuthSession.id,
            AuthSession.system_user_id,
            SystemUser.person_party_id,
        )
        .join(SystemUser, SystemUser.id == AuthSession.system_user_id)
        .join(Party, Party.id == SystemUser.person_party_id)
        .where(
            AuthSession.status == SessionStatus.active,
            AuthSession.revoked_at.is_(None),
            AuthSession.party_id.is_(None),
            SystemUser.is_active.is_(True),
            SystemUser.person_party_id.is_not(None),
            Party.party_type == PartyType.person.value,
        )
        .order_by(AuthSession.id)
        .limit(maximum_items)
    ).all()
    items: list[StaffSessionPartyProjectionItem] = []
    for row in rows:
        if row.system_user_id is None or row.person_party_id is None:
            _refuse(
                StaffSessionProjectionRefusalCode.changed_input,
                "A selected session lost its exact Party binding",
            )
        evidence_sha256 = _item_evidence_sha256(
            session_id=row.id,
            system_user_id=row.system_user_id,
            person_party_id=row.person_party_id,
        )
        decision_id = uuid5(
            NAMESPACE_URL,
            "dotmac-sub:staff-session-party-projection:"
            f"v1:{row.id}:{row.system_user_id}:{row.person_party_id}:{evidence_sha256}",
        )
        items.append(
            StaffSessionPartyProjectionItem(
                decision_id=decision_id,
                session_id=row.id,
                system_user_id=row.system_user_id,
                person_party_id=row.person_party_id,
                evidence_sha256=evidence_sha256,
            )
        )
    return build_plan(items=items, planned_at=planned_at)


def _command_context(
    *,
    plan: StaffSessionPartyProjectionPlan,
    approval: StaffSessionPartyProjectionApproval,
    item: StaffSessionPartyProjectionItem,
) -> CommandContext:
    command_id = uuid5(
        NAMESPACE_URL,
        "dotmac-sub:staff-session-party-projection:"
        f"v1:{plan.plan_digest}:{item.decision_id}",
    )
    correlation_id = uuid5(
        NAMESPACE_URL,
        f"dotmac-sub:staff-session-party-projection:v1:{plan.plan_digest}",
    )
    return CommandContext.system(
        actor=f"user:{approval.approved_by_user_id}",
        scope=owner.COMMAND_SCOPE,
        reason=(
            "approved staff session Party projection; "
            f"approval_id={approval.approval_id};"
            f"approval_sha256={_approval_digest(approval)};"
            f"reason_sha256={approval.reason_sha256}"
        ),
        command_id=command_id,
        correlation_id=correlation_id,
        idempotency_key=(
            f"staff-session-party-projection:{plan.plan_digest}:{item.decision_id}"
        ),
    )


def _execute_approved_plan(
    db: Session,
    *,
    plan: StaffSessionPartyProjectionPlan,
    approval: StaffSessionPartyProjectionApproval,
    plan_file_sha256: str,
    executed_at: datetime | None = None,
) -> StaffSessionPartyProjectionExecutionOutcome:
    fixed_now = executed_at

    def revalidate() -> None:
        validate_approval(
            plan=plan,
            approval=approval,
            plan_file_sha256=plan_file_sha256,
            executed_at=fixed_now or datetime.now(UTC),
        )

    revalidate()
    applied = 0
    replays = 0
    approval_sha256 = _approval_digest(approval)
    for item in plan.items:
        revalidate()
        try:
            outcome = owner.project_staff_session_party(
                db,
                ProjectStaffSessionPartyCommand(
                    context=_command_context(
                        plan=plan,
                        approval=approval,
                        item=item,
                    ),
                    session_id=item.session_id,
                    expected_system_user_id=item.system_user_id,
                    person_party_id=item.person_party_id,
                    decision_id=item.decision_id,
                    plan_digest=plan.plan_digest,
                    evidence_sha256=item.evidence_sha256,
                    approval_id=approval.approval_id,
                    approval_sha256=approval_sha256,
                ),
            )
        except DomainError as exc:
            _refuse(
                StaffSessionProjectionRefusalCode.owner_refused,
                "The canonical owner refused an approved session projection",
                decision_id=item.decision_id,
                owner_code=exc.code,
            )
        replays += int(outcome.replayed)
        applied += int(not outcome.replayed)
    return StaffSessionPartyProjectionExecutionOutcome(
        plan_digest=plan.plan_digest,
        projections_applied=applied,
        projection_replays=replays,
    )


def execute_approved_plan(
    db: Session,
    *,
    plan: StaffSessionPartyProjectionPlan,
    approval: StaffSessionPartyProjectionApproval,
    plan_file_sha256: str,
    executed_at: datetime | None = None,
) -> StaffSessionPartyProjectionExecutionOutcome:
    """Return one typed outcome or one PII-free typed refusal."""

    try:
        return _execute_approved_plan(
            db,
            plan=plan,
            approval=approval,
            plan_file_sha256=plan_file_sha256,
            executed_at=executed_at,
        )
    except SQLAlchemyError as exc:
        raise StaffSessionProjectionRefused(
            StaffSessionProjectionRefusalCode.database_failure,
            "A database operation failed; no identity values were emitted",
        ) from exc


_PUBLIC_CONTRACT_TYPES: tuple[type[object], ...] = (
    StaffSessionPartyProjectionItem,
    StaffSessionPartyProjectionPlan,
    StaffSessionPartyProjectionApproval,
    StaffSessionPartyProjectionExecutionOutcome,
    StaffSessionPartyProjectionReport,
    StaffSessionProjectionRefused,
    ProjectStaffSessionPartyCommand,
    StaffSessionPartyProjectionOutcome,
)
_PUBLIC_CONTRACT_FUNCTIONS = (
    build_plan,
    parse_plan_payload,
    parse_approval_payload,
    validate_approval,
    build_projection_report,
    build_plan_from_database,
    execute_approved_plan,
)


def _contains_any(annotation: object) -> bool:
    return annotation is typing.Any or any(
        _contains_any(argument) for argument in get_args(annotation)
    )


def public_contract_type_errors() -> tuple[str, ...]:
    """Return public projection boundaries containing an untyped annotation."""

    errors: list[str] = []
    for contract_type in _PUBLIC_CONTRACT_TYPES:
        hints = get_type_hints(contract_type)
        if is_dataclass(contract_type):
            names = {field.name for field in fields(contract_type)}
            errors.extend(
                f"{contract_type.__name__}.{name}: missing"
                for name in sorted(names - hints.keys())
            )
        elif issubclass(contract_type, BaseModel):
            model_type = cast(type[BaseModel], contract_type)
            errors.extend(
                f"{contract_type.__name__}.{name}: missing"
                for name in sorted(model_type.model_fields.keys() - hints.keys())
            )
        errors.extend(
            f"{contract_type.__name__}.{name}: Any"
            for name, annotation in hints.items()
            if _contains_any(annotation)
        )
    for function in _PUBLIC_CONTRACT_FUNCTIONS:
        hints = get_type_hints(function)
        parameters = set(inspect.signature(function).parameters)
        errors.extend(
            f"{function.__name__}.{name}: missing"
            for name in sorted(parameters - hints.keys())
        )
        if "return" not in hints:
            errors.append(f"{function.__name__}.return: missing")
        errors.extend(
            f"{function.__name__}.{name}: Any"
            for name, annotation in hints.items()
            if _contains_any(annotation)
        )
    return tuple(sorted(errors))


def _assert_private_file(path: Path, field: str) -> None:
    if path.is_symlink() or not path.is_file():
        _refuse(
            StaffSessionProjectionRefusalCode.invalid_file,
            f"{field} must be a regular, non-symlink file",
        )
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        _refuse(
            StaffSessionProjectionRefusalCode.invalid_file,
            f"{field} must have mode 0o600",
        )


def _file_sha256(path: Path) -> str:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise StaffSessionProjectionRefused(
            StaffSessionProjectionRefusalCode.invalid_file,
            "An execution input could not be read",
        ) from exc
    return hashlib.sha256(content).hexdigest()


def _load_json_object(path: Path, field: str) -> Mapping[str, object]:
    _assert_private_file(path, field)
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StaffSessionProjectionRefused(
            StaffSessionProjectionRefusalCode.invalid_file,
            f"{field} is not valid JSON",
        ) from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        _refuse(
            StaffSessionProjectionRefusalCode.invalid_file,
            f"{field} must be a JSON object with string keys",
        )
    return cast(dict[str, object], raw)


def load_plan(path: Path) -> StaffSessionPartyProjectionPlan:
    return parse_plan_payload(_load_json_object(path, "plan file"))


def load_approval(path: Path) -> StaffSessionPartyProjectionApproval:
    return parse_approval_payload(_load_json_object(path, "approval file"))


def _write_private_json(path: Path, contract: BaseModel) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise StaffSessionProjectionRefused(
            StaffSessionProjectionRefusalCode.invalid_file,
            "The private output file could not be created exclusively",
        ) from exc
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(contract.model_dump_json(indent=2))
            handle.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _parse_aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("must include a timezone")
    return parsed.astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--report", action="store_true")
    mode.add_argument("--build-plan", type=Path, metavar="OUTPUT")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--planned-at", type=_parse_aware_datetime)
    parser.add_argument("--maximum-items", type=int, default=MAX_ITEMS_PER_EXECUTION)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--confirm-plan-digest")
    return parser


def _build_plan_mode(output: Path, planned_at: datetime | None, maximum: int) -> int:
    if planned_at is None:
        print("REFUSED: --planned-at is required with --build-plan")
        return 2
    try:
        with read_only_snapshot_session() as db:
            plan = build_plan_from_database(
                db,
                planned_at=planned_at,
                maximum_items=maximum,
            )
        _write_private_json(output, plan)
    except StaffSessionProjectionRefused as exc:
        print(f"REFUSED: {exc.code.value}: {exc.message}")
        return 2
    print(
        json.dumps(
            {
                "plan_digest": plan.plan_digest,
                "plan_items": len(plan.items),
                "plan_file": str(output),
            },
            sort_keys=True,
        )
    )
    return 0


def _execute_mode(
    plan_path: Path | None,
    approval_path: Path | None,
    confirmed_digest: str | None,
) -> int:
    if plan_path is None or approval_path is None or confirmed_digest is None:
        print("REFUSED: --plan, --approval and --confirm-plan-digest are required")
        return 2
    try:
        _assert_private_file(plan_path, "plan file")
        plan_file_sha256 = _file_sha256(plan_path)
        plan = load_plan(plan_path)
        _assert_private_file(approval_path, "approval file")
        approval_file_sha256 = _file_sha256(approval_path)
        approval = load_approval(approval_path)
        if not secrets.compare_digest(confirmed_digest, plan.plan_digest):
            _refuse(
                StaffSessionProjectionRefusalCode.approval_mismatch,
                "Typed --confirm-plan-digest does not match the plan",
            )
        validate_approval(
            plan=plan,
            approval=approval,
            plan_file_sha256=plan_file_sha256,
            executed_at=datetime.now(UTC),
        )
        if not secrets.compare_digest(
            _file_sha256(plan_path), plan_file_sha256
        ) or not secrets.compare_digest(
            _file_sha256(approval_path), approval_file_sha256
        ):
            _refuse(
                StaffSessionProjectionRefusalCode.changed_input,
                "An execution input changed while it was being validated",
            )
        with SessionLocal() as db:
            outcome = execute_approved_plan(
                db,
                plan=plan,
                approval=approval,
                plan_file_sha256=plan_file_sha256,
            )
    except StaffSessionProjectionRefused as exc:
        suffix = f" owner_code={exc.owner_code}" if exc.owner_code else ""
        print(f"REFUSED: {exc.code.value}: {exc.message}{suffix}")
        return (
            1 if exc.code is StaffSessionProjectionRefusalCode.database_failure else 2
        )
    print(outcome.model_dump_json())
    return 0


def main() -> int:
    args = _parser().parse_args()
    if args.report:
        try:
            with read_only_snapshot_session() as db:
                report = build_projection_report(db)
        except SQLAlchemyError:
            print("REFUSED: database_failure: report query failed")
            return 1
        print(report.model_dump_json())
        return 0
    if args.build_plan is not None:
        return _build_plan_mode(args.build_plan, args.planned_at, args.maximum_items)
    return _execute_mode(args.plan, args.approval, args.confirm_plan_digest)


if __name__ == "__main__":
    raise SystemExit(main())
