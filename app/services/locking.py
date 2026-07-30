"""Pessimistic locking utilities for database concurrency control.

Provides consistent helpers for acquiring row-level locks to prevent
concurrent modifications. Use these when:
- Multiple processes might modify the same row simultaneously
- You need read-modify-write consistency
- Eventual consistency (optimistic locking) is not acceptable

Usage:
    from app.services.locking import lock_for_update, lock_multiple

    # Lock a single entity:
    subscriber = lock_for_update(db, Subscriber, subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="Not found")
    subscriber.balance += amount
    db.commit()

    # Lock multiple entities (sorted to prevent deadlocks):
    invoices = lock_multiple(db, Invoice, invoice_ids)
    for invoice in invoices:
        invoice.status = InvoiceStatus.paid
    db.commit()

    # Non-blocking lock (fails immediately if locked):
    from app.services.unit_of_work import ConcurrencyConflict
    try:
        subscriber = lock_for_update(db, Subscriber, subscriber_id, nowait=True)
    except ConcurrencyConflict:
        # Handle the conflict (retry, return error, etc.)
        pass
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.services.unit_of_work import ConcurrencyConflict

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

T = TypeVar("T")


def lock_for_update(
    db: Session,
    model: type[T],
    entity_id: str | UUID,
    *,
    nowait: bool = False,
    skip_locked: bool = False,
) -> T | None:
    """Acquire a pessimistic lock on a single entity.

    Executes SELECT ... FOR UPDATE to lock the row until the transaction
    completes. Other transactions attempting to lock the same row will
    block (or fail if nowait=True).

    Args:
        db: SQLAlchemy session.
        model: The model class to query.
        entity_id: Primary key of the entity to lock.
        nowait: If True, raises ConcurrencyConflict immediately if the row
                is already locked instead of waiting. Default False.
        skip_locked: If True, returns None for locked rows instead of waiting.
                    Mutually exclusive with nowait. Default False.

    Returns:
        The locked entity, or None if not found (or skipped if skip_locked=True).

    Raises:
        ConcurrencyConflict: If nowait=True and the row is already locked.

    Example:
        subscriber = lock_for_update(db, Subscriber, subscriber_id)
        if subscriber:
            subscriber.balance += 100
            db.commit()
    """
    stmt = (
        select(model)
        .where(model.id == entity_id)  # type: ignore[attr-defined]
        .with_for_update(nowait=nowait, skip_locked=skip_locked)
    )
    try:
        return db.scalars(stmt).first()
    except OperationalError as exc:
        # PostgreSQL raises OperationalError with "could not obtain lock"
        # when nowait=True and the row is locked
        error_msg = str(exc).lower()
        if "could not obtain lock" in error_msg or "lock not available" in error_msg:
            logger.debug(
                "Lock not available for %s id=%s",
                model.__name__,
                entity_id,
            )
            raise ConcurrencyConflict(
                f"Could not acquire lock on {model.__name__} {entity_id}"
            ) from exc
        raise


def lock_multiple(
    db: Session,
    model: type[T],
    entity_ids: Sequence[str | UUID],
    *,
    nowait: bool = False,
) -> list[T]:
    """Lock multiple entities in a consistent order to prevent deadlocks.

    Acquires locks on all specified entities, sorted by ID to ensure
    consistent lock ordering across concurrent transactions. This prevents
    deadlocks when multiple transactions need to lock the same set of rows.

    Args:
        db: SQLAlchemy session.
        model: The model class to query.
        entity_ids: Primary keys of the entities to lock.
        nowait: If True, raises ConcurrencyConflict immediately if any row
                is already locked instead of waiting. Default False.

    Returns:
        List of locked entities (may be fewer than requested if some IDs
        don't exist).

    Raises:
        ConcurrencyConflict: If nowait=True and any row is already locked.

    Example:
        invoices = lock_multiple(db, Invoice, [id1, id2, id3])
        for invoice in invoices:
            invoice.status = InvoiceStatus.paid
        db.commit()
    """
    if not entity_ids:
        return []

    # Sort IDs to ensure consistent lock order across transactions
    # This prevents deadlocks like: T1 locks A then B, T2 locks B then A
    sorted_ids = sorted(set(entity_ids), key=str)

    stmt = (
        select(model)
        .where(model.id.in_(sorted_ids))  # type: ignore[attr-defined]
        .order_by(model.id)  # type: ignore[attr-defined]
        .with_for_update(nowait=nowait)
    )

    try:
        return list(db.scalars(stmt).all())
    except OperationalError as exc:
        error_msg = str(exc).lower()
        if "could not obtain lock" in error_msg or "lock not available" in error_msg:
            logger.debug(
                "Lock not available for %s ids=%s",
                model.__name__,
                sorted_ids,
            )
            raise ConcurrencyConflict(
                f"Could not acquire locks on {model.__name__} entities"
            ) from exc
        raise


def lock_for_update_or_raise(
    db: Session,
    model: type[T],
    entity_id: str | UUID,
    *,
    not_found_message: str = "Entity not found",
    nowait: bool = False,
) -> T:
    """Lock an entity or raise an exception if not found.

    Convenience wrapper around lock_for_update that raises HTTPException
    if the entity doesn't exist.

    Args:
        db: SQLAlchemy session.
        model: The model class to query.
        entity_id: Primary key of the entity to lock.
        not_found_message: Message for HTTPException if not found.
        nowait: If True, raises ConcurrencyConflict if locked.

    Returns:
        The locked entity.

    Raises:
        HTTPException: 404 if entity not found.
        ConcurrencyConflict: If nowait=True and the row is already locked.
    """
    from fastapi import HTTPException

    entity = lock_for_update(db, model, entity_id, nowait=nowait)
    if entity is None:
        raise HTTPException(status_code=404, detail=not_found_message)
    return entity


def serial_advisory_lock(
    db: Session,
    serial_number: str,
    *,
    nowait: bool = False,
) -> bool:
    """Acquire PostgreSQL advisory lock for ONT serial number.

    Uses hash-based key to prevent concurrent authorization of same serial.
    Lock is released on commit/rollback.

    Args:
        db: SQLAlchemy session.
        serial_number: ONT serial number to lock.
        nowait: If True, return False immediately if lock unavailable.

    Returns:
        True if lock was acquired, False if nowait=True and lock unavailable.

    Note:
        Falls back to always returning True on non-PostgreSQL databases
        (e.g., SQLite in tests) where advisory locks are not supported.
    """
    import hashlib

    from sqlalchemy import text

    # Normalize serial number for consistent hashing
    normalized = serial_number.strip().upper().replace("-", "")
    hash_bytes = hashlib.sha256(normalized.encode()).digest()[:8]
    lock_key = int.from_bytes(hash_bytes, byteorder="big", signed=True)

    # Check if we're using PostgreSQL (advisory locks only work there)
    dialect_name = db.bind.dialect.name if db.bind else ""
    if dialect_name != "postgresql":
        # SQLite or other databases don't support advisory locks
        # Return True to allow operation to proceed (no locking in tests)
        logger.debug(
            "Advisory lock skipped for %s (non-PostgreSQL database: %s)",
            serial_number,
            dialect_name,
        )
        return True

    try:
        if nowait:
            result = db.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": lock_key},
            )
            acquired = bool(result.scalar())
            if not acquired:
                logger.debug(
                    "Advisory lock not available for serial %s (key=%d)",
                    serial_number,
                    lock_key,
                )
            return acquired
        else:
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
            return True
    except OperationalError as exc:
        # Handle case where advisory lock function doesn't exist
        error_msg = str(exc).lower()
        if "no such function" in error_msg or "pg_try_advisory" in error_msg:
            logger.debug(
                "Advisory lock function not available, skipping lock for %s",
                serial_number,
            )
            return True
        raise
