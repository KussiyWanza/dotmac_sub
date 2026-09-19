"""Authoritative Automation Center rule-definition lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.automation import (
    AutomationRule,
    AutomationRuleStatus,
    AutomationRuleVersion,
)
from app.services import automation_capabilities
from app.services.automation_contracts import (
    AutomationActionCapability,
    AutomationConditionField,
    AutomationOperator,
    AutomationValueType,
)
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "automation.rule_definitions"
RULE_READ_PERMISSION = "automation:rule:read"
RULE_CREATE_PERMISSION = "automation:rule:create"
RULE_UPDATE_PERMISSION = "automation:rule:update"
RULE_PUBLISH_PERMISSION = "automation:rule:publish"
RULE_OPERATE_PERMISSION = "automation:rule:operate"

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_CREATE = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation rule definitions and immutable versions",
    name="create_automation_rule",
)
_REPLACE_DRAFT = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation rule definitions and immutable versions",
    name="replace_automation_rule_draft",
)
_PUBLISH = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation rule definitions and immutable versions",
    name="publish_automation_rule",
)
_CHANGE_STATUS = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation rule definitions and immutable versions",
    name="change_automation_rule_status",
)


class AutomationRuleError(DomainError):
    pass


class AutomationRuleOperation(StrEnum):
    pause = "pause"
    resume = "resume"
    retire = "retire"


AutomationScalar = str | int | Decimal | bool | date | datetime | UUID | None


@dataclass(frozen=True, slots=True)
class AutomationCondition:
    field_key: str
    operator: AutomationOperator
    value: AutomationScalar | tuple[AutomationScalar, ...]


@dataclass(frozen=True, slots=True)
class AutomationActionValue:
    key: str
    value: AutomationScalar | tuple[AutomationScalar, ...]


@dataclass(frozen=True, slots=True)
class AutomationActionStep:
    action_key: str
    inputs: tuple[AutomationActionValue, ...]


@dataclass(frozen=True, slots=True)
class CreateAutomationRuleCommand:
    tenant_id: UUID
    key: str
    name: str
    description: str | None
    trigger_key: str
    conditions: tuple[AutomationCondition, ...]
    actions: tuple[AutomationActionStep, ...]
    permission_keys: frozenset[str]
    context: CommandContext


@dataclass(frozen=True, slots=True)
class ReplaceAutomationRuleDraftCommand:
    tenant_id: UUID
    rule_id: UUID
    conditions: tuple[AutomationCondition, ...]
    actions: tuple[AutomationActionStep, ...]
    permission_keys: frozenset[str]
    context: CommandContext


@dataclass(frozen=True, slots=True)
class PublishAutomationRuleCommand:
    tenant_id: UUID
    rule_id: UUID
    permission_keys: frozenset[str]
    context: CommandContext


@dataclass(frozen=True, slots=True)
class ChangeAutomationRuleStatusCommand:
    tenant_id: UUID
    rule_id: UUID
    operation: AutomationRuleOperation
    permission_keys: frozenset[str]
    context: CommandContext


@dataclass(frozen=True, slots=True)
class AutomationRuleOutcome:
    rule_id: UUID
    version_id: UUID | None
    version: int | None
    status: AutomationRuleStatus


@dataclass(frozen=True, slots=True)
class ListAutomationRulesQuery:
    tenant_id: UUID
    include_retired: bool = False


def _error(code: str, message: str, **details: object) -> AutomationRuleError:
    return AutomationRuleError(
        code=f"{OWNER}.{code}",
        message=message,
        details=details,
    )


def _require_permission(permission_keys: frozenset[str], required: str) -> None:
    if "*" not in permission_keys and required not in permission_keys:
        raise _error(
            "permission_denied",
            "The automation command is not authorized.",
            required_permission=required,
        )


def _canonical_value(value: object) -> object:
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _value_matches(field: AutomationConditionField, value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, tuple):
        return all(_value_matches(field, item) for item in value)
    expected = field.value_type
    if expected is AutomationValueType.string:
        return isinstance(value, str)
    if expected is AutomationValueType.integer:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is AutomationValueType.decimal:
        return isinstance(value, Decimal | int) and not isinstance(value, bool)
    if expected is AutomationValueType.boolean:
        return isinstance(value, bool)
    if expected is AutomationValueType.date:
        return isinstance(value, date) and not isinstance(value, datetime)
    if expected is AutomationValueType.datetime:
        return isinstance(value, datetime) and value.tzinfo is not None
    if expected is AutomationValueType.uuid:
        return isinstance(value, UUID)
    if expected is AutomationValueType.enum:
        return isinstance(value, str) and value in field.enum_values
    return False


def _stored_value_matches(field: AutomationConditionField, value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, list):
        return all(_stored_value_matches(field, item) for item in value)
    expected = field.value_type
    if expected in {AutomationValueType.string, AutomationValueType.enum}:
        return isinstance(value, str) and (
            expected is AutomationValueType.string or value in field.enum_values
        )
    if expected is AutomationValueType.integer:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is AutomationValueType.boolean:
        return isinstance(value, bool)
    if expected is AutomationValueType.decimal:
        if not isinstance(value, str | int) or isinstance(value, bool):
            return False
        try:
            Decimal(value)
        except InvalidOperation:
            return False
        return True
    if expected is AutomationValueType.uuid:
        try:
            UUID(str(value))
        except (TypeError, ValueError):
            return False
        return True
    if expected is AutomationValueType.date:
        try:
            parsed_date = date.fromisoformat(str(value))
        except (TypeError, ValueError):
            return False
        return not isinstance(parsed_date, datetime)
    if expected is AutomationValueType.datetime:
        try:
            parsed_datetime = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return False
        return parsed_datetime.tzinfo is not None
    return False


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


def _validate_conditions(
    trigger_key: str, conditions: tuple[AutomationCondition, ...]
) -> list[dict[str, object]]:
    trigger = automation_capabilities.trigger_capability(trigger_key)
    declared = {field.key: field for field in trigger.fields}
    serialized: list[dict[str, object]] = []
    for condition in conditions:
        field = declared.get(condition.field_key)
        if field is None:
            raise _error(
                "condition_field_undeclared",
                "A condition references a field not declared by its trigger.",
                field_key=condition.field_key,
            )
        if condition.operator not in field.operators:
            raise _error(
                "condition_operator_unsupported",
                "A condition operator is not supported for its field.",
                field_key=condition.field_key,
                operator=condition.operator.value,
            )
        if not _value_matches(field, condition.value):
            raise _error(
                "condition_value_invalid",
                "A condition value does not match its declared field type.",
                field_key=condition.field_key,
                value_type=field.value_type.value,
            )
        serialized.append(
            {
                "field_key": condition.field_key,
                "operator": condition.operator.value,
                "value": _canonical_value(condition.value),
            }
        )
    return serialized


def _validate_action_inputs(
    capability: AutomationActionCapability,
    values: tuple[AutomationActionValue, ...],
) -> list[dict[str, object]]:
    if len({item.key for item in values}) != len(values):
        raise _error("action_input_duplicate", "An action repeats an input key.")
    supplied = {item.key: item.value for item in values}
    declared = {item.key: item for item in capability.inputs}
    unknown = sorted(set(supplied) - set(declared))
    missing = sorted(
        key for key, item in declared.items() if item.required and key not in supplied
    )
    if unknown or missing:
        raise _error(
            "action_inputs_invalid",
            "An action has unknown or missing inputs.",
            unknown=tuple(unknown),
            missing=tuple(missing),
        )
    result: list[dict[str, object]] = []
    for key, value in supplied.items():
        definition = declared[key]
        field = AutomationConditionField(
            key=definition.key,
            label=definition.label,
            value_type=definition.value_type,
            operators=(AutomationOperator.equals,),
            enum_values=definition.enum_values,
        )
        if not _value_matches(field, value):
            raise _error(
                "action_input_value_invalid",
                "An action input does not match its declared type.",
                action_key=capability.key,
                input_key=key,
            )
        result.append({"key": key, "value": _canonical_value(value)})
    return result


def _legacy_conflicts(selected_scopes: set[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            surface.key
            for module in automation_capabilities.registered_module_manifests()
            for surface in module.legacy_surfaces
            if selected_scopes.intersection(surface.conflict_scopes)
        )
    )


def _validate_definition(
    *,
    trigger_key: str,
    conditions: tuple[AutomationCondition, ...],
    actions: tuple[AutomationActionStep, ...],
    permission_keys: frozenset[str],
) -> tuple[int, list[dict[str, object]], list[dict[str, object]]]:
    automation_capabilities.require_valid_capability_registry()
    trigger = automation_capabilities.trigger_capability(trigger_key)
    _require_permission(permission_keys, trigger.author_permission)
    serialized_conditions = _validate_conditions(trigger_key, conditions)
    if not actions:
        raise _error("actions_required", "An automation rule requires an action.")
    serialized_actions: list[dict[str, object]] = []
    selected_scopes = {trigger.key}
    for position, step in enumerate(actions):
        capability = automation_capabilities.action_capability(step.action_key)
        if capability.entity_type != trigger.entity_type:
            raise _error(
                "action_target_mismatch",
                "An action cannot operate on the trigger target type.",
                action_key=capability.key,
                trigger_target=trigger.entity_type,
                action_target=capability.entity_type,
            )
        _require_permission(permission_keys, capability.author_permission)
        selected_scopes.add(capability.key)
        serialized_actions.append(
            {
                "position": position,
                "action_key": capability.key,
                "schema_version": capability.input_schema_version,
                "inputs": _validate_action_inputs(capability, step.inputs),
            }
        )
    conflicts = _legacy_conflicts(selected_scopes)
    if conflicts:
        raise _error(
            "legacy_scope_conflict",
            "The rule overlaps an exclusively legacy-owned automation scope.",
            legacy_surfaces=conflicts,
        )
    return trigger.event_schema_version, serialized_conditions, serialized_actions


def _validate_persisted_definition(
    *,
    rule: AutomationRule,
    version: AutomationRuleVersion,
    permission_keys: frozenset[str],
) -> None:
    automation_capabilities.require_valid_capability_registry()
    trigger = automation_capabilities.trigger_capability(rule.trigger_key)
    _require_permission(permission_keys, trigger.author_permission)
    if version.trigger_schema_version != trigger.event_schema_version:
        raise _error(
            "trigger_schema_stale",
            "The draft trigger schema is no longer current.",
        )
    fields = {field.key: field for field in trigger.fields}
    for condition in version.conditions:
        field_key = str(condition.get("field_key") or "")
        field = fields.get(field_key)
        try:
            operator = AutomationOperator(str(condition.get("operator") or ""))
        except ValueError as exc:
            raise _error(
                "condition_operator_unsupported",
                "A stored condition operator is no longer supported.",
                field_key=field_key,
            ) from exc
        if (
            field is None
            or operator not in field.operators
            or not _stored_value_matches(field, condition.get("value"))
        ):
            raise _error(
                "condition_contract_stale",
                "A stored condition no longer matches its trigger contract.",
                field_key=field_key,
            )
    selected_scopes = {trigger.key}
    for position, step in enumerate(version.actions):
        action_key = str(step.get("action_key") or "")
        capability = automation_capabilities.action_capability(action_key)
        if capability.entity_type != trigger.entity_type:
            raise _error(
                "action_target_mismatch",
                "A stored action no longer matches the trigger target.",
                action_key=action_key,
            )
        if _stored_int(step.get("position")) != position:
            raise _error("action_order_invalid", "Stored action order is invalid.")
        if _stored_int(step.get("schema_version")) != capability.input_schema_version:
            raise _error(
                "action_schema_stale",
                "A stored action schema is no longer current.",
                action_key=action_key,
            )
        _require_permission(permission_keys, capability.author_permission)
        declared = {item.key: item for item in capability.inputs}
        stored_inputs = _stored_mapping_list(step.get("inputs"))
        if stored_inputs is None:
            raise _error(
                "action_contract_stale",
                "Stored action inputs no longer match their contract.",
                action_key=action_key,
            )
        supplied = {
            str(item.get("key") or ""): item.get("value") for item in stored_inputs
        }
        if set(supplied) - set(declared) or any(
            item.required and key not in supplied for key, item in declared.items()
        ):
            raise _error(
                "action_contract_stale",
                "Stored action inputs no longer match their contract.",
                action_key=action_key,
            )
        for key, value in supplied.items():
            definition = declared[key]
            field = AutomationConditionField(
                key=key,
                label=definition.label,
                value_type=definition.value_type,
                operators=(AutomationOperator.equals,),
                enum_values=definition.enum_values,
            )
            if not _stored_value_matches(field, value):
                raise _error(
                    "action_contract_stale",
                    "A stored action value no longer matches its contract.",
                    action_key=action_key,
                    input_key=key,
                )
        selected_scopes.add(action_key)
    conflicts = _legacy_conflicts(selected_scopes)
    if conflicts:
        raise _error(
            "legacy_scope_conflict",
            "The rule overlaps an exclusively legacy-owned automation scope.",
            legacy_surfaces=conflicts,
        )


def _content_hash(*, trigger_key: str, conditions: object, actions: object) -> str:
    encoded = json.dumps(
        {"trigger_key": trigger_key, "conditions": conditions, "actions": actions},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rule(db: Session, *, tenant_id: UUID, rule_id: UUID, lock: bool) -> AutomationRule:
    statement = select(AutomationRule).where(
        AutomationRule.id == rule_id,
        AutomationRule.tenant_id == tenant_id,
    )
    if lock:
        statement = statement.with_for_update()
    row = db.scalar(statement)
    if row is None:
        raise _error("not_found", "Automation rule not found.")
    return row


def _draft(db: Session, rule_id: UUID) -> AutomationRuleVersion | None:
    return db.scalar(
        select(AutomationRuleVersion)
        .where(
            AutomationRuleVersion.rule_id == rule_id,
            AutomationRuleVersion.published_at.is_(None),
        )
        .with_for_update()
    )


def _emit_change(
    db: Session,
    *,
    rule: AutomationRule,
    version: AutomationRuleVersion | None,
    change: str,
) -> None:
    emit_event(
        db,
        EventType.automation_rule_changed,
        {
            "tenant_id": str(rule.tenant_id),
            "rule_id": str(rule.id),
            "version_id": str(version.id) if version else None,
            "version": version.version if version else None,
            "change": change,
            "status": rule.status,
        },
        actor=OWNER,
    )


def create_rule(
    db: Session, command: CreateAutomationRuleCommand
) -> AutomationRuleOutcome:
    def operation() -> AutomationRuleOutcome:
        _require_permission(command.permission_keys, RULE_CREATE_PERMISSION)
        key = command.key.strip()
        name = command.name.strip()
        if not _KEY_PATTERN.fullmatch(key) or not name:
            raise _error("identity_invalid", "Rule key or name is invalid.")
        trigger_schema, conditions, actions = _validate_definition(
            trigger_key=command.trigger_key,
            conditions=command.conditions,
            actions=command.actions,
            permission_keys=command.permission_keys,
        )
        content_sha256 = _content_hash(
            trigger_key=command.trigger_key,
            conditions=conditions,
            actions=actions,
        )
        existing = db.scalar(
            select(AutomationRule)
            .where(
                AutomationRule.tenant_id == command.tenant_id,
                AutomationRule.key == key,
            )
            .with_for_update()
        )
        if existing is not None:
            existing_draft = _draft(db, existing.id)
            if (
                existing.name == name
                and existing.trigger_key == command.trigger_key
                and existing_draft is not None
                and existing_draft.content_sha256 == content_sha256
            ):
                return AutomationRuleOutcome(
                    rule_id=existing.id,
                    version_id=existing_draft.id,
                    version=existing_draft.version,
                    status=AutomationRuleStatus(existing.status),
                )
            raise _error("key_conflict", "A rule with this key already exists.")
        rule = AutomationRule(
            tenant_id=command.tenant_id,
            key=key,
            name=name,
            description=(command.description or "").strip() or None,
            trigger_key=command.trigger_key,
            status=AutomationRuleStatus.draft.value,
            created_by=command.context.actor,
        )
        db.add(rule)
        db.flush()
        version = AutomationRuleVersion(
            tenant_id=command.tenant_id,
            rule_id=rule.id,
            version=1,
            trigger_schema_version=trigger_schema,
            conditions=conditions,
            actions=actions,
            content_sha256=content_sha256,
            created_by=command.context.actor,
        )
        db.add(version)
        db.flush()
        _emit_change(db, rule=rule, version=version, change="created")
        return AutomationRuleOutcome(
            rule_id=rule.id,
            version_id=version.id,
            version=version.version,
            status=AutomationRuleStatus(rule.status),
        )

    return execute_owner_command(
        db, definition=_CREATE, context=command.context, operation=operation
    )


def replace_draft(
    db: Session, command: ReplaceAutomationRuleDraftCommand
) -> AutomationRuleOutcome:
    def operation() -> AutomationRuleOutcome:
        _require_permission(command.permission_keys, RULE_UPDATE_PERMISSION)
        rule = _rule(
            db, tenant_id=command.tenant_id, rule_id=command.rule_id, lock=True
        )
        if rule.status == AutomationRuleStatus.retired.value:
            raise _error("retired", "A retired rule cannot be changed.")
        trigger_schema, conditions, actions = _validate_definition(
            trigger_key=rule.trigger_key,
            conditions=command.conditions,
            actions=command.actions,
            permission_keys=command.permission_keys,
        )
        version = _draft(db, rule.id)
        content_sha256 = _content_hash(
            trigger_key=rule.trigger_key,
            conditions=conditions,
            actions=actions,
        )
        if version is not None and version.content_sha256 == content_sha256:
            return AutomationRuleOutcome(
                rule_id=rule.id,
                version_id=version.id,
                version=version.version,
                status=AutomationRuleStatus(rule.status),
            )
        if version is None:
            next_version = (
                int(
                    db.scalar(
                        select(
                            func.coalesce(func.max(AutomationRuleVersion.version), 0)
                        ).where(AutomationRuleVersion.rule_id == rule.id)
                    )
                    or 0
                )
                + 1
            )
            version = AutomationRuleVersion(
                tenant_id=command.tenant_id,
                rule_id=rule.id,
                version=next_version,
                trigger_schema_version=trigger_schema,
                conditions=conditions,
                actions=actions,
                content_sha256=content_sha256,
                created_by=command.context.actor,
            )
            db.add(version)
        else:
            version.trigger_schema_version = trigger_schema
            version.conditions = conditions
            version.actions = actions
            version.content_sha256 = content_sha256
            version.created_by = command.context.actor
            version.created_at = datetime.now(UTC)
        db.flush()
        _emit_change(db, rule=rule, version=version, change="draft_replaced")
        return AutomationRuleOutcome(
            rule_id=rule.id,
            version_id=version.id,
            version=version.version,
            status=AutomationRuleStatus(rule.status),
        )

    return execute_owner_command(
        db, definition=_REPLACE_DRAFT, context=command.context, operation=operation
    )


def publish_rule(
    db: Session, command: PublishAutomationRuleCommand
) -> AutomationRuleOutcome:
    def operation() -> AutomationRuleOutcome:
        _require_permission(command.permission_keys, RULE_PUBLISH_PERMISSION)
        rule = _rule(
            db, tenant_id=command.tenant_id, rule_id=command.rule_id, lock=True
        )
        if rule.status == AutomationRuleStatus.retired.value:
            raise _error("retired", "A retired rule cannot be published.")
        version = _draft(db, rule.id)
        if version is None:
            active = db.get(AutomationRuleVersion, rule.active_version_id)
            if (
                active is not None
                and rule.status == AutomationRuleStatus.published.value
            ):
                return AutomationRuleOutcome(
                    rule_id=rule.id,
                    version_id=active.id,
                    version=active.version,
                    status=AutomationRuleStatus.published,
                )
            raise _error("draft_not_found", "The rule has no draft to publish.")
        _validate_persisted_definition(
            rule=rule,
            version=version,
            permission_keys=command.permission_keys,
        )
        version.published_at = datetime.now(UTC)
        version.published_by = command.context.actor
        rule.active_version_id = version.id
        rule.status = AutomationRuleStatus.published.value
        db.flush()
        _emit_change(db, rule=rule, version=version, change="published")
        return AutomationRuleOutcome(
            rule_id=rule.id,
            version_id=version.id,
            version=version.version,
            status=AutomationRuleStatus.published,
        )

    return execute_owner_command(
        db, definition=_PUBLISH, context=command.context, operation=operation
    )


def change_rule_status(
    db: Session, command: ChangeAutomationRuleStatusCommand
) -> AutomationRuleOutcome:
    def operation() -> AutomationRuleOutcome:
        _require_permission(command.permission_keys, RULE_OPERATE_PERMISSION)
        rule = _rule(
            db, tenant_id=command.tenant_id, rule_id=command.rule_id, lock=True
        )
        if command.operation is AutomationRuleOperation.pause:
            if rule.status == AutomationRuleStatus.paused.value:
                active = db.get(AutomationRuleVersion, rule.active_version_id)
                return AutomationRuleOutcome(
                    rule_id=rule.id,
                    version_id=active.id if active else None,
                    version=active.version if active else None,
                    status=AutomationRuleStatus.paused,
                )
            if rule.status != AutomationRuleStatus.published.value:
                raise _error("status_conflict", "Only a published rule can be paused.")
            rule.status = AutomationRuleStatus.paused.value
        elif command.operation is AutomationRuleOperation.resume:
            if rule.status == AutomationRuleStatus.published.value:
                active = db.get(AutomationRuleVersion, rule.active_version_id)
                return AutomationRuleOutcome(
                    rule_id=rule.id,
                    version_id=active.id if active else None,
                    version=active.version if active else None,
                    status=AutomationRuleStatus.published,
                )
            if rule.status != AutomationRuleStatus.paused.value:
                raise _error("status_conflict", "Only a paused rule can be resumed.")
            rule.status = AutomationRuleStatus.published.value
        else:
            if rule.status == AutomationRuleStatus.retired.value:
                active = db.get(AutomationRuleVersion, rule.active_version_id)
                return AutomationRuleOutcome(
                    rule_id=rule.id,
                    version_id=active.id if active else None,
                    version=active.version if active else None,
                    status=AutomationRuleStatus.retired,
                )
            rule.status = AutomationRuleStatus.retired.value
        db.flush()
        active = db.get(AutomationRuleVersion, rule.active_version_id)
        _emit_change(db, rule=rule, version=active, change=command.operation.value)
        return AutomationRuleOutcome(
            rule_id=rule.id,
            version_id=active.id if active else None,
            version=active.version if active else None,
            status=AutomationRuleStatus(rule.status),
        )

    return execute_owner_command(
        db, definition=_CHANGE_STATUS, context=command.context, operation=operation
    )


def list_rules(
    db: Session, query: ListAutomationRulesQuery
) -> tuple[AutomationRule, ...]:
    statement = select(AutomationRule).where(
        AutomationRule.tenant_id == query.tenant_id
    )
    if not query.include_retired:
        statement = statement.where(
            AutomationRule.status != AutomationRuleStatus.retired.value
        )
    return tuple(db.scalars(statement.order_by(AutomationRule.name, AutomationRule.id)))


def get_rule(db: Session, *, tenant_id: UUID, rule_id: UUID) -> AutomationRule:
    return _rule(db, tenant_id=tenant_id, rule_id=rule_id, lock=False)
