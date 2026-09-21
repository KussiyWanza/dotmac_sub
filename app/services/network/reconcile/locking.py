"""Row-level locking for reconcile passes.

A reconcile holds ``SELECT FOR UPDATE`` on the ``OntUnit`` row for its entire
duration. Two callers reconciling the same ONT serialize on the row lock; on
PostgreSQL this is correct and atomic. (Under sqlite — used in tests —
``FOR UPDATE`` is a no-op but the session is still single-writer, which is
fine for the unit tests; concurrency behavior is exercised in production.)

The lock context manager also detects a crashed prior reconcile. The invariant
is:

    A successful reconcile commits ``sync_status`` to either ``synced`` or
    ``out_of_sync`` before exiting the locked transaction. If the row is found
    with ``sync_status='reconciling'`` at acquisition time, the prior process
    must have died — because if it were still running, it would still hold the
    lock and our SELECT FOR UPDATE would be blocked.

In that case the lock entry-point flips the status to ``out_of_sync`` with a
"prior reconcile did not finalise" note, then yields the row to the new
reconcile so it can attempt its own pass.

Transaction lifecycle is the caller's responsibility — ``reconcile_ont``
commits when it sets the final ``sync_status``. The lock module just provides
the row-locking primitive and the crash-recovery check; it does not call
``commit`` or ``rollback`` itself.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.network import OntSyncStatus, OntUnit

logger = logging.getLogger(__name__)


class LockError(Exception):
    """Base class for reconcile-lock errors."""


class OntNotFound(LockError):
    """Raised when the requested ONT does not exist."""


class LockConflict(LockError):
    """Raised when ``nowait=True`` and another transaction holds the row lock."""


@contextmanager
def acquire_reconcile_lock(
    db: Session,
    ont_unit_id: uuid.UUID | str,
    *,
    nowait: bool = False,
) -> Iterator[OntUnit]:
    """Acquire ``SELECT FOR UPDATE`` on an OntUnit row for the duration of a
    reconcile pass.

    Behavior:

    * If the row doesn't exist, raises :class:`OntNotFound`.
    * If ``nowait=True`` and the row is already locked by another transaction,
      raises :class:`LockConflict` immediately (no waiting).
    * Otherwise blocks until the lock is acquired.
    * Once locked, if ``sync_status`` is found at ``reconciling`` (a crashed
      prior pass), the status is flipped to ``out_of_sync`` with a crash note
      inside the same transaction. The current reconcile then proceeds.
    * The row is yielded to the caller, which is responsible for mutating
      ``sync_status`` to ``synced`` or ``out_of_sync`` before exiting and for
      committing/rolling back its own transaction.

    The lock is held for the duration of the surrounding transaction. Callers
    must ``commit`` or ``rollback`` to release it.

    Sweeper mode (no proposed_change) uses the same lock; ``mode``-specific
    blocking semantics (e.g. refusing sync writes against an ``out_of_sync``
    ONT) live in ``reconcile_ont`` itself.
    """
    coerced_id = _coerce_uuid(ont_unit_id)
    stmt = (
        select(OntUnit).where(OntUnit.id == coerced_id).with_for_update(nowait=nowait)
    )

    try:
        ont = db.execute(stmt).scalar_one_or_none()
    except OperationalError as exc:
        # PostgreSQL raises this for `SELECT ... FOR UPDATE NOWAIT` when the
        # row is locked by another transaction. The SQLSTATE is 55P03; we
        # match on the exception class rather than the code so this works
        # across dialects.
        if nowait:
            raise LockConflict(
                f"OntUnit {ont_unit_id} is locked by another reconcile"
            ) from exc
        raise

    if ont is None:
        raise OntNotFound(f"OntUnit {ont_unit_id} not found")

    if ont.sync_status == OntSyncStatus.reconciling:
        prior_started = ont.last_reconcile_started_at
        logger.warning(
            "reconcile_crashed_prior_detected",
            extra={
                "ont_unit_id": str(ont.id),
                "prior_started_at": prior_started.isoformat()
                if prior_started is not None
                else None,
            },
        )
        from app.services.network.ont_status import set_sync_status

        set_sync_status(ont, OntSyncStatus.out_of_sync, reason="crashed_prior_detected")
        ont.last_error = (
            "Prior reconcile started at "
            f"{prior_started.isoformat() if prior_started else 'unknown'} "
            "did not finalise (process crash assumed)."
        )
        # Don't commit here — the caller controls transaction lifecycle.
        # The crash mark lives in the same transaction as the new reconcile
        # attempt; if the new reconcile succeeds, it'll overwrite to 'synced'
        # before commit, which is the right outcome.

    yield ont


def _coerce_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
