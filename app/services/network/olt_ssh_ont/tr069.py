"""TR-069 server profile binding functions via OLT SSH."""

from __future__ import annotations

import logging
import re

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network.huawei_cli_response import (
    describe_huawei_rejection,
    is_huawei_cli_unsupported,
    is_huawei_ont_offline,
)
from app.services.network.huawei_command_profiles import get_huawei_command_profile
from app.services.network.olt_ssh_ont._common import (
    _SSH_CONNECTION_ERRORS,
    _run_ont_config_command,
    invalid_fsp_message,
)
from app.services.network.parsers import parse_ont_info_detail
from app.services.network.parsers.cli import canonical_fsp

logger = logging.getLogger(__name__)

_ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

_TR069_BIND_WITH_PORT_RE = re.compile(
    r"\bont\s+tr069-server-config\s+"
    r"(?P<port>\d+)\s+(?P<ont_id>\d+)\s+profile-id\s+(?P<profile_id>\d+)",
    re.IGNORECASE,
)
_TR069_BIND_PORTLESS_RE = re.compile(
    r"\bont\s+tr069-server-config\s+"
    r"(?P<ont_id>\d+)\s+profile-id\s+(?P<profile_id>\d+)",
    re.IGNORECASE,
)


def parse_tr069_binding(output: str, *, port: int, ont_id: int) -> int | None:
    """Return the bound TR-069 profile ID for one ONT from Huawei config text.

    Huawei displays this binding in two forms depending on software build:
    - ``ont tr069-server-config <port> <ont_id> profile-id <id>``
    - ``ont tr069-server-config <ont_id> profile-id <id>``

    The second form appears after entering ``interface gpon F/S`` where the GPON
    port is already implied by context, so readback must accept both.
    """
    for match in _TR069_BIND_WITH_PORT_RE.finditer(output or ""):
        if int(match.group("port")) == int(port) and int(match.group("ont_id")) == int(
            ont_id
        ):
            return int(match.group("profile_id"))
    for match in _TR069_BIND_PORTLESS_RE.finditer(output or ""):
        if int(match.group("ont_id")) == int(ont_id):
            return int(match.group("profile_id"))
    return None


def _clean_cli_output(output: str) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", output or "")
    return cleaned.replace("\r", "")


def _exact_prompt(core, output: str, fallback: str) -> str:
    derive = getattr(core, "_derive_prompt_regex", None)
    if callable(derive):
        return derive(output, fallback)
    return fallback


def _read_tr069_binding_from_ont_info(
    core,
    *,
    olt: OLTDevice,
    channel,
    fsp: str,
    port_num: int,
    ont_id: int,
    prompt: str,
) -> tuple[int | None, str | None]:
    """Fallback readback via ``display ont info`` for builds that omit config lines."""
    run_paged = getattr(core, "_run_huawei_paged_cmd", core._run_huawei_cmd)
    commands = [f"display ont info {port_num} {ont_id}"]
    try:
        commands.append(get_huawei_command_profile(olt).display_ont_info(fsp, ont_id))
    except ValueError:
        pass

    seen: set[str] = set()
    for command in commands:
        if command in seen:
            continue
        seen.add(command)
        output = run_paged(channel, command, prompt=prompt)
        if core.is_error_output(output):
            continue
        entry = parse_ont_info_detail(_clean_cli_output(output))
        if entry and entry.tr069_profile_id is not None:
            return entry.tr069_profile_id, command
    return None, None


def _read_tr069_binding_from_current_config(
    core,
    *,
    channel,
    port_num: int,
    ont_id: int,
    prompt: str,
) -> tuple[int | None, str | None]:
    """Fallback readback via global config grep for TR-069 binding lines."""
    # Leave interface and config mode; MA5800-X2 rejects this command from
    # config mode, so run it from enable mode.
    config_output = core._run_huawei_cmd(channel, "quit", prompt=prompt)
    config_prompt = _exact_prompt(core, config_output, r"#\s*$")
    enable_output = core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
    enable_prompt = _exact_prompt(core, enable_output, r"#\s*$")
    command = "display current-configuration | include tr069-server-config"
    run_paged = getattr(core, "_run_huawei_paged_cmd", core._run_huawei_cmd)
    output = run_paged(channel, command, prompt=enable_prompt)
    if core.is_error_output(output):
        return None, None
    profile_id = parse_tr069_binding(
        _clean_cli_output(output),
        port=port_num,
        ont_id=ont_id,
    )
    if profile_id is None:
        return None, None
    return profile_id, command


def get_tr069_server_profile_binding(
    olt: OLTDevice, fsp: str, ont_id: int
) -> tuple[bool, str, int | None]:
    """Read the active TR-069 profile binding for an ONT from the GPON config."""
    from app.services.network import olt_ssh as core

    parts = canonical_fsp(fsp)
    if parts is None:
        return False, invalid_fsp_message(fsp), None

    frame_slot = parts.frame_slot
    port_num = int(parts.port)

    try:
        transport, channel, policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    try:
        enable_prompt = getattr(policy, "prompt_regex", r"#\s*$")
        channel.send("enable\n")
        core._read_until_prompt(channel, enable_prompt, timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        core._read_until_prompt(channel, enable_prompt, timeout_sec=5)

        generic_config_prompt = r"[#)]\s*$"
        config_output = core._run_huawei_cmd(
            channel, "config", prompt=generic_config_prompt
        )
        config_prompt = _exact_prompt(
            core,
            config_output,
            generic_config_prompt,
        )
        interface_output = core._run_huawei_cmd(
            channel,
            f"interface gpon {frame_slot}",
            prompt=config_prompt,
        )
        interface_prompt = _exact_prompt(core, interface_output, config_prompt)
        run_paged = getattr(core, "_run_huawei_paged_cmd", core._run_huawei_cmd)
        output = run_paged(channel, "display this", prompt=interface_prompt)
        profile_id = parse_tr069_binding(
            _clean_cli_output(output),
            port=port_num,
            ont_id=ont_id,
        )
        if profile_id is None:
            profile_id, source_command = _read_tr069_binding_from_ont_info(
                core,
                olt=olt,
                channel=channel,
                fsp=fsp,
                port_num=port_num,
                ont_id=ont_id,
                prompt=interface_prompt,
            )
            if profile_id is not None:
                return (
                    True,
                    (
                        f"TR-069 profile {profile_id} bound for ONT {ont_id} on {fsp} "
                        f"(via {source_command})"
                    ),
                    profile_id,
                )
        if profile_id is None:
            profile_id, source_command = _read_tr069_binding_from_current_config(
                core,
                channel=channel,
                port_num=port_num,
                ont_id=ont_id,
                prompt=interface_prompt,
            )
            if profile_id is not None:
                return (
                    True,
                    (
                        f"TR-069 profile {profile_id} bound for ONT {ont_id} on {fsp} "
                        f"(via {source_command})"
                    ),
                    profile_id,
                )
        if profile_id is None:
            return (
                True,
                f"No TR-069 profile binding found for ONT {ont_id} on {fsp}",
                None,
            )
        return (
            True,
            f"TR-069 profile {profile_id} bound for ONT {ont_id} on {fsp}",
            profile_id,
        )
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error reading TR-069 binding from OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()


def bind_tr069_server_profile(
    olt: OLTDevice, fsp: str, ont_id: int, profile_id: int
) -> tuple[bool, str]:
    """Bind a TR-069 server profile to an ONT via OLT SSH."""
    from app.services.network import olt_ssh as core

    parts = canonical_fsp(fsp)
    if parts is None:
        return False, invalid_fsp_message(fsp)

    frame_slot = parts.frame_slot
    port_num = parts.port
    logger.info(
        "TR-069 bind requested: olt=%s fsp=%s ont_id=%s profile_id=%s",
        olt.name,
        fsp,
        ont_id,
        profile_id,
    )

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

        cmd = f"ont tr069-server-config {port_num} {ont_id} profile-id {profile_id}"
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        if core.is_error_output(output) and is_huawei_cli_unsupported(output):
            # Some Huawei builds expect only the ONT ID after entering
            # `interface gpon F/S`; the port is already implied by context.
            fallback_cmd = f"ont tr069-server-config {ont_id} profile-id {profile_id}"
            fallback_output = core._run_huawei_cmd(
                channel, fallback_cmd, prompt=config_prompt
            )
            if not core.is_error_output(fallback_output):
                output = fallback_output
        if core.is_error_output(output):
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            logger.warning(
                "TR-069 profile bind failed: olt=%s fsp=%s ont_id=%s profile_id=%s output=%s",
                olt.name,
                fsp,
                ont_id,
                profile_id,
                output.strip()[-150:],
            )
            return False, describe_huawei_rejection(output, detail_limit=150)

        reset_out = core._run_huawei_cmd(
            channel, f"ont reset {port_num} {ont_id}", prompt=r"[#)]\s*$|y/n"
        )
        if "y/n" in reset_out:
            channel.send("y\n")
            reset_out += core._read_until_prompt(channel, config_prompt, timeout_sec=8)

        if core.is_error_output(reset_out):
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            if is_huawei_ont_offline(reset_out):
                logger.info(
                    "TR-069 bind succeeded but reset skipped because ONT is offline: olt=%s fsp=%s ont_id=%s profile_id=%s",
                    olt.name,
                    fsp,
                    ont_id,
                    profile_id,
                )
                return (
                    True,
                    f"TR-069 profile {profile_id} bound to ONT {ont_id}; ONT is offline, so reset will occur when it next boots.",
                )
            logger.warning(
                "TR-069 bind succeeded but reset failed: olt=%s fsp=%s ont_id=%s profile_id=%s output=%s",
                olt.name,
                fsp,
                ont_id,
                profile_id,
                reset_out.strip()[-150:],
            )
            return (
                False,
                f"TR-069 profile bound but reset failed: {reset_out.strip()[-150:]}",
            )

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        logger.info(
            "TR-069 bind succeeded and reset triggered: olt=%s fsp=%s ont_id=%s profile_id=%s",
            olt.name,
            fsp,
            ont_id,
            profile_id,
        )
        return (
            True,
            f"TR-069 profile {profile_id} bound to ONT {ont_id} (reset triggered)",
        )
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error binding TR-069 profile on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def unbind_tr069_server_profile(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
) -> tuple[bool, str]:
    """Best-effort removal of an ONT TR-069 server profile binding."""
    parts = canonical_fsp(fsp)
    port_num = parts.port if parts else "0"
    return _run_ont_config_command(
        olt,
        fsp,
        f"undo ont tr069-server-config {port_num} {ont_id}",
        success_message="TR-069 profile binding cleared",
    )
