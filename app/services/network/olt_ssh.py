"""OLT SSH connection helpers with model-specific transport policies.

This module provides SSH connectivity and CLI parsing for Huawei OLTs.
Parsing is done via TextFSM templates (see parsers/ subdirectory) with
fallback to legacy regex parsing for robustness.

SSH policies are now managed by olt_vendor_adapters.py. This module
provides the resolve_policy() function for backward compatibility.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from dataclasses import asdict, replace
from typing import Any, cast

from paramiko.channel import Channel
from paramiko.ssh_exception import SSHException
from paramiko.transport import Transport

from app.services.network.huawei_cli_response import describe_huawei_rejection

logger = logging.getLogger(__name__)

# Specific SSH-related exceptions that can occur during OLT operations
_SSH_CONNECTION_ERRORS = (
    SSHException,
    OSError,
    socket.timeout,
    TimeoutError,
    ConnectionError,
)

from app.config import settings
from app.models.network import OLTDevice
from app.services.credential_crypto import decrypt_credential
from app.services.network.olt_command_gen import build_service_port_command
from app.services.network.olt_ssh_ont._common import (
    _safe_profile_name,
    invalid_fsp_message,
)
from app.services.network.olt_ssh_ont.tr069 import bind_tr069_server_profile
from app.services.network.olt_ssh_profiles import (
    Tr069ServerProfile as Tr069ServerProfile,
)
from app.services.network.olt_ssh_profiles import (
    create_tr069_server_profile,
    get_tr069_server_profiles,
)

# Import SSH policy from adapter (re-export for backward compatibility)
from app.services.network.olt_vendor_adapters import (
    OltSshPolicy,
    get_olt_adapter,
)
from app.services.network.parsers import (
    HUAWEI_OPTIONAL_ARG_PROMPT,
    FirmwareInfo,
    ServicePortEntry,
    canonical_fsp,
    is_error_output,
    needs_huawei_command_confirm,
    normalize_fsp,
    parse_firmware_info,
    parse_service_port_table,
    parse_service_port_table_legacy,
    validate_fsp,
    validate_readonly_command,
    validate_serial,
)
from app.services.network.tr069_profile_matching import match_tr069_profile


def _normalized(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def resolve_policy(olt: OLTDevice) -> OltSshPolicy:
    """Get SSH policy for an OLT device.

    Delegates to the vendor adapter system for policy resolution.

    Args:
        olt: OLT device model instance

    Returns:
        SSH policy for the OLT's vendor/model

    Raises:
        ValueError: If vendor doesn't support SSH or is unknown
    """
    adapter = get_olt_adapter(olt)

    if not adapter.supports_ssh():
        raise ValueError(
            f"{adapter.vendor_name} OLT SSH not implemented. Model: {olt.model!r}. "
            "Consider SNMP or NETCONF for this vendor."
        )

    return adapter.get_ssh_policy(model=olt.model)


def _load_linked_acs_payload(olt: OLTDevice) -> dict[str, object] | None:
    """Load linked ACS config for legacy explicit TR-069 bind helper."""
    server = None
    try:
        server = getattr(olt, "tr069_acs_server", None)
    except AttributeError:
        server = None

    if server is None and getattr(olt, "tr069_acs_server_id", None):
        try:
            from app.models.tr069 import Tr069AcsServer
            from app.services.db_session_adapter import db_session_adapter

            with db_session_adapter.create_session() as db:
                server = db.get(Tr069AcsServer, str(olt.tr069_acs_server_id))
                if server is None:
                    return None
                password = (
                    decrypt_credential(server.cwmp_password)
                    if server.cwmp_password
                    else ""
                )
                return {
                    "name": server.name,
                    "acs_url": server.cwmp_url or "",
                    "username": server.cwmp_username or "",
                    "password": password or "",
                    "inform_interval": server.periodic_inform_interval
                    or settings.tr069_periodic_inform_interval,
                }
        except (ImportError, LookupError, AttributeError) as exc:
            logger.warning("Failed to load linked ACS for OLT %s: %s", olt.name, exc)
            return None

    if server is None or not getattr(server, "cwmp_url", None):
        return None
    password = (
        decrypt_credential(server.cwmp_password)
        if getattr(server, "cwmp_password", None)
        else ""
    )
    return {
        "name": getattr(server, "name", "ACS"),
        "acs_url": server.cwmp_url or "",
        "username": server.cwmp_username or "",
        "password": password or "",
        "inform_interval": getattr(server, "periodic_inform_interval", None)
        or settings.tr069_periodic_inform_interval,
    }


def _auto_bind_tr069_after_authorize(
    olt: OLTDevice, fsp: str, ont_id: int | None
) -> None:
    """Best-effort bind of a newly authorized ONT to the linked ACS profile."""
    if ont_id is None:
        return
    try:
        payload = _load_linked_acs_payload(olt)
        if payload is None or not str(payload.get("acs_url") or "").strip():
            logger.info("Skipping TR-069 auto-bind for OLT %s: no linked ACS", olt.name)
            return

        ok, _msg, profiles = get_tr069_server_profiles(olt)
        if not ok:
            return
        target_username = str(payload.get("username") or "").strip()
        profile = match_tr069_profile(
            profiles,
            acs_url=str(payload["acs_url"]),
            acs_username=target_username,
        )
        profile_id = profile.profile_id if profile else None

        if profile_id is None:
            inform_interval = payload.get("inform_interval")
            if inform_interval is None:
                logger.warning(
                    "Skipping TR-069 profile auto-create for OLT %s: "
                    "linked ACS has no inform interval",
                    olt.name,
                )
                return
            profile_name = f"ACS {_safe_profile_name(str(payload.get('name') or ''))}"
            ok, msg = create_tr069_server_profile(
                olt,
                profile_name=profile_name,
                acs_url=str(payload["acs_url"]),
                username=target_username,
                password=str(payload.get("password") or ""),
                inform_interval=int(str(inform_interval)),
            )
            if not ok:
                logger.warning(
                    "Auto-create TR-069 profile failed for OLT %s: %s",
                    olt.name,
                    msg,
                )
                return
            ok, _msg, profiles = get_tr069_server_profiles(olt)
            if not ok:
                return
            profile = match_tr069_profile(
                profiles,
                acs_url=str(payload["acs_url"]),
                acs_username=target_username,
            )
            profile_id = profile.profile_id if profile else None
        if profile_id is None:
            logger.warning(
                "Could not resolve TR-069 profile for linked ACS on OLT %s",
                olt.name,
            )
            return

        ok, msg = bind_tr069_server_profile(
            olt, fsp=fsp, ont_id=ont_id, profile_id=profile_id
        )
        if ok:
            logger.info(
                "Auto-bound ONT %d on %s to TR-069 profile %d",
                ont_id,
                fsp,
                profile_id,
            )
        else:
            logger.warning(
                "Auto-bind TR-069 failed for ONT %d on %s: %s",
                ont_id,
                fsp,
                msg,
            )
    except Exception as exc:
        logger.warning("TR-069 auto-bind skipped for OLT %s: %s", olt.name, exc)


def _apply_preferred_algorithms(transport: Transport, policy: OltSshPolicy) -> None:
    opts = transport.get_security_options()
    opts.kex = list(policy.kex) + [item for item in opts.kex if item not in policy.kex]
    opts.key_types = list(policy.host_key_types) + [
        item for item in opts.key_types if item not in policy.host_key_types
    ]
    opts.ciphers = list(policy.ciphers) + [
        item for item in opts.ciphers if item not in policy.ciphers
    ]
    opts.digests = list(policy.macs) + [
        item for item in opts.digests if item not in policy.macs
    ]


def _read_until_prompt(
    channel: Channel, prompt_regex: str, timeout_sec: float = 8.0
) -> str:
    import time

    compiled = re.compile(prompt_regex)
    buffer = ""
    deadline = time.monotonic() + timeout_sec
    channel.settimeout(0.8)
    while True:
        if time.monotonic() > deadline:
            return buffer
        try:
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
        except TimeoutError:
            if compiled.search(buffer):
                return buffer
            continue
        if not chunk:
            return buffer
        buffer += chunk
        if compiled.search(buffer):
            return buffer


def read_until_prompt_strict(
    channel: Channel, prompt_regex: str, timeout_sec: float = 8.0
) -> str:
    """Read to the prompt, or raise when the shelf never returns one.

    ``_read_until_prompt`` returns whatever it has when the deadline passes.
    On a *read* path that degrades to a short answer, but on a *write* path it
    is unsafe: an empty buffer carries no Huawei error marker, so callers
    concluded "no error, therefore success" for a command that timed out.
    Write paths must use this variant so a timeout stays a failure.
    """
    buffer = _read_until_prompt(channel, prompt_regex, timeout_sec=timeout_sec)
    if not re.search(prompt_regex, buffer):
        raise TimeoutError(
            f"Huawei shelf did not return a prompt within {timeout_sec}s; "
            "the command outcome is unknown"
        )
    return buffer


def _prime_shell_prompt(
    channel: Channel,
    prompt_regex: str,
    *,
    attempts: int = 2,
) -> str:
    """Wake an interactive shell and read the first prompt.

    Some Huawei OLTs do not emit a prompt immediately after ``invoke_shell()``.
    Sending a newline is harmless on devices that are already ready, and it
    reliably coaxes a prompt out of quieter MA5800 builds.
    """

    output = ""
    for attempt in range(attempts):
        channel.send("\n")
        chunk = _read_until_prompt(
            channel,
            prompt_regex,
            timeout_sec=8.0 if attempt == 0 else 4.0,
        )
        output += chunk
        if chunk.strip():
            break
    return output


def _derive_prompt_regex(output: str, fallback: str) -> str:
    """Return a prompt regex anchored to the actual device prompt.

    Huawei running configs use bare ``#`` lines as section separators, so a loose
    ``[>#]`` prompt matcher truncates config capture at the first separator.
    """
    for raw_line in reversed(output.splitlines()):
        line = raw_line.strip()
        if not line or line == "#":
            continue
        if re.search(r"[>#]\s*$", line):
            return rf"(?:^|\r?\n){re.escape(line)}\s*$"
    return fallback


def _send_huawei_command(
    channel: Channel,
    command: str,
    *,
    word_delay_sec: float = 0.1,
) -> None:
    """Send one complete command line to Huawei's interactive CLI.

    Huawei shelves can coalesce separately written space characters while the
    line editor is active (for example, receiving ``display traffictable...``).
    Sending the complete line atomically preserves command boundaries; the
    optional delay remains available for callers that need to yield before the
    device begins responding.
    """
    if command:
        channel.send(f"{command}\n")
        if word_delay_sec:
            time.sleep(word_delay_sec)


def _prepare_huawei_read_shell(channel: Channel, prompt_regex: str) -> str:
    """Enter privileged mode, derive its real prompt, and disable paging."""
    _send_huawei_command(channel, "enable", word_delay_sec=0)
    output = _read_until_prompt(
        channel,
        r"(?:^|\r?\n)[^\r\n]*#\s*$",
        timeout_sec=_setup_prompt_timeout(),
    )
    fallback_prompt = prompt_regex.replace(">", "#")
    if "#" not in fallback_prompt:
        fallback_prompt = r"#\s*$"
    privileged_prompt = _derive_prompt_regex(output, fallback_prompt)
    _run_huawei_cmd(
        channel,
        "screen-length 0 temporary",
        prompt=privileged_prompt,
    )
    return privileged_prompt


def _replace_policy_prompt_regex(
    policy: OltSshPolicy, prompt_regex: str
) -> OltSshPolicy:
    try:
        return replace(policy, prompt_regex=prompt_regex)
    except TypeError:
        mutable_policy = cast(Any, policy)
        mutable_policy.prompt_regex = prompt_regex
        return mutable_policy


def run_version_probe(olt: OLTDevice) -> tuple[str, str]:
    """SSH into an OLT and run the version command."""
    transport, channel, policy = _open_shell(olt)
    try:
        channel.send(f"{policy.version_command}\n")
        output = _read_until_prompt(channel, policy.prompt_regex, timeout_sec=12)
        return policy.key, output
    finally:
        transport.close()


def _normalize_fsp(fsp: str) -> str:
    """Normalize FSP by stripping common port name prefixes like 'pon-'."""
    return normalize_fsp(fsp)


def _validate_fsp(fsp: str) -> tuple[bool, str]:
    """Validate Frame/Slot/Port format is strictly numeric (e.g. '0/2/1')."""
    return validate_fsp(fsp)


def _validate_serial(serial_number: str) -> tuple[bool, str]:
    """Validate ONT serial number contains only alphanumeric chars and dashes."""
    return validate_serial(serial_number)


def _setup_prompt_timeout() -> float:
    """Prompt timeout for session-setup reads (``enable`` / ``screen-length``).

    Env-tunable via ``OLT_SSH_PROMPT_TIMEOUT_SECONDS`` (default 15, floor 5;
    resolved once at startup by ``app.config``). The old hardcoded 5s left
    ~1.5s headroom over a healthy ~3s connect+enable (measured against
    Jabi/Gwarimpa 2026-07-17) and failed intermittently under VTY contention
    or mgmt-path jitter.
    """
    return settings.olt_ssh_prompt_timeout_seconds


def _open_shell(olt: OLTDevice) -> tuple[Transport, Channel, OltSshPolicy]:
    """Open an SSH shell session to an OLT. Caller must close transport."""
    host = (olt.mgmt_ip or olt.hostname or "").strip()
    if not host:
        raise ValueError("Management IP or hostname is required")
    if not olt.ssh_username:
        raise ValueError("SSH username is required")
    if not olt.ssh_password:
        raise ValueError("SSH password is required")

    policy = resolve_policy(olt)
    password = decrypt_credential(olt.ssh_password)
    if not password:
        raise ValueError("SSH password could not be decrypted")

    port = int(olt.ssh_port or 22)
    sock = socket.create_connection((host, port), timeout=20)
    transport = Transport(sock)
    _apply_preferred_algorithms(transport, policy)
    transport.start_client(timeout=20)
    transport.auth_password(username=olt.ssh_username, password=password)
    if not transport.is_authenticated():
        transport.close()
        raise RuntimeError("SSH authentication failed")
    channel = transport.open_session(timeout=20)
    # Use wider PTY and set terminal type to avoid control sequence issues
    channel.get_pty(term="dumb", width=400, height=50)
    channel.invoke_shell()
    initial_output = _prime_shell_prompt(channel, policy.prompt_regex)
    prompt_regex = _derive_prompt_regex(initial_output, policy.prompt_regex)
    if prompt_regex != policy.prompt_regex:
        policy = _replace_policy_prompt_regex(policy, prompt_regex)
    return transport, channel, policy


def _cached_service_ports(olt: OLTDevice, fsp: str) -> list[ServicePortEntry] | None:
    from app.services.network.olt_read_cache import olt_cache

    cached = olt_cache.get_service_ports(str(olt.id), fsp)
    if cached is None:
        return None
    try:
        return [ServicePortEntry(**entry) for entry in cached]
    except (TypeError, ValueError) as exc:
        logger.debug(
            "Ignoring invalid cached service-port entries for OLT %s %s: %s",
            olt.id,
            fsp,
            exc,
        )
        olt_cache.invalidate(str(olt.id), "service_ports")
        return None


def _cache_service_ports(
    olt: OLTDevice, fsp: str, entries: list[ServicePortEntry]
) -> None:
    from app.services.network.olt_read_cache import olt_cache

    olt_cache.set_service_ports(str(olt.id), fsp, [asdict(entry) for entry in entries])


def _invalidate_olt_read_cache(
    olt: OLTDevice,
    *operations: str,
) -> None:
    from app.services.network.olt_read_cache import olt_cache

    for operation in operations:
        olt_cache.invalidate(str(olt.id), operation)


def _parse_service_port_table_legacy(output: str) -> list[ServicePortEntry]:
    """Backward-compatible wrapper for the parser package implementation."""
    return parse_service_port_table_legacy(output)


def _parse_service_port_table(output: str) -> list[ServicePortEntry]:
    """Parse Huawei ``display service-port`` output into structured entries.

    Uses TextFSM template for robust parsing with fallback to legacy regex.
    """
    return parse_service_port_table(output).data


def _needs_huawei_command_confirm(output: str) -> bool:
    """Return true when Huawei CLI is waiting for Enter to accept defaults."""
    return needs_huawei_command_confirm(output)


def _run_huawei_cmd(channel: Channel, command: str, prompt: str = r"#\s*$") -> str:
    """Send a command to a Huawei shell, accepting optional-argument prompts."""
    logger.debug("OLT command: %r", command)
    _send_huawei_command(channel, command)
    out = _read_until_prompt(
        channel, rf"{prompt}|<cr>|{HUAWEI_OPTIONAL_ARG_PROMPT}", timeout_sec=12
    )
    if _needs_huawei_command_confirm(out):
        channel.send("\n")
        out = _read_until_prompt(channel, prompt, timeout_sec=12)
    return out


def _run_huawei_paged_cmd(
    channel: Channel, command: str, prompt: str = r"#\s*$", *, timeout_sec: int = 60
) -> str:
    """Send a paged command within one cumulative deadline."""
    logger.debug("OLT paged command: %r", command)
    _send_huawei_command(channel, command)
    output_parts: list[str] = []
    pager_pattern = r"---- More(?:\s*\([^)]*\)\s*)?----|<cr>|Press any key"
    combined_pattern = rf"{prompt}|{pager_pattern}"
    deadline = time.monotonic() + max(float(timeout_sec), 0.1)

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Huawei command did not return a shell prompt within {timeout_sec}s"
            )
        chunk = _read_until_prompt(
            channel,
            combined_pattern,
            timeout_sec=remaining,
        )
        output_parts.append(chunk)

        # A single recv can contain an earlier continuation marker and the final
        # prompt. The anchored shell prompt is authoritative when both appear.
        if re.search(prompt, chunk):
            break
        elif "---- More" in chunk or "Press any key" in chunk:
            channel.send(" ")  # Send space to continue
            continue
        elif "<cr>" in chunk:
            channel.send("\n")
            continue
        else:
            raise TimeoutError(
                f"Huawei command output ended before the shell prompt within {timeout_sec}s"
            )

    return "".join(output_parts)


def get_service_ports(
    olt: OLTDevice, fsp: str
) -> tuple[bool, str, list[ServicePortEntry]]:
    """SSH into OLT and list service-ports on a PON port.

    Returns:
        Tuple of (success, message, list of ServicePortEntry).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err, []

    cached_entries = _cached_service_ports(olt, fsp)
    if cached_entries is not None:
        return (
            True,
            f"Found {len(cached_entries)} service-ports on {fsp} (cached)",
            cached_entries,
        )

    try:
        from app.services.network.olt_ssh_pool import pooled_ssh_connection

        ssh_context = pooled_ssh_connection(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []

    try:
        with ssh_context as (channel, policy):
            prompt = _prepare_huawei_read_shell(channel, policy.prompt_regex)

            output = _run_huawei_paged_cmd(
                channel,
                f"display service-port port {fsp}",
                prompt=prompt,
            )
            entries = _parse_service_port_table(output)
            _cache_service_ports(olt, fsp, entries)
            return True, f"Found {len(entries)} service-ports on {fsp}", entries
    except (*_SSH_CONNECTION_ERRORS, RuntimeError, ValueError) as exc:
        logger.error(
            "Error reading service-ports from OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}", []


def create_service_ports(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    reference_ports: list[ServicePortEntry],
) -> tuple[bool, str]:
    """Create service-ports on an OLT for a new ONT, copying VLAN pattern from reference.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port e.g. "0/2/1".
        ont_id: The ONT-ID assigned by the OLT during authorization.
        reference_ports: Service-port entries from a reference ONT to replicate.

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err
    if not reference_ports:
        return False, "No reference service-ports to replicate"

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=_setup_prompt_timeout())
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=_setup_prompt_timeout())

        config_prompt = r"[#)]\s*$"
        _run_huawei_cmd(channel, "config", prompt=config_prompt)

        created = 0
        errors = 0
        for sp in reference_ports:
            user_vlan: int | str | None = None
            if str(getattr(sp, "flow_para", "") or "").isdigit():
                user_vlan = int(str(sp.flow_para))
            elif getattr(sp, "flow_para", None) in {"untagged", "transparent"}:
                user_vlan = str(sp.flow_para)

            tag_transform = getattr(sp, "tag_transform", None) or "translate"
            if user_vlan == "untagged" and tag_transform == "translate":
                tag_transform = "default"

            cmd = build_service_port_command(
                fsp=fsp,
                ont_id=ont_id,
                gem_index=sp.gem_index,
                vlan_id=sp.vlan_id,
                user_vlan=user_vlan,
                tag_transform=tag_transform,
            )
            output = _run_huawei_cmd(channel, cmd, prompt=config_prompt)
            if is_error_output(output):
                logger.warning(
                    "Service-port VLAN %d GEM %d failed on OLT %s: %s",
                    sp.vlan_id,
                    sp.gem_index,
                    olt.name,
                    output.strip()[-150:],
                )
                errors += 1
            else:
                created += 1

        _run_huawei_cmd(channel, "quit", prompt=config_prompt)

        msg = f"Created {created} service-port(s) for ONT {ont_id} on {fsp}"
        if errors:
            msg += f" ({errors} failed)"
        logger.info(msg)
        _invalidate_olt_read_cache(olt, "service_ports", "running_config")
        return errors == 0, msg
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error creating service-ports on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def upgrade_firmware(
    olt: OLTDevice, file_url: str, *, method: str = "sftp"
) -> tuple[bool, str]:
    """Trigger firmware upgrade on an OLT via SSH.

    Args:
        olt: The OLT device to upgrade.
        file_url: URL/path of the firmware file (e.g. sftp://user:pass@host/path).
        method: Transfer method — sftp, tftp, or ftp.

    Returns:
        Tuple of (success, message).
    """
    # Reject newlines and shell metacharacters in file_url
    if not file_url or "\n" in file_url or "\r" in file_url or ";" in file_url:
        return False, "Invalid firmware URL"

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=_setup_prompt_timeout())
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=_setup_prompt_timeout())

        # Huawei firmware upgrade command
        cmd = f"system-software upgrade {file_url}"
        output = _run_huawei_cmd(channel, cmd, prompt=r"#\s*$|y/n")

        # Huawei may ask for confirmation
        if "y/n" in output.lower():
            channel.send("y\n")
            output += _read_until_prompt(channel, r"#\s*$", timeout_sec=30)

        if "success" in output.lower() or "download" in output.lower():
            logger.info("Firmware upgrade initiated on OLT %s: %s", olt.name, file_url)
            return (
                True,
                "Firmware upgrade initiated — OLT will reboot when download completes",
            )
        if is_error_output(output):
            logger.warning(
                "Firmware upgrade failed on OLT %s: %s", olt.name, output.strip()[-200:]
            )
            return False, describe_huawei_rejection(output, action="firmware upgrade")

        logger.info(
            "Firmware upgrade command sent to OLT %s, output: %s",
            olt.name,
            output.strip()[-200:],
        )
        return True, "Firmware upgrade command sent"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error during firmware upgrade on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def _parse_firmware_info(output: str) -> FirmwareInfo:
    """Backward-compatible wrapper for the parser package implementation."""
    return parse_firmware_info(output)


def get_firmware_info(olt: OLTDevice) -> tuple[bool, str, FirmwareInfo]:
    """Get current and standby firmware versions from OLT via 'display version'.

    Args:
        olt: The OLT device.

    Returns:
        Tuple of (success, message, FirmwareInfo).
    """
    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", FirmwareInfo()

    try:
        prompt = _prepare_huawei_read_shell(channel, policy.prompt_regex)

        output = _run_huawei_paged_cmd(
            channel,
            "display version",
            prompt=prompt,
            timeout_sec=30,
        )

        info = _parse_firmware_info(output)

        if not info.current_version:
            return False, "Could not parse firmware version from output", info

        message = f"Running: {info.current_version}"
        if info.standby_version:
            message += f", Standby: {info.standby_version}"

        logger.info(
            "Retrieved firmware info from OLT %s: %s",
            olt.name,
            message,
        )
        return True, message, info
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error getting firmware info from OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}", FirmwareInfo()
    finally:
        transport.close()


def rollback_firmware(olt: OLTDevice) -> tuple[bool, str]:
    """Switch to standby/backup firmware image.

    This issues 'startup system-software' to swap the active and standby images.
    The OLT will boot to the previous image on next reboot.

    Args:
        olt: The OLT device.

    Returns:
        Tuple of (success, message).
    """
    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        _read_until_prompt(
            channel, policy.prompt_regex, timeout_sec=_setup_prompt_timeout()
        )

        # First check if we have dual image support
        ok, msg, info = get_firmware_info(olt)
        if not info.has_dual_image:
            return False, "OLT does not appear to have dual-image support"
        if not info.standby_version:
            return False, "No standby firmware version available for rollback"

        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(
            channel, policy.prompt_regex, timeout_sec=_setup_prompt_timeout()
        )

        # Switch startup system software to standby
        # Huawei: startup system-software slave (or backup/standby depending on model)
        cmd = "startup system-software"
        output = _run_huawei_cmd(channel, cmd, prompt=r"#\s*$|y/n|\[Y/N\]")

        # Handle confirmation prompt
        if re.search(r"y/n|\[Y/N\]", output, re.IGNORECASE):
            channel.send("y\n")
            output += _read_until_prompt(channel, policy.prompt_regex, timeout_sec=15)

        if is_error_output(output):
            logger.warning(
                "Firmware rollback failed on OLT %s: %s",
                olt.name,
                output.strip()[-200:],
            )
            return False, f"Rollback command failed: {output.strip()[-200:]}"

        logger.info(
            "Firmware rollback initiated on OLT %s: switching from %s to %s",
            olt.name,
            info.current_version,
            info.standby_version,
        )
        return (
            True,
            f"Rollback scheduled: will boot to {info.standby_version} on next reboot",
        )
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error during firmware rollback on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def test_reachability(olt: OLTDevice, timeout_sec: int = 10) -> tuple[bool, str]:
    """Test if OLT is reachable via SSH.

    Args:
        olt: The OLT device.
        timeout_sec: Connection timeout in seconds.

    Returns:
        Tuple of (reachable, message).
    """
    try:
        transport, channel, policy = _open_shell(olt)
        transport.close()
        return True, f"OLT {olt.name} is reachable via SSH"
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"OLT {olt.name} is not reachable: {exc}"


def fetch_running_config_ssh(olt: OLTDevice) -> tuple[bool, str, str]:
    """Fetch the full running configuration from an OLT via SSH.

    Returns (success, message, config_text).
    """
    from app.services.network.olt_read_cache import olt_cache

    cached_config = olt_cache.get(str(olt.id), "running_config")
    if isinstance(cached_config, str) and cached_config.strip():
        return True, "Configuration retrieved (cached)", cached_config

    try:
        from app.services.network.olt_ssh_pool import pooled_ssh_connection

        ssh_context = pooled_ssh_connection(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", ""

    try:
        with ssh_context as (channel, policy):
            channel.send("enable\n")
            _read_until_prompt(
                channel, policy.prompt_regex, timeout_sec=_setup_prompt_timeout()
            )

            output = _run_huawei_paged_cmd(
                channel,
                "display current-configuration",
                prompt=policy.prompt_regex,
                timeout_sec=60,
            )

        # Strip echoed command and trailing prompt
        lines = output.splitlines()
        if lines and "display current-configuration" in lines[0]:
            lines = lines[1:]
        if lines and re.search(policy.prompt_regex, lines[-1]):
            lines = lines[:-1]
        config_text = "\n".join(lines).strip()

        if len(config_text) < 50:
            return (
                False,
                "Config output too short — device may not support this command",
                config_text,
            )
        if is_error_output(config_text):
            return (
                False,
                describe_huawei_rejection(output, action="running-config"),
                config_text,
            )
        if "return" not in config_text.lower():
            return (
                False,
                "Config output incomplete — missing return marker",
                config_text,
            )
        olt_cache.set(str(olt.id), "running_config", config_text)
        return True, "Configuration retrieved", config_text
    except (*_SSH_CONNECTION_ERRORS, RuntimeError, ValueError) as exc:
        logger.error(
            "Error fetching config from OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}", ""


def _validate_readonly_command(command: str) -> tuple[bool, str]:
    """Validate that a CLI command is read-only (display/show only).

    Returns:
        Tuple of (is_valid, error_message).
    """
    return validate_readonly_command(command)


def run_cli_command(olt: OLTDevice, command: str) -> tuple[bool, str, str]:
    """Execute a read-only CLI command on an OLT via SSH.

    Only allows commands starting with safe prefixes like 'display', 'show'.
    Config and mutating commands are rejected.

    Args:
        olt: The OLT device to connect to.
        command: The CLI command to run (must be read-only).

    Returns:
        Tuple of (success, message, command_output).
    """
    # Validate command is read-only before executing
    is_valid, error_msg = _validate_readonly_command(command)
    if not is_valid:
        logger.warning(
            "Rejected non-read-only CLI command on OLT %s: %s",
            olt.name,
            command[:100],
        )
        return False, error_msg, ""

    from app.services.network.olt_read_cache import olt_cache

    cache_params = f"cli:{command.strip()}"
    cached_output = olt_cache.get(str(olt.id), "running_config", cache_params)
    if isinstance(cached_output, str):
        return True, "Command executed (cached)", cached_output

    try:
        from app.services.network.olt_ssh_pool import pooled_ssh_connection

        ssh_context = pooled_ssh_connection(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", ""

    try:
        with ssh_context as (channel, policy):
            channel.send("enable\n")
            _read_until_prompt(
                channel, policy.prompt_regex, timeout_sec=_setup_prompt_timeout()
            )

            channel.send(f"{command}\n")
            output = _read_until_prompt(channel, policy.prompt_regex, timeout_sec=30)

        # Strip the echoed command and trailing prompt from the output
        lines = output.splitlines()
        if lines and command in lines[0]:
            lines = lines[1:]
        # Remove trailing prompt line
        if lines and re.search(policy.prompt_regex, lines[-1]):
            lines = lines[:-1]
        clean_output = "\n".join(lines).strip()
        olt_cache.set(str(olt.id), "running_config", clean_output, cache_params, ttl=60)
        return True, "Command executed", clean_output
    except (*_SSH_CONNECTION_ERRORS, RuntimeError, ValueError) as exc:
        logger.error(
            "Error running CLI command on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}", ""


def get_service_ports_for_ont(
    olt: OLTDevice, fsp: str, ont_id: int
) -> tuple[bool, str, list[ServicePortEntry]]:
    """Get service-ports for a specific ONT on a PON port.

    Filters the full port list by ONT-ID.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port e.g. "0/2/1".
        ont_id: The ONT-ID to filter by.

    Returns:
        Tuple of (success, message, filtered entries).
    """
    ok, msg, all_ports = get_service_ports(olt, fsp)
    if not ok:
        return ok, msg, []
    filtered = [p for p in all_ports if p.ont_id == ont_id]
    return True, f"Found {len(filtered)} service-port(s) for ONT {ont_id}", filtered


def delete_service_port(olt: OLTDevice, index: int) -> tuple[bool, str]:
    """Delete a service-port from the OLT by index.

    Args:
        olt: The OLT device.
        index: The service-port index to delete.

    Returns:
        Tuple of (success, message).
    """
    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=_setup_prompt_timeout())

        config_prompt = r"[#)]\s*$"
        _run_huawei_cmd(channel, "config", prompt=config_prompt)

        output = _run_huawei_cmd(
            channel, f"undo service-port {index}", prompt=config_prompt
        )

        _run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if is_error_output(output):
            logger.warning(
                "Failed to delete service-port %d on OLT %s: %s",
                index,
                olt.name,
                output.strip()[-150:],
            )
            return False, describe_huawei_rejection(output, detail_limit=150)

        logger.info("Deleted service-port %d on OLT %s", index, olt.name)
        _invalidate_olt_read_cache(olt, "service_ports", "running_config")
        return True, f"Service-port {index} deleted"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error deleting service-port on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def update_ont_profiles(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    line_profile_id: int | None = None,
    service_profile_id: int | None = None,
) -> tuple[bool, str]:
    """Update an existing Huawei ONT's line and/or service profile."""
    parts = canonical_fsp(fsp)
    if parts is None:
        return False, invalid_fsp_message(fsp)
    if line_profile_id is None and service_profile_id is None:
        return False, "No ONT profile changes requested"

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        from app.services.network.olt_ssh_ont._common import send_ont_command

        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=_setup_prompt_timeout())

        config_prompt = r"[#)]\s*$"
        _run_huawei_cmd(channel, "config", prompt=config_prompt)

        frame_slot = parts.frame_slot
        port_num = parts.port
        send_ont_command(olt, channel, f"interface gpon {frame_slot}")
        _read_until_prompt(channel, config_prompt, timeout_sec=_setup_prompt_timeout())

        cmd = f"ont modify {port_num} {ont_id}"
        if line_profile_id is not None:
            cmd += f" ont-lineprofile-id {line_profile_id}"
        if service_profile_id is not None:
            cmd += f" ont-srvprofile-id {service_profile_id}"

        send_ont_command(olt, channel, cmd)
        output = _read_until_prompt(channel, r"[#)]\s*$|<cr>", timeout_sec=10)
        if "<cr>" in output:
            channel.send("\n")
            output += _read_until_prompt(channel, config_prompt, timeout_sec=10)

        _run_huawei_cmd(channel, "quit", prompt=config_prompt)
        _run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if is_error_output(output):
            logger.warning(
                "ONT profile update failed on OLT %s %s ONT %d: %s",
                olt.name,
                fsp,
                ont_id,
                output.strip()[-200:],
            )
            return False, describe_huawei_rejection(output)

        profile_bits: list[str] = []
        if line_profile_id is not None:
            profile_bits.append(f"line profile {line_profile_id}")
        if service_profile_id is not None:
            profile_bits.append(f"service profile {service_profile_id}")
        message = f"Updated ONT {ont_id} on {fsp}: {', '.join(profile_bits)}"
        logger.info("%s on OLT %s", message, olt.name)
        _invalidate_olt_read_cache(olt, "profiles", "running_config")
        return True, message
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error updating ONT profiles on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def set_ont_description(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    description: str,
) -> tuple[bool, str]:
    """Set an ONT's description via Huawei ``ont modify ... desc "..."``.

    The reconciler's ``OltModifyDescription`` action calls this. Description
    drift is rare (operators don't usually mutate desc on existing ONTs) but
    when it happens the reconciler should be able to correct it without
    re-running the full authorize sequence.

    The description is sanitised by the caller before reaching this
    function — the reconciler resolves it through
    ``OntDesiredState.description`` which the validator constrains. Pass
    a non-empty string; the OLT will silently accept the write if it's
    identical to the existing desc.
    """
    parts = canonical_fsp(fsp)
    if parts is None:
        return False, invalid_fsp_message(fsp)
    if not description:
        return False, "description is empty"

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        from app.services.network.olt_ssh_ont._common import send_ont_command

        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=_setup_prompt_timeout())

        config_prompt = r"[#)]\s*$"
        _run_huawei_cmd(channel, "config", prompt=config_prompt)

        frame_slot = parts.frame_slot
        port_num = parts.port
        send_ont_command(olt, channel, f"interface gpon {frame_slot}")
        _read_until_prompt(channel, config_prompt, timeout_sec=_setup_prompt_timeout())

        # Escape any embedded double-quotes; the wrapped-in-quotes form is
        # how the rest of the codebase writes descriptions (matches
        # lifecycle.py's `ont add ... desc "..."`).
        safe = description.replace('"', '\\"')
        cmd = f'ont modify {port_num} {ont_id} desc "{safe}"'
        send_ont_command(olt, channel, cmd)
        output = _read_until_prompt(channel, r"[#)]\s*$|<cr>", timeout_sec=10)
        if "<cr>" in output:
            channel.send("\n")
            output += _read_until_prompt(channel, config_prompt, timeout_sec=10)

        _run_huawei_cmd(channel, "quit", prompt=config_prompt)
        _run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if is_error_output(output):
            logger.warning(
                "ONT description update failed on OLT %s %s ONT %d: %s",
                olt.name,
                fsp,
                ont_id,
                output.strip()[-200:],
            )
            return False, describe_huawei_rejection(output)

        logger.info(
            "ONT %d description updated on OLT %s %s",
            ont_id,
            olt.name,
            fsp,
        )
        _invalidate_olt_read_cache(olt, "ont_info", "running_config")
        return True, f"Set ONT {ont_id} description on {fsp}"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error setting ONT description on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def create_single_service_port(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    gem_index: int,
    vlan_id: int,
    *,
    user_vlan: int | str | None = None,
    tag_transform: str = "translate",
    port_index: int | None = None,
    traffic_table_inbound: int | None = None,
    traffic_table_outbound: int | None = None,
) -> tuple[bool, str, int | None]:
    """Create a single service-port on an OLT.

    Note: This is a legacy function. Prefer using the version from
    app.services.network.olt_ssh_service_ports which integrates with
    the DB-backed allocator.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port e.g. "0/2/1".
        ont_id: The ONT-ID assigned by the OLT.
        gem_index: GEM port index.
        vlan_id: VLAN ID for the service-port.
        user_vlan: User VLAN (default: same as vlan_id).
        tag_transform: VLAN tag transform mode.
        port_index: Pre-allocated service-port index. If None, OLT auto-assigns.
        traffic_table_inbound: OLT traffic-table index for inbound QoS (optional).
        traffic_table_outbound: OLT traffic-table index for outbound QoS (optional).

    Returns:
        Tuple of (success, message, assigned_index).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err, None

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=_setup_prompt_timeout())

        config_prompt = r"[#)]\s*$"
        _run_huawei_cmd(channel, "config", prompt=config_prompt)

        cmd = build_service_port_command(
            fsp=fsp,
            ont_id=ont_id,
            gem_index=gem_index,
            vlan_id=vlan_id,
            user_vlan=user_vlan,
            tag_transform=tag_transform,
            port_index=port_index,
            traffic_table_inbound=traffic_table_inbound,
            traffic_table_outbound=traffic_table_outbound,
        )
        output = _run_huawei_cmd(channel, cmd, prompt=config_prompt)

        _run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if is_error_output(output):
            logger.warning(
                "Service-port creation failed on OLT %s: %s",
                olt.name,
                output.strip()[-150:],
            )
            return False, describe_huawei_rejection(output, detail_limit=150), None

        idx_str = str(port_index) if port_index is not None else "auto"
        logger.info(
            "Created service-port %s VLAN %d GEM %d for ONT %d on OLT %s %s",
            idx_str,
            vlan_id,
            gem_index,
            ont_id,
            olt.name,
            fsp,
        )
        return (
            True,
            f"Service-port created (VLAN {vlan_id}, GEM {gem_index})",
            port_index,
        )
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error creating service-port on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()


def test_connection(olt: OLTDevice) -> tuple[bool, str, str | None]:
    try:
        policy_key, output = run_version_probe(olt)
    except (SSHException, OSError, TimeoutError) as exc:
        return False, f"Connection failed: {type(exc).__name__}: {exc}", None
    if not output.strip():
        return False, "SSH connected but no CLI output returned", policy_key
    return True, "SSH connection test successful", policy_key
