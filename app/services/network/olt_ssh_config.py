"""OLT SSH actions for device-level configuration and diagnostics."""

from __future__ import annotations

import logging
import re
import socket

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
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
    from app.services.network.olt_ssh import (
        _open_shell,
        _read_until_prompt,
        _run_huawei_cmd,
        is_error_output,
    )

    # Reject newlines and shell metacharacters in file_url
    if not file_url or "\n" in file_url or "\r" in file_url or ";" in file_url:
        return False, "Invalid firmware URL"

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

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
                "Firmware upgrade failed on OLT %s: %s",
                olt.name,
                output.strip()[-200:],
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


def fetch_running_config_ssh(olt: OLTDevice) -> tuple[bool, str, str]:
    """Fetch the full running configuration from an OLT via SSH.

    Returns (success, message, config_text).
    """
    from app.services.network.olt_ssh import (
        _open_shell,
        _read_until_prompt,
        _run_huawei_paged_cmd,
    )

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", ""

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

        output = _run_huawei_paged_cmd(
            channel, "display current-configuration", prompt=policy.prompt_regex
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
        return True, "Configuration retrieved", config_text
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error fetching config from OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}", ""
    finally:
        transport.close()


def _restore_config_commands(config_text: str) -> list[str]:
    """Normalize backup text into executable Huawei config commands."""
    commands: list[str] = []
    for raw_line in config_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", "!", "//")):
            continue
        lower = stripped.lower()
        if lower in {"return", "end"}:
            continue
        if lower.startswith(
            ("display ", "show ", "ping ", "traceroute ", "screen-length ")
        ):
            continue
        commands.append(stripped)
    return commands


def _save_running_config(
    channel, prompt_regex: str, is_error_output
) -> tuple[bool, str]:
    """Persist the running config, handling Huawei confirmation prompts."""
    from app.services.network.olt_ssh import _read_until_prompt, _run_huawei_cmd

    output = _run_huawei_cmd(channel, "save", prompt=rf"{prompt_regex}|y/n|\[Y/N\]")
    if re.search(r"y/n|\[Y/N\]", output, re.IGNORECASE):
        channel.send("y\n")
        output += _read_until_prompt(channel, prompt_regex, timeout_sec=30)
    if is_error_output(output):
        return False, output.strip()[-200:]
    return True, ""


def restore_config_from_backup(
    olt: OLTDevice, config_text: str, *, persist: bool = True
) -> tuple[bool, str]:
    """Replay a backed-up running config to an OLT over SSH."""
    from app.services.network.olt_ssh import (
        _open_shell,
        _read_until_prompt,
        _run_huawei_cmd,
        is_error_output,
    )

    commands = _restore_config_commands(config_text)
    if not commands:
        return False, "Backup contains no restorable configuration commands"

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

        config_prompt = r"\][ \t]*$"
        system_view = _run_huawei_cmd(channel, "system-view", prompt=config_prompt)
        if is_error_output(system_view):
            return False, describe_huawei_rejection(system_view, action="config mode")

        errors: list[str] = []
        applied = 0
        for command in commands:
            output = _run_huawei_cmd(channel, command, prompt=config_prompt)
            if is_error_output(output):
                errors.append(f"{command}: {output.strip()[-160:]}")
                if len(errors) >= 10:
                    break
                continue
            applied += 1

        quit_output = _run_huawei_cmd(channel, "quit", prompt=policy.prompt_regex)
        if is_error_output(quit_output):
            errors.append(f"quit: {quit_output.strip()[-160:]}")

        if errors:
            return (
                False,
                f"Restore applied {applied}/{len(commands)} commands; errors: {' | '.join(errors[:3])}",
            )
        if persist:
            saved, save_error = _save_running_config(
                channel, policy.prompt_regex, is_error_output
            )
            if not saved:
                return (
                    False,
                    f"Restore applied {applied}/{len(commands)} commands but failed to save startup config: {save_error}",
                )
            return (
                True,
                f"Restored {applied} configuration commands from backup and saved startup config",
            )
        return True, f"Restored {applied} configuration commands from backup"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error restoring config on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def run_cli_command(olt: OLTDevice, command: str) -> tuple[bool, str, str]:
    """Execute a read-only CLI command on an OLT via SSH.

    Args:
        olt: The OLT device to connect to.
        command: The CLI command to run (must be read-only).

    Returns:
        Tuple of (success, message, command_output).
    """
    from app.services.network.olt_ssh import (
        _open_shell,
        _read_until_prompt,
        _run_huawei_paged_cmd,
    )

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", ""

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

        output = _run_huawei_paged_cmd(channel, command, prompt=policy.prompt_regex)

        # Strip the echoed command and trailing prompt from the output
        lines = output.splitlines()
        if lines and command in lines[0]:
            lines = lines[1:]
        # Remove trailing prompt line
        if lines and re.search(policy.prompt_regex, lines[-1]):
            lines = lines[:-1]
        clean_output = "\n".join(lines).strip()
        return True, "Command executed", clean_output
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error running CLI command on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}", ""
    finally:
        transport.close()
