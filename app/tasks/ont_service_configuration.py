"""Durable worker adapter for ONT service-configuration operations."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.network.ont_service_configuration import (
    ExecuteOntServiceConfigurationCommand,
    execute_ont_service_configuration,
)
from app.services.network_operation_dispatch import managed_network_operation_dispatch
from app.services.owner_commands import CommandContext


@celery_app.task(
    name="app.tasks.ont_service_configuration.apply",
    soft_time_limit=150,
    time_limit=180,
)
@managed_network_operation_dispatch("app.tasks.ont_service_configuration.apply")
def apply(
    ont_id: str,
    operation_id: str,
    configuration_head_id: str,
    revision: int,
    *,
    verification_attempt: int = 0,
    explicit_repair: bool = False,
    _network_dispatch_id: str | None = None,
) -> dict[str, Any]:
    """Execute an already-tracked exact assignment/revision command."""

    command_id = UUID(_network_dispatch_id) if _network_dispatch_id else uuid4()
    context = CommandContext.system(
        actor="system:ont_service_configuration_worker",
        scope="network:ont:execute",
        reason=(
            "Explicit current-revision configuration repair"
            if explicit_repair
            else "Durable ONT service configuration delivery"
        ),
        command_id=command_id,
        correlation_id=UUID(operation_id),
        causation_id=command_id,
        idempotency_key=(
            f"ont-service-config-execute:{operation_id}:{verification_attempt}"
        ),
    )
    with db_session_adapter.owner_command_session() as db:
        outcome = execute_ont_service_configuration(
            db,
            ExecuteOntServiceConfigurationCommand(
                context=context,
                ont_unit_id=UUID(ont_id),
                operation_id=UUID(operation_id),
                configuration_head_id=UUID(configuration_head_id),
                revision=int(revision),
                verification_attempt=int(verification_attempt),
                explicit_repair=bool(explicit_repair),
            ),
        )
    return {
        "operation_id": str(outcome.operation_id),
        "phase": outcome.phase.value,
        "executed": outcome.executed,
        "stale": outcome.stale,
        "message": outcome.message,
    }
