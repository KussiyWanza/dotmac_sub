"""ONT IPHOST configuration functions via OLT SSH."""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network.huawei_cli_response import describe_huawei_rejection
from app.services.network.olt_ssh_ont._common import (
    _SSH_CONNECTION_ERRORS,
    OntIphostConfig,
    OntIphostResult,
    _run_ont_config_command,
    invalid_fsp_message,
)
from app.services.network.olt_validators import (
    ValidationError,
    validate_ip_address,
    validate_ont_id,
    validate_subnet_mask,
    validate_vlan_id,
)
from app.services.network.parsers.cli import canonical_fsp

logger = logging.getLogger(__name__)


def _verify_iphost_applied(
    channel,
    port_num: str,
    ont_id: int,
    config_prompt: str,
) -> str:
    """Run display ont ipconfig as a best-effort post-apply readback.

    The returned output is informational — we don't parse/validate it here
    because the apply command already succeeded. Its purpose is to leave an
    audit trail in the SSH session log showing the applied state, and to
    surface any device-side inconsistency in logs for operator review.
    Failures are swallowed since the configuration itself was accepted.
    """
    from app.services.network import olt_ssh as core

    try:
        return core._run_huawei_cmd(
            channel,
            f"display ont ipconfig {port_num} {ont_id}",
            prompt=config_prompt,
        )
    except Exception as exc:
        logger.debug("IPHOST readback skipped for ONT %d: %s", ont_id, exc)
        return ""


def _configure_single_ont_in_session(
    channel,
    olt: OLTDevice,
    cfg: OntIphostConfig,
    config_prompt: str,
    is_ma5800: bool,
) -> OntIphostResult:
    """Configure a single ONT's IPHOST within an existing SSH session.

    Internal helper for configure_ont_iphost_batch().
    """
    from app.services.network import olt_ssh as core

    # Validate inputs
    try:
        validate_ont_id(cfg.ont_id)
        validate_vlan_id(cfg.vlan_id)
        if cfg.ip_mode != "dhcp":
            validate_ip_address(cfg.ip_address, "ip_address")
            validate_subnet_mask(cfg.subnet, "subnet_mask")
            if cfg.gateway:
                validate_ip_address(cfg.gateway, "gateway")
    except ValidationError as e:
        return OntIphostResult(
            fsp=cfg.fsp,
            ont_id=cfg.ont_id,
            success=False,
            message=e.message,
            serial_number=cfg.serial_number,
        )

    parts = canonical_fsp(cfg.fsp)
    port_num = parts.port if parts else "0"

    # Derive gateway if not provided
    gateway = cfg.gateway
    if not gateway and cfg.ip_address:
        ip_parts = cfg.ip_address.split(".")
        gateway = f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.1"

    # Build command
    priority_clause = f" priority {cfg.priority}" if cfg.priority is not None else ""
    if cfg.ip_mode == "dhcp":
        cmd = (
            f"ont ipconfig {port_num} {cfg.ont_id} ip-index {cfg.ip_index} "
            f"dhcp vlan {cfg.vlan_id}{priority_clause}"
        )
    else:
        cmd = (
            f"ont ipconfig {port_num} {cfg.ont_id} "
            f"ip-index {cfg.ip_index} static ip-address {cfg.ip_address} "
            f"mask {cfg.subnet} gateway {gateway} vlan {cfg.vlan_id}{priority_clause}"
        )

    # Clear any existing IPHOST config first to ensure new settings are applied.
    # Without this, if ONT already has IPHOST configured with different IP/VLAN,
    # OLT returns "make configuration repeatedly" and leaves old config in place.
    undo_cmd = f"undo ont ipconfig {port_num} {cfg.ont_id} ip-index {cfg.ip_index}"
    channel.send(f"{undo_cmd}\n")
    time.sleep(0.3)
    core._read_until_prompt(channel, config_prompt, timeout_sec=5)

    # Send command with appropriate method for OLT model
    time.sleep(0.1)
    if is_ma5800:
        # MA5800 needs slow character-by-character send
        for char in cmd:
            channel.send(char)
            if char == " ":
                time.sleep(0.15)
            else:
                time.sleep(0.03)
        time.sleep(0.3)
        channel.send("\n")
    else:
        channel.send(f"{cmd}\n")

    time.sleep(0.3)
    output = core._read_until_prompt(channel, rf"{config_prompt}|<cr>", timeout_sec=10)

    if "<cr>" in output.lower():
        channel.send("\n")
        output += core._read_until_prompt(channel, config_prompt, timeout_sec=5)

    # Check result
    if "make configuration repeatedly" in output.lower():
        _verify_iphost_applied(channel, port_num, cfg.ont_id, config_prompt)
        return OntIphostResult(
            fsp=cfg.fsp,
            ont_id=cfg.ont_id,
            success=True,
            message=f"Already configured ({cfg.ip_mode} VLAN {cfg.vlan_id})",
            serial_number=cfg.serial_number,
        )

    if core.is_error_output(output):
        logger.debug(
            "IPHOST failed for ONT %d on %s: %s",
            cfg.ont_id,
            cfg.fsp,
            output.strip()[-100:],
        )
        return OntIphostResult(
            fsp=cfg.fsp,
            ont_id=cfg.ont_id,
            success=False,
            message=describe_huawei_rejection(output, detail_limit=80),
            serial_number=cfg.serial_number,
        )

    _verify_iphost_applied(channel, port_num, cfg.ont_id, config_prompt)

    return OntIphostResult(
        fsp=cfg.fsp,
        ont_id=cfg.ont_id,
        success=True,
        message=f"Configured ({cfg.ip_mode} VLAN {cfg.vlan_id})",
        serial_number=cfg.serial_number,
    )


def configure_ont_iphost(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    vlan_id: int,
    ip_index: int = 0,
    ip_mode: str = "dhcp",
    priority: int | None = None,
    ip_address: str | None = None,
    subnet: str | None = None,
    gateway: str | None = None,
) -> tuple[bool, str]:
    """Configure ONT management IP (IPHOST) via OLT SSH CLI."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err
    if ip_mode == "static" and not subnet:
        return False, "Subnet is required for static ONT IPHOST configuration."

    # Build config and use batch function (single-item batch)
    config = OntIphostConfig(
        fsp=fsp,
        ont_id=ont_id,
        vlan_id=vlan_id,
        ip_index=ip_index,
        ip_address=ip_address or "",
        subnet=subnet or "",
        gateway=gateway,
        ip_mode=ip_mode,
        priority=priority,
    )

    results = configure_ont_iphost_batch(olt, [config])
    if results:
        result = results[0]
        return result.success, result.message
    return False, "No result from batch configuration"


def configure_ont_iphost_batch(
    olt: OLTDevice,
    configs: list[OntIphostConfig],
    *,
    inter_command_delay: float = 0.3,
) -> list[OntIphostResult]:
    """Configure IPHOST for multiple ONTs using a single SSH session.

    This is more efficient than calling configure_ont_iphost() repeatedly,
    as it maintains one SSH connection and groups commands by board/slot.

    Args:
        olt: The OLT device to configure.
        configs: List of ONT IPHOST configurations.
        inter_command_delay: Delay between commands (seconds).

    Returns:
        List of results for each ONT configuration attempt.
    """
    from app.services.network import olt_ssh as core

    if not configs:
        return []

    results: list[OntIphostResult] = []

    # Group configs by frame/slot for efficient interface switching
    by_frame_slot: dict[str, list[OntIphostConfig]] = defaultdict(list)
    for cfg in configs:
        parts = canonical_fsp(cfg.fsp)
        if parts is not None:
            by_frame_slot[parts.frame_slot].append(cfg)

    # Detect OLT model for terminal handling
    is_ma5800 = "ma5800" in (olt.model or "").lower()

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        # Return failure for all configs
        return [
            OntIphostResult(
                fsp=cfg.fsp,
                ont_id=cfg.ont_id,
                success=False,
                message=f"Connection failed: {exc}",
                serial_number=cfg.serial_number,
            )
            for cfg in configs
        ]

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"

        # Set wide terminal width
        channel.send("screen-width 512 temporary\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        time.sleep(0.2)

        current_interface: str | None = None

        for frame_slot, slot_configs in by_frame_slot.items():
            # Enter interface for this frame/slot
            if current_interface != frame_slot:
                if current_interface is not None:
                    # Exit previous interface
                    core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
                    time.sleep(0.1)

                channel.send(f"interface gpon {frame_slot}\n")
                core._read_until_prompt(channel, config_prompt, timeout_sec=8)
                current_interface = frame_slot
                time.sleep(0.2)

            # Configure each ONT on this slot
            for cfg in slot_configs:
                result = _configure_single_ont_in_session(
                    channel=channel,
                    olt=olt,
                    cfg=cfg,
                    config_prompt=config_prompt,
                    is_ma5800=is_ma5800,
                )
                results.append(result)
                time.sleep(inter_command_delay)

        # Exit interface and config mode
        if current_interface is not None:
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        success_count = sum(1 for r in results if r.success)
        logger.info(
            "Batch IPHOST config on OLT %s: %d/%d successful",
            olt.name,
            success_count,
            len(results),
        )

    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error in batch IPHOST config on OLT %s: %s", olt.name, exc, exc_info=True
        )
        # Mark remaining configs as failed
        configured_fsps = {(r.fsp, r.ont_id) for r in results}
        for cfg in configs:
            if (cfg.fsp, cfg.ont_id) not in configured_fsps:
                results.append(
                    OntIphostResult(
                        fsp=cfg.fsp,
                        ont_id=cfg.ont_id,
                        success=False,
                        message=f"Session error: {exc}",
                        serial_number=cfg.serial_number,
                    )
                )
    finally:
        transport.close()

    return results


def parse_iphost_config_output(output: str) -> dict[str, str]:
    """Parse Huawei ``display ont ipconfig`` output into normalized fields."""
    config: dict[str, str] = {}
    aliases = {
        "ont ip host index": "ip_index",
        "ont iphost index": "ip_index",
        "ont config type": "mode",
        "ont ip": "ip_address",
        "ont subnet mask": "subnet_mask",
        "ont gateway": "gateway",
        "ont primary dns": "primary_dns",
        "ont slave dns": "secondary_dns",
        "ont mac": "mac_address",
        "ont manage vlan": "vlan",
        "ont manage priority": "priority",
        "dscp mapping table index": "dscp_mapping_table_index",
    }
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        raw_key = " ".join(key.strip().lower().split())
        raw_value = value.strip()
        if not raw_key:
            continue
        config[key.strip()] = raw_value
        normalized_key = aliases.get(raw_key)
        if normalized_key:
            config[normalized_key] = raw_value
    return config


def clear_ont_ipconfig(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
) -> tuple[bool, str]:
    """Best-effort removal of ONT IP configuration for a given IP index."""
    parts = canonical_fsp(fsp)
    port_num = parts.port if parts else "0"
    return _run_ont_config_command(
        olt,
        fsp,
        f"undo ont ipconfig {port_num} {ont_id} ip-index {ip_index}",
        success_message=f"ONT ipconfig cleared for ip-index {ip_index}",
    )


def get_ont_iphost_config(
    olt: OLTDevice, fsp: str, ont_id: int
) -> tuple[bool, str, dict[str, str]]:
    """Query current ONT IPHOST configuration from OLT."""
    from app.services.network import olt_ssh as core

    parts = canonical_fsp(fsp)
    if parts is None:
        return False, invalid_fsp_message(fsp), {}

    port_num = parts.port

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", {}

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {parts.frame_slot}", prompt=config_prompt
        )

        cmd = f"display ont ipconfig {port_num} {ont_id}"
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        config = parse_iphost_config_output(output)
        return True, "IPHOST config retrieved", config
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error getting IPHOST config from OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}", {}
    finally:
        transport.close()
