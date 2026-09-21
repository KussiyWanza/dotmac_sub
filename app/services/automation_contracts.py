"""Immutable contracts exposed by modules to the Automation Center.

These declarations contain no executable callbacks. A module opts into
automation by publishing one closed set of triggers, condition fields and
actions from its source-of-truth domain declaration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AutomationValueType(StrEnum):
    string = "string"
    integer = "integer"
    decimal = "decimal"
    boolean = "boolean"
    date = "date"
    datetime = "datetime"
    uuid = "uuid"
    enum = "enum"


class AutomationOperator(StrEnum):
    equals = "equals"
    not_equals = "not_equals"
    in_values = "in"
    not_in_values = "not_in"
    greater_than = "greater_than"
    greater_than_or_equal = "greater_than_or_equal"
    less_than = "less_than"
    less_than_or_equal = "less_than_or_equal"
    contains = "contains"
    is_empty = "is_empty"
    is_not_empty = "is_not_empty"


@dataclass(frozen=True, slots=True)
class AutomationConditionField:
    key: str
    label: str
    value_type: AutomationValueType
    operators: tuple[AutomationOperator, ...]
    enum_values: tuple[str, ...] = ()
    sensitive: bool = False


@dataclass(frozen=True, slots=True)
class AutomationTriggerCapability:
    key: str
    label: str
    event_type: str
    event_schema_version: int
    entity_type: str
    tenant_id_field: str
    entity_id_field: str
    fields: tuple[AutomationConditionField, ...]
    author_permission: str


@dataclass(frozen=True, slots=True)
class AutomationActionInput:
    key: str
    label: str
    value_type: AutomationValueType
    required: bool = True
    enum_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AutomationActionCapability:
    key: str
    label: str
    entity_type: str
    command_owner: str
    command_name: str
    input_schema_version: int
    inputs: tuple[AutomationActionInput, ...]
    author_permission: str
    runtime_scope: str
    idempotency: str


@dataclass(frozen=True, slots=True)
class LegacyAutomationSurface:
    key: str
    label: str
    owner_service: str
    management_path: str | None
    conflict_scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AutomationDomainCapabilities:
    """Automation contract declared by one canonical SOT domain."""

    target_types: tuple[str, ...] = ()
    triggers: tuple[AutomationTriggerCapability, ...] = ()
    actions: tuple[AutomationActionCapability, ...] = ()
    legacy_surfaces: tuple[LegacyAutomationSurface, ...] = ()
    manifest_version: int = 1


@dataclass(frozen=True, slots=True)
class AutomationModuleManifest:
    """Resolved module record presented to authoring and runtime adapters."""

    module_key: str
    label: str
    owner_domain: str
    registered: bool
    target_types: tuple[str, ...]
    triggers: tuple[AutomationTriggerCapability, ...]
    actions: tuple[AutomationActionCapability, ...]
    legacy_surfaces: tuple[LegacyAutomationSurface, ...]
    manifest_version: int | None
