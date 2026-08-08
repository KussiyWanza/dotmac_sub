"""Batched OLT management setup for synchronous authorization performance.

This module provides single-session execution of all management configuration
commands during ONT authorization, reducing SSH overhead from 5 sessions to 1.

Commands executed in batch:
1. Create management service-port (GEM index + VLAN)
2. Configure IPHOST (DHCP or static IP)
3. Activate internet-config (TCP stack)
4. Configure wan-config (route+NAT mode)
5. Bind TR-069 profile

Target: Reduce management setup from ~25s to ~5s (5x faster).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.network import OLTDevice

logger = logging.getLogger(__name__)


def _validate_non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    if not isinstance(value, int | str | bytes | bytearray):
        raise ValueError(f"{field} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return parsed


def _validate_priority(value: object) -> int:
    priority = _validate_non_negative_int(value, "ip_priority")
    if priority > 7:
        raise ValueError("ip_priority must be between 0 and 7")
    return priority


@dataclass
class BatchedMgmtSpec:
    """Specification for batched management configuration.

    All fields are optional except fsp and ont_id_on_olt.
    Only provided configurations will be executed.
    """

    fsp: str  # Frame/Slot/Port (e.g., "0/1/0")
    ont_id_on_olt: int

    # Management service-port configuration
    mgmt_vlan_tag: int | None = None
    mgmt_gem_index: int | None = None

    # IPHOST configuration
    ip_mode: str = "dhcp"  # "dhcp" or "static"
    ip_address: str | None = None
    subnet_mask: str | None = None
    gateway: str | None = None
    ip_priority: int = 0
    ip_index: int = 0

    # internet-config (TCP stack activation)
    internet_config_ip_index: int | None = None

    # wan-config (route+NAT mode)
    wan_config_profile_id: int | None = None

    # TR-069 profile binding
    tr069_profile_id: int | None = None

    @property
    def internet_ip_index(self) -> int | None:
        """Single OLT ip-index for routed internet stack commands."""
        return self.internet_config_ip_index

    @property
    def has_service_port(self) -> bool:
        """True if service-port configuration is specified."""
        return self.mgmt_vlan_tag is not None

    @property
    def has_iphost(self) -> bool:
        """True if IPHOST configuration is specified."""
        return self.mgmt_vlan_tag is not None

    @property
    def has_internet_config(self) -> bool:
        """True if internet-config is specified."""
        return self.internet_config_ip_index is not None

    @property
    def has_wan_config(self) -> bool:
        """True if wan-config is specified."""
        return self.wan_config_profile_id is not None

    @property
    def has_tr069(self) -> bool:
        """True if TR-069 profile is specified."""
        return self.tr069_profile_id is not None


@dataclass
class BatchedMgmtResult:
    """Result of batched management configuration."""

    success: bool
    steps_completed: list[str] = field(default_factory=list)
    steps_failed: list[str] = field(default_factory=list)
    error_message: str | None = None
    details: dict[str, object] = field(default_factory=dict)
    raw_output: dict[str, str] = field(default_factory=dict)

    @property
    def message(self) -> str:
        """Human-readable result message."""
        if self.success:
            return f"Management setup complete ({len(self.steps_completed)} steps)"
        return self.error_message or "Management setup failed"


def build_management_command_batch(
    spec: BatchedMgmtSpec,
) -> list[tuple[str, str]]:
    """Build command sequence for single-session management setup.

    Returns list of tuples: (command, description)

    Command ordering:
    1. Global config: service-port create
    2. Interface mode: ont ipconfig, ont internet-config, ont wan-config, ont tr069-server-config
    """
    from app.services.network.olt_validators import (
        ValidationError,
        validate_fsp_parts,
        validate_gem_index,
        validate_ip_address,
        validate_ont_id,
        validate_subnet_mask,
        validate_vlan_id,
    )

    def validate_value(validator, *args, **kwargs):
        try:
            return validator(*args, **kwargs)
        except ValidationError as exc:
            raise ValueError(exc.message) from exc

    commands: list[tuple[str, str]] = []
    fsp_parts = validate_value(validate_fsp_parts, spec.fsp)
    fsp = fsp_parts.fsp
    ont_id = validate_value(
        validate_ont_id, _validate_non_negative_int(spec.ont_id_on_olt, "ont_id_on_olt")
    )
    ip_index = _validate_non_negative_int(spec.ip_index, "ip_index")
    ip_priority = _validate_priority(spec.ip_priority)
    frame, slot, port = fsp_parts.frame, fsp_parts.slot, fsp_parts.port

    # Service-port stage (global config mode)
    if spec.has_service_port:
        if spec.mgmt_gem_index is None:
            raise ValueError(
                "Management service-port requires an explicit management GEM index"
            )
        mgmt_vlan_tag = validate_value(
            validate_vlan_id,
            _validate_non_negative_int(spec.mgmt_vlan_tag, "mgmt_vlan_tag"),
        )
        mgmt_gem_index = validate_value(
            validate_gem_index,
            _validate_non_negative_int(spec.mgmt_gem_index, "mgmt_gem_index"),
        )
        sp_cmd = (
            f"service-port vlan {mgmt_vlan_tag} "
            f"gpon {fsp} ont {ont_id} "
            f"gemport {mgmt_gem_index} "
            f"multi-service user-vlan {mgmt_vlan_tag} "
            f"tag-transform translate"
        )
        commands.append((sp_cmd, "create_mgmt_service_port"))
    else:
        mgmt_vlan_tag = None

    # ONT-configuration stage (interface mode)
    interface_commands: list[tuple[str, str]] = []

    # IPHOST configuration
    if spec.has_iphost:
        ip_mode = str(spec.ip_mode or "").strip().lower()
        if ip_mode in {"static", "static_ip"}:
            missing = [
                label
                for label, value in (
                    ("ip_address", spec.ip_address),
                    ("subnet_mask", spec.subnet_mask),
                    ("gateway", spec.gateway),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "Static management IP configuration is incomplete; missing "
                    + ", ".join(missing)
                )
            ip_address = validate_value(validate_ip_address, str(spec.ip_address))
            subnet_mask = validate_value(validate_subnet_mask, str(spec.subnet_mask))
            gateway = validate_value(
                validate_ip_address, str(spec.gateway), field="gateway"
            )
            iphost_cmd = (
                f"ont ipconfig {port} {ont_id} "
                f"ip-index {ip_index} static "
                f"ip-address {ip_address} "
                f"mask {subnet_mask} "
                f"gateway {gateway} "
                f"vlan {mgmt_vlan_tag} "
                f"priority {ip_priority}"
            )
        elif ip_mode in {"", "dhcp", "dynamic"}:
            iphost_cmd = (
                f"ont ipconfig {port} {ont_id} "
                f"ip-index {ip_index} dhcp "
                f"vlan {mgmt_vlan_tag}"
            )
        else:
            raise ValueError(f"Unsupported management IP mode: {spec.ip_mode!r}")
        interface_commands.append((iphost_cmd, "configure_iphost"))

    # internet-config (TCP stack activation)
    if spec.has_internet_config:
        internet_config_ip_index = _validate_non_negative_int(
            spec.internet_config_ip_index, "internet_config_ip_index"
        )
        inet_cmd = (
            f"ont internet-config {port} {ont_id} ip-index {internet_config_ip_index}"
        )
        interface_commands.append((inet_cmd, "activate_internet_config"))

    # wan-config (route+NAT mode)
    if spec.has_wan_config and spec.internet_config_ip_index is not None:
        internet_config_ip_index = _validate_non_negative_int(
            spec.internet_config_ip_index, "internet_config_ip_index"
        )
        wan_config_profile_id = _validate_non_negative_int(
            spec.wan_config_profile_id, "wan_config_profile_id"
        )
        wan_cmd = (
            f"ont wan-config {port} {ont_id} "
            f"ip-index {internet_config_ip_index} "
            f"profile-id {wan_config_profile_id}"
        )
        interface_commands.append((wan_cmd, "configure_wan"))

    # TR-069 profile binding
    if spec.has_tr069:
        tr069_profile_id = _validate_non_negative_int(
            spec.tr069_profile_id, "tr069_profile_id"
        )
        tr069_cmd = (
            f"ont tr069-server-config {port} {ont_id} profile-id {tr069_profile_id}"
        )
        interface_commands.append((tr069_cmd, "bind_tr069"))

    # Wrap interface commands with enter/exit
    if interface_commands:
        commands.append((f"interface gpon {frame}/{slot}", "enter_interface_mode"))
        commands.extend(interface_commands)
        commands.append(("quit", "exit_interface_mode"))

    return commands


def execute_batched_management_setup(
    olt: OLTDevice,
    spec: BatchedMgmtSpec,
) -> BatchedMgmtResult:
    """Execute all management commands in ONE SSH session.

    This function handles:
    - Single SSH connection for entire flow
    - Config mode entry/exit
    - Interface mode transitions
    - Error handling with partial completion tracking

    Args:
        olt: OLT device object
        spec: Batched management specification

    Returns:
        BatchedMgmtResult with success status and step tracking
    """
    from app.services.network.olt_ssh_session import CliMode, olt_session

    result = BatchedMgmtResult(success=False)
    try:
        commands = build_management_command_batch(spec)
    except ValueError as exc:
        result.error_message = str(exc)
        result.steps_failed.append("configure_iphost")
        return result

    if not commands:
        result.success = True
        result.error_message = "No management configuration specified"
        return result

    logger.info(
        "Starting batched management setup for ONT %d on %s %s (%d commands)",
        spec.ont_id_on_olt,
        olt.name,
        spec.fsp,
        len(commands),
    )

    try:
        with olt_session(olt) as session:
            for cmd, description in commands:
                # Skip navigation commands in step tracking
                is_navigation = description in (
                    "enter_interface_mode",
                    "exit_interface_mode",
                )

                cmd_result = session.run_command(cmd, require_mode=CliMode.CONFIG)
                result.raw_output[description] = cmd_result.output

                if not cmd_result.success:
                    # Check for idempotent cases (already exists, etc.)
                    output_lower = cmd_result.output.lower()
                    is_idempotent = (
                        "already exist" in output_lower
                        or "existed already" in output_lower
                        or "has been configured" in output_lower
                    )

                    if is_idempotent:
                        logger.info(
                            "Batched mgmt command idempotent (already exists): %s",
                            description,
                        )
                        if not is_navigation:
                            result.steps_completed.append(f"{description} (exists)")
                        continue

                    # Real failure
                    result.error_message = f"{description}: {cmd_result.output[:200]}"
                    if not is_navigation:
                        result.steps_failed.append(description)
                    logger.error(
                        "Batched mgmt setup failed at '%s': %s",
                        description,
                        cmd_result.output[:200],
                    )
                    # Continue with remaining commands to maximize setup
                    # (e.g., TR-069 bind can succeed even if iphost fails)
                    continue

                if not is_navigation:
                    result.steps_completed.append(description)
                    logger.debug(
                        "Batched mgmt command succeeded: %s",
                        description,
                    )

        # Success if no failures, or if critical steps succeeded. The management
        # service-port is required for the ONT to reach ACS over the management
        # VLAN; accepting that failure can leave IPHOST/TR-069 configured but
        # unreachable.
        critical_steps = {"create_mgmt_service_port", "configure_iphost", "bind_tr069"}
        failed_critical = set(result.steps_failed) & critical_steps

        if not result.steps_failed:
            result.success = True
            logger.info(
                "Batched management setup complete for ONT %d on %s %s: %d steps",
                spec.ont_id_on_olt,
                olt.name,
                spec.fsp,
                len(result.steps_completed),
            )
        elif not failed_critical:
            # Non-critical failures (internet-config, wan-config can fail on some ONTs)
            result.success = True
            logger.warning(
                "Batched management setup partial success for ONT %d: "
                "completed=%s, failed=%s",
                spec.ont_id_on_olt,
                result.steps_completed,
                result.steps_failed,
            )
        else:
            logger.warning(
                "Batched management setup failed for ONT %d: %s",
                spec.ont_id_on_olt,
                result.error_message,
            )

    except Exception as e:
        logger.exception("Batched management setup failed: %s", e)
        result.error_message = str(e)

    return result


def create_batched_mgmt_spec_from_config_pack(
    config_pack,
    fsp: str,
    ont_id_on_olt: int,
    *,
    mgmt_gem_index: int | None = None,
    allocated_ip: str | None = None,
    subnet_mask: str | None = None,
    gateway: str | None = None,
    ip_priority: int | None = None,
    internet_config_ip_index: int | None = None,
    wan_config_profile_id: int | None = None,
    tr069_profile_id: int | None = None,
) -> BatchedMgmtSpec:
    """Create BatchedMgmtSpec from OLT Config Pack.

    Args:
        config_pack: OltConfigPack instance
        fsp: Frame/Slot/Port string
        ont_id_on_olt: ONT ID on the OLT
        mgmt_gem_index: Imported management GEM index for the resolved profile
        allocated_ip: Allocated management IP (for static mode)
        subnet_mask: Subnet mask (for static mode)
        gateway: Gateway IP (for static mode)
        ip_priority: Huawei IPHOST priority matching the management GEM mapping

    Returns:
        BatchedMgmtSpec ready for execution
    """
    mgmt_vlan_tag = config_pack.management_vlan.tag

    static_parts = {
        "allocated_ip": allocated_ip,
        "subnet_mask": subnet_mask,
        "gateway": gateway,
    }
    provided_static_parts = [name for name, value in static_parts.items() if value]
    if provided_static_parts and len(provided_static_parts) != len(static_parts):
        missing = [name for name, value in static_parts.items() if not value]
        raise ValueError(
            "Static management IP configuration is incomplete; missing "
            + ", ".join(missing)
        )

    # Determine IP mode
    if provided_static_parts:
        ip_mode = "static"
    else:
        ip_mode = "dhcp"

    return BatchedMgmtSpec(
        fsp=fsp,
        ont_id_on_olt=ont_id_on_olt,
        mgmt_vlan_tag=mgmt_vlan_tag,
        mgmt_gem_index=mgmt_gem_index,
        ip_mode=ip_mode,
        ip_address=allocated_ip,
        subnet_mask=subnet_mask,
        gateway=gateway,
        ip_priority=int(ip_priority) if ip_priority is not None else 0,
        ip_index=0,
        internet_config_ip_index=(
            internet_config_ip_index
            if internet_config_ip_index is not None
            else config_pack.internet_config_ip_index
        ),
        wan_config_profile_id=(
            wan_config_profile_id
            if wan_config_profile_id is not None
            else config_pack.wan_config_profile_id
        ),
        tr069_profile_id=(
            tr069_profile_id
            if tr069_profile_id is not None
            else config_pack.tr069_olt_profile_id
        ),
    )
