"""ONT status query functions via OLT SSH."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network._common import encode_to_hex_serial
from app.services.network.huawei_cli_response import (
    HuaweiCliResource,
    classify_huawei_cli_response,
    describe_huawei_rejection,
    is_huawei_resource_absent,
)
from app.services.network.olt_ssh_ont._common import (
    _SSH_CONNECTION_ERRORS,
    OntStatusEntry,
    RegisteredOntEntry,
)

logger = logging.getLogger(__name__)

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])")

# A port read is "recognized" when the device accepted the inventory command and
# answered with a known table header or an explicit empty marker, even when it
# lists zero ONTs. This distinguishes an authoritative empty port (an ONT was
# removed on the device but is still active in Sub inventory) from an unparsable
# or truncated read. The former is trustworthy and must not abort the OLT poll;
# the latter is a poll failure that must fail closed and be retried.
_ONT_INVENTORY_RECOGNITION_RE = re.compile(
    r"the total of ONTs are:"  # MA5800 `display ont info summary` banner
    r"|ONT\s+SN\s+Type"  # MA5800 serial table header
    r"|ONT\s+Run\s+Last"  # MA5800 run-state table header
    r"|F/S/P\s+ONT-ID\s+SN"  # MA5608 legacy inventory header
    r"|No\s+ONT",  # explicit "No ONT is configured" empty response
    re.IGNORECASE,
)


def _ont_inventory_response_recognized(clean_output: str) -> bool:
    """Whether the device returned a known inventory response (possibly empty)."""
    return bool(_ONT_INVENTORY_RECOGNITION_RE.search(clean_output))


def parse_registered_ont_inventory(
    output: str,
    fsp: str,
) -> list[RegisteredOntEntry]:
    """Parse Huawei per-port inventory across MA5800 and MA5608 formats."""
    from app.services.network.parsers.loader import parse_ont_info

    clean_output = _ANSI_ESCAPE_RE.sub("", output)
    states: dict[int, str] = {}
    serials: dict[int, str] = {}
    section: str | None = None
    for line in clean_output.splitlines():
        if re.search(r"\bONT\s+Run\s+Last\b", line, re.IGNORECASE):
            section = "states"
            continue
        if re.search(r"\bONT\s+SN\s+Type\b", line, re.IGNORECASE):
            section = "serials"
            continue
        if section == "states":
            match = re.match(
                r"\s*(\d+)\s+(online|offline|unknown)\b",
                line,
                re.IGNORECASE,
            )
            if match:
                states[int(match.group(1))] = match.group(2).lower()
        elif section == "serials":
            match = re.match(r"\s*(\d+)\s+([A-Za-z0-9-]{8,})\s+", line)
            if match:
                serials[int(match.group(1))] = match.group(2)

    if serials:
        return [
            RegisteredOntEntry(
                fsp=fsp,
                onu_id=ont_id,
                real_serial=serial,
                run_state=states.get(ont_id, "unknown"),
            )
            for ont_id, serial in sorted(serials.items())
        ]

    parsed = parse_ont_info(clean_output)
    return [
        RegisteredOntEntry(
            fsp=entry.fsp or fsp,
            onu_id=entry.ont_id,
            real_serial=entry.serial_number,
            run_state=entry.run_state or "unknown",
        )
        for entry in parsed.data
        if entry.serial_number
    ]


def parse_ont_info_detail(output: str) -> dict[str, str | int | None]:
    """Extract richer fields from a ``display ont info <fsp> <id>`` block.

    Returns a dict with keys: ``description``, ``line_profile_id``,
    ``service_profile_id``, ``tr069_profile_id``, ``mgmt_ip``, ``mgmt_vlan``,
    ``distance_m``.
    Missing values are ``None``. Designed to be fed plain-text output —
    callers that want to use it from already-collected SSH output can call
    this directly without re-running the command.

    Multi-line ``Description`` values (Huawei wraps long descs) are joined
    with stripped whitespace on continuations.
    """
    result: dict[str, str | int | None] = {
        "description": None,
        "line_profile_id": None,
        "service_profile_id": None,
        "tr069_profile_id": None,
        "mgmt_ip": None,
        "mgmt_vlan": None,
        "distance_m": None,
    }

    lines = output.splitlines()
    desc_parts: list[str] = []
    in_description = False
    for line in lines:
        # A continuation line is indented and has no field delimiter. Huawei
        # pads long labels beyond column 26, so a fixed-column check can absorb
        # legitimate fields such as ``TR069 server profile ID`` into the
        # description.
        if in_description and line.startswith(" ") and ":" not in line:
            desc_parts.append(line.strip())
            continue
        in_description = False

        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key_norm = key.strip().lower()
        value = value.strip()

        if key_norm == "description":
            desc_parts = [value]
            in_description = True
        elif key_norm == "line profile id":
            try:
                result["line_profile_id"] = int(value)
            except ValueError:
                pass
        elif key_norm == "service profile id":
            try:
                result["service_profile_id"] = int(value)
            except ValueError:
                pass
        elif key_norm in {
            "tr069 server profile",
            "tr069 server profile id",
            "tr-069 server profile",
            "tr-069 server profile id",
        }:
            try:
                result["tr069_profile_id"] = int(value)
            except ValueError:
                pass
        elif key_norm == "ont ip 0 address/mask":
            # Format: "172.16.210.20/24"
            ip = value.split("/")[0].strip()
            if ip and ip != "-":
                result["mgmt_ip"] = ip
        elif key_norm == "ont manage vlan":
            try:
                result["mgmt_vlan"] = int(value)
            except ValueError:
                pass
        elif key_norm == "ont distance(m)":
            try:
                result["distance_m"] = int(value)
            except ValueError:
                pass

    if desc_parts:
        joined = "".join(desc_parts)
        result["description"] = joined.strip() or None

    return result


def get_ont_info_detail(
    olt: OLTDevice, fsp: str, ont_id: int
) -> tuple[bool, str, dict[str, str | int | None] | None]:
    """Like ``get_ont_status`` but returns the richer field set parsed from
    the same ``display ont info`` output.

    Used by the reconciler's OLT reader to populate ``OltObservedFields``
    description / profile ids / mgmt ip / mgmt vlan / distance.
    """
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err, None

    try:
        transport, channel, policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    try:
        prompt = core._prepare_huawei_read_shell(channel, policy.prompt_regex)

        from app.services.network.huawei_command_profiles import (
            get_huawei_command_profile,
        )

        cmd = get_huawei_command_profile(olt).display_ont_info(fsp, ont_id)
        output = core._run_huawei_cmd(channel, cmd, prompt=prompt)

        if core.is_error_output(output):
            return False, describe_huawei_rejection(output), None

        return True, "ONT info retrieved", parse_ont_info_detail(output)
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error getting detailed ONT info from OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()


def get_ont_status(
    olt: OLTDevice, fsp: str, ont_id: int
) -> tuple[bool, str, OntStatusEntry | None]:
    """Query the status of a specific ONT on an OLT port via SSH."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err, None

    try:
        transport, channel, policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    try:
        prompt = core._prepare_huawei_read_shell(channel, policy.prompt_regex)

        from app.services.network.huawei_command_profiles import (
            get_huawei_command_profile,
        )

        cmd = get_huawei_command_profile(olt).display_ont_info(fsp, ont_id)
        output = core._run_huawei_cmd(channel, cmd, prompt=prompt)

        if core.is_error_output(output):
            return False, describe_huawei_rejection(output), None

        kv: dict[str, str] = {}
        for line in output.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                kv[key.strip().lower()] = value.strip()

        serial = kv.get("serial number", kv.get("sn", ""))
        entry = OntStatusEntry(
            serial_number=serial,
            run_state=kv.get("run state", "unknown"),
            config_state=kv.get("config state", "unknown"),
            match_state=kv.get("match state", "unknown"),
        )
        return True, "ONT status retrieved", entry
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error getting ONT status from OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()


def get_registered_ont_serials(
    olt: OLTDevice,
    fsps: Iterable[str],
    *,
    timeout_sec: int = 180,
) -> tuple[bool, str, list[RegisteredOntEntry]]:
    """Query registered ONTs on explicit ports within one bounded SSH session.

    Returns ``ok=True`` only when every requested port returned a recognized
    response — parsed ONT rows or an authoritative empty result. A port whose
    output cannot be recognized (garbled, truncated, or an unknown format) makes
    the whole call fail closed, so callers never mistake an unreadable port for
    an empty one. A recognized-but-empty port contributes no rows and does not
    fail the call: it is authoritative evidence that the port holds no ONTs.
    """
    from app.services.network import olt_ssh as core

    normalized_fsps: list[str] = []
    for raw_fsp in fsps:
        fsp = core._normalize_fsp(raw_fsp)
        ok, error = core._validate_fsp(fsp)
        if not ok:
            return False, error, []
        if fsp not in normalized_fsps:
            normalized_fsps.append(fsp)
    if not normalized_fsps:
        return True, "No Huawei PON ports requested", []

    started = time.monotonic()
    try:
        transport, channel, policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []

    try:
        prompt = core._prepare_huawei_read_shell(channel, policy.prompt_regex)
        from app.services.network.huawei_command_profiles import (
            get_huawei_command_profile,
        )

        profile = get_huawei_command_profile(olt)
        entries: list[RegisteredOntEntry] = []
        for fsp in normalized_fsps:
            remaining = timeout_sec - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError(
                    f"Huawei inventory exceeded {timeout_sec}s before port {fsp}"
                )
            command = profile.display_ont_status_inventory(fsp)
            output = core._run_huawei_paged_cmd(
                channel,
                command,
                prompt=prompt,
                timeout_sec=max(1, min(30, int(remaining))),
            )
            port_entries = parse_registered_ont_inventory(output, fsp)
            if port_entries:
                entries.extend(port_entries)
                continue
            # No rows parsed. A recognized inventory response (including an
            # explicit empty one) is authoritative: the port simply holds no
            # ONTs. Check recognition before the shared error scan so a customer
            # description containing words like "error" or "invalid" in a data
            # row cannot be misread as a device rejection.
            clean_output = _ANSI_ESCAPE_RE.sub("", output)
            if _ont_inventory_response_recognized(clean_output):
                continue
            if core.is_error_output(output):
                return (
                    False,
                    describe_huawei_rejection(output, action=f"inventory read {fsp}"),
                    [],
                )
            return (
                False,
                f"Huawei inventory response for {fsp} was not recognized",
                [],
            )
        return (
            True,
            f"Found {len(entries)} registered ONTs on {len(normalized_fsps)} ports",
            entries,
        )
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error getting registered ONT serials from OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}", []
    finally:
        transport.close()


def find_ont_by_serial(
    olt: OLTDevice,
    serial_number: str,
) -> tuple[bool, str, RegisteredOntEntry | None]:
    """Find where an ONT serial is already registered on an OLT.

    Uses 'display ont info by-sn' for direct lookup which is more reliable
    than parsing all registered ONTs.

    Returns:
        (success, message, entry) where entry contains fsp, onu_id, run_state
        if the serial is found, or None if not registered.
    """
    from app.services.network import olt_ssh as core

    # The full hex representation is the safe direct-lookup form for compatible
    # Huawei serials. Some older shelves reject the display form (for example
    # ``HWTC1234ABCD``) with ``Parameter error`` even though the same ONT is
    # registered as ``485754431234ABCD``.
    normalized_serial = serial_number.replace("-", "").strip().upper()
    lookup_serial = encode_to_hex_serial(normalized_serial) or normalized_serial

    try:
        transport, channel, policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    try:
        prompt = core._prepare_huawei_read_shell(channel, policy.prompt_regex)

        # Use direct serial lookup - much more reliable than parsing all ONTs
        output = core._run_huawei_cmd(
            channel,
            f"display ont info by-sn {lookup_serial}",
            prompt=prompt,
        )

        # Absence is authoritative only when Huawei explicitly reports that
        # the ONT does not exist. A rejected command, parameter error, empty
        # response, or unfamiliar response is an unavailable observation and
        # must fail closed rather than authorizing a duplicate registration.
        response = classify_huawei_cli_response(output)
        if is_huawei_resource_absent(output, HuaweiCliResource.ONT):
            logger.info(
                "ONT serial %s not found on OLT %s",
                serial_number,
                olt.name,
            )
            return True, f"ONT {serial_number} is not registered on {olt.name}", None
        if response.has_error_marker:
            logger.warning(
                "OLT rejected ONT serial lookup on %s: %s",
                olt.name,
                response.error_code.value,
            )
            return (
                False,
                describe_huawei_rejection(
                    output,
                    action="ONT serial lookup",
                    code=response.error_code,
                ),
                None,
            )

        # Parse the output for F/S/P, ONT-ID, and Run state
        fsp_match = re.search(r"F/S/P\s*:\s*(\d+/\d+/\d+)", output)
        ont_id_match = re.search(r"ONT-ID\s*:\s*(\d+)", output)
        run_state_match = re.search(r"Run state\s*:\s*(\w+)", output, re.IGNORECASE)

        if fsp_match and ont_id_match:
            fsp = fsp_match.group(1)
            ont_id = int(ont_id_match.group(1))
            run_state = (
                run_state_match.group(1).lower() if run_state_match else "unknown"
            )

            logger.info(
                "Found existing ONT registration: serial=%s on %s port %s ont_id=%d state=%s",
                serial_number,
                olt.name,
                fsp,
                ont_id,
                run_state,
            )
            return (
                True,
                f"ONT {serial_number} is registered on {fsp} as ONT-ID {ont_id} ({run_state})",
                RegisteredOntEntry(
                    fsp=fsp,
                    onu_id=ont_id,
                    real_serial=normalized_serial,
                    run_state=run_state,
                ),
            )

        # If we got output but couldn't parse it, log for debugging
        logger.warning(
            "Could not parse ONT info output on OLT %s",
            olt.name,
        )
        return (
            False,
            f"Huawei ONT serial lookup response was not recognized on {olt.name}.",
            None,
        )

    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error finding ONT by serial %s on OLT %s: %s",
            serial_number,
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()
