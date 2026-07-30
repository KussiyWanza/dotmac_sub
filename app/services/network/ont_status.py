"""ONT state helpers and the shared binary runtime-status projection."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from math import ceil

from sqlalchemy import and_, func, not_, or_
from sqlalchemy.orm import Session

from app.models.network import (
    OntAuthorizationStatus,
    OntProvisioningStatus,
    OntStatusSource,
    OntUnit,
    OnuOfflineReason,
    OnuOnlineStatus,
)

DEFAULT_ACS_ONLINE_WINDOW_MINUTES = 15
ACS_INFORM_GRACE_MINUTES = 5
ONT_STATUS_FRESH_SECONDS = 1800

logger = logging.getLogger(__name__)

_AUTHORIZATION_TRANSITIONS: dict[
    OntAuthorizationStatus | None, set[OntAuthorizationStatus]
] = {
    None: {
        OntAuthorizationStatus.pending,
        OntAuthorizationStatus.authorized,
        OntAuthorizationStatus.failed,
    },
    OntAuthorizationStatus.pending: {
        OntAuthorizationStatus.authorized,
        OntAuthorizationStatus.deauthorized,
        OntAuthorizationStatus.failed,
    },
    OntAuthorizationStatus.authorized: {
        OntAuthorizationStatus.pending,
        OntAuthorizationStatus.deauthorized,
        OntAuthorizationStatus.failed,
    },
    OntAuthorizationStatus.deauthorized: {
        OntAuthorizationStatus.pending,
        OntAuthorizationStatus.authorized,
        OntAuthorizationStatus.failed,
    },
    OntAuthorizationStatus.failed: {
        OntAuthorizationStatus.pending,
        OntAuthorizationStatus.authorized,
        OntAuthorizationStatus.deauthorized,
    },
}

_PROVISIONING_TRANSITIONS: dict[
    OntProvisioningStatus | None, set[OntProvisioningStatus]
] = {
    None: {
        OntProvisioningStatus.unprovisioned,
        OntProvisioningStatus.partial,
        OntProvisioningStatus.pending_acs_registration,
        OntProvisioningStatus.pending_service_config,
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.drift_detected,
        OntProvisioningStatus.failed,
    },
    OntProvisioningStatus.unprovisioned: {
        OntProvisioningStatus.partial,
        OntProvisioningStatus.pending_acs_registration,
        OntProvisioningStatus.pending_service_config,
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.failed,
    },
    OntProvisioningStatus.partial: {
        OntProvisioningStatus.pending_acs_registration,
        OntProvisioningStatus.pending_service_config,
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.drift_detected,
        OntProvisioningStatus.failed,
        OntProvisioningStatus.unprovisioned,
    },
    OntProvisioningStatus.pending_acs_registration: {
        OntProvisioningStatus.partial,
        OntProvisioningStatus.pending_service_config,
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.drift_detected,
        OntProvisioningStatus.failed,
        OntProvisioningStatus.unprovisioned,
    },
    OntProvisioningStatus.pending_service_config: {
        OntProvisioningStatus.partial,
        OntProvisioningStatus.pending_acs_registration,
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.drift_detected,
        OntProvisioningStatus.failed,
        OntProvisioningStatus.unprovisioned,
    },
    OntProvisioningStatus.provisioned: {
        OntProvisioningStatus.partial,
        OntProvisioningStatus.drift_detected,
        OntProvisioningStatus.failed,
        OntProvisioningStatus.unprovisioned,
    },
    OntProvisioningStatus.drift_detected: {
        OntProvisioningStatus.partial,
        OntProvisioningStatus.pending_acs_registration,
        OntProvisioningStatus.pending_service_config,
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.failed,
        OntProvisioningStatus.unprovisioned,
    },
    OntProvisioningStatus.failed: {
        OntProvisioningStatus.partial,
        OntProvisioningStatus.pending_acs_registration,
        OntProvisioningStatus.pending_service_config,
        OntProvisioningStatus.unprovisioned,
        OntProvisioningStatus.provisioned,
    },
}


@dataclass(frozen=True)
class OntStatusSnapshot:
    olt_status: OnuOnlineStatus
    olt_status_seen_at: datetime | None
    acs_last_inform_at: datetime | None
    last_seen_at: datetime | None


@dataclass(frozen=True)
class OntStateReconciliationResult:
    """Outcome of reconciling OLT and ACS status observations."""

    ont_id: uuid.UUID
    snapshot: OntStatusSnapshot
    conflict: bool
    reason: str
    authoritative_source: OntStatusSource
    recommended_action: str | None = None


class StatusProviderMode(str, Enum):
    auto = "auto"
    snmp = "snmp"
    tr069 = "tr069"


@dataclass(frozen=True)
class OpticalMetrics:
    olt_rx_dbm: float | None = None
    onu_rx_dbm: float | None = None
    onu_tx_dbm: float | None = None
    temperature_c: float | None = None
    voltage_v: float | None = None
    bias_current_ma: float | None = None
    distance_m: int | None = None
    source: str = "persisted"
    fetched_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "olt_rx_dbm": self.olt_rx_dbm,
            "onu_rx_dbm": self.onu_rx_dbm,
            "onu_tx_dbm": self.onu_tx_dbm,
            "temperature_c": self.temperature_c,
            "voltage_v": self.voltage_v,
            "bias_current_ma": self.bias_current_ma,
            "distance_m": self.distance_m,
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }

    @property
    def has_signal_data(self) -> bool:
        return any(
            value is not None
            for value in (self.olt_rx_dbm, self.onu_rx_dbm, self.onu_tx_dbm)
        )


@dataclass(frozen=True)
class OntStatusResult:
    status: OnuOnlineStatus
    status_source: OntStatusSource
    acs_last_inform_at: datetime | None = None
    resolved_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    optical_metrics: OpticalMetrics | None = None
    error: str | None = None
    reason: str | None = None
    retry_pending: bool = False

    @property
    def is_online(self) -> bool:
        return self.status == OnuOnlineStatus.online

    @property
    def success(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class EffectiveOntStatus:
    """Binary operator state resolved from OLT, ACS and last-seen evidence."""

    status: OnuOnlineStatus
    source: OntStatusSource
    reason: str
    retry_pending: bool = False

    @property
    def is_online(self) -> bool:
        return self.status == OnuOnlineStatus.online


def _normalize_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _normalize_olt_status(value: OnuOnlineStatus | str | None) -> OnuOnlineStatus:
    if isinstance(value, OnuOnlineStatus):
        return value
    value = getattr(value, "value", value)
    if isinstance(value, str):
        try:
            return OnuOnlineStatus(value)
        except ValueError:
            return OnuOnlineStatus.offline
    return OnuOnlineStatus.offline


def _is_fresh(value: datetime | None, now: datetime, seconds: int) -> bool:
    value = _normalize_timestamp(value)
    return value is not None and now - value <= timedelta(seconds=seconds)


def resolve_effective_ont_status(
    ont: OntUnit | object,
    *,
    now: datetime | None = None,
) -> EffectiveOntStatus:
    """Return exactly online/offline while distinguishing telemetry retries.

    Positive evidence from either OLT or ACS wins. If evidence ages out, the
    last confirmed binary state is retained and marked retry-pending; only a
    fresh explicit OLT-offline observation is authoritative negative evidence.
    """
    current = now or datetime.now(UTC)
    raw_status = _normalize_olt_status(getattr(ont, "olt_status", None))
    olt_seen = _normalize_timestamp(getattr(ont, "olt_status_seen_at", None))
    acs_seen = _normalize_timestamp(getattr(ont, "acs_last_inform_at", None))
    last_seen = _normalize_timestamp(getattr(ont, "last_seen_at", None))

    if raw_status == OnuOnlineStatus.online and _is_fresh(
        olt_seen, current, ONT_STATUS_FRESH_SECONDS
    ):
        return EffectiveOntStatus(raw_status, OntStatusSource.olt, "olt_online")
    if _is_fresh(acs_seen, current, ONT_STATUS_FRESH_SECONDS):
        return EffectiveOntStatus(
            OnuOnlineStatus.online, OntStatusSource.acs, "acs_inform_recent"
        )
    if _is_fresh(last_seen, current, ONT_STATUS_FRESH_SECONDS):
        return EffectiveOntStatus(
            OnuOnlineStatus.online, OntStatusSource.derived, "seen_recent"
        )
    if raw_status == OnuOnlineStatus.offline and olt_seen is not None:
        reason = _normalize_offline_reason(getattr(ont, "offline_reason", None))
        return EffectiveOntStatus(
            OnuOnlineStatus.offline,
            OntStatusSource.olt,
            reason,
            retry_pending=not _is_fresh(olt_seen, current, ONT_STATUS_FRESH_SECONDS),
        )
    if raw_status == OnuOnlineStatus.online:
        return EffectiveOntStatus(
            OnuOnlineStatus.online,
            OntStatusSource.olt,
            "last_confirmed_online_retry_pending",
            retry_pending=True,
        )
    return EffectiveOntStatus(
        OnuOnlineStatus.offline,
        OntStatusSource.derived,
        "never_seen_retry_pending",
        retry_pending=True,
    )


def effective_ont_online_clause(*, now: datetime | None = None):
    """SQL equivalent of the positive/retained-online projection."""
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=ONT_STATUS_FRESH_SECONDS)
    return or_(
        OntUnit.olt_status == OnuOnlineStatus.online,
        OntUnit.acs_last_inform_at >= cutoff,
        OntUnit.last_seen_at >= cutoff,
    )


def operational_ont_working_clause(*, now: datetime | None = None):
    """SQL equivalent of a currently confirmed ``working`` ONT outcome."""

    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=ONT_STATUS_FRESH_SECONDS)
    return or_(
        and_(
            OntUnit.olt_status == OnuOnlineStatus.online,
            OntUnit.olt_status_seen_at >= cutoff,
        ),
        OntUnit.acs_last_inform_at >= cutoff,
        OntUnit.last_seen_at >= cutoff,
    )


def operational_ont_not_working_clause(*, now: datetime | None = None):
    """Null-safe SQL complement of the confirmed-working ONT outcome."""

    return not_(func.coalesce(operational_ont_working_clause(now=now), False))


def _normalize_offline_reason(value: OnuOfflineReason | str | None) -> str:
    normalized = getattr(value, "value", value)
    return str(normalized or "observed_down")


def _coerce_auth_status(
    status: OntAuthorizationStatus | str,
) -> OntAuthorizationStatus:
    if isinstance(status, OntAuthorizationStatus):
        return status
    return OntAuthorizationStatus(str(status))


def _coerce_provisioning_status(
    status: OntProvisioningStatus | str,
) -> OntProvisioningStatus:
    if isinstance(status, OntProvisioningStatus):
        return status
    return OntProvisioningStatus(str(status))


def set_authorization_status(
    ont: OntUnit,
    status: OntAuthorizationStatus | str,
    *,
    strict: bool = True,
) -> None:
    next_status = _coerce_auth_status(status)
    current = ont.authorization_status
    if current == next_status:
        return
    allowed = _AUTHORIZATION_TRANSITIONS.get(current, set())
    is_valid_transition = next_status in allowed
    if not is_valid_transition:
        message = (
            f"Illegal ONT authorization status transition: "
            f"{current.value if current else 'none'} -> {next_status.value}"
        )
        if strict:
            raise ValueError(message)
        logger.warning(message)
    logger.info(
        "ont_status_transition",
        extra={
            "event": "ont_status_transition",
            "ont_id": str(ont.id),
            "field": "authorization_status",
            "from": current.value if current else None,
            "to": next_status.value,
            "valid": is_valid_transition,
        },
    )
    ont.authorization_status = next_status


def set_provisioning_status(
    ont: OntUnit,
    status: OntProvisioningStatus | str,
    *,
    strict: bool = True,
) -> None:
    next_status = _coerce_provisioning_status(status)
    current = ont.provisioning_status
    if current == next_status:
        return
    allowed = _PROVISIONING_TRANSITIONS.get(current, set())
    is_valid_transition = next_status in allowed
    if not is_valid_transition:
        message = (
            f"Illegal ONT provisioning status transition: "
            f"{current.value if current else 'none'} -> {next_status.value}"
        )
        if strict:
            raise ValueError(message)
        logger.warning(message)
    logger.info(
        "ont_status_transition",
        extra={
            "event": "ont_status_transition",
            "ont_id": str(ont.id),
            "field": "provisioning_status",
            "from": current.value if current else None,
            "to": next_status.value,
            "valid": is_valid_transition,
        },
    )
    ont.provisioning_status = next_status


def clear_authorization_status(ont: OntUnit, *, reason: str) -> None:
    """Reset ``authorization_status`` to ``None`` (inventory reset / decommission).

    A deliberate CLEAR, not a transition — there is no legal transition-to-None,
    so this is audited but not validated. Routing the reset/decommission paths
    through here keeps every authorization_status write observable in one place.
    """
    current = ont.authorization_status
    if current is None:
        return
    logger.info(
        "ont_status_transition",
        extra={
            "event": "ont_status_transition",
            "ont_id": str(ont.id),
            "field": "authorization_status",
            "from": current.value if current else None,
            "to": None,
            "valid": True,
            "reason": reason,
        },
    )
    ont.authorization_status = None


def clear_provisioning_status(ont: OntUnit, *, reason: str) -> None:
    """Reset ``provisioning_status`` to ``None`` (decommission). Audited clear."""
    current = ont.provisioning_status
    if current is None:
        return
    logger.info(
        "ont_status_transition",
        extra={
            "event": "ont_status_transition",
            "ont_id": str(ont.id),
            "field": "provisioning_status",
            "from": current.value if current else None,
            "to": None,
            "valid": True,
            "reason": reason,
        },
    )
    ont.provisioning_status = None


def _window_minutes_from_interval_seconds(interval_seconds: int | None) -> int:
    if not interval_seconds or interval_seconds <= 0:
        return DEFAULT_ACS_ONLINE_WINDOW_MINUTES
    interval_minutes = ceil(interval_seconds / 60)
    return max(
        DEFAULT_ACS_ONLINE_WINDOW_MINUTES,
        interval_minutes + ACS_INFORM_GRACE_MINUTES,
    )


def resolve_acs_online_window_minutes_for_model(ont: OntUnit) -> int:
    interval_seconds = None
    acs_server = getattr(ont, "tr069_acs_server", None)
    if acs_server is not None:
        interval_seconds = getattr(acs_server, "periodic_inform_interval", None)

    if interval_seconds is None:
        olt = getattr(ont, "olt_device", None)
        if olt is not None:
            olt_acs_server = getattr(olt, "tr069_acs_server", None)
            if olt_acs_server is not None:
                interval_seconds = getattr(
                    olt_acs_server, "periodic_inform_interval", None
                )

    return _window_minutes_from_interval_seconds(interval_seconds)


def ont_has_acs_management(
    ont: OntUnit,
    *,
    acs_last_inform_at: datetime | None = None,
) -> bool:
    if getattr(ont, "tr069_acs_server_id", None) or getattr(
        ont, "tr069_acs_server", None
    ):
        return True
    olt = getattr(ont, "olt_device", None)
    if olt is not None and (
        getattr(olt, "tr069_acs_server_id", None)
        or getattr(olt, "tr069_acs_server", None)
    ):
        return True
    return bool(acs_last_inform_at or getattr(ont, "acs_last_inform_at", None))


def resolve_effective_last_seen_at(
    ont: OntUnit | object,
    *,
    acs_last_inform_at: datetime | None = None,
) -> datetime | None:
    candidates = [
        _normalize_timestamp(getattr(ont, "last_seen_at", None)),
        _normalize_timestamp(getattr(ont, "olt_status_seen_at", None)),
        _normalize_timestamp(
            acs_last_inform_at
            if acs_last_inform_at is not None
            else getattr(ont, "acs_last_inform_at", None)
        ),
    ]
    present = [value for value in candidates if value is not None]
    return max(present) if present else None


def resolve_ont_status_for_model(
    ont: OntUnit,
    *,
    acs_last_inform_at: datetime | None = None,
    now: datetime | None = None,
    online_window_minutes: int | None = None,
) -> OntStatusSnapshot:
    effective_inform = (
        acs_last_inform_at
        if acs_last_inform_at is not None
        else getattr(ont, "acs_last_inform_at", None)
    )
    _ = now, online_window_minutes
    olt_status = _normalize_olt_status(getattr(ont, "olt_status", None))
    olt_seen_at = _normalize_timestamp(getattr(ont, "olt_status_seen_at", None))
    acs_seen_at = _normalize_timestamp(effective_inform)
    last_seen = max(
        [
            value
            for value in [
                olt_seen_at if olt_status == OnuOnlineStatus.online else None,
                acs_seen_at,
            ]
            if value is not None
        ],
        default=None,
    )
    return OntStatusSnapshot(
        olt_status=olt_status,
        olt_status_seen_at=olt_seen_at,
        acs_last_inform_at=acs_seen_at,
        last_seen_at=last_seen,
    )


def resolve_ont_status_snapshot(
    *,
    olt_status: OnuOnlineStatus | str | None,
    acs_last_inform_at: datetime | None,
    now: datetime | None = None,
    online_window_minutes: int = DEFAULT_ACS_ONLINE_WINDOW_MINUTES,
) -> OntStatusSnapshot:
    current = now or datetime.now(UTC)
    _ = online_window_minutes
    normalized_olt = _normalize_olt_status(olt_status)
    olt_seen_at = current if normalized_olt == OnuOnlineStatus.online else None
    acs_seen_at = _normalize_timestamp(acs_last_inform_at)
    last_seen = max(
        [value for value in [olt_seen_at, acs_seen_at] if value is not None],
        default=None,
    )
    return OntStatusSnapshot(
        olt_status=normalized_olt,
        olt_status_seen_at=olt_seen_at,
        acs_last_inform_at=acs_seen_at,
        last_seen_at=last_seen,
    )


def apply_status_snapshot(ont: OntUnit, snapshot: OntStatusSnapshot) -> OntUnit:
    """Persist raw OLT/ACS observation timestamps without combined status writes."""
    ont.olt_status = snapshot.olt_status
    ont.olt_status_seen_at = snapshot.olt_status_seen_at
    ont.acs_last_inform_at = snapshot.acs_last_inform_at
    if snapshot.last_seen_at is not None:
        ont.last_seen_at = snapshot.last_seen_at
    return ont


def apply_resolved_status_for_model(
    ont: OntUnit,
    *,
    now: datetime | None = None,
) -> OntUnit:
    """Persist raw status timestamps from current observations."""
    snapshot = resolve_ont_status_for_model(ont, now=now)
    return apply_status_snapshot(ont, snapshot)


def reset_status_for_inventory(ont: OntUnit) -> None:
    """Clear persisted status observations for an ONT returned to inventory."""
    ont.olt_status = OnuOnlineStatus.offline
    ont.olt_status_seen_at = None
    ont.acs_last_inform_at = None
    ont.last_seen_at = None
    ont.offline_reason = None


def apply_olt_status_observation(
    ont: OntUnit,
    olt_status: OnuOnlineStatus,
    offline_reason: OnuOfflineReason | None = None,
    *,
    now: datetime | None = None,
) -> OntStatusSnapshot:
    """Apply a raw OLT diagnostic/status observation to an ONT."""
    current = now or datetime.now(UTC)
    normalized_status = _normalize_olt_status(olt_status)
    ont.olt_status = normalized_status
    ont.olt_status_seen_at = current

    if normalized_status == OnuOnlineStatus.online:
        ont.last_seen_at = current
        ont.offline_reason = None
    else:
        ont.offline_reason = offline_reason or OnuOfflineReason.unknown

    return resolve_ont_status_snapshot(
        olt_status=normalized_status,
        acs_last_inform_at=getattr(ont, "acs_last_inform_at", None),
        now=current,
    )


def apply_status_with_hysteresis(
    ont: OntUnit,
    polled_status: OnuOnlineStatus,
    offline_reason: OnuOfflineReason | None = None,
    *,
    now: datetime | None = None,
) -> None:
    apply_olt_status_observation(ont, polled_status, offline_reason, now=now)


def apply_acs_inform_observation(
    ont: OntUnit,
    *,
    acs_last_inform_at: datetime | None = None,
    now: datetime | None = None,
) -> OntStatusSnapshot:
    current = now or datetime.now(UTC)
    ont.acs_last_inform_at = _normalize_timestamp(acs_last_inform_at) or current
    if ont.acs_last_inform_at is not None:
        ont.last_seen_at = ont.acs_last_inform_at
    return resolve_ont_status_snapshot(
        olt_status=getattr(ont, "olt_status", None),
        acs_last_inform_at=ont.acs_last_inform_at,
        now=current,
    )


def update_ont_acs_status_from_inform(
    db: Session,
    ont: OntUnit,
    *,
    informed_at: datetime | None = None,
    commit: bool = False,
) -> OntStatusSnapshot:
    snapshot = apply_acs_inform_observation(ont, acs_last_inform_at=informed_at)
    if commit:
        db.commit()
    return snapshot


def reconcile_ont_state(
    ont: OntUnit, *, now: datetime | None = None
) -> OntStateReconciliationResult:
    snapshot = resolve_ont_status_for_model(ont, now=now)
    conflict = False
    return OntStateReconciliationResult(
        ont_id=ont.id,
        snapshot=snapshot,
        conflict=conflict,
        reason="native_status_projection",
        authoritative_source=resolve_effective_ont_status(ont, now=now).source,
        recommended_action=None,
    )


def _optical_metrics_from_ont(ont: OntUnit) -> OpticalMetrics:
    return OpticalMetrics(
        olt_rx_dbm=getattr(ont, "olt_rx_signal_dbm", None),
        onu_rx_dbm=getattr(ont, "onu_rx_signal_dbm", None),
        onu_tx_dbm=getattr(ont, "onu_tx_signal_dbm", None),
        temperature_c=getattr(ont, "ont_temperature_c", None),
        voltage_v=getattr(ont, "ont_voltage_v", None),
        bias_current_ma=getattr(ont, "ont_bias_current_ma", None),
        distance_m=getattr(ont, "distance_meters", None),
        fetched_at=getattr(ont, "signal_updated_at", None),
    )


def get_ont_status(
    db: Session,
    ont: OntUnit,
    *,
    include_optical: bool = False,
    mode: StatusProviderMode | str = StatusProviderMode.auto,
) -> OntStatusResult:
    _ = db, mode
    effective = resolve_effective_ont_status(ont)
    return OntStatusResult(
        status=effective.status,
        status_source=effective.source,
        acs_last_inform_at=getattr(ont, "acs_last_inform_at", None),
        resolved_at=datetime.now(UTC),
        optical_metrics=_optical_metrics_from_ont(ont) if include_optical else None,
        reason=effective.reason,
        retry_pending=effective.retry_pending,
    )


def get_optical_metrics(
    db: Session,
    ont: OntUnit,
    *,
    mode: StatusProviderMode | str = StatusProviderMode.auto,
) -> OpticalMetrics:
    _ = db, mode
    return _optical_metrics_from_ont(ont)


def refresh_ont_status(
    db: Session,
    ont: OntUnit,
    *,
    mode: StatusProviderMode | str = StatusProviderMode.auto,
) -> OntStatusResult:
    _ = mode
    try:
        from app.services.network.ont_runtime_status import refresh_single_ont_status

        refresh_single_ont_status(db, ont)
    except Exception as exc:
        logger.warning(
            "ONT status refresh failed for %s; retaining confirmed state: %s",
            getattr(ont, "id", None),
            exc,
        )
        effective = resolve_effective_ont_status(ont)
        return OntStatusResult(
            status=effective.status,
            status_source=effective.source,
            acs_last_inform_at=getattr(ont, "acs_last_inform_at", None),
            resolved_at=datetime.now(UTC),
            error=str(exc),
            reason=effective.reason,
            retry_pending=True,
        )
    return get_ont_status(db, ont)


def ont_status_summary(
    db: Session, *, low_signal_threshold_dbm: float = -25
) -> dict[str, int]:
    """Fleet ONT operational counts from locally persisted observations.

    Canonical owner of the overview's working/not-working/low-signal counts.
    Observation age is evaluated inside ``operational_ont_working_clause``;
    consumers do not retain or relabel an expired online observation.
    """
    counts = (
        db.query(
            func.count(OntUnit.id).label("total"),
            func.count(OntUnit.id)
            .filter(operational_ont_working_clause())
            .label("working"),
            func.count(OntUnit.id)
            .filter(OntUnit.olt_rx_signal_dbm.is_not(None))
            .filter(OntUnit.olt_rx_signal_dbm < low_signal_threshold_dbm)
            .label("low_signal"),
        )
        .filter(OntUnit.is_active.is_(True))
        .one()
    )
    total = counts.total or 0
    working = counts.working or 0
    return {
        "total": total,
        "working": working,
        "not_working": max(total - working, 0),
        "low_signal": counts.low_signal or 0,
    }
