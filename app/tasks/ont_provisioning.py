"""Celery tasks for ONT provisioning."""

import logging
from typing import Any
from uuid import UUID

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.network_operation_dispatch import managed_network_operation_dispatch
from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ont_provisioning.authorize_ont")
@managed_network_operation_dispatch("app.tasks.ont_provisioning.authorize_ont")
def authorize_ont(
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool = False,
    preset_id: str | None = None,
    scoped_ont_id: str | None = None,
    initiated_by: str | None = None,
    operation_id: str | None = None,
    _network_dispatch_id: str | None = None,
) -> dict[str, Any]:
    """Authorize an ONT outside the web request timeout path."""
    if not _network_dispatch_id:
        # Pre-cutover envelopes are converted to durable commands; they never
        # enter device code without an outbox execution claim.
        with db_session_adapter.session() as db:
            from app.services.domain_errors import DomainError
            from app.services.network.ont_authorization_contracts import (
                RequestAssignedOntAuthorization,
            )
            from app.services.network.ont_provisioning_commands import (
                request_ont_authorization,
            )

            try:
                request_command = RequestAssignedOntAuthorization.from_transport(
                    context=CommandContext.system(
                        actor=initiated_by or "system",
                        scope="network:ont:authorize",
                        reason="rehome pre-cutover assigned authorization envelope",
                    ),
                    ont_id=scoped_ont_id or "",
                    olt_id=olt_id,
                    fsp=fsp,
                    serial_number=serial_number,
                    force_reauthorize=force_reauthorize,
                    preset_id=preset_id,
                )
            except DomainError as exc:
                return {
                    "success": False,
                    "waiting": False,
                    "message": exc.message,
                    "operation_id": None,
                    "dispatch_id": None,
                    "duplicate": False,
                    "legacy_envelope_rehomed": False,
                }
            command = request_ont_authorization(db, request_command)
            return {
                "success": command.accepted,
                "waiting": command.waiting,
                "message": command.message,
                "operation_id": (
                    str(command.operation_id)
                    if command.operation_id is not None
                    else None
                ),
                "dispatch_id": (
                    str(command.dispatch_id)
                    if command.dispatch_id is not None
                    else None
                ),
                "duplicate": command.duplicate,
                "legacy_envelope_rehomed": True,
            }
    if not operation_id:
        raise ValueError("Tracked assigned ONT authorization operation is required.")

    if not scoped_ont_id:
        # Admitted before the assigned-authorization contract existed, so it
        # carries no ONT and can never satisfy it. Terminalize through the
        # owner instead of raising: an unhandled raise here leaves the
        # operation non-terminal forever, looking queued rather than dead.
        with db_session_adapter.session() as db:
            from app.services.network.ont_provisioning_execution import (
                fail_unexecutable_authorization,
            )

            try:
                unexecutable_id = UUID(operation_id)
            except ValueError:
                raise ValueError(
                    "Tracked assigned ONT authorization operation must be a UUID."
                ) from None
            outcome = fail_unexecutable_authorization(
                db,
                operation_id=unexecutable_id,
                message=(
                    "Authorize & provision requires an assigned ONT. This "
                    "operation predates that requirement and cannot be "
                    "executed; reissue it as Authorize & provision on the "
                    "assigned device, or use Commission ONT."
                ),
            )
            return outcome.to_dict()

    with db_session_adapter.session() as db:
        try:
            from app.services.network.ont_authorization_contracts import (
                ExecuteAssignedOntAuthorization,
            )
            from app.services.network.ont_provisioning_execution import (
                execute_ont_authorization,
            )

            operation_uuid = UUID(operation_id)
            outcome = execute_ont_authorization(
                db,
                ExecuteAssignedOntAuthorization.from_transport(
                    context=CommandContext.system(
                        actor=initiated_by or "system",
                        scope="network:ont:authorize",
                        reason="execute durable assigned ONT authorization",
                        command_id=operation_uuid,
                        correlation_id=operation_uuid,
                    ),
                    operation_id=operation_uuid,
                    ont_id=scoped_ont_id,
                    olt_id=olt_id,
                    fsp=fsp,
                    serial_number=serial_number,
                    force_reauthorize=force_reauthorize,
                    preset_id=preset_id,
                ),
            )
            return outcome.to_dict()
        except Exception:
            logger.exception(
                "Background ONT authorization failed olt_id=%s fsp=%s serial=%s",
                olt_id,
                fsp,
                serial_number,
            )
            db.rollback()
            raise


@celery_app.task(name="app.tasks.ont_provisioning.provision_ont")
@managed_network_operation_dispatch("app.tasks.ont_provisioning.provision_ont")
def provision_ont(
    ont_id: str,
    *,
    dry_run: bool = False,
    initiated_by: str | None = None,
    correlation_key: str | None = None,
    bulk_run_id: str | None = None,
    bulk_item_id: str | None = None,
    allow_low_optical_margin: bool = False,
    wait_for_acs: bool = True,
    apply_acs_config: bool = True,
    operation_id: str | None = None,
    _network_dispatch_id: str | None = None,
) -> dict[str, Any]:
    """Repair/re-apply OLT authorization baseline for one ONT.

    Normal authorization applies this baseline automatically. The ACS flags are
    retained for backward-compatible task payloads and intentionally ignored.
    """
    del wait_for_acs, apply_acs_config
    effective_correlation = correlation_key or f"provision:{ont_id}"
    if not dry_run and not _network_dispatch_id:
        with db_session_adapter.session() as db:
            from app.services.network.ont_provisioning_commands import (
                request_ont_provisioning,
            )

            command = request_ont_provisioning(
                db,
                ont_id,
                initiated_by=initiated_by,
                correlation_key=effective_correlation,
                bulk_run_id=bulk_run_id,
                bulk_item_id=bulk_item_id,
                allow_low_optical_margin=allow_low_optical_margin,
            )
            return {
                "success": command.accepted,
                "waiting": command.waiting,
                "message": command.message,
                "ont_id": ont_id,
                "operation_id": command.operation_id,
                "dispatch_id": command.dispatch_id,
                "duplicate": command.duplicate,
                "legacy_envelope_rehomed": True,
            }
    if not dry_run and not operation_id:
        raise ValueError("Tracked provisioning operation is required.")
    with db_session_adapter.session() as db:
        try:
            from app.services.network.ont_provisioning_execution import (
                execute_ont_provisioning,
                execute_ont_provisioning_command,
            )

            if dry_run:
                return execute_ont_provisioning(
                    db,
                    ont_id=ont_id,
                    dry_run=True,
                    initiated_by=initiated_by,
                    correlation_key=effective_correlation,
                    bulk_run_id=bulk_run_id,
                    bulk_item_id=bulk_item_id,
                    allow_low_optical_margin=allow_low_optical_margin,
                    operation_id=None,
                )
            if not operation_id:
                raise ValueError("Tracked provisioning operation is required.")
            return execute_ont_provisioning_command(
                db,
                ont_id=ont_id,
                initiated_by=initiated_by,
                correlation_key=effective_correlation,
                bulk_run_id=bulk_run_id,
                bulk_item_id=bulk_item_id,
                allow_low_optical_margin=allow_low_optical_margin,
                operation_id=operation_id,
            )
        except Exception:
            logger.exception("ONT provisioning task failed for %s", ont_id)
            db.rollback()
            raise


@celery_app.task(name="app.tasks.ont_provisioning.queue_bulk_provisioning")
def queue_bulk_provisioning(
    ont_ids: list[str],
    *,
    dry_run: bool = False,
    initiated_by: str | None = None,
    max_parallel: int = 10,
    chunk_delay_seconds: int = 15,
    bulk_run_id: str | None = None,
    allow_low_optical_margin: bool = False,
    wait_for_acs: bool = True,
    apply_acs_config: bool = True,
) -> dict[str, Any]:
    """Repair/re-apply OLT authorization baseline for many ONTs synchronously.

    Normal authorization applies this baseline automatically. The ACS flags are
    retained for backward-compatible task payloads and intentionally ignored.
    """
    del wait_for_acs, apply_acs_config, max_parallel, chunk_delay_seconds
    from app.services.network.bulk_provisioning import (
        dispatch_bulk_provisioning_commands,
    )

    with db_session_adapter.session() as db:
        stats = dispatch_bulk_provisioning_commands(
            db,
            ont_ids,
            dry_run=dry_run,
            initiated_by=initiated_by,
            bulk_run_id=bulk_run_id,
            allow_low_optical_margin=allow_low_optical_margin,
        )
    logger.info("Bulk provisioning dispatched: %s", stats)
    return stats
