"""ONT OMCI-based configuration functions (internet, WAN, PPPoE, port) via OLT SSH."""

from __future__ import annotations

import logging
import re

from app.models.network import OLTDevice
from app.services.network.huawei_cli_response import describe_huawei_rejection
from app.services.network.olt_ssh_ont._common import (
    _SSH_CONNECTION_ERRORS,
    _run_ont_config_command,
    invalid_fsp_message,
    send_ont_command,
)
from app.services.network.parsers.cli import canonical_fsp

logger = logging.getLogger(__name__)


def _is_ma5800(olt: OLTDevice) -> bool:
    return "ma5800" in str(getattr(olt, "model", "") or "").lower()


def _wan_config_display_commands(olt: OLTDevice, fsp: str, ont_id: int) -> list[str]:
    parts = canonical_fsp(fsp)
    port_num = parts.port if parts else "0"
    full_cmd = f"display ont wan-config {fsp} {ont_id}"
    short_cmd = f"display ont wan-config {port_num} {ont_id}"
    if _is_ma5800(olt):
        return [full_cmd, short_cmd]
    return [short_cmd, full_cmd]


def _parse_wan_config_display(output: str) -> dict[str, int | str] | None:
    ip_match = re.search(r"WAN IP index\s*:\s*(\d+)", output, re.IGNORECASE)
    profile_match = re.search(r"WAN profile ID\s*:\s*(\d+)", output, re.IGNORECASE)
    if not ip_match or not profile_match:
        return None
    name_match = re.search(r"WAN profile name\s*:\s*(.+)", output, re.IGNORECASE)
    return {
        "ip_index": int(ip_match.group(1)),
        "profile_id": int(profile_match.group(1)),
        "profile_name": name_match.group(1).strip() if name_match else "",
    }


def read_ont_wan_config(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
) -> tuple[bool, str, dict[str, int | str] | None]:
    """Read ONT WAN profile binding, using model-specific Huawei syntax."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err, None

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (*_SSH_CONNECTION_ERRORS, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        errors: list[str] = []
        for command in _wan_config_display_commands(olt, fsp, ont_id):
            output = core._run_huawei_cmd(channel, command)
            parsed = _parse_wan_config_display(output)
            if parsed:
                return True, command, parsed
            if core.is_error_output(output):
                errors.append(output.strip()[-180:])
                continue
            errors.append(f"Unrecognized WAN config readback from {command!r}")
        return False, "; ".join(errors), None
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        return False, f"Error: {exc}", None
    finally:
        transport.close()


def _mask_pppoe_password(text: str) -> str:
    return re.sub(r"(password\s+)\S+", r"\1***", text)


def configure_ont_internet_config(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
) -> tuple[bool, str]:
    """Activate TCP stack on ONT management WAN via internet-config."""
    parts = canonical_fsp(fsp)
    port_num = parts.port if parts else "0"
    return _run_ont_config_command(
        olt,
        fsp,
        f"ont internet-config {port_num} {ont_id} ip-index {ip_index}",
        success_message=f"Internet config activated (ip-index {ip_index})",
    )


def clear_ont_internet_config(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
) -> tuple[bool, str]:
    """Best-effort removal of ONT internet-config state."""
    parts = canonical_fsp(fsp)
    port_num = parts.port if parts else "0"
    return _run_ont_config_command(
        olt,
        fsp,
        f"undo ont internet-config {port_num} {ont_id} ip-index {ip_index}",
        success_message=f"Internet config cleared for ip-index {ip_index}",
    )


def configure_ont_wan_config(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
    profile_id: int = 0,
) -> tuple[bool, str]:
    """Set route+NAT mode on ONT management WAN via wan-config."""
    parts = canonical_fsp(fsp)
    port_num = parts.port if parts else "0"
    ok, message = _run_ont_config_command(
        olt,
        fsp,
        f"ont wan-config {port_num} {ont_id} ip-index {ip_index} profile-id {profile_id}",
        success_message=(
            f"WAN route+NAT mode set (ip-index {ip_index}, profile-id {profile_id})"
        ),
    )
    if not ok:
        return ok, message

    read_ok, read_message, readback = read_ont_wan_config(olt, fsp, ont_id)
    if not read_ok or not readback:
        return False, f"{message}; readback failed: {read_message}"
    if readback.get("ip_index") != ip_index or readback.get("profile_id") != profile_id:
        return (
            False,
            (
                f"{message}; readback mismatch: "
                f"ip-index={readback.get('ip_index')} profile-id={readback.get('profile_id')}"
            ),
        )
    return True, f"{message}; verified via {read_message}"


def clear_ont_wan_config(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
) -> tuple[bool, str]:
    """Best-effort removal of ONT wan-config state."""
    parts = canonical_fsp(fsp)
    port_num = parts.port if parts else "0"
    return _run_ont_config_command(
        olt,
        fsp,
        f"undo ont wan-config {port_num} {ont_id} ip-index {ip_index}",
        success_message=f"WAN config cleared for ip-index {ip_index}",
    )


def configure_ont_pppoe_omci(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 1,
    vlan_id: int,
    priority: int = 0,  # Kept for backward compatibility, not used in command
    username: str,
    password: str,
) -> tuple[bool, str]:
    """Configure PPPoE WAN via OMCI (OLT-side, not TR-069).

    Uses the Huawei OLT command:
        ont ipconfig <port> <ont-id> ip-index <idx> pppoe vlan <vlan> priority <pri>
            user-account username <user> password <pass>

    This creates a PPPoE WAN connection on the ONT with the specified VLAN
    and credentials. The ip-index should typically be 1 to avoid conflicting
    with management IP on index 0.
    """
    from app.services.network import olt_ssh as core

    parts = canonical_fsp(fsp)
    if parts is None:
        return False, invalid_fsp_message(fsp)

    frame_slot = parts.frame_slot
    port_num = parts.port

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (*_SSH_CONNECTION_ERRORS, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)

        send_ont_command(olt, channel, f"interface gpon {frame_slot}")
        core._read_until_prompt(channel, config_prompt, timeout_sec=8)

        # Huawei MA5608T/MA5800 syntax for PPPoE via OMCI
        cmd = (
            f"ont ipconfig {port_num} {ont_id} ip-index {ip_index} "
            f"pppoe vlan {vlan_id} priority {priority} "
            f"user-account username {username} password {password}"
        )
        send_ont_command(olt, channel, cmd)
        output = core._read_until_prompt(channel, config_prompt, timeout_sec=12)

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            clean_output = _mask_pppoe_password(output.strip()[-150:])
            logger.warning(
                "PPPoE OMCI config failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                clean_output,
            )
            return False, describe_huawei_rejection(clean_output)

        logger.info(
            "Configured PPPoE via OMCI for ONT %d on OLT %s (VLAN %d, user %s)",
            ont_id,
            olt.name,
            vlan_id,
            username,
        )
        return True, f"PPPoE configured via OMCI (VLAN {vlan_id}, user {username})"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error configuring PPPoE OMCI on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def configure_ont_port_native_vlan(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    eth_port: int,
    vlan_id: int,
    priority: int = 0,
) -> tuple[bool, str]:
    """Set native VLAN on ONT Ethernet port for bridging mode."""
    parts = canonical_fsp(fsp)
    port_num = parts.port if parts else "0"
    return _run_ont_config_command(
        olt,
        fsp,
        (
            f"ont port native-vlan {port_num} {ont_id} eth {eth_port} "
            f"vlan {vlan_id} priority {priority}"
        ),
        success_message=f"Native VLAN {vlan_id} set on eth port {eth_port}",
    )
