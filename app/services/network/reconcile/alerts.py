"""Alert escalation for reconcile failure modes.

Two escalations live here:

* **sweep unreachable** — the ONT's mgmt plane did not answer for N cycles.
* **ACS delivery fault** — the ONT's ACS half could not be written for N
  consecutive reconciles (unknown/ambiguous GenieACS device identity, a CPE
  that has never informed, a faulted NBI write). The sweep re-picks the
  least-recently-reconciled ONT forever, so without this a permanent fault
  retries silently until someone notices a customer is offline.

The sweeper increments ``OntUnit.consecutive_sweep_unreachable`` each cycle
it cannot reach an ONT. After N consecutive cycles (default 3 ≈ 12h on a
4h sweep cadence) the operator needs to know — this module bridges the
in-process counter to the monitoring stack.

Single output path, best-effort: a **structured log line**, always emitted on
threshold crossings. Any log aggregator (Promtail, Splunk, etc.) can route on
``alert_kind=ont.sweep_unreachable`` plus the per-ONT metadata. (The Zabbix
trapper push that used to accompany it was retired with the native monitoring
cutover.)

Failure of the path does not propagate — the sweep cycle continues.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


# ── Sweep-unreachable alert escalation ─────────────────────────────────────


DEFAULT_SWEEP_THRESHOLD = 3
SWEEP_ALERT_KIND = "ont.sweep_unreachable"


def default_threshold_from_env() -> int:
    """``RECONCILE_SWEEP_ALERT_THRESHOLD`` env override, default 3."""
    raw = os.getenv("RECONCILE_SWEEP_ALERT_THRESHOLD", "").strip()
    if not raw:
        return DEFAULT_SWEEP_THRESHOLD
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "reconcile_sweep_alert_threshold_invalid",
            extra={"value": raw},
        )
        return DEFAULT_SWEEP_THRESHOLD
    return value if value > 0 else DEFAULT_SWEEP_THRESHOLD


def escalate_sweep_unreachable(
    *,
    ont_id: str,
    serial_number: str,
    mgmt_ip: str | None,
    before: int,
    after: int,
    threshold: int = DEFAULT_SWEEP_THRESHOLD,
) -> None:
    """Called by the sweeper after incrementing ``after``.

    Emits a structured ERROR log only on threshold crossings (``before <
    threshold <= after``) to avoid alert-fatigue per-sweep.

    Resolution is handled by ``resolve_sweep_unreachable``, called from
    the success path in core.py.
    """
    crossing = before < threshold <= after
    log_level = logging.ERROR if crossing else logging.DEBUG
    logger.log(
        log_level,
        SWEEP_ALERT_KIND,
        extra={
            "alert_kind": SWEEP_ALERT_KIND,
            "alert_action": "escalate" if crossing else "still_unreachable",
            "ont_id": str(ont_id),
            "serial_number": serial_number,
            "mgmt_ip": mgmt_ip,
            "before": before,
            "after": after,
            "threshold": threshold,
        },
    )


def resolve_sweep_unreachable(
    *,
    ont_id: str,
    serial_number: str,
    mgmt_ip: str | None,
    before: int,
) -> None:
    """Called from the reconcile core when the counter resets from a
    non-zero value to zero. Emits a single INFO ``resolved`` log."""
    if before <= 0:
        return

    logger.info(
        SWEEP_ALERT_KIND,
        extra={
            "alert_kind": SWEEP_ALERT_KIND,
            "alert_action": "resolved",
            "ont_id": str(ont_id),
            "serial_number": serial_number,
            "mgmt_ip": mgmt_ip,
            "before": before,
            "after": 0,
        },
    )


# ── ACS delivery-fault escalation ──────────────────────────────────────────

# Fixed, not environment-tunable: this module already owns one env-backed
# threshold and the decision-input contract keeps raw environment reads to the
# declared owners. Callers may pass an explicit ``threshold`` instead.
DEFAULT_ACS_FAULT_THRESHOLD = 3
ACS_FAULT_ALERT_KIND = "ont.acs_delivery_fault"


def escalate_acs_delivery_fault(
    *,
    ont_id: str,
    serial_number: str,
    reason: str,
    message: str,
    streak: int,
    threshold: int | None = None,
) -> None:
    """Surface an ONT whose ACS half keeps failing to be delivered.

    The reconcile sweep selects the least-recently-reconciled ONTs, so it
    re-picks a permanently broken one forever. Before this existed, an ONT
    whose ACS writes could never land (unknown device identity, a CPE that
    never informs) simply consumed a sweep slot every cycle in silence.

    Emits a structured ERROR once per threshold crossing so a fleet of
    not-yet-informed ONTs cannot drown the log, then DEBUG while it persists.
    ``message`` is reconciler-produced and carries no credential values.
    """
    effective = threshold or DEFAULT_ACS_FAULT_THRESHOLD
    crossing = streak == effective
    logger.log(
        logging.ERROR if crossing else logging.DEBUG,
        ACS_FAULT_ALERT_KIND,
        extra={
            "alert_kind": ACS_FAULT_ALERT_KIND,
            "alert_action": "escalate" if crossing else "still_faulting",
            "ont_id": str(ont_id),
            "serial_number": serial_number,
            "failure_reason": reason,
            "failure_message": message,
            "streak": streak,
            "threshold": effective,
        },
    )


def resolve_acs_delivery_fault(
    *,
    ont_id: str,
    serial_number: str,
    before: int,
) -> None:
    """Emit a single INFO ``resolved`` line when a fault streak clears."""
    if before <= 0:
        return
    logger.info(
        ACS_FAULT_ALERT_KIND,
        extra={
            "alert_kind": ACS_FAULT_ALERT_KIND,
            "alert_action": "resolved",
            "ont_id": str(ont_id),
            "serial_number": serial_number,
            "streak": 0,
            "before": before,
        },
    )


__all__ = (
    "ACS_FAULT_ALERT_KIND",
    "DEFAULT_ACS_FAULT_THRESHOLD",
    "DEFAULT_SWEEP_THRESHOLD",
    "default_threshold_from_env",
    "escalate_acs_delivery_fault",
    "escalate_sweep_unreachable",
    "resolve_acs_delivery_fault",
    "resolve_sweep_unreachable",
)
