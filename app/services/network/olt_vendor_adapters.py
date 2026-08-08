"""OLT Vendor Adapter pattern for vendor-specific operations.

Provides a clean abstraction for vendor-specific OLT operations including:
- SNMP OID sets and scale factors
- SSH transport policies
- CLI command generation
- Output parsing
- Index encoding/decoding

Usage:
    from app.services.network.olt_vendor_adapters import get_olt_adapter

    adapter = get_olt_adapter(olt)
    oids = adapter.get_oid_set()
    policy = adapter.get_ssh_policy()
    commands = adapter.generate_authorize_commands(fsp="0/1/0", serial="HWTC12345678", ont_id=1)
"""

from __future__ import annotations

import logging
import re
from abc import ABC
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.models.network import OLTDevice

from app.services.network.parsers.cli import canonical_fsp

logger = logging.getLogger(__name__)


# ============================================================================
# Data Classes
# ============================================================================


@dataclass(frozen=True)
class OltSshPolicy:
    """SSH transport policy for connecting to an OLT."""

    key: str
    kex: tuple[str, ...]
    host_key_types: tuple[str, ...]
    ciphers: tuple[str, ...]
    macs: tuple[str, ...]
    prompt_regex: str = r"[>#]\s*$"
    version_command: str = "display version"


@dataclass(frozen=True)
class OidSet:
    """SNMP OID set for polling ONT data from an OLT."""

    olt_rx: str  # OLT receive power (from ONT)
    onu_rx: str  # ONU receive power
    onu_tx: str = ""  # ONU transmit power
    temperature: str = ""  # ONU laser temperature
    bias_current: str = ""  # ONU laser bias current
    voltage: str = ""  # ONU supply voltage
    distance: str = ""  # ONU distance from OLT
    status: str = ""  # ONU OLT status
    offline_reason: str = ""  # Last offline reason
    serial_number: str = ""  # ONU serial number

    def to_dict(self) -> dict[str, str]:
        """Convert to dictionary for compatibility."""
        return {
            "olt_rx": self.olt_rx,
            "onu_rx": self.onu_rx,
            "onu_tx": self.onu_tx,
            "temperature": self.temperature,
            "bias_current": self.bias_current,
            "voltage": self.voltage,
            "distance": self.distance,
            "status": self.status,
            "offline_reason": self.offline_reason,
            "serial_number": self.serial_number,
        }


@dataclass(frozen=True)
class SignalScales:
    """Scale factors for converting raw SNMP values to real units."""

    signal_dbm: float = 0.01  # Signal values in 0.01 dBm units
    temperature_c: float = 1.0  # Temperature in degrees C
    voltage_v: float = 0.01  # Voltage in V
    bias_current_ma: float = 0.001  # Bias current in mA


@dataclass
class OntCandidate:
    """An ONT discovered via autofind."""

    serial: str
    pon_port: str
    state: str = "unknown"
    model: str = ""
    vendor: str = ""
    distance_m: int | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServicePortInfo:
    """Parsed service port information."""

    index: int
    vlan: int
    fsp: str
    ont_id: int
    gem_port: int
    user_vlan: int | None = None
    state: str = "unknown"
    raw_data: dict[str, Any] = field(default_factory=dict)


# Sentinel values indicating invalid/unavailable optics readings
SIGNAL_SENTINELS: set[int] = {
    2147483647,
    2147483646,
    65535,
    65534,
    32767,
    -2147483648,
}


# ============================================================================
# Protocol Definition
# ============================================================================


@runtime_checkable
class OltVendorAdapter(Protocol):
    """Protocol for vendor-specific OLT operations.

    Implementations encapsulate all vendor-specific logic including:
    - SNMP OIDs and value scaling
    - SSH policies and transport settings
    - CLI command generation
    - Output parsing
    """

    @property
    def vendor_name(self) -> str:
        """Human-readable vendor name."""
        ...

    @property
    def supported_models(self) -> tuple[str, ...]:
        """Model identifiers this adapter supports."""
        ...

    # ========== SNMP Operations ==========

    def get_oid_set(self) -> OidSet:
        """Get SNMP OIDs for polling ONT data."""
        ...

    def get_signal_scale(self) -> float:
        """Get scale factor for signal dBm values (typically 0.01)."""
        ...

    def get_ddm_scales(self) -> SignalScales:
        """Get scale factors for DDM values (temp, voltage, bias)."""
        ...

    def decode_snmp_index(self, raw_index: int) -> str | None:
        """Decode vendor-specific SNMP index to FSP string.

        Args:
            raw_index: Raw SNMP index value

        Returns:
            Frame/Slot/Port string (e.g., "0/1/0") or None if invalid
        """
        ...

    def is_sentinel_value(self, value: int) -> bool:
        """Check if a value is a sentinel indicating invalid data."""
        ...

    # ========== SSH Operations ==========

    def get_ssh_policy(self, model: str | None = None) -> OltSshPolicy:
        """Get SSH transport policy for this vendor/model."""
        ...

    def supports_ssh(self) -> bool:
        """Whether this vendor supports SSH CLI access."""
        ...

    # ========== Command Generation ==========

    def generate_authorize_commands(
        self,
        fsp: str,
        serial: str,
        ont_id: int,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
        description: str = "",
    ) -> list[str]:
        """Generate commands to authorize an ONT.

        Args:
            fsp: Frame/Slot/Port (e.g., "0/1/0")
            serial: ONT serial number
            ont_id: ONT ID to assign
            line_profile_id: Line profile ID
            service_profile_id: Service profile ID
            description: Optional description

        Returns:
            List of CLI commands
        """
        ...

    def generate_service_port_command(
        self,
        fsp: str,
        ont_id: int,
        gem_index: int,
        vlan_id: int,
        *,
        user_vlan: int | str | None = None,
        tag_transform: str = "translate",
        port_index: int | None = None,
    ) -> str:
        """Generate command to create a service port."""
        ...

    def generate_delete_ont_commands(self, fsp: str, ont_id: int) -> list[str]:
        """Generate commands to delete/deauthorize an ONT."""
        ...

    # ========== Parsing ==========

    def parse_autofind_output(self, raw_output: str) -> list[OntCandidate]:
        """Parse autofind command output to list of ONT candidates."""
        ...

    def parse_service_port_output(self, raw_output: str) -> list[ServicePortInfo]:
        """Parse service port table output."""
        ...


# ============================================================================
# Base Implementation
# ============================================================================


class BaseOltAdapter(ABC, OltVendorAdapter):
    """Base class with common functionality for OLT adapters."""

    def is_sentinel_value(self, value: int) -> bool:
        """Check if value is a sentinel (common across vendors)."""
        return value in SIGNAL_SENTINELS

    def supports_ssh(self) -> bool:
        """Override in subclasses that support SSH."""
        return False

    def get_ssh_policy(self, model: str | None = None) -> OltSshPolicy:
        """Default: raise if SSH not supported."""
        raise NotImplementedError(
            f"{self.vendor_name} SSH not implemented. "
            "Consider SNMP or NETCONF for this vendor."
        )

    def decode_snmp_index(self, raw_index: int) -> str | None:
        """Default: no special decoding."""
        return None

    def generate_authorize_commands(
        self,
        fsp: str,
        serial: str,
        ont_id: int,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
        description: str = "",
    ) -> list[str]:
        raise NotImplementedError(f"{self.vendor_name} authorization not implemented")

    def generate_service_port_command(
        self,
        fsp: str,
        ont_id: int,
        gem_index: int,
        vlan_id: int,
        *,
        user_vlan: int | str | None = None,
        tag_transform: str = "translate",
        port_index: int | None = None,
    ) -> str:
        raise NotImplementedError(
            f"{self.vendor_name} service port command not implemented"
        )

    def generate_delete_ont_commands(self, fsp: str, ont_id: int) -> list[str]:
        raise NotImplementedError(f"{self.vendor_name} ONT deletion not implemented")

    def parse_autofind_output(self, raw_output: str) -> list[OntCandidate]:
        return []

    def parse_service_port_output(self, raw_output: str) -> list[ServicePortInfo]:
        return []


# ============================================================================
# Huawei Implementation
# ============================================================================


class HuaweiOltAdapter(BaseOltAdapter):
    """Huawei OLT adapter supporting MA5608T, MA5800, MA5600 series."""

    OIDS = OidSet(
        olt_rx=".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4",
        onu_rx=".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.6",
        onu_tx=".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.2",
        temperature=".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.3",
        bias_current=".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.5",
        voltage=".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.7",
        distance=".1.3.6.1.4.1.2011.6.128.1.1.2.46.1.20",
        status=".1.3.6.1.4.1.2011.6.128.1.1.2.46.1.15",
        offline_reason=".1.3.6.1.4.1.2011.6.128.1.1.2.43.1.12",
        serial_number=".1.3.6.1.4.1.2011.6.128.1.1.2.43.1.2",
    )

    SCALES = SignalScales(
        signal_dbm=0.01,
        temperature_c=0.1,  # Huawei uses 0.1°C units
        voltage_v=0.01,
        bias_current_ma=0.001,
    )

    # SSH policies per model
    _LEGACY_KEX = (
        "diffie-hellman-group-exchange-sha1",
        "diffie-hellman-group1-sha1",
    )
    _HOST_KEYS = ("ssh-rsa",)
    _MACS = ("hmac-sha1",)

    SSH_POLICIES = {
        "ma5608t": OltSshPolicy(
            key="huawei_ma5608t",
            kex=_LEGACY_KEX,
            host_key_types=_HOST_KEYS,
            ciphers=("aes128-cbc",),
            macs=_MACS,
        ),
        "ma5800": OltSshPolicy(
            key="huawei_ma5800",
            kex=_LEGACY_KEX,
            host_key_types=_HOST_KEYS,
            ciphers=("aes256-ctr",),
            macs=_MACS,
        ),
        "ma5600": OltSshPolicy(
            key="huawei_ma5600",
            kex=_LEGACY_KEX,
            host_key_types=_HOST_KEYS,
            ciphers=("aes128-cbc",),
            macs=_MACS,
        ),
    }

    # Packed FSP base value for SNMP index decoding
    _PACKED_FSP_BASE = 0xFA000000
    _PORTS_PER_SLOT = 32

    def __init__(self, model: str | None = None):
        self._model = model

    @property
    def vendor_name(self) -> str:
        return "Huawei"

    @property
    def supported_models(self) -> tuple[str, ...]:
        return ("MA5608T", "MA5800", "MA5800-X2", "MA5600", "MA5600T")

    def get_oid_set(self) -> OidSet:
        return self.OIDS

    def get_signal_scale(self) -> float:
        return self.SCALES.signal_dbm

    def get_ddm_scales(self) -> SignalScales:
        return self.SCALES

    def supports_ssh(self) -> bool:
        return True

    def get_ssh_policy(self, model: str | None = None) -> OltSshPolicy:
        """Get SSH policy based on model."""
        model_str = (model or self._model or "").lower()

        if "ma5608t" in model_str:
            return self.SSH_POLICIES["ma5608t"]
        if "ma5800" in model_str:
            return self.SSH_POLICIES["ma5800"]
        if "ma5600" in model_str:
            return self.SSH_POLICIES["ma5600"]

        raise ValueError(
            f"Huawei SSH policy is not configured for model {model!r}. "
            "Supported models: MA5608T, MA5800, MA5600."
        )

    def decode_snmp_index(self, raw_index: int) -> str | None:
        """Decode Huawei packed FSP index.

        Huawei encodes FSP as: base (0xFA000000) + (slot * ports_per_slot + port) * 256
        """
        if raw_index < self._PACKED_FSP_BASE:
            return None
        delta = raw_index - self._PACKED_FSP_BASE
        if delta % 256 != 0:
            return None
        slot_port = delta // 256
        frame = 0
        slot = slot_port // self._PORTS_PER_SLOT
        port = slot_port % self._PORTS_PER_SLOT
        if slot < 0 or port < 0:
            return None
        return f"{frame}/{slot}/{port}"

    def generate_authorize_commands(
        self,
        fsp: str,
        serial: str,
        ont_id: int,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
        description: str = "",
    ) -> list[str]:
        """Generate Huawei ONT authorization commands."""
        # Parse FSP
        parts = canonical_fsp(fsp)
        if parts is None:
            raise ValueError(f"Invalid FSP format: {fsp}")
        frame, slot, port = parts.frame, parts.slot, parts.port

        commands = [
            f"interface gpon {frame}/{slot}",
        ]

        # Build ont add command
        ont_cmd = f"ont add {port} {ont_id} sn-auth {serial}"
        if line_profile_id is not None:
            ont_cmd += f" omci ont-lineprofile-id {line_profile_id}"
        if service_profile_id is not None:
            ont_cmd += f" ont-srvprofile-id {service_profile_id}"
        if description:
            # Escape quotes in description
            safe_desc = description.replace('"', "'")[:64]
            ont_cmd += f' desc "{safe_desc}"'

        commands.append(ont_cmd)
        commands.append("quit")

        return commands

    def generate_service_port_command(
        self,
        fsp: str,
        ont_id: int,
        gem_index: int,
        vlan_id: int,
        *,
        user_vlan: int | str | None = None,
        tag_transform: str = "translate",
        port_index: int | None = None,
    ) -> str:
        """Generate Huawei service-port command."""
        resolved_user_vlan = user_vlan if user_vlan is not None else vlan_id

        if port_index is not None:
            return (
                f"service-port {port_index} vlan {vlan_id} gpon {fsp} "
                f"ont {ont_id} gemport {gem_index} "
                f"multi-service user-vlan {resolved_user_vlan} "
                f"tag-transform {tag_transform}"
            )
        else:
            return (
                f"service-port vlan {vlan_id} gpon {fsp} "
                f"ont {ont_id} gemport {gem_index} "
                f"multi-service user-vlan {resolved_user_vlan} "
                f"tag-transform {tag_transform}"
            )

    def generate_delete_ont_commands(self, fsp: str, ont_id: int) -> list[str]:
        """Generate Huawei ONT deletion commands."""
        parts = canonical_fsp(fsp)
        if parts is None:
            raise ValueError(f"Invalid FSP format: {fsp}")
        frame, slot, port = parts.frame, parts.slot, parts.port

        return [
            f"interface gpon {frame}/{slot}",
            f"ont delete {port} {ont_id}",
            "quit",
        ]

    def parse_autofind_output(self, raw_output: str) -> list[OntCandidate]:
        """Parse Huawei autofind output.

        Example output:
        Number    FSP       SerialNumber        Password
        1         0/1/0     HWTC12345678        -
        2         0/1/0     HWTC87654321        -
        """
        candidates: list[OntCandidate] = []

        # Pattern for Huawei autofind table rows
        pattern = re.compile(
            r"^\s*(\d+)\s+"  # Number
            r"(\d+/\d+/\d+)\s+"  # FSP
            r"([A-Z0-9]+)\s+"  # Serial
            r"(\S+)",  # Password
            re.MULTILINE,
        )

        for match in pattern.finditer(raw_output):
            candidates.append(
                OntCandidate(
                    serial=match.group(3),
                    pon_port=match.group(2),
                    state="discovered",
                    raw_data={
                        "number": match.group(1),
                        "password": match.group(4),
                    },
                )
            )

        return candidates

    def parse_service_port_output(self, raw_output: str) -> list[ServicePortInfo]:
        """Parse Huawei service port table output.

        Example:
        INDEX   VLAN  VLAN  PORT  F/S/P   ONT  GEM  ...
                ID   ATTR  TYPE         ID   ID
        1       100   ...  gpon  0/1/0   1    1    ...
        """
        ports: list[ServicePortInfo] = []

        # Simplified pattern - real implementation uses TextFSM
        pattern = re.compile(
            r"^\s*(\d+)\s+"  # Index
            r"(\d+)\s+"  # VLAN
            r"\S+\s+"  # VLAN attr
            r"\S+\s+"  # Port type
            r"(\d+/\d+/\d+)\s+"  # FSP
            r"(\d+)\s+"  # ONT ID
            r"(\d+)",  # GEM ID
            re.MULTILINE,
        )

        for match in pattern.finditer(raw_output):
            ports.append(
                ServicePortInfo(
                    index=int(match.group(1)),
                    vlan=int(match.group(2)),
                    fsp=match.group(3),
                    ont_id=int(match.group(4)),
                    gem_port=int(match.group(5)),
                )
            )

        return ports


# ============================================================================
# ZTE Implementation
# ============================================================================


class ZteOltAdapter(BaseOltAdapter):
    """ZTE OLT adapter supporting C300/C600 series."""

    OIDS = OidSet(
        olt_rx=".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.2",
        onu_rx=".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.3",
        onu_tx=".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.4",
        temperature=".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.5",
        bias_current=".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.8",
        voltage=".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.6",
        distance=".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.7",
        status=".1.3.6.1.4.1.3902.1082.500.10.2.2.1.1.10",
        offline_reason=".1.3.6.1.4.1.3902.1082.500.10.2.2.1.1.11",
        serial_number=".1.3.6.1.4.1.3902.1082.500.10.2.2.1.1.3",
    )

    SCALES = SignalScales(
        signal_dbm=0.01,
        temperature_c=1.0,
        voltage_v=0.01,
        bias_current_ma=0.002,  # ZTE uses 0.002 mA units
    )

    def __init__(self, model: str | None = None):
        self._model = model

    @property
    def vendor_name(self) -> str:
        return "ZTE"

    @property
    def supported_models(self) -> tuple[str, ...]:
        return ("C300", "C600", "C320", "C220")

    def get_oid_set(self) -> OidSet:
        return self.OIDS

    def get_signal_scale(self) -> float:
        return self.SCALES.signal_dbm

    def get_ddm_scales(self) -> SignalScales:
        return self.SCALES

    def supports_ssh(self) -> bool:
        # ZTE SSH could be implemented but SNMP/NETCONF preferred
        return False


# ============================================================================
# Nokia Implementation
# ============================================================================


class NokiaOltAdapter(BaseOltAdapter):
    """Nokia (Alcatel-Lucent) OLT adapter."""

    OIDS = OidSet(
        olt_rx=".1.3.6.1.4.1.637.61.1.35.10.14.1.2",
        onu_rx=".1.3.6.1.4.1.637.61.1.35.10.14.1.4",
        onu_tx=".1.3.6.1.4.1.637.61.1.35.10.14.1.3",
        temperature=".1.3.6.1.4.1.637.61.1.35.10.14.1.5",
        bias_current=".1.3.6.1.4.1.637.61.1.35.10.14.1.7",
        voltage=".1.3.6.1.4.1.637.61.1.35.10.14.1.6",
        distance=".1.3.6.1.4.1.637.61.1.35.10.1.1.9",
        status=".1.3.6.1.4.1.637.61.1.35.10.1.1.8",
        offline_reason=".1.3.6.1.4.1.637.61.1.35.10.1.1.10",
        serial_number=".1.3.6.1.4.1.637.61.1.35.10.1.1.3",
    )

    SCALES = SignalScales(
        signal_dbm=0.01,
        temperature_c=1.0,
        voltage_v=0.001,  # Nokia uses 0.001V units
        bias_current_ma=0.001,
    )

    def __init__(self, model: str | None = None):
        self._model = model

    @property
    def vendor_name(self) -> str:
        return "Nokia"

    @property
    def supported_models(self) -> tuple[str, ...]:
        return ("7360", "7362", "7368")

    def get_oid_set(self) -> OidSet:
        return self.OIDS

    def get_signal_scale(self) -> float:
        return self.SCALES.signal_dbm

    def get_ddm_scales(self) -> SignalScales:
        return self.SCALES

    def supports_ssh(self) -> bool:
        # Nokia typically uses TL1/NETCONF
        return False


# ============================================================================
# Generic/Fallback Implementation
# ============================================================================


class GenericOltAdapter(BaseOltAdapter):
    """Generic OLT adapter using ITU-T G.988 standard GPON MIB."""

    OIDS = OidSet(
        olt_rx=".1.3.6.1.4.1.17409.2.3.6.10.1.2",
        onu_rx=".1.3.6.1.4.1.17409.2.3.6.10.1.3",
        onu_tx=".1.3.6.1.4.1.17409.2.3.6.10.1.4",
        temperature=".1.3.6.1.4.1.17409.2.3.6.10.1.5",
        bias_current=".1.3.6.1.4.1.17409.2.3.6.10.1.7",
        voltage=".1.3.6.1.4.1.17409.2.3.6.10.1.6",
        distance=".1.3.6.1.4.1.17409.2.3.6.1.1.9",
        status=".1.3.6.1.4.1.17409.2.3.6.1.1.8",
    )

    SCALES = SignalScales()  # Default scales

    def __init__(self, model: str | None = None):
        self._model = model

    @property
    def vendor_name(self) -> str:
        return "Generic"

    @property
    def supported_models(self) -> tuple[str, ...]:
        return ()

    def get_oid_set(self) -> OidSet:
        return self.OIDS

    def get_signal_scale(self) -> float:
        return self.SCALES.signal_dbm

    def get_ddm_scales(self) -> SignalScales:
        return self.SCALES


# ============================================================================
# Factory
# ============================================================================


_VENDOR_ADAPTERS: dict[str, type[BaseOltAdapter]] = {
    "huawei": HuaweiOltAdapter,
    "zte": ZteOltAdapter,
    "nokia": NokiaOltAdapter,
    "alcatel": NokiaOltAdapter,  # Alias
}


def get_olt_adapter(
    olt: OLTDevice | None = None,
    *,
    vendor: str | None = None,
    model: str | None = None,
) -> OltVendorAdapter:
    """Get the appropriate OLT adapter for a vendor/model.

    Args:
        olt: OLTDevice instance (extracts vendor/model automatically)
        vendor: Vendor name override
        model: Model name override

    Returns:
        Appropriate OltVendorAdapter implementation

    Examples:
        # From OLTDevice
        adapter = get_olt_adapter(olt)

        # Manual specification
        adapter = get_olt_adapter(vendor="huawei", model="MA5800")
    """
    if olt is not None:
        vendor = vendor or olt.vendor
        model = model or olt.model

    vendor_lower = (vendor or "").lower().strip()

    for key, adapter_class in _VENDOR_ADAPTERS.items():
        if key in vendor_lower:
            return adapter_class(model=model)

    logger.debug(
        "No specific adapter for vendor '%s', using generic adapter",
        vendor,
    )
    return GenericOltAdapter(model=model)


def get_adapter_for_vendor(vendor: str, model: str | None = None) -> OltVendorAdapter:
    """Convenience function to get adapter by vendor string."""
    return get_olt_adapter(vendor=vendor, model=model)


# ============================================================================
# Compatibility Functions (for gradual migration)
# ============================================================================


def resolve_oid_set(vendor: str) -> dict[str, str]:
    """Legacy compatibility: Get OID dict for a vendor.

    Deprecated: Use get_olt_adapter(vendor=vendor).get_oid_set().to_dict()
    """
    adapter = get_olt_adapter(vendor=vendor)
    return adapter.get_oid_set().to_dict()


def get_signal_scale(vendor: str) -> float:
    """Legacy compatibility: Get signal scale for a vendor.

    Deprecated: Use get_olt_adapter(vendor=vendor).get_signal_scale()
    """
    return get_olt_adapter(vendor=vendor).get_signal_scale()


def get_ddm_scales(vendor: str) -> dict[str, float]:
    """Legacy compatibility: Get DDM scales for a vendor.

    Deprecated: Use get_olt_adapter(vendor=vendor).get_ddm_scales()
    """
    scales = get_olt_adapter(vendor=vendor).get_ddm_scales()
    return {
        "temperature": scales.temperature_c,
        "voltage": scales.voltage_v,
        "bias_current": scales.bias_current_ma,
    }
