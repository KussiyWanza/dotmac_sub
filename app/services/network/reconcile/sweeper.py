"""Periodic sweeper — long-running process, NOT a Celery task.

Walks every active ``OntUnit`` row at a fixed interval and runs
``reconcile_ont(mode="sweep")`` against each. The sweep mode proceeds even
against ``out_of_sync`` rows (this is how they clear), so the sweeper is
the primary mechanism for self-healing drift detected post-write.

Design rules (from the original architecture discussion):

* **No queue, no retry machinery.** A reconcile either converges or marks
  the ONT ``out_of_sync``. The sweeper picks it up on the next pass.
* **Reachability fast-fail.** Before spending an SSH/NBI roundtrip per
  ONT, the sweeper does a ~100ms mgmt-IP ping. If the ONT is unreachable
  the sweeper increments ``consecutive_sweep_unreachable`` on the row and
  skips the detailed reconcile. After N consecutive unreachable sweeps
  the operator gets an alert. Alert escalation is not wired in
  this commit, just the counter).
* **Process, not Celery.** Single instance, deterministic, no per-task
  queue depth to debug. Deploys as a systemd-managed process alongside
  the FastAPI app.
* **Bounded per-ONT timeout.** Each reconcile gets a hard ceiling
  (default 60s — same as the sync HTTP path) so one unhealthy device
  doesn't block the rest of the sweep.

This module exposes ``SweepLoop`` + ``run_sweep_once`` so the same logic
can run as a daemon (``run_forever``), a one-shot test pass
(``run_once``), or a CLI invocation (``scripts/run_sweeper.py``).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.models.ont_observation import OntObservation
from app.services.network.reconcile.candidates import restrict_to_reconcile_candidates
from app.services.network.reconcile.readers.reachability import (
    PingFunction,
    is_pingable,
)

from . import reconcile_ont
from .adapters import desired_from_ont_unit
from .alerts import (
    default_threshold_from_env,
    escalate_sweep_unreachable,
)

logger = logging.getLogger(__name__)


# ── Result shape ────────────────────────────────────────────────────────────


class SweepDisposition(StrEnum):
    """What happened to one ONT in a sweep pass.

    A tuple of ``(reachable, success)`` could not distinguish "we chose not to
    touch this device" from "we could not reach it", so a hold discovered at
    the point of use was counted as unreachable -- reporting a deliberate
    exclusion as an outage.
    """

    reconciled = "reconciled"
    unreachable = "unreachable"
    held = "held"
    missing = "missing"


@dataclass(frozen=True)
class SweepOutcome:
    """Typed result of one ONT's sweep step."""

    disposition: SweepDisposition
    success: bool = False


@dataclass
class SweepStats:
    """Roll-up of one sweep pass — emitted to logs and metrics."""

    started_at: datetime
    completed_at: datetime | None = None
    total_onts: int = 0
    reconciled: int = 0
    skipped_unreachable: int = 0
    #: ONTs excluded by a reviewed per-ONT hold. Reported separately from
    #: `skipped_unreachable` because "we chose not to" and "we could not" are
    #: different operational facts.
    held: int = 0
    succeeded: int = 0
    failed: int = 0
    deferred: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def duration_sec(self) -> float:
        if self.completed_at is None:
            return 0.0
        return (self.completed_at - self.started_at).total_seconds()


# ── Per-ONT pass ───────────────────────────────────────────────────────────


def _sweep_one(
    db: Session,
    ont_id,
    *,
    timeout_sec: int,
    ping_function: PingFunction | None,
    reconcile_fn: Callable = reconcile_ont,
    alert_threshold: int = 0,
) -> SweepOutcome:
    """Reconcile one ONT in sweep mode.

    Returns a typed ``SweepOutcome`` so a hold is reported as HELD rather than
    collapsing into "unreachable".

    Resolves the desired state to read the mgmt IP for the reachability
    check. If the ONT is unreachable, the per-ONT
    ``consecutive_sweep_unreachable`` counter is incremented and the
    function returns ``(False, False)`` — no SSH/NBI roundtrips fired.

    On reachable ONTs, runs ``reconcile_ont(mode="sweep")`` and returns
    ``(True, result.success)``.

    When ``alert_threshold`` is positive, calls
    ``escalate_sweep_unreachable`` after incrementing the counter so the
    operator's monitoring stack learns about the unreachable ONT on the
    cycle the threshold is crossed (structured log line).
    """
    # Point-of-use eligibility, inside THIS transaction and holding the
    # OntUnit row lock. The pass-level held set is only a pre-filter: a hold
    # placed after the pass began would be invisible to it, and the sweeper
    # would touch a device someone had just decided to protect.
    from app.services.network.ont_reconcile_eligibility import (
        eligibility_under_lock,
    )

    verdict = eligibility_under_lock(db, ont_id)
    if not verdict.eligible:
        logger.info(
            "sweep_ont_held",
            extra={
                "ont_id": str(ont_id),
                "scope": verdict.scope,
                "hold_id": verdict.hold_id,
                "reason_code": verdict.reason_code,
                "overdue": verdict.overdue,
            },
        )
        return SweepOutcome(disposition=SweepDisposition.held)

    ont = db.execute(select(OntUnit).where(OntUnit.id == ont_id)).scalar_one_or_none()
    if ont is None:
        return SweepOutcome(disposition=SweepDisposition.missing)

    # Cheap pre-flight: ping the mgmt IP. We resolve desired_state just for
    # the IP — not the full reconcile path.
    desired = desired_from_ont_unit(db, ont)
    reachable = is_pingable(desired.mgmt_ip, ping_function=ping_function)
    if not reachable:
        before = ont.consecutive_sweep_unreachable or 0
        after = before + 1
        ont.consecutive_sweep_unreachable = after
        ont.last_reconciled_at = datetime.now(UTC)
        if alert_threshold > 0:
            escalate_sweep_unreachable(
                ont_id=str(ont.id),
                serial_number=str(ont.serial_number or ""),
                mgmt_ip=desired.mgmt_ip,
                before=before,
                after=after,
                threshold=alert_threshold,
            )
        return SweepOutcome(disposition=SweepDisposition.unreachable)

    result = reconcile_fn(
        db,
        ont.id,
        proposed_change=None,
        mode="sweep",
        timeout_sec=timeout_sec,
        ping_function=ping_function,
    )
    return SweepOutcome(disposition=SweepDisposition.reconciled, success=result.success)


def run_sweep_once(
    db_factory: Callable[[], Session],
    *,
    timeout_sec: int = 60,
    ping_function: PingFunction | None = None,
    reconcile_fn: Callable = reconcile_ont,
    only_active: bool = True,
    alert_threshold: int | None = None,
    max_onts: int | None = None,
    max_duration_sec: float | None = None,
) -> SweepStats:
    """Sweep every active ONT once and return aggregated stats.

    ``db_factory`` is called per-ONT to get a fresh session — sweeps run
    long enough that holding a single session for the whole pass risks
    DB connection timeouts.
    """
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    stats = SweepStats(started_at=started)
    effective_threshold = (
        alert_threshold if alert_threshold is not None else default_threshold_from_env()
    )

    # First pass: collect target IDs (with a short-lived session).
    with db_factory() as catalog_db:
        # Eligibility comes from the one canonical predicate; this function
        # adds only the ordering the sweep needs (least-recently-reconciled
        # first, never-reconciled ahead of everything).
        stmt = restrict_to_reconcile_candidates(
            select(OntUnit.id), only_active=only_active
        )
        stmt = stmt.outerjoin(
            OntObservation, OntObservation.ont_unit_id == OntUnit.id
        ).order_by(OntObservation.last_reconciled_at.asc().nullsfirst(), OntUnit.id)
        if max_onts is not None:
            stmt = stmt.limit(max(1, int(max_onts)))
        ont_ids = [row[0] for row in catalog_db.execute(stmt).all()]
        # Read the hold set ONCE per pass rather than per ONT. The per-ONT
        # verdict remains the authority for a single decision; this is the
        # bulk read that keeps the sweep to one query.
        from app.services.network.ont_reconcile_eligibility import held_ont_ids

        held = held_ont_ids(catalog_db)

    stats.total_onts = len(ont_ids)
    logger.info(
        "sweep_cycle_begin",
        extra={"total_onts": stats.total_onts, "started_at": started.isoformat()},
    )

    for index, ont_id in enumerate(ont_ids):
        if ont_id in held:
            # Checked BEFORE ping, read or write. Contacting a device to
            # discover it is held would defeat the point of holding it.
            stats.held += 1
            logger.info(
                "sweep_ont_held",
                extra={"ont_id": str(ont_id), "scope": "automatic_sweep"},
            )
            continue
        if max_duration_sec is not None and time.monotonic() - started_monotonic >= max(
            0.0, max_duration_sec
        ):
            stats.deferred = len(ont_ids) - index
            logger.warning(
                "sweep_cycle_budget_exhausted",
                extra={
                    "deferred": stats.deferred,
                    "max_duration_sec": max_duration_sec,
                },
            )
            break
        try:
            with db_factory() as ont_db:
                outcome = _sweep_one(
                    ont_db,
                    ont_id,
                    timeout_sec=timeout_sec,
                    ping_function=ping_function,
                    reconcile_fn=reconcile_fn,
                    alert_threshold=effective_threshold,
                )
                ont_db.commit()
        except Exception as exc:  # noqa: BLE001 — defensive per-ONT
            stats.errors.append(f"{ont_id}: {exc}")
            logger.exception(
                "sweep_per_ont_error",
                extra={"ont_id": str(ont_id), "error": str(exc)},
            )
            continue

        if outcome.disposition is SweepDisposition.held:
            # A hold placed AFTER the pass-level snapshot is still honoured,
            # and is reported as a deliberate exclusion rather than an outage.
            stats.held += 1
            continue
        if outcome.disposition is SweepDisposition.missing:
            continue
        if outcome.disposition is SweepDisposition.unreachable:
            stats.skipped_unreachable += 1
            continue
        stats.reconciled += 1
        success = outcome.success
        if success:
            stats.succeeded += 1
        else:
            stats.failed += 1

    stats.completed_at = datetime.now(UTC)
    logger.info(
        "sweep_cycle_complete",
        extra={
            "total_onts": stats.total_onts,
            "reconciled": stats.reconciled,
            "skipped_unreachable": stats.skipped_unreachable,
            "held": stats.held,
            "succeeded": stats.succeeded,
            "failed": stats.failed,
            "deferred": stats.deferred,
            "errors": len(stats.errors),
            "duration_sec": stats.duration_sec,
        },
    )
    return stats


# ── Long-running loop ──────────────────────────────────────────────────────


class SweepLoop:
    """Runs ``run_sweep_once`` on a fixed interval until stopped.

    Designed for systemd-managed deployment alongside the FastAPI app.
    Single-process, single-thread — no need to coordinate with Celery
    workers. Use ``stop()`` for clean shutdown; the loop respects
    SIGTERM/SIGINT when installed via ``install_signal_handlers``.
    """

    def __init__(
        self,
        db_factory: Callable[[], Session],
        *,
        interval_sec: int = 4 * 3600,  # 4h default
        timeout_sec: int = 60,
        ping_function: PingFunction | None = None,
    ):
        self._db_factory = db_factory
        self._interval = interval_sec
        self._timeout = timeout_sec
        self._ping_function = ping_function
        self._stop = threading.Event()

    def stop(self) -> None:
        """Request a clean shutdown after the current cycle completes."""
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """Wire SIGTERM/SIGINT to ``stop()``. Call this from the daemon
        entry-point; tests shouldn't call it."""
        signal.signal(signal.SIGTERM, lambda *a: self.stop())
        signal.signal(signal.SIGINT, lambda *a: self.stop())

    def run_forever(self) -> None:
        logger.info(
            "sweep_loop_starting",
            extra={"interval_sec": self._interval, "timeout_sec": self._timeout},
        )
        while not self._stop.is_set():
            cycle_started = time.monotonic()
            try:
                run_sweep_once(
                    self._db_factory,
                    timeout_sec=self._timeout,
                    ping_function=self._ping_function,
                )
            except Exception:
                logger.exception("sweep_cycle_failed")

            # Sleep the remainder of the interval, in small wake-ups so
            # ``stop()`` from a signal handler takes effect within ~1s.
            elapsed = time.monotonic() - cycle_started
            sleep_remaining = max(0.0, self._interval - elapsed)
            deadline = time.monotonic() + sleep_remaining
            while time.monotonic() < deadline and not self._stop.is_set():
                time.sleep(min(1.0, deadline - time.monotonic()))

        logger.info("sweep_loop_stopped")


__all__ = ("SweepLoop", "SweepStats", "run_sweep_once")
