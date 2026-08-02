"""radio_signal: typed freshness contract for wireless RF observations.

Pure-object tests (no DB): resolve_effective_radio_signal reads plain
attributes, mirroring ont_status.resolve_effective_ont_status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.services.network.radio_signal import (
    RF_SIGNAL_FRESH_TTL,
    RadioSignalFreshness,
    RadioSignalSource,
    resolve_effective_radio_signal,
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


@dataclass
class _Radio:
    rf_signal_dbm: float | None = -62.0
    rf_signal_source: str | None = RadioSignalSource.uisp_ap_station.value
    rf_signal_observed_at: datetime | None = field(
        default_factory=lambda: NOW - timedelta(minutes=5)
    )
    last_uisp_status: str | None = "active"


def test_fresh_observation():
    out = resolve_effective_radio_signal(_Radio(), now=NOW)
    assert out.freshness is RadioSignalFreshness.fresh
    assert out.is_fresh
    assert out.signal_dbm == -62.0
    assert out.source == RadioSignalSource.uisp_ap_station.value
    assert out.reason == "observed"


def test_stale_after_ttl_retains_value_and_timestamp():
    observed = NOW - RF_SIGNAL_FRESH_TTL - timedelta(minutes=1)
    out = resolve_effective_radio_signal(
        _Radio(rf_signal_observed_at=observed), now=NOW
    )
    assert out.freshness is RadioSignalFreshness.stale
    assert not out.is_fresh
    # Value and timestamp survive so surfaces can say "last -62 dBm at ...".
    assert out.signal_dbm == -62.0
    assert out.observed_at == observed
    assert out.reason == "observation_expired"


def test_exactly_at_ttl_is_still_fresh():
    observed = NOW - RF_SIGNAL_FRESH_TTL
    out = resolve_effective_radio_signal(
        _Radio(rf_signal_observed_at=observed), now=NOW
    )
    assert out.freshness is RadioSignalFreshness.fresh


def test_unavailable_when_no_observation():
    out = resolve_effective_radio_signal(
        _Radio(rf_signal_dbm=None, rf_signal_observed_at=None), now=NOW
    )
    assert out.freshness is RadioSignalFreshness.unavailable
    assert out.signal_dbm is None
    assert out.reason == "no_observation"


def test_unavailable_when_value_present_but_timestamp_missing():
    out = resolve_effective_radio_signal(_Radio(rf_signal_observed_at=None), now=NOW)
    assert out.freshness is RadioSignalFreshness.unavailable
    assert out.signal_dbm is None


def test_status_guard_blocks_retained_value():
    # Read-side guard: even if a sync-side clear was missed, a value stored on
    # a disconnected/missing/vanished radio must never render.
    for status in ("disconnected", "missing", "vanished", "Disconnected"):
        out = resolve_effective_radio_signal(_Radio(last_uisp_status=status), now=NOW)
        assert out.freshness is RadioSignalFreshness.unavailable, status
        assert out.signal_dbm is None
        assert out.reason == f"radio_{status.lower()}"


def test_unauthorized_status_does_not_block():
    # Unauthorized = associated but refused; the RF observation is real.
    out = resolve_effective_radio_signal(
        _Radio(last_uisp_status="unauthorized"), now=NOW
    )
    assert out.freshness is RadioSignalFreshness.fresh


def test_naive_timestamp_is_treated_as_utc():
    naive = (NOW - timedelta(minutes=5)).replace(tzinfo=None)
    out = resolve_effective_radio_signal(_Radio(rf_signal_observed_at=naive), now=NOW)
    assert out.freshness is RadioSignalFreshness.fresh
    assert out.observed_at is not None and out.observed_at.tzinfo is not None
