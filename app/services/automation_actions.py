"""Static runtime adapters from automation actions to typed command owners."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from sqlalchemy.orm import Session

from app.services import automation_capabilities
from app.services.owner_commands import CommandContext


class AutomationActionDisposition(StrEnum):
    succeeded = "succeeded"
    skipped = "skipped"


@dataclass(frozen=True, slots=True)
class AutomationTargetReference:
    entity_type: str
    entity_id: UUID


@dataclass(frozen=True, slots=True)
class AutomationActionInputValue:
    key: str
    value: object


@dataclass(frozen=True, slots=True)
class ExecuteAutomationActionCommand:
    tenant_id: UUID
    event_id: UUID
    rule_id: UUID
    rule_version_id: UUID
    step_index: int
    target: AutomationTargetReference
    inputs: tuple[AutomationActionInputValue, ...]
    context: CommandContext


@dataclass(frozen=True, slots=True)
class AutomationActionOutcome:
    disposition: AutomationActionDisposition
    outcome_code: str


AutomationActionExecutor = Callable[
    [Session, ExecuteAutomationActionCommand], AutomationActionOutcome
]

# Module-adapter PRs add exact key -> typed adapter entries here. The immutable
# mapping prevents runtime registration from turning a configuration change
# into executable code admission.
_ACTION_EXECUTORS: Mapping[str, AutomationActionExecutor] = MappingProxyType({})


class AutomationActionExecutorError(ValueError):
    pass


def action_executor(action_key: str) -> AutomationActionExecutor:
    executor = _ACTION_EXECUTORS.get(action_key)
    if executor is None:
        raise AutomationActionExecutorError(
            f"Automation action {action_key!r} has no runtime executor."
        )
    return executor


def runtime_registry_errors() -> tuple[str, ...]:
    declared = {
        action.key
        for module in automation_capabilities.registered_module_manifests()
        for action in module.actions
    }
    executable = set(_ACTION_EXECUTORS)
    errors = [
        *(
            f"declared action {key!r} has no executor"
            for key in sorted(declared - executable)
        ),
        *(
            f"executor {key!r} has no capability declaration"
            for key in sorted(executable - declared)
        ),
    ]
    return tuple(errors)


def require_valid_runtime_registry() -> None:
    errors = runtime_registry_errors()
    if errors:
        raise AutomationActionExecutorError("; ".join(errors))


__all__ = [
    "AutomationActionDisposition",
    "AutomationActionExecutorError",
    "AutomationActionInputValue",
    "AutomationActionOutcome",
    "AutomationTargetReference",
    "ExecuteAutomationActionCommand",
    "action_executor",
    "require_valid_runtime_registry",
    "runtime_registry_errors",
]
