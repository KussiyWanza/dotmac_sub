"""Durable EventStore adapter for the Automation Center runtime."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.orm import Session

from app.services import automation_actions, automation_capabilities, automation_runtime
from app.services.domain_errors import DomainError
from app.services.events.handlers.owner_session import owner_session
from app.services.events.types import Event, EventType
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import CommandContext


def _registered_triggers():
    return tuple(
        trigger
        for module in automation_capabilities.registered_module_manifests()
        for trigger in module.triggers
    )


HANDLED_EVENT_TYPES = frozenset(
    EventType(trigger.event_type) for trigger in _registered_triggers()
)


class AutomationEventHandlerError(RuntimeError):
    """Keep the durable event retryable when any automation action fails."""


def _path_value(payload: Mapping[str, object], path: str) -> object:
    value: object = payload
    for component in path.split("."):
        if not isinstance(value, Mapping) or component not in value:
            return None
        value = value[component]
    return value


def _required_uuid(payload: Mapping[str, object], path: str) -> UUID:
    value = _path_value(payload, path)
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise AutomationEventHandlerError(
            f"Automation event identity field {path!r} is missing or invalid."
        ) from exc


def _context(
    *, event: Event, operation: str, idempotency_key: str | None = None
) -> CommandContext:
    command_id = uuid5(NAMESPACE_URL, f"dotmac:automation:{event.event_id}:{operation}")
    return CommandContext.system(
        actor="automation-event-handler",
        scope="automation:runtime",
        reason=f"Execute automation for durable event {event.event_type.value}",
        command_id=command_id,
        correlation_id=event.event_id,
        causation_id=event.event_id,
        idempotency_key=idempotency_key,
    )


class AutomationEventHandler:
    """Translate registered events into ordered, receipted module commands."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return
        payload: Mapping[str, object] = event.payload
        triggers = tuple(
            trigger
            for trigger in _registered_triggers()
            if trigger.event_type == event.event_type.value
        )
        for trigger in triggers:
            tenant_id = _required_uuid(payload, trigger.tenant_id_field)
            if tenant_id != OPERATOR_TENANT_ID:
                raise AutomationEventHandlerError(
                    "Automation event tenant does not match the operator tenant."
                )
            target_id = _required_uuid(payload, trigger.entity_id_field)
            envelope = automation_runtime.AutomationEventEnvelope(
                event_id=event.event_id,
                event_type=event.event_type.value,
                trigger_key=trigger.key,
                tenant_id=tenant_id,
                target_type=trigger.entity_type,
                target_id=target_id,
                occurred_at=event.occurred_at,
                payload=payload,
            )
            with owner_session(db) as command_db:
                runs = automation_runtime.prepare_event_runs(
                    command_db,
                    automation_runtime.PrepareAutomationEventCommand(
                        event=envelope,
                        context=_context(
                            event=event, operation=f"prepare:{trigger.key}"
                        ),
                    ),
                )
            for run in runs:
                self._execute_run(db, event=event, run=run, tenant_id=tenant_id)

    def _execute_run(
        self,
        db: Session,
        *,
        event: Event,
        run: automation_runtime.PreparedAutomationRun,
        tenant_id: UUID,
    ) -> None:
        for step in run.steps:
            step_key = f"{run.rule_version_id}:{step.step_index}"
            with owner_session(db) as command_db:
                claim = automation_runtime.claim_step(
                    command_db,
                    automation_runtime.ClaimAutomationStepCommand(
                        tenant_id=tenant_id,
                        step_id=step.step_id,
                        context=_context(event=event, operation=f"claim:{step_key}"),
                    ),
                )
            if (
                claim.disposition
                is automation_runtime.StepClaimDisposition.already_succeeded
            ):
                continue
            if claim.disposition is automation_runtime.StepClaimDisposition.busy:
                raise AutomationEventHandlerError(
                    f"Automation step {step.step_id} is already executing."
                )
            action = automation_capabilities.action_capability(step.action_key)
            executor = automation_actions.action_executor(step.action_key)
            idempotency_key = (
                f"automation:{event.event_id}:{run.rule_version_id}:{step.step_index}"
            )
            succeeded = False
            error_code: str | None = None
            try:
                with owner_session(db) as action_db:
                    executor(
                        action_db,
                        automation_actions.ExecuteAutomationActionCommand(
                            tenant_id=tenant_id,
                            event_id=event.event_id,
                            rule_id=run.rule_id,
                            rule_version_id=run.rule_version_id,
                            step_index=step.step_index,
                            target=run.target,
                            inputs=step.inputs,
                            context=CommandContext.system(
                                actor="automation-runtime",
                                scope=action.runtime_scope,
                                reason=f"Execute declared action {step.action_key}",
                                command_id=uuid5(
                                    NAMESPACE_URL,
                                    f"dotmac:{idempotency_key}",
                                ),
                                correlation_id=event.event_id,
                                causation_id=event.event_id,
                                idempotency_key=idempotency_key,
                            ),
                        ),
                    )
                succeeded = True
            except DomainError as exc:
                error_code = exc.code
            except Exception:
                error_code = "automation.execution.action_failed"
            with owner_session(db) as command_db:
                automation_runtime.finish_step(
                    command_db,
                    automation_runtime.FinishAutomationStepCommand(
                        tenant_id=tenant_id,
                        step_id=step.step_id,
                        succeeded=succeeded,
                        error_code=error_code,
                        context=_context(event=event, operation=f"finish:{step_key}"),
                    ),
                )
            if not succeeded:
                raise AutomationEventHandlerError(
                    f"Automation action {step.action_key!r} failed with {error_code}."
                )


__all__ = [
    "HANDLED_EVENT_TYPES",
    "AutomationEventHandler",
    "AutomationEventHandlerError",
]
