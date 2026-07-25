"""Previewed, atomic staff commands for dunning-case lifecycle actions."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.schemas.audit import AuditEventCreate
from app.services import audit as audit_service
from app.services.collections import _core as dunning_owner
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "financial.dunning_staff_actions"
ACTION_SCOPE = "billing:dunning:write"
MAX_SELECTED_CASES = 100
_ACTION_CONCERN = "atomic staff dunning-case transition and audit coordination"
_CONFIRM_ACTION = OwnerCommandDefinition(
    owner=OWNER,
    concern=_ACTION_CONCERN,
    name="confirm_dunning_staff_action",
)


class DunningStaffCommandError(DomainError):
    """Stable errors for dunning staff preview and confirmation."""


@dataclass(frozen=True, slots=True)
class ConfirmDunningStaffAction:
    action: dunning_owner.DunningStaffAction
    case_ids: tuple[UUID, ...]
    preview_fingerprint: str
    confirmed: bool
    actor_id: str
    context: CommandContext


@dataclass(frozen=True, slots=True)
class DunningStaffActionResult:
    preview: dunning_owner.DunningStaffActionPreview
    processed_case_ids: tuple[UUID, ...]


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> DunningStaffCommandError:
    return DunningStaffCommandError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
    )


def normalize_case_ids(values: tuple[str | UUID, ...]) -> tuple[UUID, ...]:
    """Normalize an explicit selected scope; missing never means all."""

    normalized: list[UUID] = []
    seen: set[UUID] = set()
    for value in values:
        try:
            case_id = value if isinstance(value, UUID) else UUID(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise _error(
                "invalid_selection",
                "Dunning selection contains an invalid case identifier.",
            ) from exc
        if case_id in seen:
            continue
        seen.add(case_id)
        normalized.append(case_id)
    if not normalized:
        raise _error(
            "invalid_selection",
            "Select at least one dunning case.",
        )
    if len(normalized) > MAX_SELECTED_CASES:
        raise _error(
            "invalid_selection",
            f"Select no more than {MAX_SELECTED_CASES} dunning cases at once.",
            selected_count=len(normalized),
        )
    return tuple(sorted(normalized, key=str))


def preview_staff_action(
    db: Session,
    *,
    action: dunning_owner.DunningStaffAction,
    case_ids: tuple[str | UUID, ...],
) -> dunning_owner.DunningStaffActionPreview:
    """Return the dunning owner's exact selected-scope impact."""

    normalized_ids = normalize_case_ids(case_ids)
    return dunning_owner.preview_dunning_staff_action(
        db,
        case_ids=normalized_ids,
        action=action,
    )


def _stage_audit(
    db: Session,
    *,
    command: ConfirmDunningStaffAction,
    preview: dunning_owner.DunningStaffActionPreview,
    impact: dunning_owner.DunningStaffCaseImpact,
) -> None:
    audit_service.audit_events.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=command.actor_id,
            action=preview.action.value,
            entity_type="dunning_case",
            entity_id=str(impact.case_id),
            status_code=200,
            is_success=True,
            request_id=str(command.context.correlation_id),
            metadata_={
                "owner": OWNER,
                "account_id": str(impact.account_id),
                "current_status": (
                    impact.current_status.value if impact.current_status else None
                ),
                "resulting_status": (
                    impact.resulting_status.value if impact.resulting_status else None
                ),
                "preview_fingerprint": preview.fingerprint,
                "selected_count": preview.selected_count,
                "eligible_count": preview.eligible_count,
                "skipped_count": preview.skipped_count,
                "command_id": str(command.context.command_id),
                "command_scope": command.context.scope,
                "command_reason": command.context.reason,
            },
        ),
    )


def _stage_confirmation(
    db: Session,
    command: ConfirmDunningStaffAction,
) -> DunningStaffActionResult:
    if command.context.scope != ACTION_SCOPE:
        raise _error(
            "invalid_scope",
            "Dunning staff action has an invalid authorization scope.",
        )
    actor_id = command.actor_id.strip()
    if not actor_id:
        raise _error("invalid_actor", "Authorized staff actor is required.")
    if not command.confirmed:
        raise _error(
            "confirmation_required",
            "Confirm the displayed dunning impact before applying this action.",
            field="confirmed",
        )
    case_ids = normalize_case_ids(command.case_ids)
    preview = dunning_owner.preview_dunning_staff_action(
        db,
        case_ids=case_ids,
        action=command.action,
        lock=True,
    )
    if preview.fingerprint != command.preview_fingerprint:
        raise _error(
            "stale_preview",
            "Dunning case state or eligibility changed; review the new impact.",
        )
    if not preview.eligible_case_ids:
        raise _error(
            "no_eligible_cases",
            "None of the selected dunning cases is eligible for this action.",
        )

    impacts = {impact.case_id: impact for impact in preview.impacts}
    processed: list[UUID] = []
    for case_id in preview.eligible_case_ids:
        dunning_owner.stage_dunning_staff_case_action(
            db,
            case_id=case_id,
            action=preview.action,
            actor=command.context.actor,
        )
        _stage_audit(
            db,
            command=command,
            preview=preview,
            impact=impacts[case_id],
        )
        processed.append(case_id)
    return DunningStaffActionResult(
        preview=preview,
        processed_case_ids=tuple(processed),
    )


def confirm_staff_action(
    db: Session,
    command: ConfirmDunningStaffAction,
) -> DunningStaffActionResult:
    """Confirm the exact eligible subset in one owned transaction."""

    return execute_owner_command(
        db,
        definition=_CONFIRM_ACTION,
        context=command.context,
        operation=lambda: _stage_confirmation(db, command),
    )
