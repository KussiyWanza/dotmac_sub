"""Celery adapters for the ONT commissioning owner."""

from __future__ import annotations

from datetime import UTC, datetime

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.network_operation_dispatch import managed_network_operation_dispatch
from app.services.owner_commands import CommandContext


@celery_app.task(name="app.tasks.ont_commissioning.commission_ont")
@managed_network_operation_dispatch("app.tasks.ont_commissioning.commission_ont")
def commission_ont(
    intent_id: str,
    operation_id: str,
    *,
    _network_dispatch_id: str | None = None,
) -> dict[str, object]:
    """Apply exact authorization and management-only OLT configuration."""

    if not _network_dispatch_id:
        raise ValueError("A durable commissioning dispatch claim is required.")
    with db_session_adapter.session() as db:
        from app.services.network.ont_commissioning import (
            execute_ont_commissioning,
        )

        return execute_ont_commissioning(
            db,
            intent_id=intent_id,
            operation_id=operation_id,
        )


@celery_app.task(name="app.tasks.ont_commissioning.verify_commissioned_ont")
@managed_network_operation_dispatch(
    "app.tasks.ont_commissioning.verify_commissioned_ont"
)
def verify_commissioned_ont(
    intent_id: str,
    operation_id: str,
    attempt: int,
    *,
    _network_dispatch_id: str | None = None,
) -> dict[str, object]:
    """Perform one bounded ACS management-readiness check."""

    if not _network_dispatch_id:
        raise ValueError("A durable commissioning dispatch claim is required.")
    with db_session_adapter.session() as db:
        from app.services.network.ont_commissioning import (
            verify_ont_commissioning,
        )

        return verify_ont_commissioning(
            db,
            intent_id=intent_id,
            operation_id=operation_id,
            attempt=attempt,
        )


@celery_app.task(name="app.tasks.ont_commissioning.cleanup_commissioned_ont")
@managed_network_operation_dispatch(
    "app.tasks.ont_commissioning.cleanup_commissioned_ont"
)
def cleanup_commissioned_ont(
    intent_id: str,
    operation_id: str,
    *,
    _network_dispatch_id: str | None = None,
) -> dict[str, object]:
    """Return one expired, still-unassigned commissioned ONT to inventory."""

    if not _network_dispatch_id:
        raise ValueError("A durable commissioning dispatch claim is required.")
    with db_session_adapter.session() as db:
        from app.services.network.ont_commissioning import (
            cleanup_ont_commissioning,
        )

        return cleanup_ont_commissioning(
            db,
            intent_id=intent_id,
            operation_id=operation_id,
        )


@celery_app.task(name="app.tasks.ont_commissioning.reconcile_intents")
def reconcile_intents() -> dict[str, int]:
    """Reconcile assignment conversion and expiry once per scheduler tick."""

    now = datetime.now(UTC)
    with db_session_adapter.owner_command_session() as db:
        from app.services.network.ont_commissioning import (
            reconcile_ont_commissioning,
        )

        result = reconcile_ont_commissioning(
            db,
            context=CommandContext.system(
                actor="ont_commissioning_reconciler",
                scope="network:ont:commission",
                reason="scheduled commissioning reconciliation",
                idempotency_key=f"ont-commissioning-reconcile:{now:%Y%m%d%H%M}",
            ),
            now=now,
        )
        return {
            "examined": result.examined,
            "assigned": result.assigned,
            "provisioned": result.provisioned,
            "cleanup_staged": result.cleanup_staged,
            "expired_without_device_write": result.expired_without_device_write,
        }
