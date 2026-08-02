"""Typed freshness contract for wireless radio RF observations.

The UISP topology sync writes a current-value RF observation onto the radio's
``cpe_devices`` row (``rf_signal_dbm`` / ``rf_signal_source`` /
``rf_signal_observed_at``). Every reader — Customer 360, the last-mile
diagnoser, traces — resolves the displayable state through
``resolve_effective_radio_signal`` so a stale or orphaned value can never
render as a current signal. Mirrors the ONT pattern
(``ont_status.resolve_effective_ont_status``): value + source + explicit
freshness + reason, never a bare number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

# The sync runs every 15 minutes by default (scheduler_config:
# topology_uisp_sync_interval_minutes); two missed runs make a value stale.
RF_SIGNAL_FRESH_TTL = timedelta(minutes=30)

# UISP statuses under which a stored RF value must not be presented at all:
# the radio is not (or no longer known to be) associated to its AP, so any
# retained number describes a link that does not currently exist. Read-side
# guard — protects display even if a sync-side clear was missed.
_STATUS_BLOCKS_SIGNAL = frozenset({"disconnected", "missing", "vanished"})


class RadioSignalSource(str, Enum):
    """Which observation produced the stored RF value."""

    uisp_ap_station = "uisp_ap_station"


class RadioSignalFreshness(str, Enum):
    fresh = "fresh"
    stale = "stale"
    unavailable = "unavailable"


@dataclass(frozen=True)
class EffectiveRadioSignal:
    """Displayable RF state: value only ever alongside its freshness."""

    signal_dbm: float | None
    source: RadioSignalSource | None
    observed_at: datetime | None
    freshness: RadioSignalFreshness
    reason: str

    @property
    def is_fresh(self) -> bool:
        return self.freshness == RadioSignalFreshness.fresh


def _normalize_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _coerce_source(value: object) -> RadioSignalSource | None:
    """Typed provenance from the stored column; unknown strings resolve None
    rather than leaking a raw value through the typed contract."""
    if isinstance(value, RadioSignalSource):
        return value
    if isinstance(value, str):
        try:
            return RadioSignalSource(value)
        except ValueError:
            return None
    return None


def resolve_effective_radio_signal(
    radio: object,
    *,
    now: datetime | None = None,
) -> EffectiveRadioSignal:
    """Resolve the displayable RF state for a radio (CPEDevice or duck-typed).

    ``unavailable`` when no observation exists or the radio's UISP status says
    it is not associated (the stored value would describe a dead link);
    ``stale`` when the observation is older than ``RF_SIGNAL_FRESH_TTL``
    (value and timestamp are retained so surfaces can say "last −62 dBm at
    …"); ``fresh`` otherwise.
    """
    current = now or datetime.now(UTC)
    signal = getattr(radio, "rf_signal_dbm", None)
    source = _coerce_source(getattr(radio, "rf_signal_source", None))
    observed_at = _normalize_timestamp(getattr(radio, "rf_signal_observed_at", None))
    status = (getattr(radio, "last_uisp_status", None) or "").strip().lower() or None

    if status in _STATUS_BLOCKS_SIGNAL:
        return EffectiveRadioSignal(
            signal_dbm=None,
            source=None,
            observed_at=None,
            freshness=RadioSignalFreshness.unavailable,
            reason=f"radio_{status}",
        )
    if signal is None or observed_at is None:
        return EffectiveRadioSignal(
            signal_dbm=None,
            source=None,
            observed_at=None,
            freshness=RadioSignalFreshness.unavailable,
            reason="no_observation",
        )
    if current - observed_at > RF_SIGNAL_FRESH_TTL:
        return EffectiveRadioSignal(
            signal_dbm=float(signal),
            source=source,
            observed_at=observed_at,
            freshness=RadioSignalFreshness.stale,
            reason="observation_expired",
        )
    return EffectiveRadioSignal(
        signal_dbm=float(signal),
        source=source,
        observed_at=observed_at,
        freshness=RadioSignalFreshness.fresh,
        reason="observed",
    )
