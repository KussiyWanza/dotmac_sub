"""Focused OLT SSH actions for service-port operations."""

from __future__ import annotations

import logging
import re
import time
from collections import Counter

from sqlalchemy.orm import Session

from app.models.network import OLTDevice
from app.services.network.huawei_cli_response import (
    describe_huawei_rejection,
    is_huawei_idempotent_conflict,
)
from app.services.network.olt_inventory import get_olt_or_none
from app.services.network.olt_ssh import ServicePortEntry
from app.services.network.parsers.cli import canonical_fsp

logger = logging.getLogger(__name__)

_CONFLICTED_SERVICE_PORT_RE = re.compile(
    r"Conflicted service virtual port index:\s*(\d+)", re.IGNORECASE
)
_SERVICE_PORT_VERIFY_ATTEMPTS = 3
_SERVICE_PORT_VERIFY_DELAY_SEC = 1.0


def _parse_service_port_detail(output: str) -> ServicePortEntry | None:
    """Parse Huawei's colon-delimited single service-port response."""
    fields = {
        key.casefold(): value.strip()
        for key, value in re.findall(
            r"^\s*([^:\r\n]+?)\s*:\s*(.*?)\s*$", output, re.MULTILINE
        )
    }
    required = ("index", "vlan id", "ont id", "gem port index")
    if any(not fields.get(key) for key in required):
        return None
    try:
        return ServicePortEntry(
            index=int(fields["index"]),
            vlan_id=int(fields["vlan id"]),
            ont_id=int(fields["ont id"]),
            gem_index=int(fields["gem port index"]),
            flow_type=fields.get("flow type", ""),
            flow_para=fields.get("flow para", ""),
            state=fields.get("state", ""),
            fsp=fields.get("f/s/p", ""),
            tag_transform=fields.get("tag transform", ""),
        )
    except ValueError:
        return None


def _service_port_matches_intent(
    port: ServicePortEntry,
    *,
    fsp: str,
    ont_id: int,
    gem_index: int,
    vlan_id: int,
    user_vlan: int | str | None,
    tag_transform: str,
) -> bool:
    if port.ont_id != ont_id or port.gem_index != gem_index or port.vlan_id != vlan_id:
        return False
    if port.fsp and port.fsp.strip() != fsp.strip():
        return False

    expected_user_vlan = vlan_id if user_vlan is None else user_vlan
    if (
        port.flow_para
        and str(port.flow_para).strip() != str(expected_user_vlan).strip()
    ):
        return False

    if port.tag_transform and tag_transform:
        return port.tag_transform.strip().casefold() == tag_transform.strip().casefold()
    return True


def get_service_ports_for_ont(
    olt: OLTDevice, fsp: str, ont_id: int
) -> tuple[bool, str, list[ServicePortEntry]]:
    """Return only the service-ports belonging to a specific ONT."""
    from app.services.network import olt_ssh as core

    ok, msg, all_ports = core.get_service_ports(olt, fsp)
    if not ok:
        return False, msg, []
    filtered = [p for p in all_ports if p.ont_id == ont_id]
    return True, f"Found {len(filtered)} service-port(s) for ONT {ont_id}", filtered


def get_service_port_by_index(
    olt: OLTDevice, index: int
) -> tuple[bool, str, ServicePortEntry | None]:
    """Read one service-port by global OLT index."""
    from app.services.network import olt_ssh as core

    try:
        transport, channel, policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", None

    try:
        prompt = core._prepare_huawei_read_shell(channel, policy.prompt_regex)
        output = core._run_huawei_paged_cmd(
            channel,
            f"display service-port {index}",
            prompt=prompt,
        )
        if core.is_error_output(output):
            return False, describe_huawei_rejection(output, detail_limit=150), None
        entries = core._parse_service_port_table(output)
        for entry in entries:
            if entry.index == index:
                return True, f"Found service-port {index}", entry
        detail = _parse_service_port_detail(output)
        if detail is not None and detail.index == index:
            return True, f"Found service-port {index}", detail
        return True, f"Service-port {index} was not found", None
    except Exception as exc:
        logger.error(
            "Error reading service-port %d on OLT %s: %s", index, olt.name, exc
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()


def _verify_conflicted_service_port(
    olt: OLTDevice,
    conflicted_index: int,
    *,
    fsp: str,
    ont_id: int,
    gem_index: int,
    vlan_id: int,
    user_vlan: int | str | None,
    tag_transform: str,
) -> tuple[bool, str, ServicePortEntry | None]:
    """Verify an idempotent create conflict despite Huawei readback lag."""
    last_message = f"Service-port {conflicted_index} was not found"
    for attempt in range(_SERVICE_PORT_VERIFY_ATTEMPTS):
        read_ok, read_msg, existing_port = get_service_port_by_index(
            olt, conflicted_index
        )
        last_message = read_msg
        if existing_port is not None:
            if _service_port_matches_intent(
                existing_port,
                fsp=fsp,
                ont_id=ont_id,
                gem_index=gem_index,
                vlan_id=vlan_id,
                user_vlan=user_vlan,
                tag_transform=tag_transform,
            ):
                return True, read_msg, existing_port
            return (
                True,
                "Existing service-port maps to a different intent",
                existing_port,
            )

        # A conflicted global index can lag while the per-PON table is current.
        list_ok, list_msg, ports = get_service_ports_for_ont(olt, fsp, ont_id)
        if list_ok:
            for port in ports:
                if _service_port_matches_intent(
                    port,
                    fsp=fsp,
                    ont_id=ont_id,
                    gem_index=gem_index,
                    vlan_id=vlan_id,
                    user_vlan=user_vlan,
                    tag_transform=tag_transform,
                ):
                    return True, list_msg, port
        elif not read_ok:
            last_message = f"{read_msg}; fallback readback failed: {list_msg}"

        if attempt + 1 < _SERVICE_PORT_VERIFY_ATTEMPTS:
            time.sleep(_SERVICE_PORT_VERIFY_DELAY_SEC)

    return False, last_message, None


def _find_service_port_matching_intent(
    olt: OLTDevice,
    *,
    fsp: str,
    ont_id: int,
    gem_index: int,
    vlan_id: int,
    user_vlan: int | str | None,
    tag_transform: str,
) -> tuple[bool, str, ServicePortEntry | None]:
    """Find this ONT's service-port matching the requested intent.

    Used when the OLT reports an idempotent "already exists" conflict without
    naming the conflicting index, so there is nothing to look up by index.
    Retries because the per-PON table can lag a create.
    """
    last_message = f"No service-port on {fsp} ONT {ont_id} matched the requested intent"
    for attempt in range(_SERVICE_PORT_VERIFY_ATTEMPTS):
        list_ok, list_msg, ports = get_service_ports_for_ont(olt, fsp, ont_id)
        if list_ok:
            for port in ports:
                if _service_port_matches_intent(
                    port,
                    fsp=fsp,
                    ont_id=ont_id,
                    gem_index=gem_index,
                    vlan_id=vlan_id,
                    user_vlan=user_vlan,
                    tag_transform=tag_transform,
                ):
                    return True, list_msg, port
        else:
            last_message = list_msg

        if attempt + 1 < _SERVICE_PORT_VERIFY_ATTEMPTS:
            time.sleep(_SERVICE_PORT_VERIFY_DELAY_SEC)

    return False, last_message, None


def clone_service_ports(
    db: Session, olt_id: str, fsp: str, ont_id: int
) -> tuple[bool, str]:
    """Clone service-ports for an ONT using a reference ONT on the same port."""
    from app.services.network import olt_ssh as core

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"

    ok, msg, entries = core.get_service_ports(olt, fsp)
    if not ok or not entries:
        return False, f"Cannot read service-ports: {msg}"

    ont_counts = Counter(e.ont_id for e in entries)
    if not ont_counts:
        return False, "No existing service-ports to learn from"

    new_ont_ports = [e for e in entries if e.ont_id == ont_id]
    if new_ont_ports:
        return True, f"ONT {ont_id} already has {len(new_ont_ports)} service-port(s)"

    reference_ont_id = None
    for candidate_ont_id, _count in ont_counts.most_common():
        if candidate_ont_id != ont_id:
            reference_ont_id = candidate_ont_id
            break
    if reference_ont_id is None:
        return False, "No reference ONT found to learn service-port pattern from"

    reference_ports = [e for e in entries if e.ont_id == reference_ont_id]
    logger.info(
        "Learning service-port pattern from ONT %d (%d ports) for new ONT %d on %s",
        reference_ont_id,
        len(reference_ports),
        ont_id,
        fsp,
    )

    return core.create_service_ports(olt, fsp, ont_id, reference_ports)


def provision_ont_service_ports(
    db: Session, olt_id: str, fsp: str, ont_id: int
) -> tuple[bool, str]:
    """Compatibility alias for explicit service-port cloning."""
    return clone_service_ports(db, olt_id, fsp, ont_id)


def delete_service_port(olt: OLTDevice, index: int) -> tuple[bool, str]:
    """Delete a service-port from the OLT by index."""
    from app.services.network import olt_ssh as core

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        output = core._run_huawei_cmd(
            channel, f"undo service-port {index}", prompt=config_prompt
        )
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "Failed to delete service-port %d on OLT %s: %s",
                index,
                olt.name,
                output.strip()[-150:],
            )
            return False, describe_huawei_rejection(output, detail_limit=150)

        logger.info("Deleted service-port %d on OLT %s", index, olt.name)
        core._invalidate_olt_read_cache(olt, "service_ports", "running_config")
        return True, f"Service-port {index} deleted"
    except Exception as exc:
        logger.error("Error deleting service-port on OLT %s: %s", olt.name, exc)
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

    Args:
        olt: OLT device to connect to.
        fsp: Frame/Slot/Port string.
        ont_id: ONT ID on the PON port.
        gem_index: GEM port index.
        vlan_id: Service VLAN ID.
        user_vlan: User VLAN (default: same as vlan_id).
        tag_transform: VLAN tag transform mode.
        port_index: Pre-allocated service-port index. If None, OLT auto-assigns.
        traffic_table_inbound: OLT traffic-table index for inbound QoS (optional).
        traffic_table_outbound: OLT traffic-table index for outbound QoS (optional).

    Returns:
        (success, message, assigned_index). assigned_index is the port_index used,
        or None if not available.
    """
    from app.services.network import olt_ssh as core

    parts = canonical_fsp(fsp)
    if parts is None:
        return False, f"Invalid F/S/P format: {fsp!r}", None
    fsp = parts.fsp

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", None

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        cmd = core.build_service_port_command(
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
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            conflict_match = _CONFLICTED_SERVICE_PORT_RE.search(output)
            if is_huawei_idempotent_conflict(output) and not conflict_match:
                # The OLT reports "Service virtual port has existed already"
                # without naming the conflicting index. Resolve it by exact
                # readback of this ONT's service-ports rather than failing:
                # a retry after a partial run must not fail on work the first
                # run already completed.
                read_ok, read_msg, existing_port = _find_service_port_matching_intent(
                    olt,
                    fsp=fsp,
                    ont_id=ont_id,
                    gem_index=gem_index,
                    vlan_id=vlan_id,
                    user_vlan=user_vlan,
                    tag_transform=tag_transform,
                )
                if not read_ok or existing_port is None:
                    return (
                        False,
                        (
                            "OLT reported the service-port already exists, but no "
                            f"matching service-port was found on readback: {read_msg}"
                        ),
                        None,
                    )
                logger.info(
                    "Service-port already exists on OLT %s (resolved by readback): "
                    "index=%d VLAN=%d GEM=%d ONT=%d %s",
                    olt.name,
                    existing_port.index,
                    vlan_id,
                    gem_index,
                    ont_id,
                    fsp,
                )
                core._invalidate_olt_read_cache(olt, "service_ports", "running_config")
                return (
                    True,
                    (
                        "Service-port already exists with the requested "
                        f"ONT/VLAN/GEM tuple at index {existing_port.index}"
                    ),
                    existing_port.index,
                )
            if is_huawei_idempotent_conflict(output) and conflict_match:
                conflicted_index = int(conflict_match.group(1))
                read_ok, read_msg, existing_port = _verify_conflicted_service_port(
                    olt,
                    conflicted_index,
                    fsp=fsp,
                    ont_id=ont_id,
                    gem_index=gem_index,
                    vlan_id=vlan_id,
                    user_vlan=user_vlan,
                    tag_transform=tag_transform,
                )
                if not read_ok or existing_port is None:
                    return (
                        False,
                        (
                            "Service-port conflict detected, but existing index "
                            f"{conflicted_index} could not be verified: {read_msg}"
                        ),
                        None,
                    )
                if not _service_port_matches_intent(
                    existing_port,
                    fsp=fsp,
                    ont_id=ont_id,
                    gem_index=gem_index,
                    vlan_id=vlan_id,
                    user_vlan=user_vlan,
                    tag_transform=tag_transform,
                ):
                    return (
                        False,
                        (
                            "Service-port conflict detected at index "
                            f"{conflicted_index}, but it maps to a different "
                            "ONT/VLAN/GEM tuple"
                        ),
                        None,
                    )
                logger.info(
                    "Service-port already exists on OLT %s: index=%d VLAN=%d GEM=%d ONT=%d %s",
                    olt.name,
                    existing_port.index,
                    vlan_id,
                    gem_index,
                    ont_id,
                    fsp,
                )
                core._invalidate_olt_read_cache(olt, "service_ports", "running_config")
                return (
                    True,
                    (
                        "Service-port already exists "
                        f"(index {existing_port.index}, VLAN {vlan_id}, GEM {gem_index})"
                    ),
                    existing_port.index,
                )
            logger.warning(
                "Service-port creation failed on OLT %s: %s",
                olt.name,
                output.strip()[-150:],
            )
            return False, describe_huawei_rejection(output, detail_limit=150), None

        logger.info(
            "Created service-port %s VLAN %d GEM %d for ONT %d on OLT %s %s",
            port_index or "auto",
            vlan_id,
            gem_index,
            ont_id,
            olt.name,
            fsp,
        )
        core._invalidate_olt_read_cache(olt, "service_ports", "running_config")
        return (
            True,
            f"Service-port created (VLAN {vlan_id}, GEM {gem_index})",
            port_index,
        )
    except Exception as exc:
        logger.error("Error creating service-port on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}", None
    finally:
        transport.close()
