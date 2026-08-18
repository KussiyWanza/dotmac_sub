"""apply_plan(plan, ctx) — execute the planner's action list in order.

Takes a ``Plan`` and an ``ApplyContext`` (the I/O dependencies: OLT SSH
adapter, GenieACS NBI client, secret resolver) and runs each action via a
``match``-dispatched executor. Halts on first hard failure; records every
``AppliedAction`` regardless of outcome; returns an ``ApplyResult`` describing
what happened.

Action → adapter call mapping:

    OLT actions
      OltAuthorize              → adapter.authorize_ont
      OltModifyDescription      → adapter.set_ont_description
      OltModifyLineProfile      → adapter.update_ont_profiles (line only)
      OltModifyServiceProfile   → adapter.update_ont_profiles (srv only)
      OltClearIphost            → adapter.clear_iphost_config
      OltIpconfig               → adapter.configure_iphost (mode=static)
      OltTr069ServerConfig      → adapter.bind_tr069_profile
      OltCreateServicePort      → adapter.create_service_port
      OltDeleteServicePort      → adapter.delete_service_port
      OltOmciPppoe              → adapter.configure_pppoe
      OltOmciInternetConfig     → adapter.configure_internet_config
      OltOmciWanConfig          → adapter.configure_wan_config
      OltReset                  → adapter.reboot_ont

    ACS actions
      AcsAddObject              → client.add_object
      AcsDeleteObject           → client.delete_object
      AcsSetPppoe               → client.set_parameter_values (6 params)
      AcsSetWifiSsid            → client.set_parameter_values (1 param)
      AcsSetWifiPassword        → client.set_parameter_values (1 param)
      AcsSetWifiConfig          → client.set_parameter_values (changed fields)
      AcsSetRemoteAccess        → client.set_parameter_values (SSH + Telnet guard)
      AcsSetNatEnabled          → client.set_parameter_values (1 param)
      AcsSetDhcpServer          → client.set_parameter_values (4 params)
      AcsSetManagementServer    → client.set_parameter_values (3 params)

Failure modes mapped to ``ReconcileFailureReason``:
* Adapter ``OltOperationResult.success=False``           → OLT_WRITE_REJECTED
* NBI 202 with ``connectionRequestError`` populated      → ACS_CR_FAILED
* NBI 5xx / ``GenieACSError``                            → ACS_WRITE_FAULTED
* NBI fault key set on the task (CWMP 9xxx)              → ACS_WRITE_FAULTED
* Apply pass exceeds ``deadline``                        → TIMEOUT
* Action type without a wired executor                   → OLT_WRITE_REJECTED
                                                            (with NotImplemented
                                                            in error message)

Secrets are resolved at execute time via ``ctx.resolve_secret(ref)``. The
applier never logs the resolved plaintext — callers configure logging on
the adapter/client themselves, but neither this module nor the action
``__repr__``s should ever surface a password.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.services.control_plane_intent import (
    DesiredValueProvenance,
    has_executable_desired_provenance,
)
from app.services.network.huawei_cli_response import project_huawei_result_evidence

from .actions import (
    AcsAction,
    AcsAddObject,
    AcsArmDiagnostic,
    AcsDeleteObject,
    AcsSetDhcpServer,
    AcsSetIpv6,
    AcsSetManagementServer,
    AcsSetNatEnabled,
    AcsSetPppoe,
    AcsSetRemoteAccess,
    AcsSetWanIp,
    AcsSetWifiConfig,
    AcsSetWifiPassword,
    AcsSetWifiSsid,
    Action,
    OltAuthorize,
    OltClearIphost,
    OltCreateServicePort,
    OltDeleteServicePort,
    OltIpconfig,
    OltModifyDescription,
    OltModifyLineProfile,
    OltModifyServiceProfile,
    OltOmciInternetConfig,
    OltOmciPppoe,
    OltOmciWanConfig,
    OltReset,
    OltTr069ServerConfig,
)
from .planner import Plan
from .sentinels import DesiredScalar, is_deliverable
from .state import AppliedAction, ReconcileFailure, ReconcileFailureReason

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.network.ppp_delivery_authorization import (
        PppDeliveryRuling,
        PppDeliveryScope,
    )

logger = logging.getLogger(__name__)


SecretResolver = Callable[[str], str]
"""Resolves a secret reference (e.g. ``bao://path/to/secret``) to plaintext.

The default identity resolver treats the ref string AS the plaintext — useful
for transitional periods before OpenBao is wired everywhere. Tests substitute
their own resolver.
"""


def passthrough_secret(ref: str) -> str:
    """Identity secret resolver. Treats the ref as the plaintext value."""
    return ref


@dataclass
class ApplyContext:
    """I/O dependencies for ``apply_plan``.

    ``olt_adapter`` is an ``OltProtocolAdapter`` (or any object with the
    same method surface). ``acs_client`` is a ``GenieACSClient``. Both can
    be substituted in tests. ``resolve_secret`` defaults to passthrough.

    ``wan_ppp_instances`` is a per-apply, WCD-keyed override map populated
    by the ``AcsAddObject`` arm whenever it can verify the device-returned
    instance index. Downstream ``AcsSetPppoe`` / ``AcsSetNatEnabled`` arms
    read it before falling back to the planner's prediction. This keeps the
    same apply pass internally consistent when the device's monotonic
    instance counter has advanced past ``.1`` (the planner's default
    guess).
    """

    olt_adapter: Any
    acs_client: Any
    resolve_secret: SecretResolver = passthrough_secret
    wan_ppp_instances: dict[int, int] = field(default_factory=dict)
    #: Delivery-time PPP authorization, resolved by
    #: ``network.ppp_delivery_authorization`` from the owner-managed service
    #: intent. ``None`` means the caller never asked, which is treated as a
    #: refusal: a plan may not deliver PPP merely because nobody checked.
    #:
    #: Typed end-to-end on purpose. The first version accepted ``Any`` and
    #: probed it with ``getattr``, so a caller passing the wrong object, or an
    #: object whose ``authorized`` attribute happened to be truthy, would have
    #: been believed.
    ppp_authorization: PppDeliveryRuling | None = None
    #: What is actually being delivered, derived from live state and the plan
    #: rather than copied off the ruling. The ruling must match it exactly.
    #: ``None`` refuses: a caller that cannot say what it is delivering may not
    #: deliver PPP.
    ppp_scope: PppDeliveryScope | None = None


@dataclass(frozen=True)
class RefusedAction:
    """One action withheld by PPP delivery authorization."""

    action_name: str
    purpose: str
    refusal: str


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of ``apply_plan``.

    ``actions_applied`` lists every action up to and including the failing
    one (if any). ``halted_by`` is the failure that stopped the apply pass;
    None on success.

    ``refused_ppp`` lists PPP-bearing actions withheld by delivery
    authorization. It is deliberately NOT a failure: unrelated reconciliation
    must still converge. But it is residual drift, so a pass that refused
    anything must not be finalised as ``synced`` -- the model defines ``synced``
    as "zero residual drift", and silently reporting a device as converged
    while its dialer work was withheld is how a containment becomes invisible.
    """

    success: bool
    actions_applied: tuple[AppliedAction, ...]
    halted_by: ReconcileFailure | None
    refused_ppp: tuple[RefusedAction, ...] = ()


class ApplyError(Exception):
    """One action's write failed. Carries the action + failure reason so the
    applier can attach it to the ``ApplyResult``."""

    def __init__(
        self,
        action: Action,
        reason: str,
        message: str,
        *,
        evidence: dict[str, Any] | None = None,
    ):
        self.action = action
        self.reason = reason
        self.message = message
        self.evidence = evidence
        super().__init__(f"{type(action).__name__}: {message}")


def apply_plan(
    plan: Plan,
    ctx: ApplyContext,
    *,
    deadline: datetime | None = None,
) -> ApplyResult:
    """Execute the plan's actions in order. Halt on first hard failure.

    The returned ``ApplyResult.actions_applied`` includes every successful
    write up to (but not including) the failing one. The failing action is
    described by ``halted_by.message``.

    A None deadline means "no apply-side cap" — reconcile_ont enforces the
    outer budget. When the deadline is exceeded between actions (checked
    before each call), the apply pass returns TIMEOUT with the actions
    applied so far.
    """
    from app.services.network.ppp_delivery_authorization import (
        PppActionPurpose,
        PppDeliveryRefusal,
        classify_action,
    )

    applied: list[AppliedAction] = []
    refused_ppp: list[RefusedAction] = []

    # Delivery-time authorization, independent of whatever staged the desired
    # state. pending_apply, stored values and fingerprints are evidence that
    # something once wrote desired state; none of them authorises sending it to
    # a device. An absent ruling is a refusal, not a pass: the gate must not be
    # skippable by a caller that simply forgot to resolve it.
    ruling = ctx.ppp_authorization
    # Scope-checked at the point of USE. A ruling that authorises something is
    # still the wrong ruling if it was granted for a different service or a
    # superseded instance revision.
    ppp_allowed = ruling is not None and ruling.authorizes(ctx.ppp_scope)
    if ruling is None:
        refusal = PppDeliveryRefusal.no_active_service_intent.value
    elif ruling.refusal is not None:
        refusal = ruling.refusal.value
    else:
        # Authorized ruling that does not bind to this delivery: wrong ONT,
        # wrong service, superseded intent revision, or a plan whose PPP
        # content differs from the one the ruling was granted against.
        refusal = PppDeliveryRefusal.scope_mismatch.value

    for action in plan.actions:
        purpose = classify_action(action)
        if not ppp_allowed and purpose is not PppActionPurpose.not_ppp:
            # Skipped, not failed: unrelated reconciliation must still
            # converge. Recorded as residual drift so the pass cannot be
            # finalised as synced.
            refused_ppp.append(
                RefusedAction(
                    action_name=type(action).__name__,
                    purpose=purpose.value,
                    refusal=refusal,
                )
            )
            logger.warning(
                "reconcile_ppp_delivery_refused",
                extra={
                    "action": type(action).__name__,
                    "ppp_action_purpose": purpose.value,
                    "ppp_delivery_refusal": refusal,
                    "ruling_present": ruling is not None,
                },
            )
            continue
        if deadline is not None and datetime.now(UTC) >= deadline:
            return ApplyResult(
                success=False,
                actions_applied=tuple(applied),
                halted_by=ReconcileFailure(
                    reason=ReconcileFailureReason.TIMEOUT,
                    message=(f"apply deadline exceeded before {type(action).__name__}"),
                ),
                refused_ppp=tuple(refused_ppp),
            )
        try:
            applied.append(_execute(action, ctx))
        except ApplyError as exc:
            logger.warning(
                "reconcile_apply_halted",
                extra={
                    "action": type(action).__name__,
                    "reason": exc.reason,
                    # `message` is reserved by logging.LogRecord — use `detail`.
                    "detail": exc.message,
                    "evidence": exc.evidence,
                },
            )
            return ApplyResult(
                success=False,
                actions_applied=tuple(applied),
                halted_by=ReconcileFailure(
                    reason=exc.reason,
                    message=exc.message,
                    evidence=exc.evidence,
                ),
                refused_ppp=tuple(refused_ppp),
            )

    return ApplyResult(
        success=True,
        actions_applied=tuple(applied),
        halted_by=None,
        refused_ppp=tuple(refused_ppp),
    )


# ── Per-action dispatch ─────────────────────────────────────────────────────


def _execute(action: Action, ctx: ApplyContext) -> AppliedAction:
    """Dispatch one action to the appropriate adapter/client call.

    Pattern-matched by type. Each arm:
      1. resolves any secret refs to plaintext
      2. calls the underlying writer
      3. translates the writer's success/failure into AppliedAction or
         ApplyError
    """
    started = time.monotonic()

    match action:
        # ── OLT actions ───────────────────────────────────────────────────
        case OltAuthorize():
            _refuse_unset(
                action,
                ("line_profile_id", action.line_profile_id),
                ("service_profile_id", action.service_profile_id),
            )
            result = ctx.olt_adapter.authorize_ont(
                action.fsp,
                action.serial_number,
                line_profile_id=action.line_profile_id,
                service_profile_id=action.service_profile_id,
                description=action.description,
            )
            evidence = _olt_check(action, result)
            return _ok(
                action,
                "olt_authorize",
                None,
                action.serial_number,
                started,
                evidence=evidence,
            )

        case OltModifyDescription():
            result = ctx.olt_adapter.set_ont_description(
                action.fsp,
                action.ont_id,
                action.description,
            )
            evidence = _olt_check(action, result)
            return _ok(
                action,
                "olt_description",
                None,
                action.description,
                started,
                evidence=evidence,
            )

        case OltModifyLineProfile():
            _refuse_unset(action, ("line_profile_id", action.line_profile_id))
            result = ctx.olt_adapter.update_ont_profiles(
                action.fsp,
                action.ont_id,
                line_profile_id=action.line_profile_id,
            )
            evidence = _olt_check(action, result)
            return _ok(
                action,
                "olt_line_profile_id",
                None,
                action.line_profile_id,
                started,
                evidence=evidence,
            )

        case OltModifyServiceProfile():
            _refuse_unset(action, ("service_profile_id", action.service_profile_id))
            result = ctx.olt_adapter.update_ont_profiles(
                action.fsp,
                action.ont_id,
                service_profile_id=action.service_profile_id,
            )
            evidence = _olt_check(action, result)
            return _ok(
                action,
                "olt_service_profile_id",
                None,
                action.service_profile_id,
                started,
                evidence=evidence,
            )

        case OltClearIphost():
            result = ctx.olt_adapter.clear_iphost_config(
                action.fsp,
                action.ont_id,
                ip_index=action.ip_index,
            )
            evidence = _olt_check(action, result)
            return _ok(
                action,
                f"iphost[{action.ip_index}]",
                None,
                None,
                started,
                evidence=evidence,
            )

        case OltIpconfig():
            result = ctx.olt_adapter.configure_iphost(
                action.fsp,
                action.ont_id,
                ip_index=action.ip_index,
                mode="static",
                vlan=action.vlan,
                priority=action.priority,
                ip_address=action.ip_address,
                subnet_mask=action.subnet_mask,
                gateway=action.gateway,
            )
            evidence = _olt_check(action, result)
            return _ok(
                action,
                "olt_mgmt_ip",
                None,
                action.ip_address,
                started,
                evidence=evidence,
            )

        case OltTr069ServerConfig():
            _refuse_unset(action, ("tr069_profile_id", action.profile_id))
            result = ctx.olt_adapter.bind_tr069_profile(
                action.fsp,
                action.ont_id,
                profile_id=action.profile_id,
            )
            evidence = _olt_check(action, result)
            return _ok(
                action,
                "olt_tr069_profile_id",
                None,
                action.profile_id,
                started,
                evidence=evidence,
            )

        case OltCreateServicePort():
            _refuse_unset(
                action,
                *(("wan_vlan", action.vlan),) if action.slot == "wan" else (),
            )
            result = ctx.olt_adapter.create_service_port(
                action.fsp,
                action.ont_id,
                gem_index=action.gem_index,
                vlan_id=action.vlan,
                user_vlan=action.vlan,
                port_index=action.service_port_index,
            )
            evidence = _olt_check(action, result)
            return _ok(
                action,
                f"service_port[{action.slot}]",
                None,
                action.service_port_index,
                started,
                evidence=evidence,
            )

        case OltDeleteServicePort():
            result = ctx.olt_adapter.delete_service_port(action.service_port_index)
            evidence = _olt_check(action, result)
            return _ok(
                action,
                f"service_port[{action.service_port_index}]",
                action.service_port_index,
                None,
                started,
                evidence=evidence,
            )

        case OltOmciPppoe():
            _refuse_unset(action, ("wan_vlan", action.vlan))
            password = _resolve_or_fail(ctx, action, action.password_ref)
            result = ctx.olt_adapter.configure_pppoe(
                action.fsp,
                action.ont_id,
                ip_index=action.ip_index,
                vlan_id=action.vlan,
                username=action.username,
                password=password,
            )
            evidence = _olt_check(action, result)
            return _ok(
                action,
                "omci_pppoe",
                None,
                action.username,
                started,
                evidence=evidence,
            )

        case OltOmciInternetConfig():
            result = ctx.olt_adapter.configure_internet_config(
                action.fsp,
                action.ont_id,
                ip_index=action.ip_index,
            )
            evidence = _olt_check(action, result)
            return _ok(
                action,
                "omci_internet_config",
                None,
                action.ip_index,
                started,
                evidence=evidence,
            )

        case OltOmciWanConfig():
            result = ctx.olt_adapter.configure_wan_config(
                action.fsp,
                action.ont_id,
                ip_index=action.ip_index,
                profile_id=action.profile_id,
            )
            evidence = _olt_check(action, result)
            return _ok(
                action,
                "omci_wan_config",
                None,
                action.profile_id,
                started,
                evidence=evidence,
            )

        case OltReset():
            result = ctx.olt_adapter.reboot_ont(action.fsp, action.ont_id)
            evidence = _olt_check(action, result)
            return _ok(
                action,
                "ont_reset",
                None,
                None,
                started,
                evidence=evidence,
            )

        # ── ACS actions ───────────────────────────────────────────────────
        case AcsAddObject():
            wcd_index = _require_add_object_layout(action)
            try:
                ctx.acs_client.add_object(action.device_id, action.object_path)
            except Exception as exc:  # noqa: BLE001 — translate to ApplyError
                raise ApplyError(
                    action,
                    ReconcileFailureReason.ACS_WRITE_FAULTED,
                    f"addObject failed: {exc}",
                ) from exc
            # Post-addObject: when targeting WANPPPConnection, discover the
            # device-created instance index and stash it on the context so
            # downstream AcsSetPppoe / AcsSetNatEnabled in this same apply
            # pass target the right child. The device's instance counter is
            # monotonic — if .1 has been created+deleted before, addObject
            # may have just created .2/.3/etc. Writes to the planner's
            # hardcoded .1 would otherwise silently no-op (no CWMP fault on
            # HG8546M V5R019C10S100) and we'd cache new ghosts.
            if wcd_index is not None:
                discovered = _discover_wan_ppp_instance_index(
                    ctx.acs_client,
                    action.device_id,
                    action.object_path,
                )
                if discovered is not None:
                    ctx.wan_ppp_instances[wcd_index] = discovered
            return _ok(
                action,
                f"add_object[{action.object_path.split('.')[-1]}]",
                None,
                action.object_path,
                started,
            )

        case AcsDeleteObject():
            try:
                ctx.acs_client.delete_object(action.device_id, action.object_path)
            except Exception as exc:  # noqa: BLE001 — translate to ApplyError
                raise ApplyError(
                    action,
                    ReconcileFailureReason.ACS_WRITE_FAULTED,
                    f"deleteObject failed: {exc}",
                ) from exc
            return _ok(
                action,
                f"delete_object[{action.object_path.rstrip('.').split('.')[-1]}]",
                action.object_path,
                None,
                started,
            )

        case AcsSetPppoe():
            _refuse_unset(
                action,
                ("wan_pppoe_wcd_index", action.wcd_index),
                ("wan_vlan", action.vlan),
            )
            password = _resolve_or_fail(ctx, action, action.password_ref)
            inst = ctx.wan_ppp_instances.get(action.wcd_index, action.instance_index)
            params = {
                f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{action.wcd_index}.WANPPPConnection.{inst}.Username": action.username,
                f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{action.wcd_index}.WANPPPConnection.{inst}.Password": password,
                f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{action.wcd_index}.WANPPPConnection.{inst}.Enable": True,
                f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{action.wcd_index}.WANPPPConnection.{inst}.ConnectionType": "IP_Routed",
                f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{action.wcd_index}.WANPPPConnection.{inst}.X_HW_VLAN": action.vlan,
                f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{action.wcd_index}.WANPPPConnection.{inst}.X_HW_SERVICELIST": "INTERNET",
            }
            _acs_set(action, ctx, params)
            return _ok(action, "acs_pppoe", None, action.username, started)

        case AcsSetWifiSsid():
            _refuse_unset(action, ("wifi_ssid", action.ssid))
            params = {
                "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID": (
                    action.ssid
                )
            }
            _acs_set(action, ctx, params)
            return _ok(action, "acs_ssid", None, action.ssid, started)

        case AcsSetWifiPassword():
            _refuse_unset(action, ("wifi_password_ref", action.password_ref))
            password = _resolve_or_fail(ctx, action, action.password_ref)
            params = {
                "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.PreSharedKey.1.KeyPassphrase": password
            }
            _acs_set(action, ctx, params)
            # Don't include the password in the AppliedAction — log the field name only.
            return _ok(action, "acs_wifi_password", None, "[redacted]", started)

        case AcsSetWifiConfig():
            # Whole-action refusal, before any ACS contact. A field carrying a
            # sentinel is dropped from neither the write nor the outcome: the
            # batch fails so no part of it is reported converged.
            _refuse_unset(
                action,
                *(
                    (name, value)
                    for name, value in (
                        ("wifi_ssid", action.ssid),
                        ("wifi_password_ref", action.password_ref),
                    )
                    if value is not None
                ),
            )
            wifi_params: dict[str, object] = {}
            changed: list[str] = []
            if action.enabled is not None:
                wifi_params[action.paths.enabled] = action.enabled
                changed.append("enabled")
            if action.ssid is not None:
                wifi_params[action.paths.ssid] = action.ssid
                changed.append("ssid")
            if action.channel is not None:
                wifi_params[action.paths.channel] = action.channel
                changed.append("channel")
            if action.security_mode is not None:
                wifi_params[action.paths.security_mode] = action.security_mode
                changed.append("security_mode")
            if action.password_ref is not None:
                wifi_params[action.paths.psk_path] = _resolve_or_fail(
                    ctx, action, action.password_ref
                )
                changed.append("password")
            if not wifi_params:
                raise ApplyError(
                    action,
                    ReconcileFailureReason.INVALID_CHANGE,
                    "WiFi action contained no changed fields",
                )
            _acs_set(action, ctx, wifi_params)
            return _ok(
                action,
                "acs_wifi_config",
                None,
                ",".join(changed),
                started,
            )

        case AcsSetRemoteAccess():
            remote_params: dict[str, object] = {}
            remote_changed: list[str] = []
            if action.ssh_enabled is not None:
                remote_params[action.paths.ssh_enabled] = action.ssh_enabled
                remote_changed.append("ssh_enabled")
            if action.ssh_port is not None:
                remote_params[action.paths.ssh_port] = action.ssh_port
                remote_changed.append("ssh_port")
            if action.telnet_enabled is not None:
                remote_params[action.paths.telnet_enabled] = action.telnet_enabled
                remote_changed.append("telnet_enabled")
            if not remote_params:
                raise ApplyError(
                    action,
                    ReconcileFailureReason.INVALID_CHANGE,
                    "remote-access action contained no changed fields",
                )
            _acs_set(action, ctx, remote_params)
            return _ok(
                action,
                "acs_remote_access",
                None,
                ",".join(remote_changed),
                started,
            )

        case AcsArmDiagnostic():
            # Imperative, not convergent: this asks the device to measure and
            # report on a later inform. Routed through the applier so the ACS
            # write-ownership contract holds and diagnostics are audited with
            # every other device write.
            _acs_set(action, ctx, dict(action.params))
            return _ok(action, "acs_arm_diagnostic", None, action.label, started)

        case AcsSetNatEnabled():
            _refuse_unset(action, ("wan_pppoe_wcd_index", action.wcd_index))
            inst = ctx.wan_ppp_instances.get(action.wcd_index, action.instance_index)
            params = {
                f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{action.wcd_index}.WANPPPConnection.{inst}.NATEnabled": action.enabled
            }
            _acs_set(action, ctx, params)
            return _ok(action, "acs_nat_enabled", None, action.enabled, started)

        case AcsSetIpv6():
            _refuse_unknown_provenance(
                action,
                field="ipv6_enabled",
                provenance=action.provenance,
            )
            params = {
                f"Device.IP.Interface.{action.interface_index}.IPv6Enable": action.enabled,
                f"Device.DHCPv6.Client.{action.interface_index}.Enable": action.enabled,
                f"Device.DHCPv6.Client.{action.interface_index}.RequestPrefixes": (
                    action.enabled and action.request_prefixes
                ),
                f"Device.RouterAdvertisement.InterfaceSettings.{action.interface_index}.Enable": action.enabled,
            }
            _acs_set(action, ctx, params)
            return _ok(action, "acs_ipv6_enabled", None, action.enabled, started)

        case AcsSetWanIp():
            _refuse_unset(
                action,
                ("wan_pppoe_wcd_index", action.wcd_index),
                ("wan_vlan", action.vlan),
            )
            if action.data_model_root == "Device":
                paths = action.tr181_paths
                if paths is None:
                    raise ApplyError(
                        action,
                        ReconcileFailureReason.ACS_WRITE_FAULTED,
                        "TR-181 WAN requires a resolved vendor/model parameter map",
                    )
                params = {
                    paths.ip_enable: True,
                    paths.dhcp_enable: action.mode == "dhcp",
                    paths.vlan_enable: True,
                    paths.vlan_id: action.vlan,
                    paths.nat_enable: action.nat_enabled,
                }
                if action.mode == "static":
                    params.update(
                        {
                            paths.ip_address: action.ip_address,
                            paths.subnet_mask: action.subnet_mask,
                            paths.gateway: action.gateway,
                        }
                    )
                    dns_primary, dns_secondary = _split_dns_servers(action.dns_servers)
                    if dns_primary:
                        params[paths.dns_primary] = dns_primary
                    if dns_secondary:
                        params[paths.dns_secondary] = dns_secondary
            else:
                base = (
                    "InternetGatewayDevice.WANDevice.1."
                    f"WANConnectionDevice.{action.wcd_index}."
                    f"WANIPConnection.{action.instance_index}"
                )
                params = {
                    f"{base}.Enable": True,
                    f"{base}.NATEnabled": action.nat_enabled,
                    f"{base}.ConnectionType": "IP_Routed",
                    f"{base}.AddressingType": (
                        "DHCP" if action.mode == "dhcp" else "Static"
                    ),
                    f"{base}.X_HW_VLAN": action.vlan,
                    f"{base}.X_HW_SERVICELIST": "INTERNET",
                }
                if action.mode == "static":
                    params.update(
                        {
                            f"{base}.ExternalIPAddress": action.ip_address,
                            f"{base}.SubnetMask": action.subnet_mask,
                            f"{base}.DefaultGateway": action.gateway,
                        }
                    )
                    if action.dns_servers:
                        params[f"{base}.DNSServers"] = action.dns_servers
            _acs_set(action, ctx, params)
            return _ok(action, "acs_wan_ip_mode", None, action.mode, started)

        case AcsSetDhcpServer():
            params = {
                "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.DHCPServerEnable": action.enabled,
                "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.MinAddress": action.pool_min,
                "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.MaxAddress": action.pool_max,
                "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.SubnetMask": action.subnet_mask,
            }
            if action.gateway_ip is not None:
                params[
                    "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement."
                    "IPInterface.1.IPInterfaceIPAddress"
                ] = action.gateway_ip
            _acs_set(action, ctx, params)
            return _ok(action, "acs_dhcp_server", None, action.enabled, started)

        case AcsSetManagementServer():
            _refuse_unset(
                action,
                ("cr_username", action.cr_username),
                ("cr_password_ref", action.cr_password_ref),
            )
            cr_password = _resolve_or_fail(ctx, action, action.cr_password_ref)
            root = f"{action.data_model_root}.ManagementServer"
            connection_params = {
                f"{root}.ConnectionRequestUsername": action.cr_username,
                f"{root}.ConnectionRequestPassword": cr_password,
                f"{root}.PeriodicInformEnable": True,
                f"{root}.PeriodicInformInterval": action.inform_interval_sec,
            }
            _acs_set(action, ctx, connection_params)
            if action.acs_url:
                acs_password = _resolve_or_fail(
                    ctx, action, action.acs_password_ref or ""
                )
                endpoint_params = {
                    f"{root}.URL": action.acs_url,
                    f"{root}.Username": action.acs_username or "",
                    f"{root}.Password": acs_password,
                }
                # Move the endpoint last: changing URL first can sever the
                # current ACS session before CR credentials are established.
                _acs_set(action, ctx, endpoint_params)
            return _ok(
                action,
                "acs_management_server",
                None,
                action.inform_interval_sec,
                started,
            )

        case _:
            raise ApplyError(
                action,
                ReconcileFailureReason.OLT_WRITE_REJECTED,
                f"no executor wired for action type {type(action).__name__}",
            )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _split_dns_servers(value: str | None) -> tuple[str | None, str | None]:
    servers = [item for item in re.split(r"[\s,]+", value or "") if item]
    primary = servers[0] if servers else None
    secondary = servers[1] if len(servers) > 1 else None
    return primary, secondary


def _refuse_unset(action: Action, *fields: tuple[str, DesiredScalar]) -> None:
    """Refuse the whole action if any field carries an unset sentinel.

    Defence in depth behind the planner guards: the synchronous UI delivery
    paths build actions directly, so the applier is the last boundary before a
    placeholder reaches a customer's device.

    The refusal is deliberately all-or-nothing and happens before any device
    contact. Applying an action's remaining fields and reporting success would
    record a converged write the device only partially received — the same
    false convergence the sentinels exist to prevent. Per-field outcomes are
    the intent-migration's job, not something to approximate here.
    """
    unset = [name for name, value in fields if not is_deliverable(name, value)]
    if not unset:
        return
    raise ApplyError(
        action,
        ReconcileFailureReason.INVALID_CHANGE,
        f"refusing to write unset {', '.join(unset)}",
    )


def _refuse_unknown_provenance(
    action: Action,
    *,
    field: str,
    provenance: DesiredValueProvenance,
) -> None:
    """Refuse a represented value whose source is still unknown."""
    if has_executable_desired_provenance(provenance):
        return
    raise ApplyError(
        action,
        ReconcileFailureReason.INVALID_CHANGE,
        f"refusing to write provenance-unknown {field}",
    )


def _require_add_object_layout(action: AcsAddObject) -> int | None:
    """Validate WAN PPP layout evidence before ``addObject`` device contact."""
    rendered_wcd = _wan_ppp_wcd_from_object_path(action.object_path)
    if rendered_wcd is None:
        return None
    _refuse_unset(action, ("wan_pppoe_wcd_index", action.wcd_index))
    if rendered_wcd != action.wcd_index:
        raise ApplyError(
            action,
            ReconcileFailureReason.INVALID_CHANGE,
            "WAN PPP addObject path does not match its typed WCD index",
        )
    return rendered_wcd


def _resolve_or_fail(ctx: ApplyContext, action: Action, ref: str) -> str:
    """Resolve a secret ref to plaintext via the context resolver.

    Any exception from the resolver (OpenBao 5xx, network timeout, missing
    field on the KV path, etc.) is translated to an ApplyError with
    ACS_WRITE_FAULTED so the operator sees the failing action and a clear
    message instead of an unhandled 500.

    Empty/None refs return an empty string — the action sites that handle
    optional secrets (rare) can still see the resolver completed cleanly.
    """
    if not ref:
        return ""
    try:
        value = ctx.resolve_secret(ref)
    except Exception as exc:  # noqa: BLE001 — translate to typed apply failure
        raise ApplyError(
            action,
            ReconcileFailureReason.ACS_WRITE_FAULTED,
            f"secret resolution failed: {exc}",
        ) from exc
    if value is None:
        raise ApplyError(
            action,
            ReconcileFailureReason.ACS_WRITE_FAULTED,
            "secret resolver returned None",
        )
    return value


def _olt_check(action: Action, result: Any) -> dict[str, Any] | None:
    """Raise ApplyError if the adapter reported failure.

    Per ``OltProtocolAdapter``'s contract, all writes return
    ``OltOperationResult`` with a ``success`` boolean and a ``message``.
    """
    evidence = project_huawei_result_evidence(result)
    if not getattr(result, "success", False):
        raise ApplyError(
            action,
            ReconcileFailureReason.OLT_WRITE_REJECTED,
            str(getattr(result, "message", "OLT rejected the write")),
            evidence=evidence,
        )
    return evidence


def _acs_set(action: AcsAction, ctx: ApplyContext, params: dict) -> None:
    """Push a setParameterValues batch via the NBI client.

    Today's Fix #5 made ``set_parameter_values`` pass ``connection_request=True``
    by default; the response carries ``connectionRequestError`` populated when
    the task was queued but not delivered (typically empty CR creds on a
    fresh device). The applier treats that as ACS_CR_FAILED so reconcile_ont
    can map it to a clean 4xx/5xx for the operator. Fault payloads on the
    task (CWMP 9002 etc.) are translated to ACS_WRITE_FAULTED.
    """
    try:
        task = ctx.acs_client.set_parameter_values(
            action.device_id,  # all ACS actions carry device_id
            params,
        )
    except Exception as exc:  # noqa: BLE001
        raise ApplyError(
            action,
            ReconcileFailureReason.ACS_WRITE_FAULTED,
            f"setParameterValues failed: {exc}",
        ) from exc

    cr_error = ""
    fault = None
    if isinstance(task, dict):
        cr_error = str(task.get("connectionRequestError") or "")
        fault = task.get("fault")

    if cr_error:
        raise ApplyError(
            action,
            ReconcileFailureReason.ACS_CR_FAILED,
            f"setParameterValues queued but Connection Request failed: "
            f"{cr_error}. Force OLT `ont reset` to drain.",
        )

    if isinstance(fault, dict):
        code = fault.get("code")
        message = fault.get("message") or fault.get("detail") or "CWMP fault"
        raise ApplyError(
            action,
            ReconcileFailureReason.ACS_WRITE_FAULTED,
            f"CWMP fault {code}: {message}",
        )


def _ok(
    action: Action,
    field: str,
    old_value: Any,
    new_value: Any,
    started_monotonic: float,
    *,
    evidence: dict[str, Any] | None = None,
) -> AppliedAction:
    return AppliedAction(
        field=field,
        surface=action.surface,
        old_value=old_value,
        new_value=new_value,
        duration_ms=int((time.monotonic() - started_monotonic) * 1000),
        evidence=evidence,
    )


# ``WANDevice.1.WANConnectionDevice.<N>.WANPPPConnection`` — the parent path
# we addObject against. Capturing N lets us scope the post-addObject scan
# to the right WCD slot.
_WAN_PPP_OBJECT_PATH_RE = re.compile(
    r"WANDevice\.1\.WANConnectionDevice\.(\d+)\.WANPPPConnection$"
)


def _wan_ppp_wcd_from_object_path(object_path: str) -> int | None:
    """Return the WCD index if the addObject target is WANPPPConnection,
    else None. Other addObject targets (e.g. WANIPConnection, PortMapping)
    don't need post-creation index discovery."""
    match = _WAN_PPP_OBJECT_PATH_RE.search(object_path)
    return int(match.group(1)) if match else None


def _discover_wan_ppp_instance_index(
    client: Any,
    device_id: str,
    object_path: str,
) -> int | None:
    """Resolve the WANPPPConnection child instance just created by addObject.

    Strategy: refreshObject on the parent container so GenieACS pulls the
    device's updated tree, then re-fetch the device document and return the
    highest digit-keyed child of ``object_path``. The device's instance
    counter is monotonic, so the new child is always the largest key in the
    post-refresh snapshot.

    Best-effort: any I/O failure or missing client method returns None and
    the caller falls back to the planner's predicted instance_index. We
    never raise — ``AcsAddObject`` has already succeeded by the time this
    runs, and breaking the apply pass over a post-condition probe would
    sacrifice a working write to chase a perfect one.
    """
    if not hasattr(client, "refresh_object") or not hasattr(client, "list_devices"):
        return None
    try:
        client.refresh_object(
            device_id,
            object_path,
            allow_when_pending=True,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort probe
        logger.info(
            "applier_wan_ppp_refresh_failed",
            extra={
                "device_id": device_id,
                "object_path": object_path,
                "error": str(exc),
            },
        )
        return None
    try:
        devices = client.list_devices(
            query={"_id": device_id},
            projection=object_path,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort probe
        logger.info(
            "applier_wan_ppp_lookup_failed",
            extra={
                "device_id": device_id,
                "object_path": object_path,
                "error": str(exc),
            },
        )
        return None
    if not devices:
        return None
    node = _node_at_path(devices[0], object_path)
    if not isinstance(node, dict):
        return None
    digit_keys = [int(k) for k in node.keys() if k.isdigit()]
    return max(digit_keys) if digit_keys else None


def _node_at_path(device_doc: dict[str, Any], path: str) -> Any:
    """Walk a dotted ``object_path`` into a GenieACS device document. Returns
    the leaf node (typically a dict of child instances) or None if any
    segment is missing.
    """
    node: Any = device_doc
    for segment in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(segment)
    return node


__all__ = (
    "ApplyContext",
    "ApplyError",
    "ApplyResult",
    "SecretResolver",
    "apply_plan",
    "passthrough_secret",
)
