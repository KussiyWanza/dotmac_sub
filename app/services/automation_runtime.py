"""Automation event planning, condition evaluation, and durable run ledger."""

from __future__ import annotations

import hashlib
import json
import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.automation import (
    AutomationRule,
    AutomationRuleStatus,
    AutomationRuleVersion,
    AutomationRun,
    AutomationRunStatus,
    AutomationStepRun,
    AutomationStepStatus,
)
from app.services import automation_actions, automation_capabilities
from app.services.automation_contracts import (
    AutomationConditionField,
    AutomationOperator,
    AutomationValueType,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "automation.execution"
RUN_READ_PERMISSION = "automation:run:read"
RUN_REDRIVE_PERMISSION = "automation:run:redrive"
_STEP_LEASE = timedelta(minutes=5)
_PREPARE = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation execution decisions and run evidence",
    name="prepare_automation_event_runs",
)
_CLAIM_STEP = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation execution decisions and run evidence",
    name="claim_automation_step",
)
_FINISH_STEP = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation execution decisions and run evidence",
    name="finish_automation_step",
)


class AutomationExecutionError(DomainError):
    pass


class StepClaimDisposition(StrEnum):
    execute = "execute"
    already_succeeded = "already_succeeded"
    busy = "busy"


@dataclass(frozen=True, slots=True)
class AutomationEventEnvelope:
    event_id: UUID
    event_type: str
    trigger_key: str
    tenant_id: UUID
    target_type: str
    target_id: UUID
    occurred_at: datetime
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PrepareAutomationEventCommand:
    event: AutomationEventEnvelope
    context: CommandContext


@dataclass(frozen=True, slots=True)
class PreparedAutomationStep:
    step_id: UUID
    step_index: int
    action_key: str
    inputs: tuple[automation_actions.AutomationActionInputValue, ...]


@dataclass(frozen=True, slots=True)
class PreparedAutomationRun:
    run_id: UUID
    rule_id: UUID
    rule_version_id: UUID
    target: automation_actions.AutomationTargetReference
    steps: tuple[PreparedAutomationStep, ...]


@dataclass(frozen=True, slots=True)
class ClaimAutomationStepCommand:
    tenant_id: UUID
    step_id: UUID
    context: CommandContext


@dataclass(frozen=True, slots=True)
class StepClaimOutcome:
    disposition: StepClaimDisposition
    attempt_count: int


@dataclass(frozen=True, slots=True)
class FinishAutomationStepCommand:
    tenant_id: UUID
    step_id: UUID
    succeeded: bool
    error_code: str | None
    context: CommandContext


@dataclass(frozen=True, slots=True)
class AutomationStepOutcome:
    run_id: UUID
    step_id: UUID
    step_status: AutomationStepStatus
    run_status: AutomationRunStatus


@dataclass(frozen=True, slots=True)
class ListAutomationRunsQuery:
    tenant_id: UUID
    limit: int = 100
    status: AutomationRunStatus | None = None


@dataclass(frozen=True, slots=True)
class AutomationRunSummary:
    run_id: UUID
    rule_id: UUID
    rule_version_id: UUID
    event_id: UUID
    event_type: str
    target_type: str
    target_id: UUID
    status: AutomationRunStatus
    matched: bool | None
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None


def _error(code: str, message: str, **details: object) -> AutomationExecutionError:
    return AutomationExecutionError(
        code=f"{OWNER}.{code}", message=message, details=details
    )


def _payload_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _path_value(payload: Mapping[str, object], path: str) -> object:
    value: object = payload
    for component in path.split("."):
        if not isinstance(value, Mapping) or component not in value:
            return None
        value = value[component]
    return value


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise InvalidOperation
    return Decimal(str(value))


def _typed_value(field: AutomationConditionField, value: object) -> object:
    if value is None:
        return None
    if isinstance(value, list | tuple):
        return tuple(_typed_value(field, item) for item in value)
    if field.value_type in {AutomationValueType.string, AutomationValueType.enum}:
        return str(value)
    if field.value_type is AutomationValueType.integer:
        if isinstance(value, bool):
            raise ValueError
        return int(str(value))
    if field.value_type is AutomationValueType.decimal:
        return _decimal(value)
    if field.value_type is AutomationValueType.boolean:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().casefold()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
        raise ValueError
    if field.value_type is AutomationValueType.date:
        return date.fromisoformat(str(value))
    if field.value_type is AutomationValueType.datetime:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    if field.value_type is AutomationValueType.uuid:
        return UUID(str(value))
    raise ValueError


def _stored_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _stored_mapping_list(
    value: object,
) -> tuple[Mapping[str, object], ...] | None:
    if not isinstance(value, list):
        return None
    items: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        items.append(item)
    return tuple(items)


_ORDERED_COMPARISONS: dict[AutomationOperator, Callable[[object, object], bool]] = {
    AutomationOperator.greater_than: cast(
        Callable[[object, object], bool], operator.gt
    ),
    AutomationOperator.greater_than_or_equal: cast(
        Callable[[object, object], bool], operator.ge
    ),
    AutomationOperator.less_than: cast(Callable[[object, object], bool], operator.lt),
    AutomationOperator.less_than_or_equal: cast(
        Callable[[object, object], bool], operator.le
    ),
}


def _matches_condition(
    *,
    field: AutomationConditionField,
    condition: Mapping[str, object],
    observed: object,
) -> bool:
    try:
        operator = AutomationOperator(str(condition.get("operator") or ""))
        if operator not in field.operators:
            return False
        left = _typed_value(field, observed)
        right = _typed_value(field, condition.get("value"))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if operator is AutomationOperator.is_empty:
        return left is None or left == "" or left == ()
    if operator is AutomationOperator.is_not_empty:
        return left is not None and left != "" and left != ()
    if operator is AutomationOperator.equals:
        return left == right
    if operator is AutomationOperator.not_equals:
        return left != right
    if operator is AutomationOperator.in_values:
        return isinstance(right, tuple) and left in right
    if operator is AutomationOperator.not_in_values:
        return isinstance(right, tuple) and left not in right
    if left is None or right is None:
        return False
    comparison = _ORDERED_COMPARISONS.get(operator)
    if comparison is not None:
        try:
            return comparison(left, right)
        except TypeError:
            return False
    if operator is AutomationOperator.contains:
        return str(right) in str(left)
    return False


def _rule_matches(
    *,
    trigger_key: str,
    conditions: list[dict[str, object]],
    payload: Mapping[str, object],
) -> bool:
    trigger = automation_capabilities.trigger_capability(trigger_key)
    fields = {field.key: field for field in trigger.fields}
    for condition in conditions:
        field_key = str(condition.get("field_key") or "")
        field = fields.get(field_key)
        if field is None or not _matches_condition(
            field=field,
            condition=condition,
            observed=_path_value(payload, field_key),
        ):
            return False
    return True


def _prepared_steps(
    db: Session, *, run: AutomationRun, version: AutomationRuleVersion
) -> tuple[PreparedAutomationStep, ...]:
    rows = tuple(
        db.scalars(
            select(AutomationStepRun)
            .where(AutomationStepRun.run_id == run.id)
            .order_by(AutomationStepRun.step_index)
        )
    )
    definitions: dict[int, Mapping[str, object]] = {}
    for item in version.actions:
        position = _stored_int(item.get("position"))
        if position is None or position in definitions:
            raise _error(
                "stored_action_invalid",
                "A stored action has an invalid position.",
            )
        definitions[position] = item
    prepared: list[PreparedAutomationStep] = []
    for row in rows:
        if row.status == AutomationStepStatus.succeeded.value:
            continue
        definition = definitions.get(row.step_index)
        inputs = (
            _stored_mapping_list(definition.get("inputs"))
            if definition is not None
            else None
        )
        if definition is None or inputs is None:
            raise _error(
                "stored_action_invalid",
                "A stored action no longer matches its execution step.",
                step_index=row.step_index,
            )
        prepared.append(
            PreparedAutomationStep(
                step_id=row.id,
                step_index=row.step_index,
                action_key=row.action_key,
                inputs=tuple(
                    automation_actions.AutomationActionInputValue(
                        key=str(item.get("key") or ""), value=item.get("value")
                    )
                    for item in inputs
                ),
            )
        )
    return tuple(prepared)


def prepare_event_runs(
    db: Session, command: PrepareAutomationEventCommand
) -> tuple[PreparedAutomationRun, ...]:
    def operation() -> tuple[PreparedAutomationRun, ...]:
        automation_actions.require_valid_runtime_registry()
        trigger = automation_capabilities.trigger_capability(command.event.trigger_key)
        if trigger.event_type != command.event.event_type:
            raise _error(
                "trigger_event_mismatch",
                "The event does not match the declared trigger.",
            )
        if trigger.entity_type != command.event.target_type:
            raise _error(
                "trigger_target_mismatch",
                "The event target does not match the declared trigger.",
            )
        statement = (
            select(AutomationRule, AutomationRuleVersion)
            .join(
                AutomationRuleVersion,
                AutomationRuleVersion.id == AutomationRule.active_version_id,
            )
            .where(
                AutomationRule.tenant_id == command.event.tenant_id,
                AutomationRule.trigger_key == command.event.trigger_key,
                AutomationRule.status == AutomationRuleStatus.published.value,
            )
            .order_by(AutomationRule.id)
        )
        prepared: list[PreparedAutomationRun] = []
        for rule, version in db.execute(statement).tuples():
            existing = db.scalar(
                select(AutomationRun)
                .where(
                    AutomationRun.rule_version_id == version.id,
                    AutomationRun.event_id == command.event.event_id,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.status in {
                    AutomationRunStatus.succeeded.value,
                    AutomationRunStatus.skipped.value,
                }:
                    continue
                existing.status = AutomationRunStatus.running.value
                existing.error_code = None
                existing.completed_at = None
                prepared.append(
                    PreparedAutomationRun(
                        run_id=existing.id,
                        rule_id=rule.id,
                        rule_version_id=version.id,
                        target=automation_actions.AutomationTargetReference(
                            entity_type=command.event.target_type,
                            entity_id=command.event.target_id,
                        ),
                        steps=_prepared_steps(db, run=existing, version=version),
                    )
                )
                continue
            matched = _rule_matches(
                trigger_key=rule.trigger_key,
                conditions=version.conditions,
                payload=command.event.payload,
            )
            now = datetime.now(UTC)
            run = AutomationRun(
                tenant_id=command.event.tenant_id,
                rule_id=rule.id,
                rule_version_id=version.id,
                event_id=command.event.event_id,
                event_type=command.event.event_type,
                target_type=command.event.target_type,
                target_id=command.event.target_id,
                status=(
                    AutomationRunStatus.running.value
                    if matched
                    else AutomationRunStatus.skipped.value
                ),
                matched=matched,
                payload_sha256=_payload_sha256(command.event.payload),
                started_at=now,
                completed_at=None if matched else now,
            )
            db.add(run)
            db.flush()
            if not matched:
                continue
            for step in version.actions:
                position = _stored_int(step.get("position"))
                if position is None:
                    raise _error(
                        "stored_action_invalid",
                        "A stored action has an invalid position.",
                    )
                db.add(
                    AutomationStepRun(
                        tenant_id=command.event.tenant_id,
                        run_id=run.id,
                        step_index=position,
                        action_key=str(step["action_key"]),
                        status=AutomationStepStatus.pending.value,
                        idempotency_key=(
                            f"automation:{command.event.event_id}:{version.id}:"
                            f"{position}"
                        ),
                    )
                )
            db.flush()
            prepared.append(
                PreparedAutomationRun(
                    run_id=run.id,
                    rule_id=rule.id,
                    rule_version_id=version.id,
                    target=automation_actions.AutomationTargetReference(
                        entity_type=command.event.target_type,
                        entity_id=command.event.target_id,
                    ),
                    steps=_prepared_steps(db, run=run, version=version),
                )
            )
        return tuple(prepared)

    return execute_owner_command(
        db, definition=_PREPARE, context=command.context, operation=operation
    )


def claim_step(db: Session, command: ClaimAutomationStepCommand) -> StepClaimOutcome:
    def operation() -> StepClaimOutcome:
        step = db.scalar(
            select(AutomationStepRun)
            .where(
                AutomationStepRun.id == command.step_id,
                AutomationStepRun.tenant_id == command.tenant_id,
            )
            .with_for_update()
        )
        if step is None:
            raise _error("step_not_found", "Automation step not found.")
        if step.status == AutomationStepStatus.succeeded.value:
            return StepClaimOutcome(
                disposition=StepClaimDisposition.already_succeeded,
                attempt_count=step.attempt_count,
            )
        now = datetime.now(UTC)
        if (
            step.status == AutomationStepStatus.running.value
            and step.started_at is not None
            and step.started_at > now - _STEP_LEASE
        ):
            return StepClaimOutcome(
                disposition=StepClaimDisposition.busy,
                attempt_count=step.attempt_count,
            )
        step.status = AutomationStepStatus.running.value
        step.attempt_count += 1
        step.started_at = now
        step.completed_at = None
        step.error_code = None
        db.flush()
        return StepClaimOutcome(
            disposition=StepClaimDisposition.execute,
            attempt_count=step.attempt_count,
        )

    return execute_owner_command(
        db, definition=_CLAIM_STEP, context=command.context, operation=operation
    )


def finish_step(
    db: Session, command: FinishAutomationStepCommand
) -> AutomationStepOutcome:
    def operation() -> AutomationStepOutcome:
        step = db.scalar(
            select(AutomationStepRun)
            .where(
                AutomationStepRun.id == command.step_id,
                AutomationStepRun.tenant_id == command.tenant_id,
            )
            .with_for_update()
        )
        if step is None:
            raise _error("step_not_found", "Automation step not found.")
        run = db.scalar(
            select(AutomationRun)
            .where(
                AutomationRun.id == step.run_id,
                AutomationRun.tenant_id == command.tenant_id,
            )
            .with_for_update()
        )
        if run is None:
            raise _error("run_not_found", "Automation run not found.")
        if step.status == AutomationStepStatus.succeeded.value and command.succeeded:
            return AutomationStepOutcome(
                run_id=run.id,
                step_id=step.id,
                step_status=AutomationStepStatus.succeeded,
                run_status=AutomationRunStatus(run.status),
            )
        now = datetime.now(UTC)
        step.status = (
            AutomationStepStatus.succeeded.value
            if command.succeeded
            else AutomationStepStatus.failed.value
        )
        step.error_code = None if command.succeeded else command.error_code
        step.completed_at = now
        if not command.succeeded:
            run.status = AutomationRunStatus.failed.value
            run.error_code = command.error_code or "action_failed"
            run.completed_at = now
            future_steps = tuple(
                db.scalars(
                    select(AutomationStepRun).where(
                        AutomationStepRun.run_id == run.id,
                        AutomationStepRun.step_index > step.step_index,
                        AutomationStepRun.status
                        != AutomationStepStatus.succeeded.value,
                    )
                )
            )
            for future in future_steps:
                future.status = AutomationStepStatus.blocked.value
        else:
            remaining = int(
                db.scalar(
                    select(func.count(AutomationStepRun.id)).where(
                        AutomationStepRun.run_id == run.id,
                        AutomationStepRun.id != step.id,
                        AutomationStepRun.status
                        != AutomationStepStatus.succeeded.value,
                    )
                )
                or 0
            )
            if remaining == 0:
                run.status = AutomationRunStatus.succeeded.value
                run.error_code = None
                run.completed_at = now
            else:
                run.status = AutomationRunStatus.running.value
                run.completed_at = None
        db.flush()
        return AutomationStepOutcome(
            run_id=run.id,
            step_id=step.id,
            step_status=AutomationStepStatus(step.status),
            run_status=AutomationRunStatus(run.status),
        )

    return execute_owner_command(
        db, definition=_FINISH_STEP, context=command.context, operation=operation
    )


def list_runs(
    db: Session, query: ListAutomationRunsQuery
) -> tuple[AutomationRunSummary, ...]:
    limit = min(max(query.limit, 1), 500)
    statement = select(AutomationRun).where(AutomationRun.tenant_id == query.tenant_id)
    if query.status is not None:
        statement = statement.where(AutomationRun.status == query.status.value)
    rows = tuple(
        db.scalars(
            statement.order_by(AutomationRun.created_at.desc(), AutomationRun.id).limit(
                limit
            )
        )
    )
    return tuple(
        AutomationRunSummary(
            run_id=row.id,
            rule_id=row.rule_id,
            rule_version_id=row.rule_version_id,
            event_id=row.event_id,
            event_type=row.event_type,
            target_type=row.target_type,
            target_id=row.target_id,
            status=AutomationRunStatus(row.status),
            matched=row.matched,
            error_code=row.error_code,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )
        for row in rows
    )
