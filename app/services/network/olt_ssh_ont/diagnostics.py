"""ONT diagnostics functions (service port diagnostics, remote ping) via OLT SSH."""

from __future__ import annotations

import logging
import re

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network.olt_ssh_ont._common import (
    _SSH_CONNECTION_ERRORS,
    ServicePortDiagnostics,
    invalid_fsp_message,
)
from app.services.network.olt_validators import (
    ValidationError,
    validate_ip_address,
    validate_ont_id,
)
from app.services.network.parsers.cli import canonical_fsp

logger = logging.getLogger(__name__)


def remote_ping_ont(
    olt: OLTDevice, fsp: str, ont_id: int, ip_address: str
) -> tuple[bool, str]:
    """Initiate a ping from the ONT itself via OMCI remote-ping."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    # SECURITY: Validate IP address before CLI interpolation to prevent injection
    try:
        ip_address = validate_ip_address(ip_address, "ip_address")
        validate_ont_id(ont_id)
    except ValidationError as e:
        return False, e.message

    parts = canonical_fsp(fsp)
    if parts is None:
        return False, invalid_fsp_message(fsp)
    frame_slot = parts.frame_slot
    port_num = parts.port
    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )

        cmd = f"ont remote-ping {port_num} {ont_id} ip-address {ip_address}"
        channel.send(f"{cmd}\n")
        output = core._read_until_prompt(channel, config_prompt, timeout_sec=30)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "Remote ping failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-200:],
            )
            return False, f"Ping failed: {output.strip()[-200:]}"

        logger.info(
            "Remote ping from ONT %d on OLT %s to %s", ont_id, olt.name, ip_address
        )
        return True, f"Remote ping to {ip_address}: {output.strip()[-200:]}"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error running remote ping on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def diagnose_service_ports(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
) -> tuple[bool, str, ServicePortDiagnostics | None]:
    """Run diagnostic commands to troubleshoot service port state issues.

    Runs multiple OLT commands in a single SSH session to diagnose why
    service ports may show as Down:
    - display ont info: Check ONT online/offline state
    - display ont port state: Check GEM port status
    - display service-port: Check service port details

    Args:
        olt: OLT device to connect to.
        fsp: Frame/Slot/Port (e.g., "0/2/1").
        ont_id: ONT ID on the OLT.

    Returns:
        (success, message, diagnostics) tuple.
    """
    from app.services.network import olt_ssh as core

    parts = canonical_fsp(fsp)
    if parts is None:
        return False, invalid_fsp_message(fsp), None

    frame_slot = parts.frame_slot
    port_num = parts.port

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    raw_outputs: dict[str, str] = {}
    warnings: list[str] = []

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        # 1. Get ONT info (online/offline status)
        ont_info_cmd = (
            f"display ont info {parts.frame} {parts.slot} {parts.port} {ont_id}"
        )
        ont_info_output = core._run_huawei_cmd(channel, ont_info_cmd)
        raw_outputs["ont_info"] = ont_info_output

        # Parse ONT info
        ont_kv: dict[str, str] = {}
        for line in ont_info_output.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                ont_kv[key.strip().lower()] = value.strip()

        run_state = ont_kv.get("run state", "unknown").lower()
        config_state = ont_kv.get("config state", "unknown").lower()
        match_state = ont_kv.get("match state", "unknown").lower()
        ont_online = run_state == "online"

        if not ont_online:
            warnings.append(
                f"ONT is {run_state.upper()} - service ports will show Down"
            )

        # 2. Get GEM port status
        gem_cmd = (
            f"display ont port state {frame_slot} {port_num} {ont_id} eth-port all"
        )
        gem_output = core._run_huawei_cmd(channel, gem_cmd)
        raw_outputs["gem_ports"] = gem_output

        # Parse GEM port info (basic parsing)
        gem_ports: list[dict[str, str]] = []
        for line in gem_output.splitlines():
            # Look for lines with port state info
            if re.match(r"\s*\d+\s+\d+", line):
                parts_line = line.split()
                if len(parts_line) >= 3:
                    gem_ports.append(
                        {
                            "port": parts_line[0] if len(parts_line) > 0 else "",
                            "vlan": parts_line[1] if len(parts_line) > 1 else "",
                            "state": parts_line[-1] if parts_line else "",
                        }
                    )

        # 3. Get detailed service port info for this ONT's ports
        sp_cmd = f"display service-port port {fsp}"
        sp_output = core._run_huawei_cmd(channel, sp_cmd)
        raw_outputs["service_ports"] = sp_output

        # Filter and parse service ports for this ONT
        service_port_details: list[dict[str, str]] = []
        for line in sp_output.splitlines():
            # Check if this line is for our ONT (contains the ONT-ID in VPI column)
            if "gpon" in line.lower():
                parts_line = line.split()
                if len(parts_line) >= 10:
                    try:
                        # VPI column (index 5 after gpon) is the ONT-ID
                        line_ont_id = None
                        for i, p in enumerate(parts_line):
                            if p == "gpon" and i + 2 < len(parts_line):
                                # Skip FSP tokens, find ONT-ID
                                for tok in parts_line[i + 1 :]:
                                    cleaned = tok.strip("/").replace("/", "")
                                    if "/" not in tok and cleaned.isdigit():
                                        line_ont_id = int(cleaned)
                                        break
                                break

                        if line_ont_id == ont_id:
                            state = parts_line[-1].lower()
                            service_port_details.append(
                                {
                                    "index": parts_line[0],
                                    "vlan": parts_line[1],
                                    "state": state,
                                    "raw": line.strip(),
                                }
                            )
                            if state == "down" and ont_online:
                                warnings.append(
                                    f"Service port {parts_line[0]} is Down "
                                    f"while ONT is online - check GEM/T-CONT config"
                                )
                    except (ValueError, IndexError):
                        continue

        diagnostics = ServicePortDiagnostics(
            ont_run_state=run_state,
            ont_config_state=config_state,
            ont_match_state=match_state,
            ont_online=ont_online,
            gem_ports=gem_ports,
            service_port_details=service_port_details,
            raw_outputs=raw_outputs,
            warnings=warnings,
        )

        return True, "Diagnostics completed", diagnostics

    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error running service port diagnostics on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()
