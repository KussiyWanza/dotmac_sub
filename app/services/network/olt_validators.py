"""Input validation utilities for OLT SSH operations.

This module provides strict validation for all user-supplied input
that will be interpolated into OLT CLI commands. This prevents
command injection and ensures data integrity.

SECURITY: All user input MUST be validated before use in CLI commands.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from app.services.network.parsers.cli import FspParts, canonical_fsp


class ValidationError(Exception):
    """Raised when input validation fails."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field
        self.message = message


class OltSshError(Exception):
    """Base exception for OLT SSH operations."""

    pass


class OltConnectionError(OltSshError):
    """Failed to establish SSH connection to OLT."""

    pass


class OltCommandError(OltSshError):
    """OLT rejected or failed to execute command."""

    pass


class OltTimeoutError(OltSshError):
    """SSH operation timed out."""

    pass


@dataclass(frozen=True)
class ValidationResult:
    """Result of a validation check."""

    valid: bool
    error: str | None = None
    sanitized_value: str | None = None


# Regex patterns for validation. F/S/P shape lives in parsers.cli — one owner.
_SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9\-]{4,32}$")
_PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_\-. ]{1,64}$")
_VLAN_RANGE = range(1, 4095)
_ONT_ID_RANGE = range(0, 256)
_GEM_INDEX_RANGE = range(0, 256)
_PORT_NUMBER_RANGE = range(0, 256)

# Characters that could be used for command injection
_DANGEROUS_CHARS = frozenset("\n\r;|&`$(){}[]<>\\\"'")


def validate_ip_address(ip: str, field: str = "ip_address") -> str:
    """Validate and normalize an IPv4 or IPv6 address.

    Args:
        ip: IP address string to validate.
        field: Field name for error messages.

    Returns:
        Normalized IP address string.

    Raises:
        ValidationError: If IP address is invalid.
    """
    if not ip or not isinstance(ip, str):
        raise ValidationError(f"{field} is required", field)

    ip = ip.strip()

    # Check for dangerous characters
    if any(c in ip for c in _DANGEROUS_CHARS):
        raise ValidationError(
            f"{field} contains invalid characters",
            field,
        )

    try:
        # Parse and normalize the IP address
        addr = ipaddress.ip_address(ip)
        return str(addr)
    except ValueError as e:
        raise ValidationError(f"{field} is not a valid IP address: {ip}", field) from e


def validate_subnet_mask(mask: str, field: str = "subnet_mask") -> str:
    """Validate a subnet mask in dotted-decimal notation.

    Args:
        mask: Subnet mask string (e.g., "255.255.255.0").
        field: Field name for error messages.

    Returns:
        Validated subnet mask string.

    Raises:
        ValidationError: If subnet mask is invalid.
    """
    if not mask or not isinstance(mask, str):
        raise ValidationError(f"{field} is required", field)

    mask = mask.strip()

    # Check for dangerous characters
    if any(c in mask for c in _DANGEROUS_CHARS):
        raise ValidationError(f"{field} contains invalid characters", field)

    # Validate as IP address format first
    try:
        ipaddress.ip_address(mask)
    except ValueError as e:
        raise ValidationError(
            f"{field} is not a valid subnet mask: {mask}", field
        ) from e

    # Verify it's a valid subnet mask (contiguous 1s followed by 0s)
    parts = mask.split(".")
    if len(parts) != 4:
        raise ValidationError(f"{field} must be in dotted-decimal format", field)

    try:
        binary = "".join(format(int(p), "08b") for p in parts)
        # Valid mask: all 1s followed by all 0s
        if "01" in binary:
            raise ValidationError(f"{field} is not a valid subnet mask: {mask}", field)
    except ValueError as e:
        raise ValidationError(
            f"{field} is not a valid subnet mask: {mask}", field
        ) from e

    return mask


def validate_fsp_parts(fsp: str, field: str = "fsp") -> FspParts:
    """Validate a Frame/Slot/Port and return its canonical typed parts.

    Shape and prefix normalization are owned by
    ``app.services.network.parsers.cli.canonical_fsp``; this adds the Huawei
    range checks and the raising contract. Callers build device commands from
    the returned :class:`FspParts` rather than re-splitting a string, so a
    port-name prefix can never survive into a command.

    Args:
        fsp: F/S/P string (e.g., "0/2/1" or "gpon-0/2/1").
        field: Field name for error messages.

    Raises:
        ValidationError: If F/S/P format is invalid or out of range.
    """
    if not fsp or not isinstance(fsp, str):
        raise ValidationError(f"{field} is required", field)

    parts = canonical_fsp(fsp)
    if parts is None:
        raise ValidationError(
            f"{field} must be in format 'F/S/P' (e.g., '0/2/1'): {fsp.strip()}",
            field,
        )

    frame, slot, port = int(parts.frame), int(parts.slot), int(parts.port)

    # Reasonable upper bounds for Huawei OLTs
    if frame > 63:
        raise ValidationError(f"{field} frame number too large: {frame}", field)
    if slot > 31:
        raise ValidationError(f"{field} slot number too large: {slot}", field)
    if port > 255:
        raise ValidationError(f"{field} port number too large: {port}", field)

    return parts


def validate_fsp(fsp: str, field: str = "fsp") -> str:
    """Validate a Frame/Slot/Port and return it in canonical string form."""
    return validate_fsp_parts(fsp, field).fsp


def validate_serial_number(serial: str, field: str = "serial_number") -> str:
    """Validate ONT serial number format.

    Args:
        serial: Serial number string (e.g., "HWTC-7D4733C3").
        field: Field name for error messages.

    Returns:
        Validated serial number string.

    Raises:
        ValidationError: If serial number format is invalid.
    """
    if not serial or not isinstance(serial, str):
        raise ValidationError(f"{field} is required", field)

    serial = serial.strip()

    if not _SERIAL_PATTERN.match(serial):
        raise ValidationError(
            f"{field} must be 4-32 alphanumeric characters (dashes allowed): {serial}",
            field,
        )

    return serial


def validate_ont_id(ont_id: int, field: str = "ont_id") -> int:
    """Validate ONT ID is within valid range.

    Args:
        ont_id: ONT ID number.
        field: Field name for error messages.

    Returns:
        Validated ONT ID.

    Raises:
        ValidationError: If ONT ID is out of range.
    """
    if not isinstance(ont_id, int):
        raise ValidationError(f"{field} must be an integer", field)

    if ont_id not in _ONT_ID_RANGE:
        raise ValidationError(f"{field} must be 0-255: {ont_id}", field)

    return ont_id


def validate_vlan_id(vlan_id: int, field: str = "vlan_id") -> int:
    """Validate VLAN ID is within valid range.

    Args:
        vlan_id: VLAN ID number.
        field: Field name for error messages.

    Returns:
        Validated VLAN ID.

    Raises:
        ValidationError: If VLAN ID is out of range.
    """
    if not isinstance(vlan_id, int):
        raise ValidationError(f"{field} must be an integer", field)

    if vlan_id not in _VLAN_RANGE:
        raise ValidationError(f"{field} must be 1-4094: {vlan_id}", field)

    return vlan_id


def validate_gem_index(gem_index: int, field: str = "gem_index") -> int:
    """Validate GEM port index is within valid range.

    Args:
        gem_index: GEM index number.
        field: Field name for error messages.

    Returns:
        Validated GEM index.

    Raises:
        ValidationError: If GEM index is out of range.
    """
    if not isinstance(gem_index, int):
        raise ValidationError(f"{field} must be an integer", field)

    if gem_index not in _GEM_INDEX_RANGE:
        raise ValidationError(f"{field} must be 0-255: {gem_index}", field)

    return gem_index


def validate_profile_name(name: str, field: str = "profile_name") -> str:
    """Validate OLT profile name.

    Args:
        name: Profile name string.
        field: Field name for error messages.

    Returns:
        Validated profile name.

    Raises:
        ValidationError: If profile name is invalid.
    """
    if not name or not isinstance(name, str):
        raise ValidationError(f"{field} is required", field)

    name = name.strip()

    if not _PROFILE_NAME_PATTERN.match(name):
        raise ValidationError(
            f"{field} must be 1-64 alphanumeric characters "
            "(underscores, dashes, dots, spaces allowed): {name}",
            field,
        )

    return name


def validate_url(url: str, field: str = "url") -> str:
    """Validate URL format for ACS configuration.

    Args:
        url: URL string.
        field: Field name for error messages.

    Returns:
        Validated URL string.

    Raises:
        ValidationError: If URL is invalid or contains dangerous characters.
    """
    if not url or not isinstance(url, str):
        raise ValidationError(f"{field} is required", field)

    url = url.strip()

    # Check for dangerous characters that could break CLI parsing
    if any(c in url for c in "\n\r;|&`"):
        raise ValidationError(f"{field} contains invalid characters", field)

    # Basic URL format validation
    if not url.startswith(("http://", "https://")):
        raise ValidationError(f"{field} must start with http:// or https://", field)

    # Length limit
    if len(url) > 512:
        raise ValidationError(f"{field} is too long (max 512 characters)", field)

    return url


def validate_cli_safe_string(
    value: str,
    field: str,
    *,
    max_length: int = 128,
    allow_spaces: bool = False,
) -> str:
    """Validate a string is safe for CLI interpolation.

    Args:
        value: String to validate.
        field: Field name for error messages.
        max_length: Maximum allowed length.
        allow_spaces: Whether spaces are allowed.

    Returns:
        Validated string.

    Raises:
        ValidationError: If string is unsafe for CLI use.
    """
    if not value or not isinstance(value, str):
        raise ValidationError(f"{field} is required", field)

    value = value.strip()

    if len(value) > max_length:
        raise ValidationError(
            f"{field} is too long (max {max_length} characters)",
            field,
        )

    # Check for dangerous characters
    dangerous_found = [c for c in value if c in _DANGEROUS_CHARS]
    if dangerous_found:
        raise ValidationError(
            f"{field} contains invalid characters: {dangerous_found}",
            field,
        )

    if not allow_spaces and " " in value:
        raise ValidationError(f"{field} cannot contain spaces", field)

    return value
