"""Network operation tracking service.

Provides lifecycle management for tracked network device operations.
Operations wrap existing service functions to record initiation, progress,
results, and errors for UI visibility, retry support, and audit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.control_plane_intent import (
    ControlPlaneTransitionError,
    assert_phase_transition,
    phase_for_network_operation,
)
from app.services.network.action_logging import looks_like_prerequisite_failure
from app.services.response import ListResponseMixin

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
STALE_WAITING_OPERATION_AGE = timedelta(hours=6)
DEFAULT_STALE_ACTIVE_OPERATION_AGE = timedelta(hours=4)
STALE_ACTIVE_OPERATION_AGE_BY_TYPE: dict[NetworkOperationType, timedelta] = {
    NetworkOperationType.ont_authorize: timedelta(minutes=15),
    NetworkOperationType.ont_provision: timedelta(minutes=30),
    NetworkOperationType.tr069_bootstrap: timedelta(minutes=15),
}

# Statuses that count as "active" for dedup purposes
_ACTIVE_STATUSES = (
    NetworkOperationStatus.pending,
    NetworkOperationStatus.running,
    NetworkOperationStatus.waiting,
)

# Terminal statuses — no further transitions allowed
_TERMINAL_STATUSES = frozenset(
    {
        NetworkOperationStatus.succeeded,
        NetworkOperationStatus.warning,
        NetworkOperationStatus.failed,
        NetworkOperationStatus.canceled,
    }
)

_EXPECTED_WARNING_PATTERNS = (
    "no tr-069 device found in genieacs",
    "no matching genieacs device found",
    "cpe device not found",
)


@dataclass(frozen=True)
class NetworkOperationRedriveMetadata:
    source_id: UUID
    reason: str
    reviewed_head: str
    idempotency_key: str
    retry_count: int
    max_retries: int


def _operation_extra(op: NetworkOperation) -> dict[str, object]:
    extra: dict[str, object] = {
        "event": "network_operation",
        "operation_id": str(op.id),
        "operation_type": op.operation_type.value,
        "target_type": op.target_type.value,
        "target_id": str(op.target_id),
        "operation_status": op.status.value,
        "control_plane_phase": phase_for_network_operation(op.status).value,
        "correlation_key": op.correlation_key,
        "parent_id": str(op.parent_id) if op.parent_id else None,
        "redrive_of_id": str(op.redrive_of_id) if op.redrive_of_id else None,
        "is_redrive": op.redrive_of_id is not None,
        "retry_count": int(op.retry_count or 0),
        "max_retries": int(op.max_retries or 0),
        "initiated_by": op.initiated_by,
    }
    now = datetime.now(UTC)
    if op.created_at:
        extra["operation_age_seconds"] = max(
            0.0,
            (now - _as_aware_utc(op.created_at)).total_seconds(),
        )
    if op.started_at:
        end = _as_aware_utc(op.completed_at) if op.completed_at else now
        extra["run_duration_seconds"] = max(
            0.0,
            (end - _as_aware_utc(op.started_at)).total_seconds(),
        )
    if op.redrive_source and op.redrive_source.completed_at:
        extra["recovery_latency_seconds"] = max(
            0.0,
            (now - _as_aware_utc(op.redrive_source.completed_at)).total_seconds(),
        )
    return extra


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _get_operation(db: Session, operation_id: str) -> NetworkOperation:
    """Fetch an operation by ID or raise 404."""
    op = db.get(NetworkOperation, operation_id)
    if not op:
        raise HTTPException(status_code=404, detail="Operation not found")
    return op


def _get_active_operation_by_correlation(
    db: Session, correlation_key: str | None
) -> NetworkOperation | None:
    """Return the active operation for a dedup key, if one exists."""
    if not correlation_key:
        return None
    stmt = select(NetworkOperation).where(
        NetworkOperation.correlation_key == correlation_key,
        NetworkOperation.status.in_(_ACTIVE_STATUSES),
    )
    return db.scalars(stmt).first()


def _operation_started_reference(op: NetworkOperation) -> datetime:
    return _as_aware_utc(op.started_at or op.created_at)


def _stale_active_age_for_operation(op: NetworkOperation) -> timedelta:
    operation_type = op.operation_type
    if isinstance(operation_type, NetworkOperationType):
        return STALE_ACTIVE_OPERATION_AGE_BY_TYPE.get(
            operation_type, DEFAULT_STALE_ACTIVE_OPERATION_AGE
        )
    return DEFAULT_STALE_ACTIVE_OPERATION_AGE


def _operation_is_stale_active(
    op: NetworkOperation, *, now: datetime | None = None
) -> bool:
    if op.status not in _ACTIVE_STATUSES:
        return False
    current = now or datetime.now(UTC)
    return current - _operation_started_reference(op) > _stale_active_age_for_operation(
        op
    )


def _mark_operation_stale_failed(
    op: NetworkOperation,
    *,
    now: datetime | None = None,
    reason: str | None = None,
) -> None:
    current = now or datetime.now(UTC)
    op.status = NetworkOperationStatus.failed
    op.completed_at = current
    op.waiting_reason = None
    op.error = reason or (
        "Operation timed out with no active worker task "
        f"after {_stale_active_age_for_operation(op)}."
    )


def _check_not_terminal(op: NetworkOperation) -> None:
    """Reject transitions from terminal statuses."""
    if op.status in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot transition from terminal status '{op.status.value}'",
        )


def _transition_status(
    op: NetworkOperation, destination: NetworkOperationStatus
) -> None:
    """Apply a native transition only when its shared lifecycle is valid."""
    _check_not_terminal(op)
    try:
        assert_phase_transition(
            phase_for_network_operation(op.status),
            phase_for_network_operation(destination),
        )
    except ControlPlaneTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    op.status = destination


class NetworkOperations(ListResponseMixin):
    """Manager for network operation lifecycle tracking."""

    @staticmethod
    def start(
        db: Session,
        operation_type: NetworkOperationType,
        target_type: NetworkOperationTargetType,
        target_id: str,
        *,
        correlation_key: str | None = None,
        input_payload: dict[str, Any] | None = None,
        parent_id: str | None = None,
        initiated_by: str | None = None,
        redrive: NetworkOperationRedriveMetadata | None = None,
    ) -> NetworkOperation:
        """Create a new operation in pending status.

        Args:
            db: Database session.
            operation_type: The type of network operation.
            target_type: The target device type (olt/ont/cpe).
            target_id: The UUID of the target device.
            correlation_key: Optional dedup key; rejects if an active op exists.
            input_payload: Request parameters to record.
            parent_id: Parent operation UUID for composable workflows.
            initiated_by: Who triggered this (username or "system").
            redrive: Ledger-owned recovery metadata. Routes and tasks must use
                ``start_redrive`` rather than constructing this directly.

        Returns:
            The created NetworkOperation record.

        Raises:
            HTTPException: 409 if an active operation with the same
                correlation_key already exists.
        """
        existing = _get_active_operation_by_correlation(db, correlation_key)
        if existing:
            if _operation_is_stale_active(existing):
                _mark_operation_stale_failed(
                    existing,
                    reason=(
                        "Expired stale active operation before starting a new request."
                    ),
                )
                db.flush()
            elif (
                existing.status == NetworkOperationStatus.waiting
                and existing.created_at
                and datetime.now(UTC) - _as_aware_utc(existing.created_at)
                > STALE_WAITING_OPERATION_AGE
            ):
                _mark_operation_stale_failed(
                    existing,
                    reason=(
                        "Expired stale waiting operation before starting a new request."
                    ),
                )
                db.flush()
            else:
                logger.warning(
                    "Duplicate operation blocked: %s (existing=%s)",
                    correlation_key,
                    existing.id,
                )
                raise HTTPException(
                    status_code=409,
                    detail="Operation already in progress",
                )
        existing = _get_active_operation_by_correlation(db, correlation_key)
        if existing:
            logger.warning(
                "Duplicate operation blocked: %s (existing=%s)",
                correlation_key,
                existing.id,
            )
            raise HTTPException(
                status_code=409,
                detail="Operation already in progress",
            )

        op = NetworkOperation(
            operation_type=operation_type,
            target_type=target_type,
            target_id=target_id,
            parent_id=parent_id,
            status=NetworkOperationStatus.pending,
            correlation_key=correlation_key,
            input_payload=input_payload,
            initiated_by=initiated_by,
            redrive_of_id=redrive.source_id if redrive else None,
            redrive_reason=redrive.reason if redrive else None,
            redrive_reviewed_head=redrive.reviewed_head if redrive else None,
            redrive_idempotency_key=redrive.idempotency_key if redrive else None,
            retry_count=redrive.retry_count if redrive else 0,
            max_retries=redrive.max_retries if redrive else 3,
        )
        try:
            from app.services.owner_commands import (
                execute_owner_savepoint,
                owner_command_active,
            )

            if owner_command_active(db):

                def _persist_operation() -> None:
                    db.add(op)
                    db.flush()

                execute_owner_savepoint(
                    db,
                    _persist_operation,
                )
            else:
                db.add(op)
                db.flush()
        except IntegrityError as e:
            if not owner_command_active(db):
                db.rollback()
            if "uq_netops_active_correlation" in str(e):
                logger.warning(
                    "Concurrent duplicate blocked by DB constraint: %s",
                    correlation_key,
                )
                raise HTTPException(
                    status_code=409,
                    detail="Operation already in progress",
                ) from e
            raise
        db.refresh(op)
        logger.info(
            "Operation started: type=%s target=%s:%s id=%s",
            operation_type.value,
            target_type.value,
            target_id,
            op.id,
            extra=_operation_extra(op),
        )
        return op

    @staticmethod
    def start_redrive(
        db: Session,
        source: NetworkOperation,
        *,
        correlation_key: str,
        input_payload: dict[str, Any],
        reason: str,
        reviewed_head: str,
        idempotency_key: str,
        initiated_by: str,
    ) -> tuple[NetworkOperation, bool]:
        """Create or replay one immutable, linked operation attempt.

        The caller must lock and review ``source`` before invoking this method.
        The composite idempotency key is scoped to that source operation, so a
        browser retry returns the already-created attempt without redispatching
        a second side effect.
        """
        existing = db.scalars(
            select(NetworkOperation).where(
                NetworkOperation.redrive_of_id == source.id,
                NetworkOperation.redrive_idempotency_key == idempotency_key,
            )
        ).first()
        if existing is not None:
            return existing, True

        retry_count = int(source.retry_count or 0) + 1
        max_retries = int(source.max_retries or 0)
        if retry_count > max_retries:
            raise HTTPException(
                status_code=409,
                detail="Operation retry limit has been reached",
            )

        operation = NetworkOperations.start(
            db,
            source.operation_type,
            source.target_type,
            str(source.target_id),
            correlation_key=correlation_key,
            input_payload=input_payload,
            initiated_by=initiated_by,
            redrive=NetworkOperationRedriveMetadata(
                source_id=source.id,
                reason=reason,
                reviewed_head=reviewed_head,
                idempotency_key=idempotency_key,
                retry_count=retry_count,
                max_retries=max_retries,
            ),
        )
        logger.info(
            "Network operation redrive created",
            extra={**_operation_extra(operation), "event": "network_operation_redrive"},
        )
        return operation, False

    @staticmethod
    def mark_running(db: Session, operation_id: str) -> NetworkOperation:
        """Transition operation to running status."""
        op = _get_operation(db, operation_id)
        first_start = op.started_at is None
        _transition_status(op, NetworkOperationStatus.running)
        if first_start:
            op.started_at = datetime.now(UTC)
        op.waiting_reason = None
        db.flush()
        extra = _operation_extra(op)
        if first_start and op.created_at and op.started_at:
            extra["queue_delay_seconds"] = max(
                0.0,
                (
                    _as_aware_utc(op.started_at) - _as_aware_utc(op.created_at)
                ).total_seconds(),
            )
        logger.info("Operation running", extra=extra)
        return op

    @staticmethod
    def merge_output_payload(
        db: Session,
        operation_id: str,
        fragment: dict[str, Any],
    ) -> NetworkOperation:
        """Merge durable evidence into an operation's output payload.

        The ledger owns its own rows. Executors use this to record a fact that
        must survive later failures (for example "the OLT accepted the write")
        without waiting for, or being overwritten by, the terminal payload.
        """
        op = _get_operation(db, operation_id)
        existing = op.output_payload if isinstance(op.output_payload, dict) else {}
        op.output_payload = {**existing, **fragment}
        db.flush()
        logger.info(
            "Operation output payload merged",
            extra={**_operation_extra(op), "output_payload_keys": sorted(fragment)},
        )
        return op

    @staticmethod
    def mark_succeeded(
        db: Session,
        operation_id: str,
        *,
        output_payload: dict[str, Any] | None = None,
    ) -> NetworkOperation:
        """Transition operation to succeeded status."""
        op = _get_operation(db, operation_id)
        _transition_status(op, NetworkOperationStatus.succeeded)
        op.completed_at = datetime.now(UTC)
        if output_payload is not None:
            op.output_payload = output_payload
        db.flush()
        extra = _operation_extra(op)
        extra["output_payload"] = output_payload
        logger.info(
            "Operation %s succeeded on %s %s",
            op.operation_type.value,
            op.target_type.value,
            op.target_id,
            extra=extra,
        )
        return op

    @staticmethod
    def mark_failed(
        db: Session,
        operation_id: str,
        error: str,
        *,
        output_payload: dict[str, Any] | None = None,
    ) -> NetworkOperation:
        """Transition operation to failed status."""
        op = _get_operation(db, operation_id)
        _transition_status(op, NetworkOperationStatus.failed)
        op.completed_at = datetime.now(UTC)
        op.error = error
        if output_payload is not None:
            op.output_payload = output_payload
        db.flush()
        error_text = str(error).strip().lower()
        log = (
            logger.warning
            if any(pattern in error_text for pattern in _EXPECTED_WARNING_PATTERNS)
            else logger.error
        )
        extra = _operation_extra(op)
        extra["error"] = error
        extra["output_payload"] = output_payload
        # Include key details in log message for text-based log viewers
        error_preview = str(error)[:100] + ("..." if len(str(error)) > 100 else "")
        log(
            "Operation %s failed on %s %s: %s",
            op.operation_type.value,
            op.target_type.value,
            op.target_id,
            error_preview,
            extra=extra,
        )
        if looks_like_prerequisite_failure(str(error)):
            prereq_extra = dict(extra)
            prereq_extra["event"] = "network_action_prerequisite_blocked"
            prereq_extra["network_resource_type"] = op.target_type.value
            prereq_extra["network_resource_id"] = str(op.target_id)
            prereq_extra["network_action"] = op.operation_type.value
            prereq_extra["reason"] = error
            logger.error(
                "Network action blocked by missing prerequisite: resource=%s resource_id=%s action=%s reason=%s",
                op.target_type.value,
                op.target_id,
                op.operation_type.value,
                error_preview,
                extra=prereq_extra,
            )
        return op

    @staticmethod
    def mark_warning(
        db: Session,
        operation_id: str,
        warning: str,
        *,
        output_payload: dict[str, Any] | None = None,
    ) -> NetworkOperation:
        """Transition operation to warning status for degraded success."""
        op = _get_operation(db, operation_id)
        _transition_status(op, NetworkOperationStatus.warning)
        op.completed_at = datetime.now(UTC)
        op.error = warning
        if output_payload is not None:
            op.output_payload = output_payload
        db.flush()
        extra = _operation_extra(op)
        extra["warning"] = warning
        extra["output_payload"] = output_payload
        logger.warning(
            "Operation %s completed with warning on %s %s: %s",
            op.operation_type.value,
            op.target_type.value,
            op.target_id,
            str(warning)[:100],
            extra=extra,
        )
        return op

    @staticmethod
    def mark_waiting(
        db: Session,
        operation_id: str,
        reason: str,
    ) -> NetworkOperation:
        """Transition operation to waiting status."""
        op = _get_operation(db, operation_id)
        _transition_status(op, NetworkOperationStatus.waiting)
        op.waiting_reason = reason
        db.flush()
        extra = _operation_extra(op)
        extra["waiting_reason"] = reason
        logger.info("Operation waiting", extra=extra)
        return op

    @staticmethod
    def mark_canceled(db: Session, operation_id: str) -> NetworkOperation:
        """Transition operation to canceled status."""
        op = _get_operation(db, operation_id)
        _transition_status(op, NetworkOperationStatus.canceled)
        op.completed_at = datetime.now(UTC)
        db.flush()
        logger.info(
            "Operation %s canceled on %s %s",
            op.operation_type.value,
            op.target_type.value,
            op.target_id,
            extra=_operation_extra(op),
        )
        return op

    @staticmethod
    def get(db: Session, operation_id: str) -> NetworkOperation:
        """Fetch a single operation by ID."""
        return _get_operation(db, operation_id)

    @staticmethod
    def list_for_device(
        db: Session,
        target_type: NetworkOperationTargetType,
        target_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[NetworkOperation]:
        """List operations for a specific device, newest first.

        Args:
            db: Database session.
            target_type: Device type enum value.
            target_id: Device UUID string.
            limit: Maximum number of records.
            offset: Pagination offset.

        Returns:
            List of NetworkOperation records ordered by created_at DESC.
        """
        NetworkOperations.mark_stale_for_device(db, target_type, target_id)
        stmt = (
            select(NetworkOperation)
            .where(
                NetworkOperation.target_type == target_type,
                NetworkOperation.target_id == target_id,
                NetworkOperation.parent_id.is_(None),  # Top-level only
            )
            .order_by(NetworkOperation.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(db.scalars(stmt).all())

    @staticmethod
    def mark_stale_for_device(
        db: Session,
        target_type: NetworkOperationTargetType,
        target_id: str,
    ) -> int:
        """Mark timed-out active operations for a device as failed.

        This is intentionally cheap and DB-only. It lets status pages stop
        showing dead Celery work as running without pinging workers on every
        page render.
        """
        stmt = select(NetworkOperation).where(
            NetworkOperation.target_type == target_type,
            NetworkOperation.target_id == target_id,
            NetworkOperation.status.in_(_ACTIVE_STATUSES),
        )
        now = datetime.now(UTC)
        marked = 0
        for op in db.scalars(stmt).all():
            if not _operation_is_stale_active(op, now=now):
                continue
            _mark_operation_stale_failed(op, now=now)
            marked += 1
        if marked:
            db.flush()
        return marked

    @staticmethod
    def update_parent_status(db: Session, parent_id: str) -> NetworkOperation:
        """Derive and update a parent operation's status from its children.

        Status derivation rules:
        - Any child running -> parent running
        - Any child failed and none running -> parent failed
        - Any child pending and none running/failed -> parent pending
        - Any child waiting and none running/failed/pending -> parent waiting
        - Any child warning and none running/failed/pending/waiting -> parent warning
        - All children succeeded -> parent succeeded
        """
        parent = db.get(NetworkOperation, parent_id)
        if not parent:
            raise HTTPException(status_code=404, detail="Parent operation not found")

        children_stmt = select(NetworkOperation).where(
            NetworkOperation.parent_id == parent_id
        )
        children = list(db.scalars(children_stmt).all())
        if not children:
            logger.warning(
                "update_parent_status called for %s but it has no children",
                parent_id,
            )
            return parent

        statuses = {c.status for c in children}

        if NetworkOperationStatus.running in statuses:
            derived = NetworkOperationStatus.running
        elif NetworkOperationStatus.failed in statuses:
            derived = NetworkOperationStatus.failed
        elif NetworkOperationStatus.pending in statuses:
            derived = NetworkOperationStatus.pending
        elif NetworkOperationStatus.waiting in statuses:
            derived = NetworkOperationStatus.waiting
        elif NetworkOperationStatus.warning in statuses:
            derived = NetworkOperationStatus.warning
        else:
            derived = NetworkOperationStatus.succeeded

        # Intentionally bypasses _check_not_terminal: parent status is
        # always derived from children and must be re-derivable as children
        # complete, even if the parent was previously marked terminal.
        parent.status = derived
        if derived == NetworkOperationStatus.failed:
            failed_children = [
                child
                for child in children
                if child.status == NetworkOperationStatus.failed
            ]
            failed_child = max(
                failed_children,
                key=lambda child: child.completed_at or child.created_at,
            )
            child_payload = failed_child.output_payload or {}
            parent.error = str(
                failed_child.error
                or child_payload.get("message")
                or f"Child operation {failed_child.id} failed without an error message."
            )
        elif derived == NetworkOperationStatus.warning:
            warning_child = next(
                child
                for child in children
                if child.status == NetworkOperationStatus.warning
            )
            parent.error = warning_child.error
        else:
            parent.error = None
        if derived == NetworkOperationStatus.waiting:
            waiting_child = next(
                child
                for child in children
                if child.status == NetworkOperationStatus.waiting
            )
            parent.waiting_reason = (
                parent.waiting_reason or waiting_child.waiting_reason
            )
        else:
            parent.waiting_reason = None
        if derived in (
            NetworkOperationStatus.succeeded,
            NetworkOperationStatus.warning,
            NetworkOperationStatus.failed,
        ):
            parent.completed_at = datetime.now(UTC)
        else:
            parent.completed_at = None
        db.flush()
        return parent

    @staticmethod
    def health_snapshot(
        db: Session,
        *,
        now: datetime | None = None,
        window: timedelta = timedelta(hours=24),
    ) -> dict[str, float]:
        """Return a bounded aggregate for scheduled observability export."""
        current = now or datetime.now(UTC)
        cutoff = current - window
        snapshot: dict[str, float] = {"window_seconds": window.total_seconds()}
        for status in NetworkOperationStatus:
            snapshot[f"operations_{status.value}"] = 0.0
            snapshot[f"redrives_{status.value}"] = 0.0

        status_rows = db.execute(
            select(NetworkOperation.status, func.count(NetworkOperation.id))
            .where(NetworkOperation.created_at >= cutoff)
            .group_by(NetworkOperation.status)
        ).all()
        for status, count in status_rows:
            value = (
                status.value if isinstance(status, NetworkOperationStatus) else status
            )
            snapshot[f"operations_{value}"] = float(count)

        redrive_rows = db.execute(
            select(NetworkOperation.status, func.count(NetworkOperation.id))
            .where(
                NetworkOperation.created_at >= cutoff,
                NetworkOperation.redrive_of_id.is_not(None),
            )
            .group_by(NetworkOperation.status)
        ).all()
        for status, count in redrive_rows:
            value = (
                status.value if isinstance(status, NetworkOperationStatus) else status
            )
            snapshot[f"redrives_{value}"] = float(count)

        oldest_active = db.scalar(
            select(func.min(NetworkOperation.created_at)).where(
                NetworkOperation.status.in_(_ACTIVE_STATUSES)
            )
        )
        snapshot["active_oldest_age_seconds"] = (
            max(0.0, (current - _as_aware_utc(oldest_active)).total_seconds())
            if oldest_active
            else 0.0
        )
        snapshot["active"] = float(
            db.scalar(
                select(func.count(NetworkOperation.id)).where(
                    NetworkOperation.status.in_(_ACTIVE_STATUSES)
                )
            )
            or 0
        )
        return snapshot


network_operations = NetworkOperations()


@contextmanager
def tracked_operation(
    db: Session,
    operation_type: NetworkOperationType,
    target_type: NetworkOperationTargetType,
    target_id: str,
    **kwargs: Any,
) -> Generator[NetworkOperation, None, None]:
    """Context manager that creates, runs, and finalizes a NetworkOperation.

    Usage::

        with tracked_operation(db, NetworkOperationType.ont_reboot,
                               NetworkOperationTargetType.ont, ont_id) as op:
            result = existing_reboot_function(db, ont_id)
            op.output_payload = result.data

    On normal exit the operation is marked succeeded. On exception it is
    marked failed with the exception message, then the exception is re-raised.
    If the session is in an error state when recording the failure, the context
    manager will rollback the session and retry. Callers should be aware that
    uncommitted work in the session may be lost on exception.
    """
    op = network_operations.start(db, operation_type, target_type, target_id, **kwargs)
    network_operations.mark_running(db, str(op.id))
    db.flush()
    try:
        yield op
        network_operations.mark_succeeded(db, str(op.id))
        db.flush()
    except Exception as exc:
        try:
            # Try recording failure directly — works for most exceptions.
            # If the session is corrupted (e.g., IntegrityError), rollback
            # first and retry.
            try:
                network_operations.mark_failed(db, str(op.id), str(exc))
                db.flush()
            except Exception:
                db.rollback()
                network_operations.mark_failed(db, str(op.id), str(exc))
                db.flush()
        except Exception as record_err:
            logger.error(
                "Failed to record operation failure for %s: %s (original error: %s)",
                op.id,
                record_err,
                exc,
            )
            try:
                db.rollback()
            except Exception as rollback_err:
                logger.debug("Rollback also failed: %s", rollback_err)
        raise


def run_tracked_action(
    db: Session,
    operation_type: NetworkOperationType,
    target_type: NetworkOperationTargetType,
    target_id: str,
    action_fn: Callable[[], Any],
    *,
    correlation_key: str | None = None,
    initiated_by: str | None = None,
) -> Any:
    """Run a network action with operation tracking.

    Creates a NetworkOperation, executes the action function, and records
    the outcome. Designed for functions that return an ``ActionResult``
    (with ``.success``, ``.message``, and optional ``.data``).

    If a 409 conflict occurs (duplicate active operation), returns an
    ``ActionResult`` with ``success=False`` instead of raising.

    Args:
        db: Database session.
        operation_type: The operation type enum.
        target_type: The target device type enum.
        target_id: Target device UUID string.
        action_fn: Zero-argument callable that executes the action.
        correlation_key: Optional dedup key.
        initiated_by: Who triggered this operation.

    Returns:
        The return value of ``action_fn()``, or an ``ActionResult`` on 409.
    """
    from app.services.network.ont_action_common import ActionResult

    try:
        op = network_operations.start(
            db,
            operation_type,
            target_type,
            target_id,
            correlation_key=correlation_key,
            initiated_by=initiated_by,
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            existing = _get_active_operation_by_correlation(db, correlation_key)
            if existing and existing.status == NetworkOperationStatus.waiting:
                waiting_reason = existing.waiting_reason or "next_inform"
                return ActionResult(
                    success=False,
                    message="This operation is already waiting for the ONT to inform ACS.",
                    data={
                        "operation_id": str(existing.id),
                        "waiting_reason": waiting_reason,
                    },
                    waiting=True,
                )
            return ActionResult(
                success=False,
                message="This operation is already in progress.",
                data={
                    "operation_id": str(existing.id) if existing else None,
                    "conflict": True,
                },
            )
        raise
    network_operations.mark_running(db, str(op.id))
    db.flush()

    try:
        result = action_fn()
        result_data = dict(getattr(result, "data", None) or {})
        result_data.setdefault("operation_id", str(op.id))
        result.data = result_data
        try:
            if getattr(result, "waiting", False):
                waiting_reason = (getattr(result, "data", None) or {}).get(
                    "waiting_reason"
                ) or "next_inform"
                network_operations.mark_waiting(db, str(op.id), str(waiting_reason))
            elif getattr(result, "success", False):
                network_operations.mark_succeeded(
                    db, str(op.id), output_payload=result_data
                )
            else:
                network_operations.mark_failed(
                    db, str(op.id), getattr(result, "message", "Unknown error")
                )
        except Exception as track_err:
            logger.error(
                "Failed to record operation outcome for %s: %s", op.id, track_err
            )
        return result
    except Exception as exc:
        try:
            network_operations.mark_failed(db, str(op.id), str(exc))
        except Exception as track_err:
            logger.error(
                "Failed to record operation failure for %s: %s (original: %s)",
                op.id,
                track_err,
                exc,
            )
            try:
                db.rollback()
            except Exception as rb_err:
                logger.debug("Rollback also failed: %s", rb_err)
        raise


def commit_tracked_action(db: Session) -> None:
    """Commit a completed synchronous operation and its terminal evidence."""
    db.commit()
