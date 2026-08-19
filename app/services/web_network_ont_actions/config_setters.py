"""Configuration setters for ONT web actions."""

from __future__ import annotations

import logging
from ipaddress import ip_address

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OntUnit
from app.services.genieacs_service import genieacs_service
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.huawei_cli_response import is_huawei_cli_unsupported
from app.services.network.olt_config_pack import resolve_olt_config_pack
from app.services.network.ont_action_common import ActionResult
from app.services.network.ont_desired_config import set_access_flag
from app.services.network.ont_features import OntFeatureService
from app.services.network.provisioning_settings import (
    get_olt_write_mode_enabled,
    get_pppoe_provisioning_method,
)
from app.services.web_network_ont_actions._common import (
    _intent_saved_result,
    _log_action_audit,
    _persist_ont_plan_step,
    action_result_audit_metadata,
    actor_name_from_request,
)

logger = logging.getLogger(__name__)


def _wan_mode_to_instance_type(wan_mode: str | None) -> str:
    normalized = str(wan_mode or "").strip().lower()
    if normalized in {"dhcp", "static", "static_ip", "bridge", "bridged"}:
        return "ip"
    return "ppp"


def _int_or_none(value: object) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _huawei_command_unsupported(message: object) -> bool:
    return is_huawei_cli_unsupported(
        message,
        parameter_error_is_unsupported=True,
    )


def _tr069_value(node: object, *path: str) -> object | None:
    current = node
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, dict) and "_value" in current:
        return current.get("_value")
    return current


def _has_usable_ip_address(value: str) -> bool:
    try:
        return not ip_address(value).is_unspecified
    except ValueError:
        return False


def _find_internet_wan_ppp(
    raw_device: dict[str, object],
) -> tuple[int, int, dict[str, object]] | None:
    wcd_root = _tr069_value(
        raw_device,
        "InternetGatewayDevice",
        "WANDevice",
        "1",
        "WANConnectionDevice",
    )
    if not isinstance(wcd_root, dict):
        return None

    candidates: list[tuple[int, int, dict[str, object], int]] = []
    for wcd_key in sorted(str(key) for key in wcd_root if str(key).isdigit()):
        wcd = wcd_root.get(wcd_key)
        if not isinstance(wcd, dict):
            continue
        ppp_root = wcd.get("WANPPPConnection")
        if not isinstance(ppp_root, dict):
            continue
        for ppp_key in sorted(str(key) for key in ppp_root if str(key).isdigit()):
            ppp = ppp_root.get(ppp_key)
            if not isinstance(ppp, dict):
                continue
            status = str(_tr069_value(ppp, "ConnectionStatus") or "").lower()
            service = str(_tr069_value(ppp, "X_HW_SERVICELIST") or "").upper()
            ip_address = str(_tr069_value(ppp, "ExternalIPAddress") or "").strip()
            score = 0
            if status == "connected":
                score += 4
            if _has_usable_ip_address(ip_address):
                score += 3
            if "INTERNET" in service:
                score += 2
            candidates.append((int(wcd_key), int(ppp_key), ppp, score))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[3], reverse=True)
    wcd_index, ppp_index, ppp_data, _score = candidates[0]
    return wcd_index, ppp_index, ppp_data


def _plain_acs_bind_error(exc: Exception) -> str:
    raw = str(exc or "").strip()
    lowered = raw.lower()
    if "pending" in lowered and "task" in lowered:
        return (
            "The ONT already has pending ACS work. Wait for the next inform, "
            "or clear stale ACS tasks, then retry Bind Internet WAN."
        )
    if "401" in lowered or "unauthorized" in lowered:
        return (
            "ACS cannot log in to the ONT right now. Wait for the next inform "
            "to refresh the connection request credentials, then retry."
        )
    if "connection request" in lowered or "cr failed" in lowered:
        return (
            "ACS cannot reach the ONT from the management side right now. "
            "Wait for the ONT to inform, then retry."
        )
    if "timeout" in lowered or "timed out" in lowered:
        return (
            "The ONT did not confirm the change in time. Check that it is "
            "still informing ACS, then retry."
        )
    return (
        "The WAN bind could not be sent through ACS. Check ACS device status "
        "and retry after the device informs."
    )


def _pppoe_omci_ip_index(
    effective_values: dict[str, object],
    *,
    instance_index: int,
) -> int:
    """Resolve Huawei OLT ip-index for PPPoE.

    OLT ip-index N is normally exposed to TR-069 as WCD N+1. Prefer the
    PPPoE WCD index over the generic internet_config_ip_index because the
    latter can point at the management/default stack on older config packs.
    """
    pppoe_wcd_index = _int_or_none(effective_values.get("pppoe_wcd_index"))
    if pppoe_wcd_index is not None:
        return max(pppoe_wcd_index - 1, 0)

    internet_config_ip_index = _int_or_none(
        effective_values.get("internet_config_ip_index")
    )
    if internet_config_ip_index is not None:
        return internet_config_ip_index

    if instance_index > 1:
        return instance_index - 1
    return 1


def _validate_olt_write_dependencies(
    db: Session,
    olt: object,
    *,
    cached_only: bool = False,
) -> ActionResult | None:
    """Return a failed action result when live OLT dependencies are invalid."""
    from app.services.network.olt_dependency_preflight import (
        get_cached_olt_dependency_validation,
        validate_olt_profile_dependencies,
    )

    olt_id = getattr(olt, "id", None)
    if olt_id is None:
        return ActionResult(
            success=False,
            message="OLT ID not available for dependency audit.",
            data={"delivery_pending": False},
        )
    if cached_only:
        result = get_cached_olt_dependency_validation(str(olt_id))
        if result is None:
            result = validate_olt_profile_dependencies(
                db,
                olt_id=str(olt_id),
                operation="manual OLT write",
            )
    else:
        result = validate_olt_profile_dependencies(
            db,
            olt_id=str(olt_id),
            operation="manual OLT write",
        )
    if result.success:
        return None
    return ActionResult(
        success=False,
        message=result.message,
        data={
            "delivery_pending": False,
            "dependency_audit": result.audit,
        },
    )


def _set_pppoe_config_omci(
    db: Session,
    ont_id: str,
    *,
    username: str,
    password: str,
    wan_vlan: int,
    instance_index: int,
) -> ActionResult:
    """Apply PPPoE WAN credentials through the OLT/OMCI path."""
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.network.ont_provisioning.context import resolve_olt_context

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return ActionResult(
            success=False,
            message="ONT not found.",
            data={"delivery_transport": "olt_omci", "delivery_pending": False},
        )

    effective = resolve_effective_ont_config(db, ont)
    effective_values = (
        effective.get("values", {}) if isinstance(effective, dict) else {}
    )
    if not isinstance(effective_values, dict):
        effective_values = {}

    ctx, err = resolve_olt_context(db, ont_id)
    if ctx is None:
        return ActionResult(
            success=False,
            message=f"WAN PPPoE OMCI apply failed: {err}",
            data={"delivery_transport": "olt_omci", "delivery_pending": False},
        )

    # Re-running the full live OLT dependency audit here turns an interactive
    # Apply WAN click into a multi-minute request. The actual OLT command is
    # authoritative for missing-profile failures, so reuse only a recent
    # successful audit when one exists and otherwise proceed directly.
    dependency_failure = _validate_olt_write_dependencies(db, ctx.olt, cached_only=True)
    if dependency_failure is not None:
        dependency_failure.data = {
            **(dependency_failure.data or {}),
            "delivery_transport": "olt_omci",
        }
        return dependency_failure

    adapter = get_protocol_adapter(ctx.olt)
    ip_index = _pppoe_omci_ip_index(
        effective_values,
        instance_index=instance_index,
    )
    steps: list[dict[str, object]] = []

    pppoe_result = adapter.configure_pppoe(
        ctx.fsp,
        ctx.olt_ont_id,
        ip_index=ip_index,
        vlan_id=wan_vlan,
        username=username,
        password=password,
    )
    steps.append(
        {
            "step": "configure_pppoe_omci",
            "success": pppoe_result.success,
            "message": pppoe_result.message,
        }
    )
    if not pppoe_result.success:
        return ActionResult(
            success=False,
            message=f"WAN PPPoE OMCI apply failed: {pppoe_result.message}",
            data={
                "delivery_transport": "olt_omci",
                "delivery_pending": False,
                "steps": steps,
                "ip_index": ip_index,
                "wan_vlan": wan_vlan,
            },
        )

    inet_result = adapter.configure_internet_config(
        ctx.fsp,
        ctx.olt_ont_id,
        ip_index=ip_index,
    )
    steps.append(
        {
            "step": "internet_config_olt",
            "success": inet_result.success,
            "message": inet_result.message,
        }
    )
    if not inet_result.success:
        if _huawei_command_unsupported(inet_result.message):
            steps[-1]["success"] = True
            steps[-1]["skipped"] = True
        else:
            return ActionResult(
                success=False,
                message=f"WAN PPPoE OMCI apply failed: {inet_result.message}",
                data={
                    "delivery_transport": "olt_omci",
                    "delivery_pending": False,
                    "steps": steps,
                    "ip_index": ip_index,
                    "wan_vlan": wan_vlan,
                },
            )

    wan_profile_id = _int_or_none(effective_values.get("wan_config_profile_id"))
    if wan_profile_id is not None and wan_profile_id > 0:
        wan_result = adapter.configure_wan_config(
            ctx.fsp,
            ctx.olt_ont_id,
            ip_index=ip_index,
            profile_id=wan_profile_id,
        )
        steps.append(
            {
                "step": "configure_wan_olt",
                "success": wan_result.success,
                "message": wan_result.message,
            }
        )
        if not wan_result.success:
            return ActionResult(
                success=False,
                message=f"WAN PPPoE OMCI apply failed: {wan_result.message}",
                data={
                    "delivery_transport": "olt_omci",
                    "delivery_pending": False,
                    "steps": steps,
                    "ip_index": ip_index,
                    "wan_vlan": wan_vlan,
                    "wan_config_profile_id": wan_profile_id,
                },
            )

    return ActionResult(
        success=True,
        message=(
            "PPPoE WAN config sent via OLT/OMCI; waiting for PPP session "
            "and runtime verification."
        ),
        data={
            "delivery_transport": "olt_omci",
            "verification_pending": True,
            "steps": steps,
            "ip_index": ip_index,
            "wan_vlan": wan_vlan,
            "pppoe_username": username,
        },
        waiting=True,
    )


def set_wifi_ssid(
    db: Session, ont_id: str, ssid: str, *, request: Request | None = None
) -> ActionResult:
    """Set WiFi SSID by routing through ``reconcile_ont`` in sync mode.

    SSID is observable (the device returns it on reads), so unlike WiFi
    password this push DOES land on the device immediately — the planner
    emits ``AcsSetWifiSsid`` whenever ``desired.wifi_ssid`` differs from
    the observed value, in any mode.
    """
    from app.services.network.reconcile import reconcile_ont

    result_obj = reconcile_ont(
        db,
        ont_id,
        proposed_change={"wifi_ssid": ssid},
        mode="sync",
    )

    action_result = _reconcile_to_action_result(
        result_obj, success_message="WiFi SSID updated."
    )

    _log_action_audit(
        db,
        request=request,
        action="set_wifi_ssid",
        ont_id=ont_id,
        metadata={
            "success": action_result.success,
            "ssid": ssid,
            "sync_status": result_obj.sync_status,
            "failure_reason": (
                result_obj.failure.reason if result_obj.failure else None
            ),
        },
    )
    return action_result


def _reconcile_to_action_result(result_obj, *, success_message: str) -> ActionResult:
    """Translate a ``ReconcileResult`` to the legacy ``ActionResult`` shape.

    ``actionable=True`` flags a failure whose message carries operator-actionable
    instructions the UI should surface verbatim (today: ``ACS_CR_FAILED`` tells
    the operator to drain via OLT ``ont reset``). The UI renders that text
    as-is when ``actionable`` is set.
    """
    from app.services.network.reconcile import ReconcileFailureReason

    if result_obj.success:
        return ActionResult(
            success=True,
            message=success_message,
            data={
                "sync_status": result_obj.sync_status,
                "actions_applied": [a.field for a in result_obj.actions_applied],
                "action_evidence": [
                    {
                        "field": action.field,
                        "evidence": getattr(action, "evidence", None),
                    }
                    for action in result_obj.actions_applied
                    if getattr(action, "evidence", None) is not None
                ],
            },
        )
    failure = result_obj.failure
    return ActionResult(
        success=False,
        message=failure.message if failure else "Reconcile failed",
        data={
            "sync_status": result_obj.sync_status,
            "failure_reason": failure.reason if failure else None,
            "failure_evidence": getattr(failure, "evidence", None),
            "actionable": (
                failure is not None
                and failure.reason == ReconcileFailureReason.ACS_CR_FAILED
            ),
        },
    )


def _emit_wifi_password_event(
    db: Session, ont_id: str, *, method: str, request: Request | None
) -> None:
    """Audit-event emission for any successful WiFi-password change path."""
    from app.services.events import emit_event
    from app.services.events.types import EventType

    ont = db.get(OntUnit, ont_id)
    emit_event(
        db,
        EventType.ont_wifi_password_set,
        {
            "ont_id": ont_id,
            "ont_serial": ont.serial_number if ont else None,
            "password_set": True,
            "method": method,
            "result": "success",
        },
        actor=actor_name_from_request(request),
    )


def set_wifi_password(
    db: Session, ont_id: str, password: str, *, request: Request | None = None
) -> ActionResult:
    """Set WiFi password by routing through ``reconcile_ont`` in sync mode.

    Per Hole 3 of the design: on a present-and-observed ONT in ``sync``
    mode, the reconciler updates ``OntDesiredState.wifi_password_ref`` but
    doesn't push the PSK to the device — there's no observable to confirm
    drift. The push happens on the next BOOTSTRAP event (after a factory
    reset, where the device's PSK was wiped) or via ``force_push_wifi_password``
    (which uses ``mode=bootstrap`` to force an immediate push).
    """
    from app.services.network.reconcile import reconcile_ont

    result_obj = reconcile_ont(
        db,
        ont_id,
        proposed_change={"wifi_password_ref": password},
        mode="sync",
    )

    action_result = _reconcile_to_action_result(
        result_obj, success_message="WiFi password updated."
    )
    if action_result.success:
        _emit_wifi_password_event(db, ont_id, method="reconciler", request=request)

    _log_action_audit(
        db,
        request=request,
        action="set_wifi_password",
        ont_id=ont_id,
        metadata={
            "success": action_result.success,
            "sync_status": result_obj.sync_status,
            "failure_reason": (
                result_obj.failure.reason if result_obj.failure else None
            ),
        },
    )
    return action_result


def force_push_wifi_password(
    db: Session, ont_id: str, password: str, *, request: Request | None = None
) -> ActionResult:
    """Force-push the WiFi password to the device.

    Uses ``mode=bootstrap`` so the planner emits an ``AcsSetWifiPassword``
    action regardless of whether the ONT is currently present and observed.
    The legacy ``set_wifi_password`` semantics — "push every time, trust it
    landed" — are restored here for operators who explicitly want immediate
    push. Sync-mode remains the default for routine changes.

    Use cases:
      * Customer reports WiFi password doesn't work after a sync change
        (suggesting the device PSK drifted from desired_state — for instance
        after a factory reset that wasn't accompanied by a BOOTSTRAP event).
      * Field tech setting up a new ONT mid-bootstrap and wants the PSK
        applied immediately rather than waiting for the next Inform.
    """
    from app.services.network.reconcile import reconcile_ont

    result_obj = reconcile_ont(
        db,
        ont_id,
        proposed_change={"wifi_password_ref": password},
        mode="bootstrap",
    )

    action_result = _reconcile_to_action_result(
        result_obj, success_message="WiFi password push attempted."
    )
    if action_result.success:
        _emit_wifi_password_event(
            db, ont_id, method="reconciler_force_push", request=request
        )

    _log_action_audit(
        db,
        request=request,
        action="force_push_wifi_password",
        ont_id=ont_id,
        metadata={
            "success": action_result.success,
            "sync_status": result_obj.sync_status,
            "failure_reason": (
                result_obj.failure.reason if result_obj.failure else None
            ),
        },
    )
    return action_result


def force_resync_ont(
    db: Session, ont_id: str, *, request: Request | None = None
) -> ActionResult:
    """Force a reconcile in sweep mode — used to clear an ``out_of_sync`` row.

    Sync-mode reconciles refuse against ``out_of_sync`` rows (the design
    principle is that ``sync_status=out_of_sync`` means "the system noticed
    something went wrong, an operator should look"). This endpoint is how
    operators clear the state once they've checked: a sweep-mode reconcile
    that re-attempts whatever the prior pass couldn't finish.

    No ``proposed_change`` — the function only triggers reconciliation of the
    existing desired state against live observed state.
    """
    from app.services.network.reconcile import reconcile_ont

    result_obj = reconcile_ont(
        db,
        ont_id,
        proposed_change=None,
        mode="sweep",
    )

    action_result = _reconcile_to_action_result(
        result_obj, success_message="ONT reconciled."
    )

    _log_action_audit(
        db,
        request=request,
        action="force_resync_ont",
        ont_id=ont_id,
        metadata={
            "success": action_result.success,
            "sync_status": result_obj.sync_status,
            "failure_reason": (
                result_obj.failure.reason if result_obj.failure else None
            ),
        },
    )
    return action_result


def set_wifi_config(
    db: Session,
    ont_id: str,
    *,
    enabled: bool | None = None,
    ssid: str | None = None,
    password: str | None = None,
    channel: int | None = None,
    security_mode: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Set WiFi radio, SSID, security, and password fields."""
    result = OntFeatureService.set_wifi_config(
        db,
        ont_id,
        enabled=enabled,
        ssid=ssid,
        password=password,
        channel=channel,
        security_mode=security_mode,
    )
    if result.success:
        ont = db.get(OntUnit, ont_id)
        _persist_ont_plan_step(
            db,
            ont_id,
            "configure_wifi_tr069",
            {
                "enabled": enabled,
                "ssid": ssid,
                "password_set": bool(password),
                "channel": channel,
                "security_mode": security_mode,
            },
        )
        # Emit audit event for WiFi config change
        from app.services.events import emit_event
        from app.services.events.types import EventType

        emit_event(
            db,
            EventType.ont_wifi_config_updated,
            {
                "ont_id": ont_id,
                "ont_serial": ont.serial_number if ont else None,
                "enabled": enabled,
                "ssid_updated": ssid is not None,
                "password_set": password is not None,
                "channel": channel,
                "security_mode": security_mode,
                "method": "tr069",
                "result": "success",
            },
            actor=actor_name_from_request(request),
        )
        result = _intent_saved_result(result)
    else:
        logger.warning(
            "WiFi config apply failed for ONT %s: %s data=%s",
            ont_id,
            result.message,
            action_result_audit_metadata(result).get("data"),
        )
    _log_action_audit(
        db,
        request=request,
        action="set_wifi_config",
        ont_id=ont_id,
        metadata={
            **action_result_audit_metadata(result),
            "enabled": enabled,
            "ssid": ssid,
            "channel": channel,
            "security_mode": security_mode,
        },
    )
    return result


def toggle_lan_port(
    db: Session,
    ont_id: str,
    port: int,
    enabled: bool,
    *,
    request: Request | None = None,
) -> ActionResult:
    """Toggle a LAN port and return result."""
    result = genieacs_service.toggle_lan_port(db, ont_id, port, enabled)
    _log_action_audit(
        db,
        request=request,
        action="toggle_lan_port",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "port": port,
            "enabled": enabled,
        },
    )
    return result


def set_lan_config(
    db: Session,
    ont_id: str,
    *,
    lan_ip: str | None = None,
    lan_subnet: str | None = None,
    dhcp_enabled: bool | None = None,
    dhcp_start: str | None = None,
    dhcp_end: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Set LAN gateway and DHCP server config on ONT via GenieACS TR-069.

    LAN settings are explicit ONT-local desired config because they are
    customer-specific runtime intent, not OLT config-pack defaults.
    """
    result = genieacs_service.set_lan_config(
        db,
        ont_id,
        lan_ip=lan_ip,
        lan_subnet=lan_subnet,
        dhcp_enabled=dhcp_enabled,
        dhcp_start=dhcp_start,
        dhcp_end=dhcp_end,
    )
    if result.success:
        _persist_ont_plan_step(
            db,
            ont_id,
            "configure_lan_tr069",
            {
                "lan_ip": lan_ip,
                "lan_subnet": lan_subnet,
                "dhcp_enabled": dhcp_enabled,
                "dhcp_start": dhcp_start,
                "dhcp_end": dhcp_end,
            },
        )
        result = _intent_saved_result(result)
    else:
        logger.warning(
            "LAN config apply failed for ONT %s: %s data=%s",
            ont_id,
            result.message,
            action_result_audit_metadata(result).get("data"),
        )
    _log_action_audit(
        db,
        request=request,
        action="set_lan_config",
        ont_id=ont_id,
        metadata={
            **action_result_audit_metadata(result),
            "lan_ip": lan_ip,
            "lan_subnet": lan_subnet,
            "dhcp_enabled": dhcp_enabled,
            "dhcp_start": dhcp_start,
            "dhcp_end": dhcp_end,
        },
    )
    return result


def configure_management_ip(
    db: Session,
    ont_id: str,
    ip_mode: str = "dhcp",
    priority: int | None = None,
    ip_address: str | None = None,
    subnet: str | None = None,
    gateway: str | None = None,
) -> tuple[bool, str]:
    """Configure ONT management IP via OLT IPHOST command."""
    from app.services.network.iphost_priority import resolve_management_iphost_priority
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if ont is None:
        return False, "ONT not found"
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"
    dependency_failure = _validate_olt_write_dependencies(db, olt, cached_only=True)
    if dependency_failure is not None:
        return False, dependency_failure.message
    config_pack = resolve_olt_config_pack(db, olt.id)
    vlan_id = (
        config_pack.management_vlan.tag
        if config_pack and config_pack.management_vlan
        else None
    )
    if vlan_id is None:
        return False, "OLT config pack management VLAN is not configured."
    resolved_priority = priority
    if resolved_priority is None:
        effective = resolve_effective_ont_config(db, ont)
        values = effective.get("values", {}) if isinstance(effective, dict) else {}
        resolved_priority = resolve_management_iphost_priority(
            db,
            olt_id=olt.id,
            fsp=fsp,
            ont_id_on_olt=olt_ont_id,
            mgmt_vlan_tag=vlan_id,
            mgmt_gem_index=values.get("mgmt_gem_index"),
            line_profile_id=values.get("authorization_line_profile_id"),
        )
    if resolved_priority is None and str(ip_mode or "").strip().lower() in {
        "static",
        "static_ip",
    }:
        return (
            False,
            "Management IPHOST priority could not be resolved from imported OLT state.",
        )
    result = get_protocol_adapter(olt).configure_iphost(
        fsp,
        olt_ont_id,
        vlan=vlan_id,
        mode=ip_mode,
        priority=resolved_priority,
        ip_address=ip_address,
        subnet_mask=subnet,
        gateway=gateway,
    )
    return result.success, result.message


def bind_tr069_profile(db: Session, ont_id: str) -> tuple[bool, str]:
    """Bind and verify the configured TR-069 profile through the reconciler."""
    from app.services.network.ont_profile_reconcile import (
        reconcile_tr069_profile_binding,
    )
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if ont is None:
        return False, "ONT not found"
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"
    dependency_failure = _validate_olt_write_dependencies(db, olt, cached_only=True)
    if dependency_failure is not None:
        return False, dependency_failure.message
    effective = resolve_effective_ont_config(db, ont)
    values = effective.get("values", {}) if isinstance(effective, dict) else {}
    profile_id = _int_or_none(values.get("tr069_olt_profile_id"))
    if profile_id is None:
        return False, "No effective OLT TR-069 profile is configured."
    return reconcile_tr069_profile_binding(db, ont_id, profile_id)


# ---------------------------------------------------------------------------
# WAN Configuration Setters (TR-069)
# ---------------------------------------------------------------------------


def set_pppoe_credentials(
    db: Session,
    ont_id: str,
    *,
    username: str,
    password: str,
    instance_index: int = 1,
    wan_vlan: int | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Write the CPE's PPPoE dialer values, via ``reconcile_ont``.

    SCOPE — this changes what the customer's ONT *dials with*. It does not
    touch RADIUS. The authoritative access credential is ``AccessCredential`` /
    ``RadiusUser``, projected into the FreeRADIUS auth tables by
    ``access.radius_projection`` (``app.services.radius_population``). Typing a
    username/password here therefore cannot grant, restore, or repair
    authentication: if the value does not match the authoritative credential
    the session will still be rejected at the BNG, and
    ``app.services.cpe_dialer_credential_reconcile`` will converge the dialer
    back onto the authoritative credential.

    Use this only to repair a CPE whose stored dialer values drifted. To change
    what the subscriber authenticates with, change the access credential.

    Unlike WiFi password, PPPoE creds ARE observable (the device returns
    ``Username`` on TR-069 reads), so the planner pushes on every change
    when the observed username differs from desired.

    The legacy ``instance_index`` parameter is honored as
    ``wan_pppoe_instance_index``; ``wan_vlan`` likewise maps to ``wan_vlan``.
    Both flow into the reconciler's desired-state target.
    """
    from app.services.network.reconcile import reconcile_ont

    proposed: dict[str, object] = {
        "wan_pppoe_username": username,
        "wan_pppoe_password_ref": password,
    }
    if wan_vlan is not None:
        proposed["wan_vlan"] = wan_vlan
    if instance_index != 1:
        proposed["wan_pppoe_instance_index"] = instance_index

    result_obj = reconcile_ont(
        db,
        ont_id,
        proposed_change=proposed,
        mode="sync",
    )

    action_result = _reconcile_to_action_result(
        result_obj,
        success_message=(
            "PPPoE dialer values written to the ONT. RADIUS authentication is "
            "unchanged — the subscriber's access credential is managed "
            "separately."
        ),
    )

    if action_result.success:
        ont = db.get(OntUnit, ont_id)
        _persist_ont_plan_step(
            db,
            ont_id,
            "set_pppoe_credentials_reconciler",
            {
                "username": username,
                "password_set": True,
                "instance_index": instance_index,
                "wan_vlan": wan_vlan,
            },
        )
        from app.services.events import emit_event
        from app.services.events.types import EventType

        emit_event(
            db,
            EventType.ont_pppoe_credentials_set,
            {
                "ont_id": ont_id,
                "ont_serial": ont.serial_number if ont else None,
                "wan_mode": "pppoe",
                "pppoe_username": username,
                "method": "reconciler",
                "result": "success",
            },
            actor=actor_name_from_request(request),
        )

    _log_action_audit(
        db,
        request=request,
        action="set_pppoe_credentials",
        ont_id=ont_id,
        metadata={
            "success": action_result.success,
            "username": username,
            "instance_index": instance_index,
            "wan_vlan": wan_vlan,
            "sync_status": result_obj.sync_status,
            "failure_reason": (
                result_obj.failure.reason if result_obj.failure else None
            ),
        },
    )
    return action_result


def set_wan_dhcp(
    db: Session,
    ont_id: str,
    *,
    instance_index: int = 1,
    wan_vlan: int | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Configure WAN for DHCP mode via TR-069."""
    from app.services.network.ont_action_wan import set_wan_dhcp as _set_wan_dhcp

    result = _set_wan_dhcp(
        db,
        ont_id,
        instance_index=instance_index,
        wan_vlan=wan_vlan,
    )

    if result.success:
        _persist_ont_plan_step(
            db,
            ont_id,
            "set_wan_dhcp_tr069",
            {
                "instance_index": instance_index,
                "wan_vlan": wan_vlan,
            },
        )

    _log_action_audit(
        db,
        request=request,
        action="set_wan_dhcp",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "waiting": result.waiting,
            "instance_index": instance_index,
            "wan_vlan": wan_vlan,
        },
    )
    return result


def set_wan_static(
    db: Session,
    ont_id: str,
    *,
    ip_address: str,
    subnet_mask: str,
    gateway: str,
    dns_servers: str | None = None,
    instance_index: int = 1,
    request: Request | None = None,
) -> ActionResult:
    """Configure WAN for static IP mode via TR-069."""
    from app.services.network.ont_action_wan import set_wan_static as _set_wan_static

    result = _set_wan_static(
        db,
        ont_id,
        ip_address=ip_address,
        subnet_mask=subnet_mask,
        gateway=gateway,
        dns_servers=dns_servers,
        instance_index=instance_index,
    )

    if result.success:
        _persist_ont_plan_step(
            db,
            ont_id,
            "set_wan_static_tr069",
            {
                "ip_address": ip_address,
                "subnet_mask": subnet_mask,
                "gateway": gateway,
                "dns_servers": dns_servers,
                "instance_index": instance_index,
            },
        )

    _log_action_audit(
        db,
        request=request,
        action="set_wan_static",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "waiting": result.waiting,
            "ip_address": ip_address,
            "instance_index": instance_index,
        },
    )
    return result


def set_wan_config(
    db: Session,
    ont_id: str,
    *,
    wan_mode: str,
    pppoe_username: str | None = None,
    pppoe_password: str | None = None,
    ip_address: str | None = None,
    subnet_mask: str | None = None,
    gateway: str | None = None,
    dns_servers: list[str] | None = None,
    instance_index: int = 1,
    wan_vlan: int | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Unified WAN configuration via TR-069."""
    from app.services.network.ont_action_wan import set_wan_config as _set_wan_config

    ont = db.get(OntUnit, ont_id)
    config_pack_wan_vlan: int | None = None
    effective_instance_index: int | None = None
    if ont and ont.olt_device_id:
        config_pack = resolve_olt_config_pack(db, ont.olt_device_id)
        if config_pack and config_pack.internet_vlan:
            config_pack_wan_vlan = config_pack.internet_vlan.tag
        from app.services.network.effective_ont_config import (
            resolve_internet_wcd_index,
        )

        effective_instance_index = resolve_internet_wcd_index(db, ont)
        if instance_index == 1 and effective_instance_index != 1:
            instance_index = effective_instance_index

    wan_mode_normalized = wan_mode.strip().lower()
    resolved_wan_vlan = (
        config_pack_wan_vlan if config_pack_wan_vlan is not None else wan_vlan
    )
    if wan_mode_normalized in {"pppoe", "dhcp", "static"} and resolved_wan_vlan is None:
        result = ActionResult(
            success=False,
            message="OLT internet VLAN is required before applying WAN config.",
        )
        _log_action_audit(
            db,
            request=request,
            action="set_wan_config",
            ont_id=ont_id,
            metadata={
                **action_result_audit_metadata(result),
                "wan_mode": wan_mode,
                "missing_config_pack_vlan": True,
            },
        )
        return result

    use_omci_pppoe = (
        wan_mode_normalized == "pppoe"
        and get_pppoe_provisioning_method(db) != "tr069"
        and get_olt_write_mode_enabled(db)
    )
    if use_omci_pppoe:
        if not pppoe_username or not pppoe_password:
            result = ActionResult(
                success=False,
                message="PPPoE username and password are required for PPPoE mode.",
                data={
                    "delivery_transport": "olt_omci",
                    "delivery_pending": False,
                },
            )
        else:
            result = _set_pppoe_config_omci(
                db,
                ont_id,
                username=pppoe_username,
                password=pppoe_password,
                wan_vlan=int(resolved_wan_vlan),
                instance_index=instance_index,
            )
    else:
        result = _set_wan_config(
            db,
            ont_id,
            wan_mode=wan_mode,
            pppoe_username=pppoe_username,
            pppoe_password=pppoe_password,
            ip_address=ip_address,
            subnet_mask=subnet_mask,
            gateway=gateway,
            dns_servers=dns_servers,
            instance_index=instance_index,
            wan_vlan=resolved_wan_vlan,
        )

    if result.success:
        _persist_ont_plan_step(
            db,
            ont_id,
            (
                "set_wan_config_omci"
                if result.data and result.data.get("delivery_transport") == "olt_omci"
                else "set_wan_config_tr069"
            ),
            {
                "wan_mode": wan_mode,
                "pppoe_username": pppoe_username,
                "password_set": bool(pppoe_password),
                "ip_address": ip_address,
                "instance_index": instance_index,
                "effective_instance_index": effective_instance_index,
                "wan_vlan": resolved_wan_vlan,
                "delivery_transport": (
                    result.data.get("delivery_transport")
                    if isinstance(result.data, dict)
                    else "tr069"
                ),
            },
        )
    else:
        logger.warning(
            "WAN config apply failed for ONT %s: %s data=%s",
            ont_id,
            result.message,
            action_result_audit_metadata(result).get("data"),
        )

    _log_action_audit(
        db,
        request=request,
        action="set_wan_config",
        ont_id=ont_id,
        metadata={
            **action_result_audit_metadata(result),
            "wan_mode": wan_mode,
            "instance_index": instance_index,
            "effective_instance_index": effective_instance_index,
            "wan_vlan": resolved_wan_vlan,
        },
    )
    return result


def set_http_management(
    db: Session,
    ont_id: str,
    *,
    enabled: bool,
    port: int = 80,
    request: Request | None = None,
) -> ActionResult:
    """Enable or disable HTTP management interface via TR-069."""
    from app.services.network.ont_action_wan import (
        set_http_management as _set_http_management,
    )

    result = _set_http_management(
        db,
        ont_id,
        enabled=enabled,
        port=port,
    )

    if result.success:
        ont = db.get(OntUnit, ont_id)
        if ont is not None:
            set_access_flag(ont, "http_management", enabled)
            db.add(ont)
            db.commit()
        _persist_ont_plan_step(
            db,
            ont_id,
            "set_http_management_tr069",
            {
                "enabled": enabled,
                "port": port,
            },
        )

    _log_action_audit(
        db,
        request=request,
        action="set_http_management",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "enabled": enabled,
            "port": port,
        },
    )
    return result


def set_connection_request_credentials(
    db: Session,
    ont_id: str,
    *,
    username: str,
    password: str,
    periodic_inform_interval: int,
    request: Request | None = None,
) -> ActionResult:
    """Set ACS connection-request credentials via TR-069."""
    from app.services.network.ont_actions import OntActions

    result = OntActions.set_connection_request_credentials(
        db,
        ont_id,
        username,
        password,
        periodic_inform_interval=periodic_inform_interval,
    )
    _log_action_audit(
        db,
        request=request,
        action="set_connection_request_credentials",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "username": username,
            "periodic_inform_interval": periodic_inform_interval,
        },
    )
    return result


def set_web_credentials(
    db: Session,
    ont_id: str,
    *,
    username: str,
    password: str,
    request: Request | None = None,
) -> ActionResult:
    """Set ONT local web credentials via TR-069."""
    from app.services.network.ont_features import OntFeatureService

    result = OntFeatureService.update_web_credentials(
        db, ont_id, username=username, password=password
    )
    _log_action_audit(
        db,
        request=request,
        action="set_web_credentials",
        ont_id=ont_id,
        metadata={"success": result.success, "username": username},
    )
    return result


def bind_internet_wan(
    db: Session,
    ont_id: str,
    *,
    ssid1: bool = True,
    lan1: bool = True,
    lan2: bool = False,
    lan3: bool = False,
    lan4: bool = False,
    request: Request | None = None,
) -> ActionResult:
    """Bind the active Huawei PPP internet WAN to customer-facing interfaces."""
    from app.services.genieacs_client import GenieACSError
    from app.services.network.ont_action_common import (
        get_ont_client_or_error,
        set_and_verify,
    )

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="No ACS device resolved for ONT.")
    ont, client, device_id = resolved
    try:
        device = client.get_device(device_id)
    except GenieACSError as exc:
        return ActionResult(
            success=False,
            message=_plain_acs_bind_error(exc),
            data={"raw_error": str(exc)},
        )

    found = _find_internet_wan_ppp(device if isinstance(device, dict) else {})
    if found is None:
        return ActionResult(
            success=False,
            message=(
                "Bind Internet WAN cannot run because the ONT does not show an "
                "internet WAN object yet. Apply PPPoE WAN first, wait for ACS "
                "to refresh, then retry the bind."
            ),
            data={"required_step": "create_or_refresh_ppp_wan"},
        )
    wcd_index, ppp_index, ppp_data = found
    status = str(_tr069_value(ppp_data, "ConnectionStatus") or "").strip()
    if status.lower() != "connected":
        return ActionResult(
            success=False,
            message=f"PPP WAN is not connected yet (status: {status or 'unknown'}).",
            data={
                "wan_connection_device_index": wcd_index,
                "wan_ppp_index": ppp_index,
            },
        )

    base_path = (
        "InternetGatewayDevice.WANDevice.1."
        f"WANConnectionDevice.{wcd_index}.WANPPPConnection.{ppp_index}."
        "X_HW_LANBIND"
    )
    # Huawei HG8546M exposes X_HW_LANBIND as unsigned-int flags. Write the
    # complete object so stale bindings are cleared as part of the same task.
    requested_binds = {
        "Lan1Enable": lan1,
        "Lan2Enable": lan2,
        "Lan3Enable": lan3,
        "Lan4Enable": lan4,
        "SSID1Enable": ssid1,
        "SSID2Enable": False,
        "SSID3Enable": False,
        "SSID4Enable": False,
    }
    params = {
        f"{base_path}.{field}": int(enabled)
        for field, enabled in requested_binds.items()
    }
    if not params:
        return ActionResult(
            success=False,
            message="Select at least one LAN port or SSID to bind.",
        )

    try:
        task = set_and_verify(
            client,
            device_id,
            params,
            expected=params,
            timeout_sec=45,
        )
    except GenieACSError as exc:
        return ActionResult(
            success=False,
            message=_plain_acs_bind_error(exc),
            data={
                "raw_error": str(exc),
                "wan_connection_device_index": wcd_index,
                "wan_ppp_index": ppp_index,
                "bound_interfaces": [
                    field.removesuffix("Enable")
                    for field, enabled in requested_binds.items()
                    if enabled
                ],
            },
        )

    bound_interfaces = [
        field.removesuffix("Enable")
        for field, enabled in requested_binds.items()
        if enabled
    ]
    _persist_ont_plan_step(
        db,
        ont_id,
        "bind_internet_wan",
        {
            "wan_connection_device_index": wcd_index,
            "wan_ppp_index": ppp_index,
            "bound_interfaces": bound_interfaces,
        },
    )
    _log_action_audit(
        db,
        request=request,
        action="bind_internet_wan",
        ont_id=ont_id,
        metadata={
            "success": True,
            "wan_connection_device_index": wcd_index,
            "wan_ppp_index": ppp_index,
            "bound_interfaces": bound_interfaces,
            "task_id": task.get("_id") if isinstance(task, dict) else None,
        },
    )
    return ActionResult(
        success=True,
        message="Internet WAN bound to " + ", ".join(bound_interfaces) + ".",
        data={
            "wan_connection_device_index": wcd_index,
            "wan_ppp_index": ppp_index,
            "bound_interfaces": bound_interfaces,
        },
    )


def set_wan_remote_access(
    db: Session,
    ont_id: str,
    *,
    enabled: bool,
    request: Request | None = None,
) -> ActionResult:
    """Enable or disable WAN-side remote access via TR-069."""
    from app.services.network.ont_features import OntFeatureService

    result = OntFeatureService.toggle_wan_remote_access(db, ont_id, enabled=enabled)
    _log_action_audit(
        db,
        request=request,
        action="set_wan_remote_access",
        ont_id=ont_id,
        metadata={"success": result.success, "enabled": enabled},
    )
    return result


def set_mgmt_remote_access(
    db: Session,
    ont_id: str,
    *,
    enabled: bool,
    request: Request | None = None,
) -> ActionResult:
    """Enable or disable management-side remote access.

    The management path is OLT-owned. Enabling applies the active assignment's
    management IPHOST intent to the OLT; disabling clears the IPHOST config.
    WAN SSH/HTTP service toggles are handled by the WAN remote-access and HTTP
    management actions so this action does not mutate global CPE access flags.
    """
    ont = db.get(OntUnit, ont_id)
    if ont is None:
        return ActionResult(success=False, message="ONT not found.")

    effective = resolve_effective_ont_config(db, ont)
    values = effective.get("values", {}) if isinstance(effective, dict) else {}
    mode = str(values.get("mgmt_ip_mode") or "inactive")
    if not enabled:
        mode = "inactive"

    if enabled:
        ok, msg = configure_management_ip(
            db,
            ont_id,
            str(mode),
            ip_address=(
                str(values.get("mgmt_ip_address"))
                if values.get("mgmt_ip_address")
                else None
            ),
            subnet=str(values.get("mgmt_subnet"))
            if values.get("mgmt_subnet")
            else None,
            gateway=(
                str(values.get("mgmt_gateway")) if values.get("mgmt_gateway") else None
            ),
        )
        result = ActionResult(success=ok, message=msg)
    else:
        from app.services.network.olt_protocol_adapters import get_protocol_adapter
        from app.services.web_network_service_ports import _resolve_ont_olt_context

        _ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
        if not olt or not fsp or olt_ont_id is None:
            result = ActionResult(
                success=False,
                message="Cannot resolve OLT context for this ONT",
            )
        else:
            clear_result = get_protocol_adapter(olt).clear_iphost_config(
                fsp,
                olt_ont_id,
            )
            result = ActionResult(
                success=clear_result.success,
                message=clear_result.message,
                data=getattr(clear_result, "data", None),
            )

    if result.success:
        set_access_flag(ont, "mgmt_remote", enabled)
        db.add(ont)
        db.commit()

    _log_action_audit(
        db,
        request=request,
        action="set_mgmt_remote_access",
        ont_id=ont_id,
        metadata={"success": result.success, "enabled": enabled, "mode": str(mode)},
    )
    return result
