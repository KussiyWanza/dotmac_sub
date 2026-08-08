"""Unified read-only ONT diagnostics aggregated from OLT/ACS sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.models.network import OLTDevice
from app.services.network.olt_ssh_diagnostics import (
    AlarmEntry,
    OntTrafficStats,
    OpticalInfo,
)
from app.services.network.parsers.cli import canonical_fsp
from app.services.network.parsers.loader import OntInfoEntry

if TYPE_CHECKING:
    from app.services.network.olt_ssh import ServicePortEntry


@dataclass(frozen=True)
class OntDiagnosticSnapshot:
    """Aggregated read-only diagnostic state for one ONT."""

    fsp: str
    ont_id: int
    ont_info: OntInfoEntry | None = None
    optical_info: OpticalInfo | None = None
    traffic_stats: OntTrafficStats | None = None
    service_ports: list[ServicePortEntry] = field(default_factory=list)
    active_alarms: list[AlarmEntry] = field(default_factory=list)
    tr069_state: dict[str, Any] | None = None
    ppp_state: dict[str, Any] | None = None
    offline_reason: str | None = None
    diagnosis: str = "No diagnosis available."
    warnings: list[str] = field(default_factory=list)


def _alarm_matches_ont(alarm: AlarmEntry, *, fsp: str, ont_id: int) -> bool:
    source = " ".join(
        str(value or "")
        for value in [
            alarm.source,
            alarm.name,
            alarm.raw.get("line") if alarm.raw else "",
        ]
    ).lower()
    parts = canonical_fsp(fsp)
    if parts is not None:
        frame, slot, port = parts.frame, parts.slot, parts.port
        fsp_patterns = [
            f"{frame}/{slot}/{port}",
            f"frameid:{frame}",
            f"slotid:{slot}",
            f"portid:{port}",
        ]
        if all(pattern in source.replace(" ", "") for pattern in fsp_patterns[1:]):
            return f"ontid:{ont_id}" in source.replace(" ", "")
        if fsp_patterns[0] in source:
            return str(ont_id) in source
    return f"ont id:{ont_id}" in source or f"ontid:{ont_id}" in source.replace(" ", "")


def _relevant_alarms(
    alarms: list[AlarmEntry],
    *,
    fsp: str,
    ont_id: int,
) -> list[AlarmEntry]:
    matched = [
        alarm for alarm in alarms if _alarm_matches_ont(alarm, fsp=fsp, ont_id=ont_id)
    ]
    return matched or alarms


def _derive_diagnosis(
    *,
    ont_info: OntInfoEntry | None,
    optical_info: OpticalInfo | None,
    traffic_stats: OntTrafficStats | None,
    service_ports: list[ServicePortEntry],
    active_alarms: list[AlarmEntry],
    warnings: list[str],
) -> tuple[str | None, str]:
    if ont_info is not None:
        run_state = str(ont_info.run_state or "").strip().lower()
        if run_state and run_state not in {"online", "up", "working"}:
            reason = ont_info.last_down_cause or ont_info.run_state
            return reason, f"ONT is not online: {reason}."

    relevant_alarm = next(
        (
            alarm
            for alarm in active_alarms
            if any(
                token in f"{alarm.name} {alarm.source}".lower()
                for token in ["offline", "los", "dying", "power"]
            )
        ),
        None,
    )
    if relevant_alarm is not None:
        return relevant_alarm.name, f"Active alarm indicates: {relevant_alarm.name}."

    if optical_info is not None and optical_info.rx_power_dbm is not None:
        if optical_info.rx_power_dbm <= -28:
            return (
                "low_optical_power",
                f"Low ONT RX optical power: {optical_info.rx_power_dbm:.2f} dBm.",
            )

    if not service_ports:
        return "missing_service_ports", "No service ports were found for this ONT."

    if traffic_stats is not None:
        has_traffic = any(
            value and value > 0
            for value in [
                traffic_stats.upstream_bytes,
                traffic_stats.downstream_bytes,
                traffic_stats.upstream_packets,
                traffic_stats.downstream_packets,
            ]
        )
        if not has_traffic:
            return (
                "no_traffic_counters",
                "Service ports exist, but traffic counters are empty.",
            )

    if warnings and ont_info is None:
        return "partial_snapshot", "Snapshot is incomplete; ONT info could not be read."

    return None, "No obvious OLT-side fault detected."


def get_ont_diagnostic_snapshot(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    tr069_state: dict[str, Any] | None = None,
    ppp_state: dict[str, Any] | None = None,
) -> OntDiagnosticSnapshot:
    """Build a read-only diagnostic snapshot for one ONT."""
    from app.services.network import olt_ssh_diagnostics
    from app.services.network.olt_ssh_service_ports import get_service_ports_for_ont

    warnings: list[str] = []

    ok, message, ont_info = olt_ssh_diagnostics.get_ont_info(olt, fsp, ont_id)
    if not ok:
        warnings.append(f"ONT info: {message}")

    ok, message, optical_info = olt_ssh_diagnostics.get_ont_optical_info(
        olt, fsp, ont_id
    )
    if not ok:
        warnings.append(f"Optical info: {message}")

    ok, message, traffic_stats = olt_ssh_diagnostics.get_ont_traffic_stats(
        olt, fsp, ont_id
    )
    if not ok:
        warnings.append(f"Traffic stats: {message}")

    ok, message, service_ports = get_service_ports_for_ont(olt, fsp, ont_id)
    if not ok:
        warnings.append(f"Service ports: {message}")
        service_ports = []

    ok, message, alarms = olt_ssh_diagnostics.get_active_alarms(olt)
    if not ok:
        warnings.append(f"Active alarms: {message}")
        alarms = []
    active_alarms = _relevant_alarms(alarms, fsp=fsp, ont_id=ont_id)

    offline_reason, diagnosis = _derive_diagnosis(
        ont_info=ont_info,
        optical_info=optical_info,
        traffic_stats=traffic_stats,
        service_ports=service_ports,
        active_alarms=active_alarms,
        warnings=warnings,
    )

    return OntDiagnosticSnapshot(
        fsp=fsp,
        ont_id=ont_id,
        ont_info=ont_info,
        optical_info=optical_info,
        traffic_stats=traffic_stats,
        service_ports=service_ports,
        active_alarms=active_alarms,
        tr069_state=tr069_state,
        ppp_state=ppp_state,
        offline_reason=offline_reason,
        diagnosis=diagnosis,
        warnings=warnings,
    )
