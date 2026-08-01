"""NAS Vendor Adapter for unified multi-vendor device operations.

This adapter abstracts vendor-specific operations for NAS devices (MikroTik,
Cisco, Huawei, Juniper, etc.) providing a clean interface for:
- Configuration backup/restore commands
- User provisioning commands
- Status/telemetry retrieval
- API authentication

Usage:
    from app.services.nas.vendor_adapter import get_nas_vendor_adapter

    adapter = get_nas_vendor_adapter(device)
    backup_cmd = adapter.get_backup_command()
    restore_cmd = adapter.get_restore_command(config_content)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.models.catalog import NasDevice

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result Types
# ---------------------------------------------------------------------------


class CommandType(str, Enum):
    """Types of NAS commands."""

    backup = "backup"
    restore = "restore"
    authorize = "authorize"
    deauthorize = "deauthorize"
    suspend = "suspend"
    unsuspend = "unsuspend"
    update_speed = "update_speed"
    get_status = "get_status"


@dataclass
class VendorCommand:
    """A vendor-specific command with metadata."""

    command: str
    command_type: CommandType
    requires_config_mode: bool = False
    requires_save: bool = False
    timeout_seconds: int = 60
    description: str = ""


@dataclass
class VendorCapabilities:
    """Capabilities supported by a vendor adapter."""

    supports_ssh: bool = True
    supports_api: bool = False
    supports_netconf: bool = False
    supports_snmp: bool = True
    supports_radius_coa: bool = False
    api_type: str | None = None  # "rest", "routeros", "nce", etc.
    config_format: str = "txt"  # "rsc", "txt", "xml", "json"
    default_ssh_port: int = 22
    default_api_port: int | None = None


@dataclass
class StatusResult:
    """Result of a status check operation."""

    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    source: str = "unknown"  # "rest", "ssh", "snmp", etc.


# ---------------------------------------------------------------------------
# Protocol Definition
# ---------------------------------------------------------------------------


@runtime_checkable
class NasVendorAdapter(Protocol):
    """Protocol for NAS vendor-specific operations."""

    @property
    def vendor_name(self) -> str:
        """Return the vendor name."""
        ...

    @property
    def capabilities(self) -> VendorCapabilities:
        """Return vendor capabilities."""
        ...

    def get_backup_command(self) -> VendorCommand:
        """Get the command to backup/export configuration."""
        ...

    def get_restore_command(self, config_content: str) -> VendorCommand:
        """Get the command to restore/import configuration."""
        ...

    def get_authorize_command(
        self,
        username: str,
        password: str,
        *,
        ip_address: str | None = None,
        speed_profile: str | None = None,
        **kwargs: Any,
    ) -> VendorCommand:
        """Get the command to authorize/create a user."""
        ...

    def get_deauthorize_command(self, username: str) -> VendorCommand:
        """Get the command to deauthorize/delete a user."""
        ...

    def get_suspend_command(self, username: str) -> VendorCommand:
        """Get the command to suspend a user."""
        ...

    def get_unsuspend_command(self, username: str) -> VendorCommand:
        """Get the command to unsuspend a user."""
        ...

    def get_status(self, device: NasDevice) -> StatusResult:
        """Get device status/telemetry."""
        ...

    def parse_command_response(
        self,
        command_type: CommandType,
        response: str,
    ) -> dict[str, Any]:
        """Parse vendor-specific command response."""
        ...


# ---------------------------------------------------------------------------
# Base Implementation
# ---------------------------------------------------------------------------


class BaseNasVendorAdapter(ABC):
    """Base class for NAS vendor adapters with common functionality."""

    def __init__(self, device: NasDevice | None = None):
        self._device = device

    @property
    @abstractmethod
    def vendor_name(self) -> str:
        """Return the vendor name."""
        ...

    @property
    @abstractmethod
    def capabilities(self) -> VendorCapabilities:
        """Return vendor capabilities."""
        ...

    @abstractmethod
    def get_backup_command(self) -> VendorCommand:
        """Get the command to backup/export configuration."""
        ...

    @abstractmethod
    def get_restore_command(self, config_content: str) -> VendorCommand:
        """Get the command to restore/import configuration."""
        ...

    def get_authorize_command(
        self,
        username: str,
        password: str,
        *,
        ip_address: str | None = None,
        speed_profile: str | None = None,
        **kwargs: Any,
    ) -> VendorCommand:
        """Default implementation - override for vendor-specific logic."""
        raise NotImplementedError(
            f"{self.vendor_name} does not support direct user authorization"
        )

    def get_deauthorize_command(self, username: str) -> VendorCommand:
        """Default implementation - override for vendor-specific logic."""
        raise NotImplementedError(
            f"{self.vendor_name} does not support direct user deauthorization"
        )

    def get_suspend_command(self, username: str) -> VendorCommand:
        """Default implementation - override for vendor-specific logic."""
        raise NotImplementedError(
            f"{self.vendor_name} does not support direct user suspension"
        )

    def get_unsuspend_command(self, username: str) -> VendorCommand:
        """Default implementation - override for vendor-specific logic."""
        raise NotImplementedError(
            f"{self.vendor_name} does not support direct user unsuspension"
        )

    def get_status(self, device: NasDevice) -> StatusResult:
        """Default implementation returns basic info."""
        return StatusResult(
            success=True,
            message="Status check not implemented for this vendor",
            data={"vendor": self.vendor_name},
            source="none",
        )

    def parse_command_response(
        self,
        command_type: CommandType,
        response: str,
    ) -> dict[str, Any]:
        """Default implementation returns raw response."""
        return {"raw_response": response}


# ---------------------------------------------------------------------------
# MikroTik Adapter
# ---------------------------------------------------------------------------


class MikroTikAdapter(BaseNasVendorAdapter):
    """NAS vendor adapter for MikroTik RouterOS devices."""

    @property
    def vendor_name(self) -> str:
        return "mikrotik"

    @property
    def capabilities(self) -> VendorCapabilities:
        return VendorCapabilities(
            supports_ssh=True,
            supports_api=True,
            supports_netconf=False,
            supports_snmp=True,
            supports_radius_coa=True,
            api_type="rest",
            config_format="rsc",
            default_ssh_port=22,
            default_api_port=8728,
        )

    def get_backup_command(self) -> VendorCommand:
        return VendorCommand(
            command="/export",
            command_type=CommandType.backup,
            requires_config_mode=False,
            requires_save=False,
            timeout_seconds=120,
            description="Export RouterOS configuration",
        )

    def get_restore_command(self, config_content: str) -> VendorCommand:
        return VendorCommand(
            command=f"/import verbose=yes\n{config_content}",
            command_type=CommandType.restore,
            requires_config_mode=False,
            requires_save=False,
            timeout_seconds=300,
            description="Import RouterOS configuration",
        )

    # get_authorize_command / get_deauthorize_command / get_suspend_command /
    # get_unsuspend_command are deliberately NOT overridden here. They used to
    # build a local PPPoE secret carrying the customer's address, and to toggle
    # its enabled flag for suspension — making the router a second authority for
    # both the address and the access state. RouterOS consults RADIUS only when
    # the username is absent from its local secret store, so such a record
    # bypasses access.radius_projection rather than supplementing it.
    #
    # Inheriting the base class's NotImplementedError is the point — a MikroTik
    # PPPoE user is provisioned by projecting RADIUS, suspended by CoA through
    # access.session_enforcement, and cleaned up through the reviewed operation
    # in app/services/nas/local_secret_policy.py. See
    # docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md and the architecture guard in
    # tests/architecture/test_nas_local_secret_prohibition.py.

    def get_status(self, device: NasDevice) -> StatusResult:
        """Get MikroTik device status via API."""
        try:
            from app.services.nas._mikrotik import get_mikrotik_api_status

            status_data = get_mikrotik_api_status(device)
            return StatusResult(
                success=True,
                message="Status retrieved successfully",
                data=status_data,
                source=str(status_data.get("api_source", "unknown")),
            )
        except Exception as exc:
            return StatusResult(
                success=False,
                message=f"Failed to get status: {exc}",
                data={},
                source="error",
            )

    def parse_command_response(
        self,
        command_type: CommandType,
        response: str,
    ) -> dict[str, Any]:
        """Parse MikroTik command responses."""
        result: dict[str, Any] = {"raw_response": response}

        if command_type == CommandType.backup:
            # Count lines in exported config
            lines = [
                l for l in response.splitlines() if l.strip() and not l.startswith("#")
            ]
            result["config_lines"] = len(lines)

        elif command_type in (CommandType.authorize, CommandType.deauthorize):
            # Check for errors
            if "failure:" in response.lower() or "error" in response.lower():
                result["success"] = False
                result["error"] = response
            else:
                result["success"] = True

        return result


# ---------------------------------------------------------------------------
# Cisco Adapter
# ---------------------------------------------------------------------------


class CiscoAdapter(BaseNasVendorAdapter):
    """NAS vendor adapter for Cisco IOS/IOS-XE devices."""

    @property
    def vendor_name(self) -> str:
        return "cisco"

    @property
    def capabilities(self) -> VendorCapabilities:
        return VendorCapabilities(
            supports_ssh=True,
            supports_api=True,
            supports_netconf=True,
            supports_snmp=True,
            supports_radius_coa=True,
            api_type="restconf",
            config_format="txt",
            default_ssh_port=22,
            default_api_port=443,
        )

    def get_backup_command(self) -> VendorCommand:
        return VendorCommand(
            command="show running-config",
            command_type=CommandType.backup,
            requires_config_mode=False,
            requires_save=False,
            timeout_seconds=120,
            description="Show running configuration",
        )

    def get_restore_command(self, config_content: str) -> VendorCommand:
        return VendorCommand(
            command=f"configure terminal\n{config_content}\nend\nwrite memory",
            command_type=CommandType.restore,
            requires_config_mode=True,
            requires_save=True,
            timeout_seconds=300,
            description="Apply configuration and save",
        )

    def parse_command_response(
        self,
        command_type: CommandType,
        response: str,
    ) -> dict[str, Any]:
        """Parse Cisco command responses."""
        result: dict[str, Any] = {"raw_response": response}

        if command_type == CommandType.backup:
            lines = [
                l for l in response.splitlines() if l.strip() and not l.startswith("!")
            ]
            result["config_lines"] = len(lines)

        elif command_type == CommandType.restore:
            if "invalid" in response.lower() or "error" in response.lower():
                result["success"] = False
                result["error"] = response
            else:
                result["success"] = True

        return result


# ---------------------------------------------------------------------------
# Huawei Adapter
# ---------------------------------------------------------------------------


class HuaweiAdapter(BaseNasVendorAdapter):
    """NAS vendor adapter for Huawei devices."""

    @property
    def vendor_name(self) -> str:
        return "huawei"

    @property
    def capabilities(self) -> VendorCapabilities:
        return VendorCapabilities(
            supports_ssh=True,
            supports_api=True,
            supports_netconf=True,
            supports_snmp=True,
            supports_radius_coa=True,
            api_type="nce",
            config_format="txt",
            default_ssh_port=22,
            default_api_port=443,
        )

    def get_backup_command(self) -> VendorCommand:
        return VendorCommand(
            command="display current-configuration",
            command_type=CommandType.backup,
            requires_config_mode=False,
            requires_save=False,
            timeout_seconds=120,
            description="Display current configuration",
        )

    def get_restore_command(self, config_content: str) -> VendorCommand:
        return VendorCommand(
            command=f"system-view\n{config_content}\nreturn\nsave",
            command_type=CommandType.restore,
            requires_config_mode=True,
            requires_save=True,
            timeout_seconds=300,
            description="Apply configuration and save",
        )

    def parse_command_response(
        self,
        command_type: CommandType,
        response: str,
    ) -> dict[str, Any]:
        """Parse Huawei command responses."""
        result: dict[str, Any] = {"raw_response": response}

        if command_type == CommandType.backup:
            lines = [
                l for l in response.splitlines() if l.strip() and not l.startswith("#")
            ]
            result["config_lines"] = len(lines)

        return result


# ---------------------------------------------------------------------------
# Juniper Adapter
# ---------------------------------------------------------------------------


class JuniperAdapter(BaseNasVendorAdapter):
    """NAS vendor adapter for Juniper Junos devices."""

    @property
    def vendor_name(self) -> str:
        return "juniper"

    @property
    def capabilities(self) -> VendorCapabilities:
        return VendorCapabilities(
            supports_ssh=True,
            supports_api=True,
            supports_netconf=True,
            supports_snmp=True,
            supports_radius_coa=True,
            api_type="rest",
            config_format="txt",
            default_ssh_port=22,
            default_api_port=3000,
        )

    def get_backup_command(self) -> VendorCommand:
        return VendorCommand(
            command="show configuration",
            command_type=CommandType.backup,
            requires_config_mode=False,
            requires_save=False,
            timeout_seconds=120,
            description="Show configuration",
        )

    def get_restore_command(self, config_content: str) -> VendorCommand:
        return VendorCommand(
            command=f"configure\nload override terminal\n{config_content}\ncommit and-quit",
            command_type=CommandType.restore,
            requires_config_mode=True,
            requires_save=True,
            timeout_seconds=300,
            description="Load and commit configuration",
        )

    def parse_command_response(
        self,
        command_type: CommandType,
        response: str,
    ) -> dict[str, Any]:
        """Parse Juniper command responses."""
        result: dict[str, Any] = {"raw_response": response}

        if command_type == CommandType.restore:
            if "commit complete" in response.lower():
                result["success"] = True
            elif "error" in response.lower():
                result["success"] = False
                result["error"] = response
            else:
                result["success"] = True

        return result


# ---------------------------------------------------------------------------
# Generic Adapter (Fallback)
# ---------------------------------------------------------------------------


class GenericNasAdapter(BaseNasVendorAdapter):
    """Generic NAS adapter for unknown/unsupported vendors."""

    @property
    def vendor_name(self) -> str:
        return "generic"

    @property
    def capabilities(self) -> VendorCapabilities:
        return VendorCapabilities(
            supports_ssh=True,
            supports_api=False,
            supports_netconf=False,
            supports_snmp=True,
            supports_radius_coa=False,
            api_type=None,
            config_format="txt",
            default_ssh_port=22,
            default_api_port=None,
        )

    def get_backup_command(self) -> VendorCommand:
        return VendorCommand(
            command="show running-config",
            command_type=CommandType.backup,
            requires_config_mode=False,
            requires_save=False,
            timeout_seconds=120,
            description="Generic config export command",
        )

    def get_restore_command(self, config_content: str) -> VendorCommand:
        return VendorCommand(
            command=config_content,
            command_type=CommandType.restore,
            requires_config_mode=False,
            requires_save=False,
            timeout_seconds=300,
            description="Generic config restore (raw content)",
        )


# ---------------------------------------------------------------------------
# Adapter Registry and Factory
# ---------------------------------------------------------------------------

_VENDOR_ADAPTERS: dict[str, type[BaseNasVendorAdapter]] = {
    "mikrotik": MikroTikAdapter,
    "cisco": CiscoAdapter,
    "huawei": HuaweiAdapter,
    "juniper": JuniperAdapter,
}


def register_vendor_adapter(
    vendor: str,
    adapter_class: type[BaseNasVendorAdapter],
) -> None:
    """Register a custom vendor adapter."""
    _VENDOR_ADAPTERS[vendor.lower()] = adapter_class


def get_nas_vendor_adapter(
    device: NasDevice,
) -> NasVendorAdapter:
    """Get the appropriate vendor adapter for a NAS device.

    Args:
        device: NAS device model instance.

    Returns:
        Vendor-specific adapter instance.
    """
    vendor_name = device.vendor.value.lower() if device.vendor else "generic"
    adapter_class = _VENDOR_ADAPTERS.get(vendor_name, GenericNasAdapter)
    return adapter_class(device)


def get_adapter_by_vendor_name(vendor: str) -> NasVendorAdapter:
    """Get a vendor adapter by vendor name string.

    Args:
        vendor: Vendor name (e.g., "mikrotik", "cisco").

    Returns:
        Vendor-specific adapter instance.
    """
    adapter_class = _VENDOR_ADAPTERS.get(vendor.lower(), GenericNasAdapter)
    return adapter_class()


# ---------------------------------------------------------------------------
# Convenience Functions
# ---------------------------------------------------------------------------


def get_backup_command(device: NasDevice) -> str:
    """Get the backup command for a device."""
    adapter = get_nas_vendor_adapter(device)
    return adapter.get_backup_command().command


def get_restore_command(device: NasDevice, config_content: str) -> str:
    """Get the restore command for a device."""
    adapter = get_nas_vendor_adapter(device)
    return adapter.get_restore_command(config_content).command


def get_config_format(device: NasDevice) -> str:
    """Get the config file format for a device."""
    adapter = get_nas_vendor_adapter(device)
    return adapter.capabilities.config_format


def get_device_status(device: NasDevice) -> StatusResult:
    """Get device status using the appropriate adapter."""
    adapter = get_nas_vendor_adapter(device)
    return adapter.get_status(device)


def supports_api(device: NasDevice) -> bool:
    """Check if device supports API operations."""
    adapter = get_nas_vendor_adapter(device)
    return adapter.capabilities.supports_api


def get_default_ports(device: NasDevice) -> dict[str, int | None]:
    """Get default ports for a device vendor."""
    adapter = get_nas_vendor_adapter(device)
    caps = adapter.capabilities
    return {
        "ssh": caps.default_ssh_port,
        "api": caps.default_api_port,
    }
