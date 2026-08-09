"""Database configuration management for ONT web actions."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.services import network as network_service
from app.services.credential_crypto import encrypt_credential
from app.services.network.ont_actions import ActionResult
from app.services.network.ont_desired_config import set_desired_config_values
from app.services.network.ont_management_ipam import (
    allocate_ont_management_ip,
    release_ont_management_ip,
)
from app.services.network.subscriber_wan_ipam import ensure_wan_static_ip_available
from app.services.web_network_ont_actions._common import (
    _is_input_error,
    _log_action_audit,
    action_result_audit_metadata,
    cache_current_user_context,
)
from app.services.web_network_ont_actions.config_setters import (
    set_lan_config,
    set_mgmt_remote_access,
    set_wifi_config,
)

logger = logging.getLogger(__name__)


# Reconcile failure reasons that mean "the change is saved but was not
# delivered yet, and will be delivered when the device next reachable/informs".
# Everything else — a rejected write, a post-apply verification mismatch, an
# invalid change — is a real failure and must never be reported as a saved,
# waiting, or applied change.
_DEFERRABLE_FAILURE_REASONS = frozenset(
    {
        "acs_cr_failed",
        "acs_unreachable",
        "acs_identity_unresolved",
        "olt_unreachable",
        "ont_not_informing",
        "ont_offline",
    }
)


def _delivery_pending_result(result: ActionResult) -> ActionResult:
    """Reclassify a *deferral* as saved-and-waiting; leave failures failing.

    Three outcomes are distinct and must stay distinct:

    * delivered   — the device accepted and verified the change;
    * deferred    — the change is persisted, nothing was delivered, and the
                    reconciler will deliver it once the CPE informs;
    * failed      — the write was attempted and rejected, or the readback
                    disagreed. The device is NOT in the requested state.

    Only the middle case may be lifted to ``success=True, waiting=True``. This
    used to lift any non-input-error, which turned rejected WAN/WiFi writes into
    "Configuration saved" for the field engineer reading the banner.
    """
    if result.success or _is_input_error(result.message):
        return result

    data = dict(result.data or {})
    if data.get("delivery_pending") is False:
        return result

    reason = data.get("failure_reason")
    if reason is not None and reason not in _DEFERRABLE_FAILURE_REASONS:
        return result

    text = (result.message or "").lower()
    if "no acs server configured" in text:
        return result

    data["delivery_pending"] = True
    data.setdefault("waiting_reason", "next_inform")
    detail = (result.message or "Device is not reachable through ACS.").strip()
    return ActionResult(
        success=True,
        message=f"saved, not yet delivered — waiting for device inform ({detail})",
        data=data,
        waiting=True,
    )


def update_ont_config(
    db: Session,
    ont_id: str,
    *,
    wan_mode: str | None = None,
    config_method: str | None = None,
    ip_protocol: str | None = None,
    wan_static_ip: str | None = None,
    wan_static_subnet: str | None = None,
    wan_static_gateway: str | None = None,
    wan_static_dns: str | None = None,
    pppoe_username: str | None = None,
    pppoe_password: str | None = None,
    mgmt_ip_mode: str | None = None,
    mgmt_ip_address: str | None = None,
    mgmt_remote_access: bool | None = None,
    lan_gateway_ip: str | None = None,
    lan_subnet_mask: str | None = None,
    lan_dhcp_enabled: bool | None = None,
    lan_dhcp_start: str | None = None,
    lan_dhcp_end: str | None = None,
    wifi_enabled: bool | None = None,
    wifi_ssid: str | None = None,
    wifi_channel: str | None = None,
    wifi_security_mode: str | None = None,
    wifi_password: str | None = None,
    voip_enabled: bool | None = None,
    pppoe_wcd_index: int | None = None,
    mgmt_wcd_index: int | None = None,
    voip_wcd_index: int | None = None,
    mgmt_service_port_index: int | None = None,
    wan_service_port_index: int | None = None,
    push_to_device: bool = False,
    push_wan: bool = True,
    push_lan: bool = True,
    push_mgmt: bool = True,
    push_wifi: bool = True,
    request: Request | None = None,
) -> ActionResult:
    """Update ONT configuration fields in the database, optionally push to device."""

    cache_current_user_context(request)

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    if not ont:
        return ActionResult(success=False, message="ONT not found")

    # Management reachability is controlled by desired_config access intent.
    mgmt_access_enabled: bool | None = None
    if mgmt_ip_mode is not None:
        mgmt_access_enabled = mgmt_ip_mode in {"dhcp", "static_ip"}
    elif mgmt_remote_access is not None:
        mgmt_access_enabled = mgmt_remote_access
    if voip_enabled is not None:
        ont.voip_enabled = voip_enabled

    try:
        desired_updates = {}
        if wan_mode is not None:
            wan_mode_value = wan_mode.strip() or None
            desired_updates["wan.mode"] = wan_mode_value
            if wan_mode_value != "pppoe":
                desired_updates["wan.pppoe_username"] = None
                desired_updates["wan.pppoe_password"] = None
        if ip_protocol is not None:
            desired_updates["wan.ip_protocol"] = ip_protocol.strip() or None
        if wan_static_ip is not None:
            desired_updates["wan.static_ip"] = ensure_wan_static_ip_available(
                db,
                ont=ont,
                requested_ip=wan_static_ip,
            )
        if wan_static_subnet is not None:
            desired_updates["wan.static_subnet"] = wan_static_subnet.strip() or None
        if wan_static_gateway is not None:
            desired_updates["wan.static_gateway"] = wan_static_gateway.strip() or None
        if wan_static_dns is not None:
            desired_updates["wan.static_dns"] = wan_static_dns.strip() or None
        if pppoe_username is not None:
            desired_updates["wan.pppoe_username"] = pppoe_username.strip() or None
        management_mode = mgmt_ip_mode.strip() if mgmt_ip_mode is not None else None
        management_address = (
            mgmt_ip_address.strip() if mgmt_ip_address is not None else None
        )
        management_allocation = None
        if management_mode == "static_ip":
            try:
                management_allocation = allocate_ont_management_ip(
                    db,
                    ont=ont,
                    requested_ip=management_address,
                )
            except ValueError as exc:
                db.rollback()
                return ActionResult(success=False, message=str(exc))
            management_address = management_allocation.address
        elif management_mode in {"inactive", "dhcp"}:
            release_ont_management_ip(db, ont=ont, mode=management_mode)
        if mgmt_ip_mode is not None:
            desired_updates["management.ip_mode"] = management_mode or None
        if mgmt_ip_address is not None:
            desired_updates["management.ip_address"] = management_address or None
        if management_mode == "static_ip" and management_allocation is not None:
            desired_updates["management.subnet"] = management_allocation.subnet
            desired_updates["management.gateway"] = management_allocation.gateway
        elif management_mode in {"inactive", "dhcp"}:
            desired_updates["management.subnet"] = None
            desired_updates["management.gateway"] = None
            desired_updates["management.vlan"] = None
            desired_updates["management.vlan_id"] = None
        if mgmt_access_enabled is not None:
            desired_updates["access.mgmt_remote"] = mgmt_access_enabled
        if lan_gateway_ip is not None:
            desired_updates["lan.ip"] = lan_gateway_ip.strip() or None
        if lan_subnet_mask is not None:
            desired_updates["lan.subnet"] = lan_subnet_mask.strip() or None
        if lan_dhcp_enabled is not None:
            desired_updates["lan.dhcp_enabled"] = lan_dhcp_enabled
        if lan_dhcp_start is not None:
            desired_updates["lan.dhcp_start"] = lan_dhcp_start.strip() or None
        if lan_dhcp_end is not None:
            desired_updates["lan.dhcp_end"] = lan_dhcp_end.strip() or None
        if wifi_enabled is not None:
            desired_updates["wifi.enabled"] = wifi_enabled
        if wifi_ssid is not None:
            desired_updates["wifi.ssid"] = wifi_ssid.strip() or None
        if wifi_channel is not None:
            desired_updates["wifi.channel"] = wifi_channel.strip() or None
        if wifi_security_mode is not None:
            desired_updates["wifi.security_mode"] = wifi_security_mode.strip() or None
        if pppoe_password:
            desired_updates["wan.pppoe_password"] = encrypt_credential(pppoe_password)
        if wifi_password:
            desired_updates["wifi.password"] = encrypt_credential(wifi_password)
        # WANConnectionDevice index overrides. A sentinel of 0 (or any
        # non-positive int) clears the override and falls back to the
        # config-pack default. Validated as positive ints upstream in the
        # form handler — defensive coercion here is the second line.
        if pppoe_wcd_index is not None:
            desired_updates["wan.pppoe_wcd_index"] = (
                int(pppoe_wcd_index) if int(pppoe_wcd_index) > 0 else None
            )
        if mgmt_wcd_index is not None:
            desired_updates["management.wcd_index"] = (
                int(mgmt_wcd_index) if int(mgmt_wcd_index) > 0 else None
            )
        if voip_wcd_index is not None:
            desired_updates["voip.wcd_index"] = (
                int(voip_wcd_index) if int(voip_wcd_index) > 0 else None
            )
        # OLT service-port indices. None clears the override (planner
        # re-allocates); positive ints are written through.
        if mgmt_service_port_index is not None:
            desired_updates["olt.mgmt_service_port_index"] = (
                int(mgmt_service_port_index)
                if int(mgmt_service_port_index) > 0
                else None
            )
        if wan_service_port_index is not None:
            desired_updates["olt.wan_service_port_index"] = (
                int(wan_service_port_index) if int(wan_service_port_index) > 0 else None
            )
        set_desired_config_values(ont, desired_updates)
    except ValueError as exc:
        db.rollback()
        return ActionResult(success=False, message=str(exc))

    db.add(ont)
    db.flush()

    push_messages: list[str] = []
    push_details: list[dict[str, object]] = []
    push_success = True
    push_waiting = False

    if push_to_device:
        # Remote ACS writes can block while the CPE is slow to consume tasks.
        # Persist the desired intent before those calls so the request does not
        # hold an idle database transaction open for the duration of TR-069
        # polling.
        db.commit()

        wan_push_requested = push_wan and any(
            value is not None
            for value in (
                wan_mode,
                config_method,
                ip_protocol,
                wan_static_ip,
                wan_static_subnet,
                wan_static_gateway,
                wan_static_dns,
                pppoe_username,
                pppoe_password,
            )
        )
        if wan_push_requested:
            from app.services.network.reconcile import (
                OntWanProposedChange,
                OntWanProposedField,
                desired_from_ont_unit,
                reconcile_ont,
            )

            # The form has already persisted the requested WAN intent. Resolve
            # that effective state back into the reconciler's typed field names
            # and carry only the fields the engineer submitted. Without this
            # proposal scope, ``reconcile_ont`` treats Apply WAN as permission
            # to repair the entire ONT, including unrelated service ports,
            # management, LAN and WiFi state.
            desired = desired_from_ont_unit(db, ont)
            changed_fields: set[OntWanProposedField] = set()
            if wan_mode is not None or config_method is not None:
                changed_fields.add("wan_mode")
            if ip_protocol is not None and ip_protocol.strip().lower() in {
                "ipv4",
                "dual_stack",
            }:
                changed_fields.add("ipv6_enabled")
            if wan_static_ip is not None:
                changed_fields.update({"wan_static_ip", "wan_static_ip_is_public"})
            if wan_static_subnet is not None:
                changed_fields.add("wan_static_subnet")
            if wan_static_gateway is not None:
                changed_fields.add("wan_static_gateway")
            if wan_static_dns is not None:
                changed_fields.add("wan_static_dns")
            if pppoe_username is not None:
                changed_fields.add("wan_pppoe_username")
            if pppoe_password is not None:
                changed_fields.add("wan_pppoe_password_ref")
            if pppoe_wcd_index is not None:
                changed_fields.add("wan_pppoe_wcd_index")

            proposed_change = OntWanProposedChange(
                changed_fields=frozenset(changed_fields),
                wan_mode=desired.wan_mode,
                wan_pppoe_username=desired.wan_pppoe_username,
                wan_pppoe_password_ref=desired.wan_pppoe_password_ref,
                wan_pppoe_wcd_index=desired.wan_pppoe_wcd_index,
                wan_static_ip=desired.wan_static_ip,
                wan_static_subnet=desired.wan_static_subnet,
                wan_static_gateway=desired.wan_static_gateway,
                wan_static_dns=desired.wan_static_dns,
                wan_static_ip_is_public=desired.wan_static_ip_is_public,
                ipv6_enabled=desired.ipv6_enabled,
            )

            reconciled = reconcile_ont(
                db,
                ont_id,
                proposed_change=proposed_change,
                mode="sync",
            )
            result = ActionResult(
                success=reconciled.success,
                message=(
                    "WAN configuration applied and verified."
                    if reconciled.success
                    else (
                        reconciled.failure.message
                        if reconciled.failure
                        else "WAN reconciliation failed."
                    )
                ),
                data={
                    "sync_status": reconciled.sync_status,
                    "actions_applied": [
                        action.field for action in reconciled.actions_applied
                    ],
                    "action_evidence": [
                        {
                            "field": action.field,
                            "evidence": getattr(action, "evidence", None),
                        }
                        for action in reconciled.actions_applied
                        if getattr(action, "evidence", None) is not None
                    ],
                    "failure_reason": (
                        reconciled.failure.reason if reconciled.failure else None
                    ),
                    "failure_evidence": getattr(reconciled.failure, "evidence", None),
                    # The WAN leg used to admit only ``acs_cr_failed`` as a
                    # deferral, so every other undelivered outcome reported as
                    # a hard failure while the LAN/WiFi legs lifted *anything*
                    # to success. Both legs now use one classification.
                    "delivery_pending": bool(
                        reconciled.failure
                        and reconciled.failure.reason in _DEFERRABLE_FAILURE_REASONS
                    ),
                },
            )
            raw_result = result
            delivered_result = _delivery_pending_result(result)
            push_details.append(
                {
                    "step": "wan",
                    "raw": action_result_audit_metadata(raw_result),
                    "reported": action_result_audit_metadata(delivered_result),
                }
            )
            result = delivered_result
            push_messages.append(f"WAN: {result.message}")
            push_waiting = push_waiting or result.waiting
            if not raw_result.success:
                # Log the raw failure for diagnostics even when
                # _delivery_pending_result has lifted it to "saved,
                # waiting for inform".
                logger.warning(
                    "Apply-all WAN step failed for ONT %s: %s data=%s",
                    ont_id,
                    raw_result.message,
                    action_result_audit_metadata(raw_result).get("data"),
                )
            # ``result`` is the post-lift value; a delivery-pending lift
            # makes the overall push "waiting" rather than "failed".
            if not result.success:
                push_success = False

        if push_lan and any(
            [
                lan_gateway_ip,
                lan_subnet_mask,
                lan_dhcp_enabled is not None,
                lan_dhcp_start,
                lan_dhcp_end,
            ]
        ):
            result = set_lan_config(
                db,
                ont_id,
                lan_ip=lan_gateway_ip.strip() if lan_gateway_ip else None,
                lan_subnet=lan_subnet_mask.strip() if lan_subnet_mask else None,
                dhcp_enabled=lan_dhcp_enabled,
                dhcp_start=lan_dhcp_start.strip() if lan_dhcp_start else None,
                dhcp_end=lan_dhcp_end.strip() if lan_dhcp_end else None,
                request=request,
            )
            raw_result = result
            delivered_result = _delivery_pending_result(result)
            push_details.append(
                {
                    "step": "lan",
                    "raw": action_result_audit_metadata(raw_result),
                    "reported": action_result_audit_metadata(delivered_result),
                }
            )
            result = delivered_result
            push_messages.append(f"LAN: {result.message}")
            push_waiting = push_waiting or result.waiting
            if not raw_result.success:
                logger.warning(
                    "Apply-all LAN step failed for ONT %s: %s data=%s",
                    ont_id,
                    raw_result.message,
                    action_result_audit_metadata(raw_result).get("data"),
                )
            if not result.success:
                push_success = False

        if push_mgmt and mgmt_ip_mode is not None:
            result = set_mgmt_remote_access(
                db,
                ont_id,
                enabled=bool(mgmt_access_enabled),
                request=request,
            )
            push_details.append(
                {
                    "step": "management",
                    "raw": action_result_audit_metadata(result),
                    "reported": action_result_audit_metadata(result),
                }
            )
            push_messages.append(f"Management: {result.message}")
            push_waiting = push_waiting or result.waiting
            if not result.success:
                logger.warning(
                    "Apply-all management step failed for ONT %s: %s data=%s",
                    ont_id,
                    result.message,
                    action_result_audit_metadata(result).get("data"),
                )
                push_success = False

        if push_wifi and any(
            [
                wifi_enabled is not None,
                wifi_ssid,
                wifi_password,
                wifi_security_mode,
                wifi_channel,
            ]
        ):
            channel_int: int | None = None
            if wifi_channel:
                try:
                    channel_int = int(wifi_channel)
                except ValueError:
                    pass
            result = set_wifi_config(
                db,
                ont_id,
                enabled=wifi_enabled,
                ssid=wifi_ssid.strip() if wifi_ssid else None,
                password=wifi_password.strip() if wifi_password else None,
                channel=channel_int,
                security_mode=wifi_security_mode.strip()
                if wifi_security_mode
                else None,
                request=request,
            )
            raw_result = result
            delivered_result = _delivery_pending_result(result)
            push_details.append(
                {
                    "step": "wifi",
                    "raw": action_result_audit_metadata(raw_result),
                    "reported": action_result_audit_metadata(delivered_result),
                }
            )
            result = delivered_result
            push_messages.append(f"WiFi: {result.message}")
            push_waiting = push_waiting or result.waiting
            if not raw_result.success:
                logger.warning(
                    "Apply-all WiFi step failed for ONT %s: %s data=%s",
                    ont_id,
                    raw_result.message,
                    action_result_audit_metadata(raw_result).get("data"),
                )
            if not result.success:
                push_success = False

    _log_action_audit(
        db,
        request=request,
        action="update_ont_config",
        ont_id=ont_id,
        metadata={
            "wan_mode": wan_mode,
            "pppoe_username": pppoe_username,
            "wifi_ssid": wifi_ssid,
            "push_to_device": push_to_device,
            "push_success": push_success if push_to_device else None,
            "push_waiting": push_waiting if push_to_device else None,
            "push_messages": push_messages if push_to_device else [],
            "push_details": push_details if push_to_device else [],
        },
    )

    if push_to_device:
        # "Saved" and "delivered" are two different facts. The local desired
        # state was committed at the top of the push block; whether any of it
        # reached the CPE is decided here, and the ONT is flagged so a failed
        # or deferred delivery is visible after the request ends.
        if push_waiting:
            set_desired_config_values(
                ont,
                {
                    "delivery.pending_apply": True,
                    "delivery.last_push_failure": None,
                    "delivery.last_push_failure_at": None,
                },
            )
            db.add(ont)
            db.flush()
        elif push_success:
            set_desired_config_values(
                ont,
                {
                    "delivery.pending_apply": None,
                    "delivery.last_push_failure": None,
                    "delivery.last_push_failure_at": None,
                },
            )
            db.add(ont)
            db.flush()
        else:
            # Hard failure: previously neither branch ran, so nothing recorded
            # that this ONT's config never reached the device.
            set_desired_config_values(
                ont,
                {
                    "delivery.pending_apply": None,
                    "delivery.last_push_failure": "; ".join(push_messages)[:400]
                    or "device delivery failed",
                    "delivery.last_push_failure_at": datetime.now(UTC).isoformat(),
                },
            )
            db.add(ont)
            db.flush()

        detail = "; ".join(push_messages)
        if not push_messages:
            message = "Saved to database. No device-delivered fields changed."
        elif not push_success:
            message = (
                "Saved to database, but delivery to the device FAILED — the "
                f"device is unchanged for the failed step(s). {detail}"
            )
        elif push_waiting:
            message = (
                "Saved to database. Not yet delivered to the device; waiting "
                f"for its next inform. {detail}"
            )
        else:
            message = f"Saved to database and delivered to the device. {detail}"
        return ActionResult(success=push_success, message=message, waiting=push_waiting)

    return ActionResult(
        success=True,
        message="Saved to database. Not sent to the device.",
    )


def set_voip_enabled(
    db: Session,
    ont_id: str,
    *,
    enabled: bool,
    request: Request | None = None,
) -> ActionResult:
    """Set VoIP enabled status on ONT."""
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    if not ont:
        return ActionResult(success=False, message="ONT not found")

    ont.voip_enabled = enabled
    db.commit()

    status = "enabled" if enabled else "disabled"
    _log_action_audit(
        db,
        request=request,
        action="set_voip_enabled",
        ont_id=ont_id,
        metadata={"voip_enabled": enabled},
    )
    return ActionResult(success=True, message=f"VoIP {status} on {ont.serial_number}")
