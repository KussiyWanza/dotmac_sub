"""Shared utilities, constants, and dataclasses for OLT SSH ONT operations."""

from __future__ import annotations

import logging
import re
import socket
import time
from dataclasses import dataclass

from paramiko.ssh_exception import SSHException

from app.services.network.huawei_cli_response import (
    HuaweiCliErrorCode,
    HuaweiDeviceOutcome,
    describe_huawei_rejection,
)
from app.services.network.parsers.cli import canonical_fsp

logger = logging.getLogger(__name__)

# Specific SSH-related exceptions that can occur during OLT operations
_SSH_CONNECTION_ERRORS = (
    SSHException,
    OSError,
    socket.timeout,
    TimeoutError,
    ConnectionError,
)

# Settle time after writing one complete command line, before reading the
# response. Shelves whose command profile sets ``requires_slow_send`` (MA5608T
# family) get the longer pace; the rest get the same 0.1s the read-path sender
# already uses. This is deliberately *between* commands — never inside one, see
# ``send_ont_command``.
_ATOMIC_SEND_PACE_SEC = 0.1
_SLOW_SHELF_PACE_SEC = 0.4

# Regex patterns for validation
_FSP_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{1,3}$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9\-]+$")
# Common PON port name prefixes to strip (case-insensitive)
_FSP_PREFIX_RE = re.compile(r"^(?:x?g?pon|epon|port|gei|ge|eth)[-_]?", re.IGNORECASE)


def normalize_fsp(fsp: str) -> str:
    """Normalize FSP by stripping common port name prefixes.

    Converts formats like:
        - "pon-0/2/3" -> "0/2/3"
        - "gpon-0/1/0" -> "0/1/0"
        - "xgpon-0/4/1" -> "0/4/1"
        - "0/2/3" -> "0/2/3" (unchanged)

    Args:
        fsp: Frame/Slot/Port string, possibly with prefix

    Returns:
        Normalized FSP without prefix
    """
    if not fsp:
        return fsp
    return _FSP_PREFIX_RE.sub("", fsp.strip())


@dataclass
class OntIphostConfig:
    """Configuration for a single ONT's IPHOST."""

    fsp: str  # Frame/Slot/Port e.g. "0/1/0"
    ont_id: int
    vlan_id: int
    ip_address: str
    subnet: str
    gateway: str | None = None  # Derived from IP if not provided
    ip_index: int = 0
    ip_mode: str = "static"
    priority: int | None = None
    serial_number: str | None = None  # For logging/tracking


@dataclass
class OntIphostResult:
    """Result of configuring a single ONT's IPHOST."""

    fsp: str
    ont_id: int
    success: bool
    message: str
    serial_number: str | None = None


@dataclass(frozen=True, slots=True)
class OntAuthorizationOutcome:
    """Typed result of one ``ont add ... sn-auth`` exchange.

    Carries the classified device code forward so callers branch on
    :attr:`code` instead of re-parsing :attr:`message`. ``ont_id`` is ``None``
    whenever the firmware accepted the write without naming an ONT-ID; the
    adapter resolves it by readback rather than guessing.
    """

    outcome: HuaweiDeviceOutcome
    ont_id: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome.succeeded

    @property
    def code(self) -> HuaweiCliErrorCode:
        return self.outcome.code

    @property
    def message(self) -> str:
        return self.outcome.message

    @property
    def device_was_silent(self) -> bool:
        """Whether the shelf returned no recognizable verdict at all.

        A silent shelf is not evidence of success and not evidence of
        rejection; the caller must confirm by reading the registration back.
        """
        return not self.succeeded and self.code is HuaweiCliErrorCode.NONE


@dataclass
class OntStatusEntry:
    """Status of a single registered ONT on an OLT port."""

    serial_number: str
    run_state: str
    config_state: str
    match_state: str


@dataclass
class RegisteredOntEntry:
    """An ONT serial registered on an OLT."""

    fsp: str
    onu_id: int
    real_serial: str
    run_state: str


@dataclass
class ServicePortDiagnostics:
    """Results from running service port diagnostics on an ONT."""

    ont_run_state: str
    ont_config_state: str
    ont_match_state: str
    ont_online: bool
    gem_ports: list[dict[str, str]]
    service_port_details: list[dict[str, str]]
    raw_outputs: dict[str, str]
    warnings: list[str]


def invalid_fsp_message(fsp: object) -> str:
    """One wording for a rejected Frame/Slot/Port, shared by every ONT write."""
    return f"Invalid F/S/P format: {fsp!r} (expected digits/digits/digits)"


def shelf_requires_pacing(olt) -> bool:
    """Whether this shelf needs extra settle time between config commands.

    Reads ``HuaweiCommandProfile.requires_slow_send``, which was previously
    computed and never consulted. Falls back to the conservative ``True`` if
    the profile cannot be resolved.
    """
    try:
        from app.services.network.huawei_command_profiles import (
            get_huawei_command_profile,
        )

        return get_huawei_command_profile(olt).requires_slow_send
    except Exception:  # pragma: no cover - profile resolution must never block a write
        logger.debug("Falling back to paced sends for OLT %r", olt, exc_info=True)
        return True


def send_ont_command(
    olt, channel, command: str, *, pace_sec: float | None = None
) -> None:
    """Write one ONT config command line to a Huawei shelf. Single owner.

    The line is always written **atomically**. Huawei line editors coalesce
    separately-written space characters while the editor is active, which is
    exactly what produced corrupted commands on Jabi's MA5608T
    (``ont internet-config414ip-index0``, ``undo ont wan-config414ip-index0``)
    even though the caller was using the old word-splitting "slow" sender.
    ``app.services.network.olt_ssh._send_huawei_command`` established the
    atomic-line rule for read paths; this is the same rule for ONT writes.

    ``requires_slow_send`` therefore now means *pace between commands*, not
    *split within one command*. Shelves that declare they do not need it
    (MA5800 profiles) get the shorter pace.
    """
    channel.send(f"{command}\n")
    delay = (
        pace_sec
        if pace_sec is not None
        else (
            _SLOW_SHELF_PACE_SEC
            if shelf_requires_pacing(olt)
            else _ATOMIC_SEND_PACE_SEC
        )
    )
    if delay > 0:
        time.sleep(delay)


def _run_ont_config_command(
    olt,
    fsp: str,
    command: str,
    *,
    success_message: str,
    timeout_sec: int = 12,
) -> tuple[bool, str]:
    """Run a single ONT-scoped config command on a GPON interface."""
    from app.services.network import olt_ssh as core

    parts = canonical_fsp(fsp)
    if parts is None:
        return False, invalid_fsp_message(fsp)

    frame_slot = parts.frame_slot

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)

        send_ont_command(olt, channel, f"interface gpon {frame_slot}")
        core._read_until_prompt(channel, config_prompt, timeout_sec=8)

        send_ont_command(olt, channel, command)
        # Strict read: a shelf that never returns to the prompt has not
        # accepted anything. The lenient read returned an empty buffer, which
        # carries no error marker, so the caller reported ``success_message``
        # for a command that timed out.
        output = core.read_until_prompt_strict(
            channel, config_prompt, timeout_sec=timeout_sec
        )

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "ONT config command failed on OLT %s: %s",
                olt.name,
                output.strip()[-150:],
            )
            return False, describe_huawei_rejection(output, detail_limit=150)
        return True, success_message
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error running ONT config command on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def _validate_fsp(fsp: str, *, allow_normalize: bool = True) -> tuple[bool, str]:
    """Validate Frame/Slot/Port format is strictly numeric (e.g. '0/2/1').

    Args:
        fsp: Frame/Slot/Port string to validate
        allow_normalize: If True, strip common prefixes before validation

    Returns:
        Tuple of (is_valid, error_message)
    """
    check_fsp = normalize_fsp(fsp) if allow_normalize else fsp
    if not _FSP_RE.match(check_fsp):
        return False, invalid_fsp_message(fsp)
    return True, ""


def _validate_serial(serial_number: str) -> tuple[bool, str]:
    """Validate ONT serial number contains only alphanumeric chars and dashes."""
    if not serial_number or not _SERIAL_RE.match(serial_number):
        return False, f"Invalid serial number format: {serial_number!r}"
    return True, ""


def _safe_profile_name(name: str | None) -> str:
    """Sanitize a profile name for use in OLT commands."""
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", " ", str(name or "ACS")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "ACS")[:48]
