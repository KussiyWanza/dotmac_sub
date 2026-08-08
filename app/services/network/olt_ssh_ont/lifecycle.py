"""ONT lifecycle operations (reboot, factory reset, authorize, deauthorize) via OLT SSH."""

from __future__ import annotations

import logging
import re

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network._common import encode_to_hex_serial
from app.services.network.huawei_cli_response import (
    HuaweiCliErrorCode,
    HuaweiDeviceOutcome,
    describe_huawei_rejection,
    parse_huawei_ont_add_result,
)
from app.services.network.olt_ssh_ont._common import (
    _SSH_CONNECTION_ERRORS,
    OntAuthorizationOutcome,
    _validate_serial,
    invalid_fsp_message,
    send_ont_command,
)
from app.services.network.olt_validators import ValidationError, validate_ont_id
from app.services.network.parsers.cli import canonical_fsp

logger = logging.getLogger(__name__)


def reboot_ont_omci(olt: OLTDevice, fsp: str, ont_id: int) -> tuple[bool, str]:
    """Reboot an ONT via OMCI from the OLT."""
    from app.services.network import olt_ssh as core

    parts = canonical_fsp(fsp)
    if parts is None:
        return False, invalid_fsp_message(fsp)

    # Validate ont_id before CLI interpolation
    try:
        validate_ont_id(ont_id)
    except ValidationError as e:
        return False, e.message

    frame_slot = parts.frame_slot
    port_num = parts.port

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

        channel.send(f"ont reset {port_num} {ont_id}\n")
        output = core._read_until_prompt(
            channel, rf"{config_prompt}|y/n|Y/N", timeout_sec=10
        )
        if "y/n" in output.lower():
            channel.send("y\n")
            output += core._read_until_prompt(channel, config_prompt, timeout_sec=10)

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "ONT reset failed for %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-150:],
            )
            return False, describe_huawei_rejection(output, detail_limit=150)

        logger.info("ONT %d reset via OMCI on OLT %s", ont_id, olt.name)
        return True, f"ONT {ont_id} reboot command sent via OMCI"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error("Error resetting ONT on OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Error: {exc}"
    finally:
        transport.close()


def factory_reset_ont_omci(olt: OLTDevice, fsp: str, ont_id: int) -> tuple[bool, str]:
    """Full factory reset of an ONT via OMCI from the OLT."""
    from app.services.network import olt_ssh as core

    parts = canonical_fsp(fsp)
    if parts is None:
        return False, invalid_fsp_message(fsp)

    # Validate ont_id before CLI interpolation
    try:
        validate_ont_id(ont_id)
    except ValidationError as e:
        return False, e.message

    frame_slot = parts.frame_slot
    port_num = parts.port

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

        channel.send(f"ont factory-setting-restore {port_num} {ont_id}\n")
        output = core._read_until_prompt(
            channel, rf"{config_prompt}|y/n|Y/N", timeout_sec=10
        )
        if "y/n" in output.lower():
            channel.send("y\n")
            output += core._read_until_prompt(channel, config_prompt, timeout_sec=10)

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "Factory reset failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-150:],
            )
            return False, describe_huawei_rejection(output, detail_limit=150)

        logger.info("Factory reset ONT %d via OMCI on OLT %s", ont_id, olt.name)
        return True, f"ONT {ont_id} factory reset command sent via OMCI"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error factory-resetting ONT on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def deauthorize_ont(olt: OLTDevice, fsp: str, ont_id: int) -> tuple[bool, str]:
    """Delete an ONT from the OLT so it can be rediscovered via autofind."""
    from app.services.network import olt_ssh as core

    parts = canonical_fsp(fsp)
    if parts is None:
        return False, invalid_fsp_message(fsp)

    # Validate ont_id before CLI interpolation
    try:
        validate_ont_id(ont_id)
    except ValidationError as e:
        return False, e.message

    frame_slot = parts.frame_slot
    port_num = parts.port

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

        delete_out = core._run_huawei_cmd(
            channel,
            f"ont delete {port_num} {ont_id}",
            prompt=r"[#)]\s*$|y/n|Y/N",
        )
        if "y/n" in delete_out.lower():
            channel.send("y\n")
            delete_out += core._read_until_prompt(
                channel, config_prompt, timeout_sec=10
            )

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(delete_out):
            logger.warning(
                "ONT delete failed for %d on OLT %s: %s",
                ont_id,
                olt.name,
                delete_out.strip()[-150:],
            )
            return False, describe_huawei_rejection(delete_out, detail_limit=150)

        logger.info("Deleted ONT %d from OLT %s on %s", ont_id, olt.name, parts.fsp)
        core._invalidate_olt_read_cache(
            olt, "autofind", "service_ports", "running_config", "ont_info"
        )
        return True, f"ONT {ont_id} deleted from OLT"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error("Error deleting ONT on OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Error: {exc}"
    finally:
        transport.close()


# Alias for backwards compatibility
delete_ont_registration = deauthorize_ont


_DESC_ALLOWED = re.compile(r"[^A-Za-z0-9_.,/\-]+")


def _sanitize_ont_description(value: str | None) -> str:
    """Reduce a description to OLT-safe characters.

    Huawei OLTs accept descriptions up to ~80 chars; spaces and certain symbols
    are unreliable in scripted SSH (and inconsistent across MA5608T/MA5800
    firmware builds). Normalize spaces to underscores and strip everything not
    alphanumeric / ``_ . , / -``. Truncate to 64 chars (leaves margin for the
    surrounding ``desc "..."`` quoting).
    """
    if not value:
        return ""
    candidate = str(value).strip().replace(" ", "_")
    cleaned = _DESC_ALLOWED.sub("", candidate)
    return cleaned[:64]


def authorize_ont(
    olt: OLTDevice,
    fsp: str,
    serial_number: str,
    *,
    line_profile_id: int | None = None,
    service_profile_id: int | None = None,
    description: str | None = None,
) -> OntAuthorizationOutcome:
    """SSH into OLT and register an ONT via sn-auth on the given port.

    Args:
        olt: The OLT device to connect to.
        fsp: Frame/Slot/Port string, e.g. "0/2/1".
        serial_number: ONT serial in vendor format, e.g. "HWTC-7D4733C3".
        line_profile_id: OLT-local line profile ID resolved before authorization.
        service_profile_id: OLT-local service profile ID resolved before authorization.
        description: Optional description to attach to the ``ont add`` command.
            Empty/None falls back to a serial-derived stub so the ONT row in
            ``display ont info`` never shows ``ONT_NO_DESCRIPTION``.

    Returns:
        :class:`OntAuthorizationOutcome`. Acceptance is decided by
        ``app.services.network.huawei_cli_response``, never by matching
        response text here, and a shelf that returns no recognizable verdict
        is reported as :attr:`~OntAuthorizationOutcome.device_was_silent`
        rather than assumed successful.
    """
    from app.services.network import olt_ssh as core

    if line_profile_id is None or service_profile_id is None:
        return OntAuthorizationOutcome(
            HuaweiDeviceOutcome.transport_failure(
                "OLT authorization profiles were not resolved; refusing to use static profile defaults.",
                code=HuaweiCliErrorCode.PROFILE_NOT_EXIST,
            )
        )
    line_pid = line_profile_id
    srv_pid = service_profile_id
    parts = canonical_fsp(fsp)
    if parts is None:
        return OntAuthorizationOutcome(
            HuaweiDeviceOutcome.transport_failure(
                invalid_fsp_message(fsp),
                code=HuaweiCliErrorCode.PARAMETER_ERROR,
            )
        )
    ok, err = _validate_serial(serial_number)
    if not ok:
        return OntAuthorizationOutcome(
            HuaweiDeviceOutcome.transport_failure(
                err, code=HuaweiCliErrorCode.PARAMETER_ERROR
            )
        )

    try:
        transport, channel, policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return OntAuthorizationOutcome(
            HuaweiDeviceOutcome.transport_failure(f"Connection failed: {exc}")
        )

    try:
        # Enter enable mode
        channel.send("enable\n")
        core._read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

        # Enter config mode
        config_prompt = r"[#)]\s*$"
        channel.send("config\n")
        core._read_until_prompt(channel, config_prompt, timeout_sec=5)

        # Enter GPON interface for the frame/slot. Commands are built from the
        # canonical F/S/P, never the caller's raw string: a port name like
        # ``gpon-0/1/0`` passes validation after normalization but would
        # otherwise produce ``interface gpon gpon-0/1``.
        frame_slot = parts.frame_slot
        port_num = parts.port

        send_ont_command(olt, channel, f"interface gpon {frame_slot}")
        core._read_until_prompt(channel, config_prompt, timeout_sec=5)

        # Authorize the ONT — use hex serial format to avoid terminal corruption
        # when serial numbers contain characters that could be interpreted as
        # escape sequences (e.g. '1B' = ESC in ASCII). Hex format is reliably
        # processed by all OLT terminals.
        sn_clean = encode_to_hex_serial(serial_number) or serial_number.replace("-", "")
        desc_clean = _sanitize_ont_description(description)
        if not desc_clean:
            # Default placeholder so the OLT row never shows ONT_NO_DESCRIPTION.
            from datetime import UTC, datetime

            desc_clean = (f"{sn_clean}_authd_{datetime.now(UTC).strftime('%Y%m%d')}")[
                :64
            ]
        auth_cmd = (
            f"ont add {port_num} sn-auth {sn_clean} omci "
            f"ont-lineprofile-id {line_pid} ont-srvprofile-id {srv_pid} "
            f'desc "{desc_clean}"'
        )
        send_ont_command(olt, channel, auth_cmd)
        # With desc supplied we no longer expect the "{ <cr>|desc<K>|ont-type<K> }:"
        # follow-up prompt, but keep the fallback to handle older Huawei firmware
        # builds that still demand a CR confirmation.
        initial = core._read_until_prompt(channel, r"[#)]\s*$|<cr>", timeout_sec=10)
        if "<cr>" in initial:
            channel.send("\n")
            output = core._read_until_prompt(channel, r"[#)]\s*$", timeout_sec=10)
        else:
            output = initial

        # Exit config mode
        channel.send("quit\n")
        core._read_until_prompt(channel, config_prompt, timeout_sec=3)
        channel.send("quit\n")
        core._read_until_prompt(channel, config_prompt, timeout_sec=3)

        # The classifier owns acceptance and rejection alike. It consults the
        # error classification first, so a rejection that happens to name an
        # ONT-ID can no longer be read as a successful authorization.
        add_result = parse_huawei_ont_add_result(output)

        if add_result.accepted:
            logger.info(
                "Authorized ONT %s on OLT %s port %s",
                serial_number,
                olt.name,
                parts.fsp,
            )
            message = f"ONT {serial_number} authorized on port {parts.fsp}"
            if add_result.ont_id is not None:
                message += f" (ONT-ID {add_result.ont_id})"
            core._invalidate_olt_read_cache(
                olt, "autofind", "service_ports", "running_config", "ont_info"
            )
            return OntAuthorizationOutcome(
                HuaweiDeviceOutcome.accepted(message, code=add_result.code),
                ont_id=add_result.ont_id,
            )

        if add_result.code is not HuaweiCliErrorCode.NONE:
            logger.warning(
                "Failed to authorize ONT %s on OLT %s: %s",
                serial_number,
                olt.name,
                add_result.code.value,
            )
            return OntAuthorizationOutcome(
                HuaweiDeviceOutcome.rejected_by_device(output, action="ont add")
            )

        # The shelf returned no recognizable verdict. This is not success. The
        # write may still have landed, so the caller must confirm by readback
        # instead of the old "command sent" optimism, which reported stale
        # caches and unverified registrations as completed authorizations.
        logger.info(
            "ONT authorize returned no verdict for %s on OLT %s; readback required",
            serial_number,
            olt.name,
        )
        core._invalidate_olt_read_cache(
            olt, "autofind", "service_ports", "running_config", "ont_info"
        )
        return OntAuthorizationOutcome(
            HuaweiDeviceOutcome(
                succeeded=False,
                code=HuaweiCliErrorCode.NONE,
                message=(
                    f"OLT returned no verdict for {serial_number} on port "
                    f"{parts.fsp}; registration must be confirmed by readback."
                ),
                device_detail=output.strip()[-200:],
            )
        )
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error authorizing ONT %s on OLT %s: %s",
            serial_number,
            olt.name,
            exc,
            exc_info=True,
        )
        return OntAuthorizationOutcome(
            HuaweiDeviceOutcome.transport_failure(f"Error: {exc}")
        )
    finally:
        transport.close()
