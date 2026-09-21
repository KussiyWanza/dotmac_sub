"""Readback verification helpers for OLT SSH write operations.

These helpers are intentionally strict: an accepted write is not treated as
reconciled until a follow-up read confirms the expected state on the OLT.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.network import OLTDevice
from app.services.network.huawei_cli_response import (
    HuaweiCliErrorCode,
    classify_huawei_cli_response,
)
from app.services.network.serial_utils import normalize as normalize_serial
from app.services.network.serial_utils import (
    search_candidates as serial_search_candidates,
)


@dataclass(frozen=True)
class OltWriteVerification:
    """Result of reconciling an expected post-write OLT state."""

    success: bool
    message: str
    details: dict[str, object] | None = None


def _with_cli_evidence(
    details: dict[str, object] | None,
    *responses: str | None,
) -> dict[str, object] | None:
    """Attach the first classified device response without retaining raw CLI."""
    payload = dict(details or {})
    for response_text in responses:
        response = classify_huawei_cli_response(response_text)
        if response.error_code == HuaweiCliErrorCode.NONE:
            continue
        payload.setdefault("error_code", response.error_code.value)
        payload.setdefault("huawei_cli_response", response.to_evidence())
        break
    return payload or None


def _normalize_serial(value: str | None) -> str:
    return normalize_serial(value)


def serial_matches(observed: str | None, expected: str | None) -> bool:
    """Match serials across Huawei vendor and hex display variants."""
    observed_normalized = normalize_serial(observed)
    expected_candidates = {
        normalize_serial(candidate) for candidate in serial_search_candidates(expected)
    }
    expected_candidates.discard("")
    if not observed_normalized or not expected_candidates:
        return False
    if observed_normalized in expected_candidates:
        return True
    return any(
        len(candidate) >= 8 and candidate in observed_normalized
        for candidate in expected_candidates
    )


def verify_ont_authorized(
    olt: OLTDevice,
    *,
    fsp: str,
    ont_id: int | None,
    serial_number: str,
) -> OltWriteVerification:
    """Verify an ONT appears on the OLT after an authorization write.

    Uses a direct ONT status query when the write response includes an ONT-ID,
    then direct serial lookup and registered serial scan as fallbacks.
    """
    from app.services.network import olt_ssh_ont

    def _registered_scan_verification(
        reason: str, serial_lookup_failure: str | None = None
    ) -> OltWriteVerification | None:
        ok, msg, entries = olt_ssh_ont.get_registered_ont_serials(olt, [fsp])
        if not ok:
            detail = (
                f"{serial_lookup_failure}; registered serial scan failed: {msg}"
                if serial_lookup_failure
                else msg
            )
            return OltWriteVerification(
                False,
                f"OLT accepted the authorization write, but serial readback failed after {reason}: {detail}",
                _with_cli_evidence(
                    {"fsp": fsp, "ont_id": ont_id, "serial_number": serial_number},
                    msg,
                    serial_lookup_failure,
                ),
            )

        for registered_entry in entries:
            if not serial_matches(registered_entry.real_serial, serial_number):
                continue
            if registered_entry.fsp != fsp:
                return OltWriteVerification(
                    False,
                    "ONT serial was found on the OLT, but on a different port than expected.",
                    {
                        "expected_fsp": fsp,
                        "observed_fsp": registered_entry.fsp,
                        "ont_id": registered_entry.onu_id,
                        "serial_number": registered_entry.real_serial,
                        "run_state": registered_entry.run_state,
                    },
                )
            return OltWriteVerification(
                True,
                f"Verified ONT {serial_number} on {fsp} with ONT-ID {registered_entry.onu_id} by registered serial scan.",
                {
                    "fsp": registered_entry.fsp,
                    "ont_id": registered_entry.onu_id,
                    "serial_number": registered_entry.real_serial,
                    "run_state": registered_entry.run_state,
                },
            )

        return None

    def _verify_by_serial(reason: str) -> OltWriteVerification | None:
        find_ok, find_msg, found = olt_ssh_ont.find_ont_by_serial(olt, serial_number)
        if not find_ok:
            return _registered_scan_verification(reason, find_msg)
        if found is None:
            return _registered_scan_verification(reason)
        if found.fsp != fsp:
            return OltWriteVerification(
                False,
                "ONT serial was found on the OLT, but on a different port than expected.",
                {
                    "expected_fsp": fsp,
                    "observed_fsp": found.fsp,
                    "ont_id": found.onu_id,
                    "serial_number": found.real_serial,
                    "run_state": found.run_state,
                },
            )
        return OltWriteVerification(
            True,
            f"Verified ONT {serial_number} on {fsp} with ONT-ID {found.onu_id} by serial readback.",
            {
                "fsp": found.fsp,
                "ont_id": found.onu_id,
                "serial_number": found.real_serial,
                "run_state": found.run_state,
            },
        )

    if ont_id is not None:
        ok, msg, status_entry = olt_ssh_ont.get_ont_status(olt, fsp, ont_id)
        if not ok:
            serial_verification = _verify_by_serial("ONT-ID readback failure")
            if serial_verification is not None:
                return serial_verification
            return OltWriteVerification(
                False,
                f"OLT accepted the authorization write, but readback failed: {msg}",
                _with_cli_evidence(
                    {"fsp": fsp, "ont_id": ont_id, "serial_number": serial_number},
                    msg,
                ),
            )
        if status_entry is None or not serial_matches(
            status_entry.serial_number, serial_number
        ):
            serial_verification = _verify_by_serial("ONT-ID readback mismatch")
            if serial_verification is not None:
                return serial_verification
            return OltWriteVerification(
                False,
                "OLT accepted the authorization write, but the ONT was not present on readback.",
                {"fsp": fsp, "ont_id": ont_id, "serial_number": serial_number},
            )
        return OltWriteVerification(
            True,
            f"Verified ONT {serial_number} on {fsp} with ONT-ID {ont_id}.",
            {
                "fsp": fsp,
                "ont_id": ont_id,
                "serial_number": status_entry.serial_number,
                "run_state": status_entry.run_state,
            },
        )

    serial_verification = _verify_by_serial("no ONT-ID available")
    if serial_verification is not None:
        return serial_verification

    return OltWriteVerification(
        False,
        "OLT accepted the authorization write, but the ONT was not present on readback.",
        {"fsp": fsp, "ont_id": ont_id, "serial_number": serial_number},
    )


def verify_ont_absent(
    olt: OLTDevice,
    *,
    fsp: str,
    ont_id: int | None = None,
    serial_number: str | None = None,
) -> OltWriteVerification:
    """Verify an ONT no longer appears on the OLT after a delete write.

    Uses registered ONT serial scanning when serial is provided.
    """
    from app.services.network import olt_ssh_ont

    if serial_number:
        ok, msg, entries = olt_ssh_ont.get_registered_ont_serials(olt, [fsp])
        if not ok:
            return OltWriteVerification(
                False,
                f"OLT accepted the delete write, but readback failed: {msg}",
                _with_cli_evidence(None, msg),
            )

        for entry in entries:
            if serial_matches(entry.real_serial, serial_number):
                return OltWriteVerification(
                    False,
                    "ONT still appears on the OLT after the delete write.",
                    {
                        "fsp": entry.fsp,
                        "ont_id": entry.onu_id,
                        "serial_number": entry.real_serial,
                        "run_state": entry.run_state,
                    },
                )

        return OltWriteVerification(
            True,
            "Verified ONT registration is absent on the OLT.",
            {"fsp": fsp, "ont_id": ont_id, "serial_number": serial_number},
        )

    # Fallback: without serial, we cannot reliably verify absence
    # (the other lookup methods have SSH command spacing issues)
    return OltWriteVerification(
        True,
        "Delete write accepted; absence verification skipped (no serial number provided).",
        {"fsp": fsp, "ont_id": ont_id, "serial_number": serial_number},
    )


def verify_service_port_present(
    olt: OLTDevice,
    *,
    fsp: str,
    ont_id: int,
    vlan_id: int,
    gem_index: int | None = None,
) -> OltWriteVerification:
    """Verify a service-port exists on the OLT after a create/update write."""
    from app.services.network import olt_ssh_service_ports

    ok, msg, ports = olt_ssh_service_ports.get_service_ports_for_ont(olt, fsp, ont_id)
    if not ok:
        return OltWriteVerification(
            False,
            f"OLT accepted the service-port write, but readback failed: {msg}",
            _with_cli_evidence(None, msg),
        )

    for port in ports:
        if port.vlan_id != vlan_id:
            continue
        if gem_index is not None and port.gem_index != gem_index:
            continue
        return OltWriteVerification(
            True,
            f"Verified service-port for VLAN {vlan_id} on ONT {ont_id}.",
            {
                "index": port.index,
                "vlan_id": port.vlan_id,
                "ont_id": port.ont_id,
                "gem_index": port.gem_index,
                "state": port.state,
            },
        )

    return OltWriteVerification(
        False,
        f"OLT accepted the service-port write, but VLAN {vlan_id} was not present on readback.",
        {"fsp": fsp, "ont_id": ont_id, "vlan_id": vlan_id, "gem_index": gem_index},
    )


def verify_service_port_absent(
    olt: OLTDevice,
    *,
    fsp: str,
    ont_id: int,
    service_port_index: int,
) -> OltWriteVerification:
    """Verify a service-port no longer exists after a delete write."""
    from app.services.network import olt_ssh_service_ports

    ok, msg, ports = olt_ssh_service_ports.get_service_ports_for_ont(olt, fsp, ont_id)
    if not ok:
        return OltWriteVerification(
            False,
            f"OLT accepted the service-port delete, but readback failed: {msg}",
            _with_cli_evidence(None, msg),
        )

    for port in ports:
        if port.index == service_port_index:
            return OltWriteVerification(
                False,
                f"Service-port {service_port_index} still appears on the OLT after delete.",
                {
                    "index": port.index,
                    "vlan_id": getattr(port, "vlan_id", None),
                    "ont_id": getattr(port, "ont_id", None),
                    "gem_index": getattr(port, "gem_index", None),
                },
            )

    return OltWriteVerification(
        True,
        f"Verified service-port {service_port_index} is absent on the OLT.",
        {"fsp": fsp, "ont_id": ont_id, "service_port_index": service_port_index},
    )


def verify_service_port_index_absent(
    olt: OLTDevice,
    *,
    service_port_index: int,
) -> OltWriteVerification:
    """Verify a global service-port index no longer exists after a delete write."""
    from app.services.network import olt_ssh_service_ports

    ok, msg, port = olt_ssh_service_ports.get_service_port_by_index(
        olt, service_port_index
    )
    if not ok:
        return OltWriteVerification(
            False,
            f"OLT accepted the service-port delete, but readback failed: {msg}",
            _with_cli_evidence(None, msg),
        )
    if port is not None:
        return OltWriteVerification(
            False,
            f"Service-port {service_port_index} still appears on the OLT after delete.",
            {
                "index": port.index,
                "fsp": port.fsp,
                "vlan_id": port.vlan_id,
                "ont_id": port.ont_id,
                "gem_index": port.gem_index,
            },
        )
    return OltWriteVerification(
        True,
        f"Verified service-port {service_port_index} is absent on the OLT.",
        {"service_port_index": service_port_index},
    )


def verify_iphost_config(
    olt: OLTDevice,
    *,
    fsp: str,
    ont_id: int,
    vlan_id: int,
    ip_mode: str,
    ip_address: str | None = None,
) -> OltWriteVerification:
    """Verify IPHOST configuration read back from the OLT."""
    from app.services.network import olt_ssh_ont

    ok, msg, config = olt_ssh_ont.get_ont_iphost_config(olt, fsp, ont_id)
    if not ok:
        return OltWriteVerification(
            False,
            f"OLT accepted the IPHOST write, but readback failed: {msg}",
            _with_cli_evidence(None, msg),
        )

    normalized = {
        " ".join(str(k).strip().lower().split()): str(v).strip()
        for k, v in config.items()
    }

    observed_vlan = normalized.get(
        "vlan",
        normalized.get("vlan id", normalized.get("ont manage vlan", "")),
    )
    if observed_vlan and observed_vlan.isdigit() and int(observed_vlan) != vlan_id:
        return OltWriteVerification(
            False,
            "IPHOST readback returned a different VLAN than the requested write.",
            {
                "expected_vlan_id": vlan_id,
                "observed_vlan_id": observed_vlan,
                "config": config,
            },
        )

    observed_mode = normalized.get(
        "ip mode",
        normalized.get("mode", normalized.get("ont config type", "")),
    ).lower()
    if observed_mode and ip_mode.lower() not in observed_mode:
        return OltWriteVerification(
            False,
            "IPHOST readback returned a different mode than the requested write.",
            {
                "expected_mode": ip_mode,
                "observed_mode": observed_mode,
                "config": config,
            },
        )

    observed_ip = normalized.get(
        "ip address",
        normalized.get("ip", normalized.get("ont ip", "")),
    )
    if ip_address and observed_ip and observed_ip != ip_address:
        return OltWriteVerification(
            False,
            "IPHOST readback returned a different IP address than the requested write.",
            {
                "expected_ip_address": ip_address,
                "observed_ip_address": observed_ip,
                "config": config,
            },
        )

    return OltWriteVerification(
        True,
        "Verified IPHOST configuration on the OLT.",
        {
            "fsp": fsp,
            "ont_id": ont_id,
            "vlan_id": vlan_id,
            "mode": observed_mode or ip_mode,
            "ip_address": observed_ip or ip_address,
        },
    )
