"""Fiber test-acceptance policy: derived verdicts and link budgets.

Observations stay facts — the technician's measurement and self-assessment are
never altered. This policy derives a typed acceptance verdict from declared
per-test-type thresholds, and an expected downstream link budget from the
canonical customer trace. Where the policy has no threshold or the inputs are
incomplete, it says so explicitly instead of guessing.

The derived verdict is snapshotted beside the technician's assertion at
capture time (``FieldFiberTestResult.derived_*``) with the policy version, so
a later threshold change never silently rewrites history.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import FiberSegment
from app.services.fiber_topology import FiberSubscriptionTrace

ACCEPTANCE_POLICY_VERSION = 1

# Attenuation coefficient applied to traced segment lengths. 1310 nm single
# mode planning figure; stated in every budget's assumptions.
FIBER_ATTENUATION_DB_PER_KM = 0.35

# Downstream launch power assumed when deriving margin from a measured ONT Rx.
# GPON class B+ planning figure; every derived margin names this assumption.
ASSUMED_OLT_TX_DBM = 2.0


class FiberTestVerdict(enum.Enum):
    """Typed vocabulary for the checked derived-verdict string column."""

    within_threshold = "within_threshold"
    exceeds_threshold = "exceeds_threshold"
    no_measurement = "no_measurement"
    no_policy = "no_policy"


@dataclass(frozen=True)
class FiberTestThreshold:
    """One declared acceptance bound for a test type."""

    test_type: str
    unit: str
    minimum: float | None
    maximum: float | None
    description: str


# Declared acceptance thresholds per test type. ``continuity`` and ``other``
# carry no numeric policy; their verdict is ``no_policy`` and the
# technician's assertion stands alone.
ACCEPTANCE_THRESHOLDS: dict[str, FiberTestThreshold] = {
    "insertion_loss": FiberTestThreshold(
        test_type="insertion_loss",
        unit="dB",
        minimum=None,
        maximum=0.30,
        description="Splice/connector insertion loss at most 0.30 dB",
    ),
    "otdr": FiberTestThreshold(
        test_type="otdr",
        unit="dB",
        minimum=None,
        maximum=0.30,
        description="OTDR event loss at most 0.30 dB",
    ),
    "optical_power": FiberTestThreshold(
        test_type="optical_power",
        unit="dBm",
        minimum=-28.0,
        maximum=-8.0,
        description="GPON class B+ receive window -28.0 to -8.0 dBm",
    ),
    "reflectance": FiberTestThreshold(
        test_type="reflectance",
        unit="dB",
        minimum=None,
        maximum=-35.0,
        description="Event reflectance at most -35 dB",
    ),
}


@dataclass(frozen=True)
class FiberTestAcceptance:
    """Derived acceptance decision for one measurement."""

    verdict: FiberTestVerdict
    passed: bool | None
    threshold: FiberTestThreshold | None
    policy_version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "passed": self.passed,
            "threshold": {
                "test_type": self.threshold.test_type,
                "unit": self.threshold.unit,
                "minimum": self.threshold.minimum,
                "maximum": self.threshold.maximum,
                "description": self.threshold.description,
            }
            if self.threshold
            else None,
            "policy_version": self.policy_version,
        }


def derive_verdict(*, test_type: str, value_db: float | None) -> FiberTestAcceptance:
    """Derive the typed acceptance verdict for one measurement."""

    threshold = ACCEPTANCE_THRESHOLDS.get(test_type)
    if threshold is None:
        return FiberTestAcceptance(
            verdict=FiberTestVerdict.no_policy,
            passed=None,
            threshold=None,
            policy_version=ACCEPTANCE_POLICY_VERSION,
        )
    if value_db is None:
        return FiberTestAcceptance(
            verdict=FiberTestVerdict.no_measurement,
            passed=None,
            threshold=threshold,
            policy_version=ACCEPTANCE_POLICY_VERSION,
        )
    within = True
    if threshold.maximum is not None and value_db > threshold.maximum:
        within = False
    if threshold.minimum is not None and value_db < threshold.minimum:
        within = False
    return FiberTestAcceptance(
        verdict=FiberTestVerdict.within_threshold
        if within
        else FiberTestVerdict.exceeds_threshold,
        passed=within,
        threshold=threshold,
        policy_version=ACCEPTANCE_POLICY_VERSION,
    )


@dataclass(frozen=True)
class FiberLinkBudgetComponent:
    """One named contribution to the expected downstream loss."""

    name: str
    loss_db: float
    basis: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "loss_db": self.loss_db, "basis": self.basis}


@dataclass(frozen=True)
class FiberLinkBudget:
    """Expected downstream loss derived from the canonical customer trace.

    ``complete`` is False when the trace itself is not physically complete —
    the partial budget is still shown, labelled, never presented as the
    whole path.
    """

    expected_loss_db: float
    complete: bool
    components: tuple[FiberLinkBudgetComponent, ...]
    assumptions: tuple[str, ...]
    measured_rx_dbm: float | None
    assumed_tx_dbm: float | None
    margin_db: float | None
    policy_version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_loss_db": self.expected_loss_db,
            "complete": self.complete,
            "components": [item.to_dict() for item in self.components],
            "assumptions": list(self.assumptions),
            "measured_rx_dbm": self.measured_rx_dbm,
            "assumed_tx_dbm": self.assumed_tx_dbm,
            "margin_db": self.margin_db,
            "policy_version": self.policy_version,
        }


def _splitter_loss_db(trace: FiberSubscriptionTrace) -> float | None:
    """The deepest cumulative reviewed splitter loss named by the trace."""

    losses: list[float] = []
    for hop in trace.hops:
        if hop.cumulative_splitter_loss_db is None:
            continue
        try:
            losses.append(float(hop.cumulative_splitter_loss_db))
        except (TypeError, ValueError):
            continue
    return max(losses) if losses else None


def derive_link_budget(
    db: Session,
    trace: FiberSubscriptionTrace,
    *,
    measured_rx_dbm: float | None = None,
) -> FiberLinkBudget | None:
    """Derive the expected downstream loss for one traced subscription.

    Returns None when the trace names no loss-bearing evidence at all
    (nothing to derive). Margin is derived only when a measured Rx exists,
    and always names the assumed launch power.
    """

    components: list[FiberLinkBudgetComponent] = []
    assumptions: list[str] = []

    splitter_loss = _splitter_loss_db(trace)
    if splitter_loss is not None:
        components.append(
            FiberLinkBudgetComponent(
                name="splitter_cumulative_loss",
                loss_db=splitter_loss,
                basis="reviewed splitter stage losses named by the trace",
            )
        )

    segment_ids = [
        hop.asset_id
        for hop in trace.hops
        if hop.kind.endswith("_segment") and hop.asset_id is not None
    ]
    if segment_ids:
        lengths = (
            db.query(FiberSegment.length_m)
            .filter(FiberSegment.id.in_(segment_ids))
            .all()
        )
        known_lengths = [row[0] for row in lengths if row[0] is not None]
        if known_lengths:
            total_km = sum(known_lengths) / 1000.0
            components.append(
                FiberLinkBudgetComponent(
                    name="fiber_attenuation",
                    loss_db=round(total_km * FIBER_ATTENUATION_DB_PER_KM, 2),
                    basis=(
                        f"{total_km:.3f} km of traced cable at "
                        f"{FIBER_ATTENUATION_DB_PER_KM} dB/km"
                    ),
                )
            )
            assumptions.append(
                f"fiber attenuation assumed {FIBER_ATTENUATION_DB_PER_KM} dB/km "
                "(1310 nm planning figure)"
            )
        if len(known_lengths) < len(segment_ids):
            assumptions.append(
                "one or more traced segments have no recorded length and "
                "contribute no attenuation"
            )

    if not components:
        return None

    assumptions.append(
        "recorded splice and connector losses are not yet included in the expected loss"
    )

    expected_loss = round(sum(item.loss_db for item in components), 2)
    margin: float | None = None
    assumed_tx: float | None = None
    if measured_rx_dbm is not None:
        assumed_tx = ASSUMED_OLT_TX_DBM
        margin = round(measured_rx_dbm - (assumed_tx - expected_loss), 2)
        assumptions.append(
            f"margin assumes {ASSUMED_OLT_TX_DBM} dBm downstream launch power "
            "(GPON class B+ planning figure)"
        )

    return FiberLinkBudget(
        expected_loss_db=expected_loss,
        complete=trace.physical_complete,
        components=tuple(components),
        assumptions=tuple(assumptions),
        measured_rx_dbm=measured_rx_dbm,
        assumed_tx_dbm=assumed_tx,
        margin_db=margin,
        policy_version=ACCEPTANCE_POLICY_VERSION,
    )
