#!/usr/bin/env python3
"""Execute one exact, approved staff Party/principal adoption plan.

The private plan contains UUIDs and evidence digests only. It never infers a
mapping from a name, email address, username, or other mutable identity field.
Each phase delegates to its registered typed owner and commits independently,
so an interrupted run is resumed by exact idempotent replay rather than a
script-owned transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import secrets
import stat
import typing
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services import credential_party_binding, staff_party_adoption
from app.services.credential_party_binding import (
    CredentialPartyBinding,
    CredentialPartyBindingOutcome,
    CredentialPrincipalKind,
)
from app.services.domain_errors import DomainError
from app.services.operator_tenant import operator_tenant_id
from app.services.owner_commands import CommandContext
from app.services.staff_party_adoption import (
    BindExistingStaffPartyCommand,
    ExistingStaffPartyBindingOutcome,
)

CONTRACT_VERSION = 1
MAX_ITEMS_PER_EXECUTION = 1000
MAX_APPROVAL_WINDOW = timedelta(hours=24)
MAX_FUTURE_APPROVAL_SKEW = timedelta(minutes=5)
CREDENTIAL_PROJECTION_SCOPE = "party:credential_authentication_projection"
STAFF_BINDING_SOURCE_PREFIX = "spa:v1:"

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
BoundedCount = Annotated[int, Field(ge=0, le=MAX_ITEMS_PER_EXECUTION)]


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StaffPartyAdoptionAction(StrEnum):
    """Closed plan action; identity selection itself is never inferred."""

    bind_principal_and_project = "bind_principal_and_project"
    project_only = "project_only"


class StaffPartyAdoptionPhase(StrEnum):
    principal_binding = "principal_binding"
    credential_projection = "credential_projection"


class StaffPartyAdoptionRefusalCode(StrEnum):
    invalid_file = "invalid_file"
    invalid_plan = "invalid_plan"
    invalid_approval = "invalid_approval"
    approval_mismatch = "approval_mismatch"
    expired_approval = "expired_approval"
    changed_input = "changed_input"
    owner_refused = "owner_refused"
    database_failure = "database_failure"


class StaffPartyAdoptionRefused(Exception):
    """PII-free, typed operator-adapter refusal."""

    code: StaffPartyAdoptionRefusalCode
    message: str
    decision_id: UUID | None
    phase: StaffPartyAdoptionPhase | None
    owner_code: str | None

    def __init__(
        self,
        code: StaffPartyAdoptionRefusalCode,
        message: str,
        *,
        decision_id: UUID | None = None,
        phase: StaffPartyAdoptionPhase | None = None,
        owner_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.decision_id = decision_id
        self.phase = phase
        self.owner_code = owner_code


class _StaffPartyAdoptionItemBase(_StrictContract):
    decision_id: UUID
    system_user_id: UUID
    person_party_id: UUID
    credential_id: UUID
    authentication_binding_id: UUID
    evidence_sha256: Sha256

    @property
    def key(self) -> tuple[str, str, str]:
        return (
            str(self.person_party_id),
            str(self.credential_id),
            str(self.decision_id),
        )


class StaffPrincipalAndCredentialAdoptionItem(_StaffPartyAdoptionItemBase):
    """Bind one exact SystemUser, then project its exact credential."""

    action: Literal[StaffPartyAdoptionAction.bind_principal_and_project] = (
        StaffPartyAdoptionAction.bind_principal_and_project
    )


class StaffCredentialProjectionItem(_StaffPartyAdoptionItemBase):
    """Project a credential whose SystemUser already carries the reviewed Party."""

    action: Literal[StaffPartyAdoptionAction.project_only] = (
        StaffPartyAdoptionAction.project_only
    )


StaffPartyAdoptionItem = Annotated[
    StaffPrincipalAndCredentialAdoptionItem | StaffCredentialProjectionItem,
    Field(discriminator="action"),
]


class StaffPartyAdoptionPlan(_StrictContract):
    contract_version: Literal[1] = 1
    planned_at: AwareDatetime
    items: tuple[StaffPartyAdoptionItem, ...]
    plan_digest: Sha256

    @property
    def principal_binding_count(self) -> int:
        return sum(
            item.action is StaffPartyAdoptionAction.bind_principal_and_project
            for item in self.items
        )

    @property
    def credential_projection_count(self) -> int:
        return len(self.items)


class StaffPartyAdoptionApproval(_StrictContract):
    contract_version: Literal[1] = 1
    approval_id: UUID
    plan_digest: Sha256
    plan_file_sha256: Sha256
    approved_by_user_id: UUID
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    reason_sha256: Sha256
    maximum_principal_bindings: BoundedCount
    maximum_credential_projections: BoundedCount


class StaffPartyAdoptionOutcome(_StrictContract):
    plan_digest: Sha256
    principal_bindings_applied: BoundedCount
    principal_binding_replays: BoundedCount
    credential_projections_applied: BoundedCount
    credential_projection_replays: BoundedCount


def _refuse(
    code: StaffPartyAdoptionRefusalCode,
    message: str,
    *,
    decision_id: UUID | None = None,
    phase: StaffPartyAdoptionPhase | None = None,
    owner_code: str | None = None,
) -> typing.NoReturn:
    raise StaffPartyAdoptionRefused(
        code,
        message,
        decision_id=decision_id,
        phase=phase,
        owner_code=owner_code,
    )


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _item_payload(item: StaffPartyAdoptionItem) -> dict[str, str]:
    payload = {
        "decision_id": str(item.decision_id),
        "action": item.action.value,
        "system_user_id": str(item.system_user_id),
        "person_party_id": str(item.person_party_id),
        "credential_id": str(item.credential_id),
        "authentication_binding_id": str(item.authentication_binding_id),
        "evidence_sha256": item.evidence_sha256,
    }
    return payload


def _digest_payload(
    *,
    planned_at: datetime,
    items: tuple[StaffPartyAdoptionItem, ...],
) -> dict[str, object]:
    return {
        "contract_version": CONTRACT_VERSION,
        "planned_at": _iso_utc(planned_at),
        "items": [_item_payload(item) for item in items],
    }


def _payload_digest(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _approval_digest(approval: StaffPartyAdoptionApproval) -> str:
    payload = cast(dict[str, object], approval.model_dump(mode="json"))
    return _payload_digest(payload)


def build_plan(
    *,
    items: Sequence[StaffPartyAdoptionItem],
    planned_at: datetime,
) -> StaffPartyAdoptionPlan:
    if planned_at.tzinfo is None:
        _refuse(
            StaffPartyAdoptionRefusalCode.invalid_plan,
            "planned_at must be timezone-aware",
        )
    normalized_at = planned_at.astimezone(UTC)
    normalized_items = tuple(sorted(items, key=lambda item: item.key))
    if not normalized_items:
        _refuse(
            StaffPartyAdoptionRefusalCode.invalid_plan,
            "The staff adoption plan has no exact decisions",
        )
    if len(normalized_items) > MAX_ITEMS_PER_EXECUTION:
        _refuse(
            StaffPartyAdoptionRefusalCode.invalid_plan,
            "The staff adoption plan exceeds the per-execution item limit",
        )
    for field_name, values in (
        ("decision_id", [item.decision_id for item in normalized_items]),
        ("credential_id", [item.credential_id for item in normalized_items]),
        (
            "tenant-Party-binding tuple",
            [
                (item.person_party_id, item.authentication_binding_id)
                for item in normalized_items
            ],
        ),
        (
            "binding system_user_id",
            [
                item.system_user_id
                for item in normalized_items
                if isinstance(item, StaffPrincipalAndCredentialAdoptionItem)
            ],
        ),
    ):
        if len(values) != len(set(values)):
            _refuse(
                StaffPartyAdoptionRefusalCode.invalid_plan,
                f"The staff adoption plan repeats {field_name}",
            )
    payload = _digest_payload(planned_at=normalized_at, items=normalized_items)
    return StaffPartyAdoptionPlan(
        planned_at=normalized_at,
        items=normalized_items,
        plan_digest=_payload_digest(payload),
    )


def parse_plan_payload(payload: Mapping[str, object]) -> StaffPartyAdoptionPlan:
    if payload.get("contract_version") != CONTRACT_VERSION:
        _refuse(
            StaffPartyAdoptionRefusalCode.invalid_plan,
            "plan.contract_version is not supported",
        )
    try:
        parsed = StaffPartyAdoptionPlan.model_validate(payload)
    except ValidationError as exc:
        raise StaffPartyAdoptionRefused(
            StaffPartyAdoptionRefusalCode.invalid_plan,
            "The plan does not match the fully typed UUID-only contract",
        ) from exc
    recomputed = build_plan(items=parsed.items, planned_at=parsed.planned_at)
    if not secrets.compare_digest(recomputed.plan_digest, parsed.plan_digest):
        _refuse(
            StaffPartyAdoptionRefusalCode.invalid_plan,
            "plan.plan_digest does not match the exact typed decisions",
        )
    return recomputed


def parse_approval_payload(
    payload: Mapping[str, object],
) -> StaffPartyAdoptionApproval:
    if payload.get("contract_version") != CONTRACT_VERSION:
        _refuse(
            StaffPartyAdoptionRefusalCode.invalid_approval,
            "approval.contract_version is not supported",
        )
    try:
        return StaffPartyAdoptionApproval.model_validate(payload)
    except ValidationError as exc:
        raise StaffPartyAdoptionRefused(
            StaffPartyAdoptionRefusalCode.invalid_approval,
            "The approval does not match the fully typed contract",
        ) from exc


def validate_approval(
    *,
    plan: StaffPartyAdoptionPlan,
    approval: StaffPartyAdoptionApproval,
    plan_file_sha256: str,
    executed_at: datetime,
) -> None:
    if executed_at.tzinfo is None:
        _refuse(
            StaffPartyAdoptionRefusalCode.invalid_approval,
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
            StaffPartyAdoptionRefusalCode.invalid_approval,
            "The approval timing does not satisfy the execution contract",
        )
    if now > expires_at:
        _refuse(
            StaffPartyAdoptionRefusalCode.expired_approval,
            "The staff adoption approval has expired",
        )
    normalized_file_digest = plan_file_sha256.strip().lower()
    valid_file_digest = len(normalized_file_digest) == 64 and all(
        character in "0123456789abcdef" for character in normalized_file_digest
    )
    if not valid_file_digest or plan_file_sha256.strip() != normalized_file_digest:
        _refuse(
            StaffPartyAdoptionRefusalCode.approval_mismatch,
            "plan_file_sha256 is not a lowercase SHA-256 digest",
        )
    if not secrets.compare_digest(plan.plan_digest, approval.plan_digest) or not (
        secrets.compare_digest(normalized_file_digest, approval.plan_file_sha256)
    ):
        _refuse(
            StaffPartyAdoptionRefusalCode.approval_mismatch,
            "The exact plan is not the approved plan",
        )
    if (
        approval.maximum_principal_bindings != plan.principal_binding_count
        or approval.maximum_credential_projections != plan.credential_projection_count
    ):
        _refuse(
            StaffPartyAdoptionRefusalCode.approval_mismatch,
            "The approved counts do not exactly match the plan",
        )


def _command_context(
    *,
    plan: StaffPartyAdoptionPlan,
    approval: StaffPartyAdoptionApproval,
    item: StaffPartyAdoptionItem,
    phase: StaffPartyAdoptionPhase,
    scope: str,
) -> CommandContext:
    command_id = uuid5(
        NAMESPACE_URL,
        "dotmac-sub:staff-party-adoption:"
        f"v1:{plan.plan_digest}:{item.decision_id}:{phase.value}",
    )
    correlation_id = uuid5(
        NAMESPACE_URL,
        f"dotmac-sub:staff-party-adoption:v1:{plan.plan_digest}",
    )
    return CommandContext.system(
        actor=f"user:{approval.approved_by_user_id}",
        scope=scope,
        reason=(
            "approved staff Party adoption; "
            f"approval_id={approval.approval_id};"
            f"approval_sha256={_approval_digest(approval)};"
            f"reason_sha256={approval.reason_sha256}"
        ),
        command_id=command_id,
        correlation_id=correlation_id,
        idempotency_key=(
            f"staff-party-adoption:{plan.plan_digest}:{item.decision_id}:{phase.value}"
        ),
    )


def _binding_reason(
    item: StaffPartyAdoptionItem,
    approval: StaffPartyAdoptionApproval,
) -> str:
    return (
        f"decision={item.decision_id};"
        f"evidence_sha256={item.evidence_sha256};"
        f"approval={approval.approval_id};"
        f"approval_sha256={_approval_digest(approval)}"
    )


def _owner_refusal(
    error: DomainError,
    *,
    item: StaffPartyAdoptionItem,
    phase: StaffPartyAdoptionPhase,
) -> typing.NoReturn:
    _refuse(
        StaffPartyAdoptionRefusalCode.owner_refused,
        "A canonical owner refused the approved staff adoption item",
        decision_id=item.decision_id,
        phase=phase,
        owner_code=error.code,
    )


def _execute_approved_plan(
    db: Session,
    *,
    plan: StaffPartyAdoptionPlan,
    approval: StaffPartyAdoptionApproval,
    plan_file_sha256: str,
    executed_at: datetime | None = None,
) -> StaffPartyAdoptionOutcome:
    """Delegate one approved plan without owning business writes or commits."""

    fixed_now = executed_at

    def revalidate() -> None:
        validate_approval(
            plan=plan,
            approval=approval,
            plan_file_sha256=plan_file_sha256,
            executed_at=fixed_now or datetime.now(UTC),
        )

    revalidate()
    source = f"{STAFF_BINDING_SOURCE_PREFIX}{plan.plan_digest}"
    principal_applied = 0
    principal_replays = 0
    projection_applied = 0
    projection_replays = 0

    for item in plan.items:
        reason = _binding_reason(item, approval)
        if isinstance(item, StaffPrincipalAndCredentialAdoptionItem):
            revalidate()
            staff_context = _command_context(
                plan=plan,
                approval=approval,
                item=item,
                phase=StaffPartyAdoptionPhase.principal_binding,
                scope=staff_party_adoption.COMMAND_SCOPE,
            )
            try:
                staff_outcome = staff_party_adoption.bind_existing_staff_party(
                    db,
                    BindExistingStaffPartyCommand(
                        context=staff_context,
                        system_user_id=item.system_user_id,
                        person_party_id=item.person_party_id,
                        binding_source=source,
                        binding_reason=reason,
                    ),
                )
            except DomainError as exc:
                _owner_refusal(
                    exc,
                    item=item,
                    phase=StaffPartyAdoptionPhase.principal_binding,
                )
            principal_replays += int(staff_outcome.replayed)
            principal_applied += int(not staff_outcome.replayed)

        revalidate()
        credential_context = _command_context(
            plan=plan,
            approval=approval,
            item=item,
            phase=StaffPartyAdoptionPhase.credential_projection,
            scope=CREDENTIAL_PROJECTION_SCOPE,
        )
        try:
            projection_outcome = credential_party_binding.bind_credential_party(
                db,
                CredentialPartyBinding(
                    context=credential_context,
                    credential_id=item.credential_id,
                    expected_principal_kind=CredentialPrincipalKind.system_user,
                    expected_principal_id=item.system_user_id,
                    party_id=item.person_party_id,
                    authentication_binding_id=item.authentication_binding_id,
                    tenant_id=operator_tenant_id(),
                    binding_source=source,
                    binding_reason=reason,
                ),
            )
        except DomainError as exc:
            _owner_refusal(
                exc,
                item=item,
                phase=StaffPartyAdoptionPhase.credential_projection,
            )
        projection_replays += int(projection_outcome.replayed)
        projection_applied += int(not projection_outcome.replayed)

    return StaffPartyAdoptionOutcome(
        plan_digest=plan.plan_digest,
        principal_bindings_applied=principal_applied,
        principal_binding_replays=principal_replays,
        credential_projections_applied=projection_applied,
        credential_projection_replays=projection_replays,
    )


def execute_approved_plan(
    db: Session,
    *,
    plan: StaffPartyAdoptionPlan,
    approval: StaffPartyAdoptionApproval,
    plan_file_sha256: str,
    executed_at: datetime | None = None,
) -> StaffPartyAdoptionOutcome:
    """Return one typed outcome or raise one PII-free typed refusal."""

    try:
        return _execute_approved_plan(
            db,
            plan=plan,
            approval=approval,
            plan_file_sha256=plan_file_sha256,
            executed_at=executed_at,
        )
    except SQLAlchemyError as exc:
        raise StaffPartyAdoptionRefused(
            StaffPartyAdoptionRefusalCode.database_failure,
            "A database operation failed; no identity values were emitted",
        ) from exc


_PUBLIC_CONTRACT_TYPES: tuple[type[object], ...] = (
    StaffPrincipalAndCredentialAdoptionItem,
    StaffCredentialProjectionItem,
    StaffPartyAdoptionPlan,
    StaffPartyAdoptionApproval,
    StaffPartyAdoptionOutcome,
    StaffPartyAdoptionRefused,
    BindExistingStaffPartyCommand,
    ExistingStaffPartyBindingOutcome,
    CredentialPartyBinding,
    CredentialPartyBindingOutcome,
)
_PUBLIC_CONTRACT_FUNCTIONS = (
    build_plan,
    parse_plan_payload,
    parse_approval_payload,
    validate_approval,
    execute_approved_plan,
)


def _contains_any(annotation: object) -> bool:
    return annotation is typing.Any or any(
        _contains_any(argument) for argument in get_args(annotation)
    )


def public_contract_type_errors() -> tuple[str, ...]:
    """Return public adoption boundaries containing an untyped annotation."""

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
            StaffPartyAdoptionRefusalCode.invalid_file,
            f"{field} must be a regular, non-symlink file",
        )
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        _refuse(
            StaffPartyAdoptionRefusalCode.invalid_file,
            f"{field} must have mode 0o600",
        )


def _file_sha256(path: Path) -> str:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise StaffPartyAdoptionRefused(
            StaffPartyAdoptionRefusalCode.invalid_file,
            "An execution input could not be read",
        ) from exc
    return hashlib.sha256(content).hexdigest()


def _load_json_object(path: Path, field: str) -> Mapping[str, object]:
    _assert_private_file(path, field)
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StaffPartyAdoptionRefused(
            StaffPartyAdoptionRefusalCode.invalid_file,
            f"{field} is not valid JSON",
        ) from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        _refuse(
            StaffPartyAdoptionRefusalCode.invalid_file,
            f"{field} must be a JSON object with string keys",
        )
    return cast(dict[str, object], raw)


def load_plan(path: Path) -> StaffPartyAdoptionPlan:
    return parse_plan_payload(_load_json_object(path, "plan file"))


def load_approval(path: Path) -> StaffPartyAdoptionApproval:
    return parse_approval_payload(_load_json_object(path, "approval file"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--confirm-plan-digest", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Acknowledge that the exact approved owner commands will commit",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.execute:
        print("REFUSED: --execute acknowledgement is required")
        return 2
    try:
        _assert_private_file(args.plan, "plan file")
        plan_file_sha256 = _file_sha256(args.plan)
        plan = load_plan(args.plan)
        _assert_private_file(args.approval, "approval file")
        approval_file_sha256 = _file_sha256(args.approval)
        approval = load_approval(args.approval)
        if not secrets.compare_digest(args.confirm_plan_digest, plan.plan_digest):
            _refuse(
                StaffPartyAdoptionRefusalCode.approval_mismatch,
                "Typed --confirm-plan-digest does not match the plan",
            )
        validate_approval(
            plan=plan,
            approval=approval,
            plan_file_sha256=plan_file_sha256,
            executed_at=datetime.now(UTC),
        )
        if not secrets.compare_digest(
            _file_sha256(args.plan), plan_file_sha256
        ) or not (
            secrets.compare_digest(_file_sha256(args.approval), approval_file_sha256)
        ):
            _refuse(
                StaffPartyAdoptionRefusalCode.changed_input,
                "An execution input changed while it was being validated",
            )
        with SessionLocal() as db:
            outcome = execute_approved_plan(
                db,
                plan=plan,
                approval=approval,
                plan_file_sha256=plan_file_sha256,
            )
    except StaffPartyAdoptionRefused as exc:
        suffix = f" owner_code={exc.owner_code}" if exc.owner_code else ""
        print(f"REFUSED: {exc.code.value}: {exc.message}{suffix}")
        return 1 if exc.code is StaffPartyAdoptionRefusalCode.database_failure else 2
    print(outcome.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
