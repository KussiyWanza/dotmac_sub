"""compute_plan(desired, observed, mode) -> Plan.

Pure function. Given the desired state and the last-read observed state,
emits an ordered list of actions that, when applied successfully, leaves the
device matching the desired state.

Ordering rules
==============

* **OLT actions before ACS actions.** The OLT establishes the device on the
  PON and routes its mgmt VLAN; without that the ACS can't reach it.
* **Authorize first.** A device that isn't in the OLT's table has nothing
  else applied to it.
* **Service-port repair before IPHOST.** IPHOST writes depend on the
  mgmt-VLAN service-port being present.
* **TR-069 binding after IPHOST.** The TR-069 URL profile is only useful
  once the mgmt IP can route to it.
* **OMCI WAN sequence (PPPoE → internet-config → wan-config) as a unit.**
  Only emitted when ``wan_config_profile_id`` is set; otherwise TR-069
  owns WAN PPP.
* **Trailing OltReset** if any earlier action sets ``requires_reset``.
* **ACS addObject before setParameterValues.** Setting a parameter on a
  non-existent object silently queues forever on HG8546M.
* **NAT/DHCP defensives unconditional in routed mode.** Today's Fix #4
  follow-up — explicit pushes guard against firmware variation.
* **ManagementServer (CR creds + inform interval) last.** Last in the
  sequence so a failure earlier doesn't leave the device in a state where
  the next NBI write can't be delivered.

ACS device identity is a hard precondition
==========================================

Every ACS action carries a GenieACS ``_id`` (``OUI-ProductClass-Serial``).
The planner **never constructs one**. It uses the persisted
``Tr069CpeDevice.genieacs_device_id`` the Inform handler owns, or the ``_id``
the ACS itself reported for this serial on this pass — and nothing else. When
the device has no ACS document, when more than one document matches, or when
the recorded and reported ids disagree, the plan fails closed: it carries the
OLT actions only and sets ``Plan.acs_wait_reason``. ``reconcile_ont`` then
reports ``ONT_NOT_INFORMING`` / ``ACS_IDENTITY_UNRESOLVED`` and the sweeper
retries after the next Inform.

This replaces an earlier placeholder that hardcoded ``00259E`` + ``HG8546M``
into every device id. The fleet runs several ONT models, so that placeholder
made every non-HG8546M ONT an unrecoverable NBI 404 — a push that could never
succeed, retried forever.

What the planner doesn't do
===========================

* No I/O — pure function.
* No write-secret resolution — passwords carry ``*_ref`` references to be
  resolved at apply time.
* No batching/chunking — the applier groups adjacent ACS writes if needed.
* No retries — the applier handles failures.
* No locking — the locking primitive lives in ``locking.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.control_plane_intent import (
    DesiredValueProvenance,
    has_executable_desired_provenance,
)

from .actions import (
    AcsAddObject,
    AcsDeleteObject,
    AcsSetDhcpServer,
    AcsSetIpv6,
    AcsSetManagementServer,
    AcsSetNatEnabled,
    AcsSetPppoe,
    AcsSetRemoteAccess,
    AcsSetWanIp,
    AcsSetWifiConfig,
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
from .sentinels import is_deliverable
from .state import (
    Drift,
    OntDesiredState,
    OntObservedState,
    ReconcileFailureReason,
    ReconcileMode,
    Tr069RemoteAccessParameterPaths,
    Tr069WifiParameterPaths,
    WriteSurface,
)
from .wifi_paths import wifi_paths_for_instance

# ── Plan ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Plan:
    """Output of ``compute_plan``.

    ``required_surfaces`` is the set of write surfaces the ``actions`` list
    will touch. The precondition layer in ``reconcile_ont`` fast-fails the
    entire reconcile if any of these surfaces was unreachable at read time —
    so a plan that needs ACS but the ACS is down returns up front, before
    any OLT write is attempted.
    """

    actions: tuple[Action, ...]
    drifts: tuple[Drift, ...]
    required_surfaces: frozenset[WriteSurface]
    # Set when the ACS half of this plan could not be built because the device
    # has no ACS document or no unambiguous GenieACS ``_id``. The plan then
    # carries OLT actions ONLY. ``reconcile_ont`` still applies those, then
    # reports this reason instead of claiming convergence — the ACS half waits
    # for the CPE's next Inform. It never means "push anyway to a guessed id".
    acs_wait_reason: str | None = None
    acs_wait_detail: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.actions

    @property
    def waiting_for_acs(self) -> bool:
        return self.acs_wait_reason is not None


@dataclass(frozen=True)
class AcsIdentity:
    """Outcome of resolving one ONT's GenieACS ``_id``.

    ``device_id`` is populated only when a real, recorded-or-observed
    identifier was found. Otherwise ``wait_reason`` carries a
    ``ReconcileFailureReason`` constant and ``detail`` an operator-readable
    explanation. There is deliberately no third "best effort" outcome.
    """

    device_id: str | None
    wait_reason: str | None
    detail: str


@dataclass(frozen=True)
class _WifiChanges:
    enabled: bool | None
    ssid: str | None
    channel: int | None
    security_mode: str | None
    drifts: tuple[tuple[str, object, object], ...]

    @property
    def has_values(self) -> bool:
        return any(
            value is not None
            for value in (self.enabled, self.ssid, self.channel, self.security_mode)
        )


# ── compute_plan ────────────────────────────────────────────────────────────

# Fields a narrow WiFi UI edit may touch. When an operator's proposed change is
# confined to these, the plan is scoped to the matching ACS writes so unrelated
# OLT drift doesn't block the action.
_WIFI_ONLY_FIELDS = frozenset(
    {
        "wifi_ssid",
        "wifi_password_ref",
        "wifi_enabled",
        "wifi_channel",
        "wifi_security_mode",
    }
)
_REMOTE_ACCESS_ONLY_FIELDS = frozenset(
    {
        "wan_remote_access_enabled",
        "wan_remote_access_expires_at",
        "wan_remote_access_source_cidrs",
        "wan_remote_access_ssh_port",
    }
)
_ACS_ENDPOINT_FIELDS = frozenset({"acs_url", "acs_username", "acs_password_ref"})
_TR069_PROFILE_ONLY_FIELDS = frozenset({"tr069_profile_id"})


def _is_wifi_only_change(mode: ReconcileMode, proposed_fields: frozenset[str]) -> bool:
    return (
        mode == "sync"
        and bool(proposed_fields)
        and proposed_fields <= _WIFI_ONLY_FIELDS
    )


def _is_remote_access_only_change(
    mode: ReconcileMode, proposed_fields: frozenset[str]
) -> bool:
    return (
        mode == "sync"
        and bool(proposed_fields)
        and proposed_fields <= _REMOTE_ACCESS_ONLY_FIELDS
    )


def _is_tr069_profile_only_change(
    mode: ReconcileMode, proposed_fields: frozenset[str]
) -> bool:
    return (
        mode == "sync"
        and bool(proposed_fields)
        and proposed_fields <= _TR069_PROFILE_ONLY_FIELDS
    )


def compute_plan(
    desired: OntDesiredState,
    observed: OntObservedState,
    mode: ReconcileMode,
    *,
    proposed_fields: frozenset[str] | None = None,
    force_proposed_writes: bool = True,
) -> Plan:
    """Diff desired vs observed; emit the ordered action list.

    Determinism guarantee: ``compute_plan(d, o, m) == compute_plan(d, o, m)``
    for all inputs. No randomness, no time-of-day dependence, no I/O.

    ``proposed_fields`` carries the operator's current requested mutations.
    For narrow WiFi edits, it scopes the plan to the relevant ACS writes so
    unrelated drift does not block the UI action. ``force_proposed_writes`` is
    used for write-only values, where observed state cannot prove drift; verify
    calls keep the scope but disable the force.
    """
    actions: list[Action] = []
    drifts: list[Drift] = []
    proposed_fields = proposed_fields or frozenset()
    wifi_only_change = _is_wifi_only_change(mode, proposed_fields)
    remote_only_change = _is_remote_access_only_change(mode, proposed_fields)
    tr069_profile_only_change = _is_tr069_profile_only_change(mode, proposed_fields)

    if tr069_profile_only_change:
        _plan_tr069_profile_only(desired, observed, actions, drifts)
        omci_wan_planned = False
    elif wifi_only_change or remote_only_change:
        omci_wan_planned = False
    else:
        _plan_olt_side(desired, observed, mode, actions, drifts)
        omci_wan_planned = _plan_olt_omci_wan(desired, observed, mode, actions, drifts)
        _append_reset_if_needed(desired, actions)
    acs_identity = AcsIdentity(device_id=None, wait_reason=None, detail="")
    if not tr069_profile_only_change:
        acs_identity = _plan_acs_side(
            desired,
            observed,
            mode,
            actions,
            drifts,
            omci_wan_planned,
            proposed_fields,
            force_proposed_writes,
        )

    required_surfaces = frozenset(a.surface for a in actions)
    return Plan(
        actions=tuple(actions),
        drifts=tuple(drifts),
        required_surfaces=required_surfaces,
        acs_wait_reason=acs_identity.wait_reason,
        acs_wait_detail=acs_identity.detail,
    )


# ── OLT-side planning ───────────────────────────────────────────────────────


def _plan_tr069_profile_only(
    desired: OntDesiredState,
    observed: OntObservedState,
    actions: list[Action],
    drifts: list[Drift],
) -> None:
    """Plan only the requested OLT TR-069 profile binding."""
    if not is_deliverable("tr069_profile_id", desired.tr069_profile_id):
        return
    if observed.olt.olt_tr069_profile_id == desired.tr069_profile_id:
        return
    actions.append(
        OltTr069ServerConfig(
            fsp=desired.fsp,
            ont_id=desired.olt_ont_id,
            profile_id=desired.tr069_profile_id,
        )
    )
    drifts.append(
        Drift(
            field="olt_tr069_profile_id",
            surface="olt",
            desired=desired.tr069_profile_id,
            observed=observed.olt.olt_tr069_profile_id,
            repairable=True,
        )
    )


def _plan_olt_side(
    desired: OntDesiredState,
    observed: OntObservedState,
    mode: ReconcileMode,
    actions: list[Action],
    drifts: list[Drift],
) -> None:
    olt_obs = observed.olt

    # 1. Authorize if absent.
    if not olt_obs.olt_present:
        # ``ont add`` carries both profile bindings, so an unset profile is
        # authorized in with the ONT. Unlike the modify path this is not a
        # silent no-op — it creates a live but wrongly-bound authorization —
        # so refuse the whole action rather than emit a partial one. The drift
        # is recorded unrepairable: the ONT genuinely cannot converge until an
        # owner supplies the profiles, and hiding that would report a blocked
        # bring-up as merely pending.
        authorizable = is_deliverable(
            "line_profile_id", desired.line_profile_id
        ) and is_deliverable("service_profile_id", desired.service_profile_id)
        if authorizable:
            actions.append(
                OltAuthorize(
                    fsp=desired.fsp,
                    ont_id=desired.olt_ont_id,
                    line_profile_id=desired.line_profile_id,
                    service_profile_id=desired.service_profile_id,
                    serial_number=desired.serial_number,
                    description=desired.description,
                )
            )
        drifts.append(
            Drift(
                field="olt_present",
                surface="olt",
                desired=True,
                observed=False,
                repairable=authorizable,
            )
        )
        # When we authorize, we provide line/srv profile + description in the
        # same `ont add` — no need to emit separate modify actions.
    else:
        # Present — diff individual fields.
        # A non-positive profile id is unset, not intended, and is silently a
        # no-op on Huawei OLTs — emitting it manufactures a successful write
        # the OLT never applied. See ``reconcile.sentinels``.
        if is_deliverable(
            "line_profile_id", desired.line_profile_id
        ) and _observed_differs(olt_obs.olt_line_profile_id, desired.line_profile_id):
            actions.append(
                OltModifyLineProfile(
                    fsp=desired.fsp,
                    ont_id=desired.olt_ont_id,
                    line_profile_id=desired.line_profile_id,
                )
            )
            drifts.append(
                Drift(
                    field="olt_line_profile_id",
                    surface="olt",
                    desired=desired.line_profile_id,
                    observed=olt_obs.olt_line_profile_id,
                    repairable=True,
                )
            )

        if is_deliverable(
            "service_profile_id", desired.service_profile_id
        ) and _observed_differs(
            olt_obs.olt_service_profile_id, desired.service_profile_id
        ):
            actions.append(
                OltModifyServiceProfile(
                    fsp=desired.fsp,
                    ont_id=desired.olt_ont_id,
                    service_profile_id=desired.service_profile_id,
                )
            )
            drifts.append(
                Drift(
                    field="olt_service_profile_id",
                    surface="olt",
                    desired=desired.service_profile_id,
                    observed=olt_obs.olt_service_profile_id,
                    repairable=True,
                )
            )

        if _observed_differs(olt_obs.olt_description, desired.description):
            actions.append(
                OltModifyDescription(
                    fsp=desired.fsp,
                    ont_id=desired.olt_ont_id,
                    description=desired.description,
                )
            )
            drifts.append(
                Drift(
                    field="olt_description",
                    surface="olt",
                    desired=desired.description,
                    observed=olt_obs.olt_description,
                    repairable=True,
                )
            )

        if is_deliverable(
            "tr069_profile_id", desired.tr069_profile_id
        ) and _observed_differs(olt_obs.olt_tr069_profile_id, desired.tr069_profile_id):
            actions.append(
                OltTr069ServerConfig(
                    fsp=desired.fsp,
                    ont_id=desired.olt_ont_id,
                    profile_id=desired.tr069_profile_id,
                )
            )
            drifts.append(
                Drift(
                    field="olt_tr069_profile_id",
                    surface="olt",
                    desired=desired.tr069_profile_id,
                    observed=olt_obs.olt_tr069_profile_id,
                    repairable=True,
                )
            )

    # 2. Service-port repair — strict, system-managed. Stale ports removed,
    # missing ports created.
    _plan_service_ports(desired, observed, actions, drifts)

    # 3. IPHOST — only meaningful when mgmt VLAN is set.
    if desired.mgmt_vlan is not None and desired.mgmt_ip is not None:
        if _iphost_differs(desired, observed):
            # Stale-clear at both common indices before writing the new one.
            # Mirrors today's Fix #2 (the baseline sweep clear).
            for ip_index in (0, 1):
                actions.append(
                    OltClearIphost(
                        fsp=desired.fsp,
                        ont_id=desired.olt_ont_id,
                        ip_index=ip_index,
                    )
                )
            actions.append(
                OltIpconfig(
                    fsp=desired.fsp,
                    ont_id=desired.olt_ont_id,
                    ip_index=0,
                    ip_address=desired.mgmt_ip,
                    subnet_mask=desired.mgmt_subnet_mask or "255.255.255.0",
                    gateway=desired.mgmt_gateway or "",
                    vlan=desired.mgmt_vlan,
                    priority=desired.mgmt_iphost_priority,
                    dns_primary=desired.mgmt_dns_primary,
                    dns_secondary=desired.mgmt_dns_secondary,
                )
            )
            drifts.append(
                Drift(
                    field="olt_mgmt_ip",
                    surface="olt",
                    desired=desired.mgmt_ip,
                    observed=olt_obs.olt_mgmt_ip,
                    repairable=True,
                )
            )

    # 4. Fresh authorization always needs the TR-069 binding. Existing ONTs
    # are handled by the observed-profile diff above.
    if not olt_obs.olt_present and is_deliverable(
        "tr069_profile_id", desired.tr069_profile_id
    ):
        actions.append(
            OltTr069ServerConfig(
                fsp=desired.fsp,
                ont_id=desired.olt_ont_id,
                profile_id=desired.tr069_profile_id,
            )
        )


def _plan_service_ports(
    desired: OntDesiredState,
    observed: OntObservedState,
    actions: list[Action],
    drifts: list[Drift],
) -> None:
    desired_indices = {
        desired.mgmt_service_port_index,
        desired.wan_service_port_index,
    }
    desired_indices.discard(None)

    # Delete stale service-ports that aren't in the desired set.
    for sp in observed.olt.olt_service_ports:
        idx = sp.get("index") if isinstance(sp, dict) else None
        if idx is None or idx in desired_indices:
            continue
        # Recover the slot from the observed VLAN. A stale port matches neither
        # desired index, so the desired state cannot name it; the VLAN is the
        # only evidence of what it was for. Unrecoverable stays "unknown",
        # which PPP delivery authorization treats as fail-closed.
        observed_vlan = sp.get("vlan_id") if isinstance(sp, dict) else None
        slot = "unknown"
        if observed_vlan is not None:
            if desired.mgmt_vlan is not None and int(observed_vlan) == int(
                desired.mgmt_vlan
            ):
                slot = "mgmt"
            elif desired.wan_vlan is not None and int(observed_vlan) == int(
                desired.wan_vlan
            ):
                slot = "wan"
        actions.append(OltDeleteServicePort(service_port_index=int(idx), slot=slot))
        drifts.append(
            Drift(
                field=f"olt_service_ports[{idx}]",
                surface="olt",
                desired=None,
                observed=sp,
                repairable=True,
            )
        )

    # Create missing service-ports — but only when the allocator has assigned
    # an index. If indices are still None, the allocator hasn't run yet and
    # this is the planner's job to flag (handled by reconcile_ont with a
    # specific failure reason; not a plannable action here).
    observed_indices = {
        sp.get("index") for sp in observed.olt.olt_service_ports if isinstance(sp, dict)
    }
    if (
        desired.mgmt_service_port_index is not None
        and desired.mgmt_service_port_index not in observed_indices
        and desired.mgmt_vlan is not None
    ):
        actions.append(
            OltCreateServicePort(
                fsp=desired.fsp,
                ont_id=desired.olt_ont_id,
                service_port_index=desired.mgmt_service_port_index,
                vlan=desired.mgmt_vlan,
                gem_index=2,  # GEM 2 is the mgmt slot in line profile 40
                slot="mgmt",
            )
        )
    if (
        desired.wan_service_port_index is not None
        and desired.wan_service_port_index not in observed_indices
        and desired.wan_vlan is not None
        and is_deliverable("wan_vlan", desired.wan_vlan)
        and desired.wan_mode == "pppoe"
    ):
        actions.append(
            OltCreateServicePort(
                fsp=desired.fsp,
                ont_id=desired.olt_ont_id,
                service_port_index=desired.wan_service_port_index,
                vlan=desired.wan_vlan,
                gem_index=desired.wan_gem_index or 1,
                slot="wan",
            )
        )


def _plan_olt_omci_wan(
    desired: OntDesiredState,
    observed: OntObservedState,
    mode: ReconcileMode,
    actions: list[Action],
    drifts: list[Drift],
) -> bool:
    """Emit the three-command OMCI WAN sequence when ``wan_config_profile_id``
    is set. Returns ``True`` if OMCI owns WAN PPP — used downstream to decide
    whether TR-069 WAN actions should also fire."""
    if (
        desired.wan_mode != "pppoe"
        or desired.wan_pppoe_provisioning_method == "tr069"
        or desired.wan_config_profile_id is None
        or desired.wan_config_profile_id <= 0
        or desired.wan_internet_config_ip_index is None
        or desired.wan_vlan is None
        or not is_deliverable("wan_vlan", desired.wan_vlan)
    ):
        return False

    ip_index = desired.wan_internet_config_ip_index
    actions.append(
        OltOmciPppoe(
            fsp=desired.fsp,
            ont_id=desired.olt_ont_id,
            ip_index=ip_index,
            vlan=desired.wan_vlan,
            username=desired.wan_pppoe_username or "",
            password_ref=desired.wan_pppoe_password_ref or "",
        )
    )
    actions.append(
        OltOmciInternetConfig(
            fsp=desired.fsp,
            ont_id=desired.olt_ont_id,
            ip_index=ip_index,
        )
    )
    actions.append(
        OltOmciWanConfig(
            fsp=desired.fsp,
            ont_id=desired.olt_ont_id,
            ip_index=ip_index,
            profile_id=desired.wan_config_profile_id,
        )
    )
    return True


def _append_reset_if_needed(desired: OntDesiredState, actions: list[Action]) -> None:
    if any(getattr(a, "requires_reset", False) for a in actions):
        actions.append(OltReset(fsp=desired.fsp, ont_id=desired.olt_ont_id))


# ── ACS-side planning ───────────────────────────────────────────────────────


def _plan_acs_side(
    desired: OntDesiredState,
    observed: OntObservedState,
    mode: ReconcileMode,
    actions: list[Action],
    drifts: list[Drift],
    omci_wan_planned: bool,
    proposed_fields: frozenset[str],
    force_proposed_writes: bool,
) -> AcsIdentity:
    if not desired.acs_server_id:
        # Without an ACS server bound, no ACS actions to plan. Reconciler
        # callers would normally refuse this in routed mode, but the planner
        # itself stays pure. This is not a wait condition — there is nothing
        # to wait for.
        return AcsIdentity(device_id=None, wait_reason=None, detail="")

    identity = resolve_acs_device_id(desired, observed)
    if identity.device_id is None:
        # Fail closed. Emitting actions here would target either a fabricated
        # ``_id`` (guaranteed NBI 404, forever) or an ambiguous one. The OLT
        # actions already in ``actions`` still apply; the ACS half waits.
        return identity
    device_id = identity.device_id

    wifi_only_change = _is_wifi_only_change(mode, proposed_fields)
    remote_only_change = _is_remote_access_only_change(mode, proposed_fields)

    # TR-069 WAN PPP — skipped when OMCI owns the WAN.
    narrow_feature_change = wifi_only_change or remote_only_change
    if (
        not narrow_feature_change
        and desired.wan_mode == "pppoe"
        and not omci_wan_planned
    ):
        _plan_acs_wan_ppp(desired, observed, device_id, actions, drifts)

    # Bring-up pushes below key off ``olt_present`` (the authorization), not
    # ``acs_present``: this code only runs for devices the ACS can deliver to.
    fresh_bring_up = not observed.olt.olt_present

    wan_wcd_index = desired.wan_pppoe_wcd_index
    wan_vlan = desired.wan_vlan
    if (
        not narrow_feature_change
        and desired.wan_mode in {"dhcp", "static"}
        and wan_wcd_index is not None
        and is_deliverable("wan_pppoe_wcd_index", wan_wcd_index)
        and wan_vlan is not None
        and is_deliverable("wan_vlan", wan_vlan)
    ):
        if fresh_bring_up or _wan_ip_differs(desired, observed):
            actions.append(
                AcsSetWanIp(
                    device_id=device_id,
                    data_model_root=(
                        observed.acs.acs_data_model_root
                        or desired.tr069_data_model_root
                        or "InternetGatewayDevice"
                    ),
                    wcd_index=wan_wcd_index,
                    instance_index=desired.wan_pppoe_instance_index,
                    mode=desired.wan_mode,
                    vlan=wan_vlan,
                    nat_enabled=desired.nat_enabled,
                    ip_address=desired.wan_static_ip,
                    subnet_mask=desired.wan_static_subnet,
                    gateway=desired.wan_static_gateway,
                    dns_servers=desired.wan_static_dns,
                    tr181_paths=desired.tr181_wan_paths,
                )
            )
            drifts.append(
                Drift(
                    field="wan_ip_mode",
                    surface="acs",
                    desired=desired.wan_mode,
                    observed=observed.acs.acs_observed_wan_addressing_type,
                    repairable=True,
                )
            )

    push_password = False
    wifi_changes = _WifiChanges(None, None, None, None, ())
    if not remote_only_change:
        push_password = _should_push_wifi_password(
            desired,
            mode,
            observed,
            proposed_fields,
            force_proposed_writes,
        )
        wifi_changes = _wifi_changes(
            desired,
            observed,
            proposed_fields=proposed_fields,
            force_proposed_writes=force_proposed_writes,
        )
    wifi_paths = desired.wifi_paths or _standard_wifi_paths(
        observed.acs.acs_data_model_root or desired.tr069_data_model_root
    )
    if observed.acs.acs_observed_wifi_instance_index is not None:
        wifi_paths = wifi_paths_for_instance(
            wifi_paths,
            observed.acs.acs_data_model_root or desired.tr069_data_model_root,
            observed.acs.acs_observed_wifi_instance_index,
        )
    for field, desired_value, observed_value in wifi_changes.drifts:
        drifts.append(
            Drift(
                field=field,
                surface="acs",
                desired=desired_value,
                observed=observed_value,
                repairable=wifi_paths is not None,
            )
        )

    # WiFi fields share one CWMP transaction. Password remains mode-gated
    # because it is write-only on deployed Huawei firmware.
    if wifi_paths is not None and (wifi_changes.has_values or push_password):
        actions.append(
            AcsSetWifiConfig(
                device_id=device_id,
                paths=wifi_paths,
                enabled=wifi_changes.enabled,
                ssid=wifi_changes.ssid,
                password_ref=(desired.wifi_password_ref if push_password else None),
                channel=wifi_changes.channel,
                security_mode=wifi_changes.security_mode,
            )
        )

    if not wifi_only_change:
        _plan_remote_access(
            desired,
            observed,
            device_id,
            actions,
            drifts,
            proposed_fields=proposed_fields,
            force_proposed_writes=force_proposed_writes,
        )

    if narrow_feature_change:
        return identity

    # Defensive NAT on routed mode (Fix #4 follow-up).
    if (
        desired.wan_mode == "pppoe"
        and not omci_wan_planned
        and desired.wan_pppoe_wcd_index is not None
        and is_deliverable("wan_pppoe_wcd_index", desired.wan_pppoe_wcd_index)
    ):
        wcd = desired.wan_pppoe_wcd_index
        inst = (
            _desired_wan_ppp_instance(desired, observed)
            or desired.wan_pppoe_instance_index
        )
        if _wan_ppp_needs_heal(desired, observed) or _observed_differs(
            observed.acs.acs_observed_nat_enabled, desired.nat_enabled
        ):
            actions.append(
                AcsSetNatEnabled(
                    device_id=device_id,
                    wcd_index=wcd,
                    instance_index=inst,
                    enabled=desired.nat_enabled,
                )
            )
            drifts.append(
                Drift(
                    field="nat_enabled",
                    surface="acs",
                    desired=desired.nat_enabled,
                    observed=observed.acs.acs_observed_nat_enabled,
                    repairable=True,
                )
            )

        for stale_wcd, stale_inst in _stale_wan_ppp_locations(desired, observed):
            actions.append(
                AcsDeleteObject(
                    device_id=device_id,
                    object_path=(
                        "InternetGatewayDevice.WANDevice.1."
                        f"WANConnectionDevice.{stale_wcd}."
                        f"WANPPPConnection.{stale_inst}."
                    ),
                )
            )

    ipv6_provenance = (
        DesiredValueProvenance.explicit
        if "ipv6_enabled" in proposed_fields
        else desired.ipv6_enabled_provenance
    )
    if (
        not wifi_only_change
        and has_executable_desired_provenance(ipv6_provenance)
        and (fresh_bring_up or _ipv6_differs(desired, observed))
    ):
        data_model_root = (
            observed.acs.acs_data_model_root or desired.tr069_data_model_root
        )
        repairable = data_model_root == "Device"
        drifts.append(
            Drift(
                field="ipv6_enabled",
                surface="acs",
                desired=desired.ipv6_enabled,
                observed=observed.acs.acs_observed_ipv6_enabled,
                repairable=repairable,
            )
        )
        if repairable:
            actions.append(
                AcsSetIpv6(
                    device_id=device_id,
                    interface_index=desired.wan_pppoe_instance_index,
                    enabled=desired.ipv6_enabled,
                    request_prefixes=desired.ipv6_enabled,
                    provenance=ipv6_provenance,
                )
            )

    # DHCP server — push the whole block when any field differs.
    if _dhcp_differs(desired, observed):
        actions.append(
            AcsSetDhcpServer(
                device_id=device_id,
                enabled=desired.dhcp_enabled,
                pool_min=desired.dhcp_pool_min,
                pool_max=desired.dhcp_pool_max,
                subnet_mask=desired.dhcp_subnet_mask,
            )
        )
        drifts.append(
            Drift(
                field="dhcp_enabled",
                surface="acs",
                desired=desired.dhcp_enabled,
                observed=observed.acs.acs_observed_dhcp_enabled,
                repairable=True,
            )
        )

    # ManagementServer (CR creds + inform interval) — last in the ACS
    # sequence. Critical for the next reconcile's NBI calls to deliver
    # synchronously.
    force_endpoint_write = bool(proposed_fields & _ACS_ENDPOINT_FIELDS) and (
        force_proposed_writes
    )
    cr_username = desired.cr_username
    cr_password_ref = desired.cr_password_ref
    cr_credentials_ready = (
        isinstance(cr_username, str)
        and isinstance(cr_password_ref, str)
        and is_deliverable("cr_username", cr_username)
        and is_deliverable("cr_password_ref", cr_password_ref)
    )
    if cr_credentials_ready and (
        force_endpoint_write or _management_server_differs(desired, observed)
    ):
        assert isinstance(cr_username, str)
        assert isinstance(cr_password_ref, str)
        actions.append(
            AcsSetManagementServer(
                device_id=device_id,
                cr_username=cr_username,
                cr_password_ref=cr_password_ref,
                inform_interval_sec=desired.periodic_inform_interval_sec,
                data_model_root=(
                    observed.acs.acs_data_model_root
                    or desired.tr069_data_model_root
                    or "InternetGatewayDevice"
                ),
                acs_url=desired.acs_url,
                acs_username=desired.acs_username,
                acs_password_ref=desired.acs_password_ref,
            )
        )
        drifts.append(
            Drift(
                field="acs_management_server",
                surface="acs",
                desired="configured",
                observed="diverged",
                repairable=True,
            )
        )

    return identity


def _plan_acs_wan_ppp(
    desired: OntDesiredState,
    observed: OntObservedState,
    device_id: str,
    actions: list[Action],
    drifts: list[Drift],
) -> None:
    """Plan WAN PPP via TR-069 — addObject if missing, then PPPoE params.

    Only reached for a device the ACS actually holds a document for (see
    ``resolve_acs_device_id``), so ``target_inst is None`` here means "this
    live device has no WANPPPConnection under the desired WCD", not "we have
    never heard from this device".
    """
    if (
        desired.wan_pppoe_wcd_index is None
        or not is_deliverable("wan_pppoe_wcd_index", desired.wan_pppoe_wcd_index)
        or desired.wan_vlan is None
        or not is_deliverable("wan_vlan", desired.wan_vlan)
    ):
        return
    target_inst = _desired_wan_ppp_instance(desired, observed)
    # If ACS doesn't have a WAN PPP instance, addObject first.
    created = target_inst is None
    if target_inst is None:
        actions.append(
            AcsAddObject(
                device_id=device_id,
                object_path=(
                    f"InternetGatewayDevice.WANDevice.1."
                    f"WANConnectionDevice.{desired.wan_pppoe_wcd_index}."
                    f"WANPPPConnection"
                ),
                wcd_index=desired.wan_pppoe_wcd_index,
            )
        )
        drifts.append(
            Drift(
                field="acs_wan_ppp_instance",
                surface="acs",
                desired=desired.wan_pppoe_instance_index,
                observed=None,
                repairable=True,
            )
        )
        target_inst = desired.wan_pppoe_instance_index

    # PPPoE params — diff or set. A just-created object has no observable
    # values to diff against, so creation itself is the write trigger. This
    # used to ride on ``_wan_ppp_differs``'s "ACS has nothing yet" shortcut,
    # which only ever fired for devices the ACS could not deliver to at all.
    if (
        created
        or _wan_ppp_needs_heal(desired, observed)
        or _wan_ppp_differs(desired, observed)
    ):
        actions.append(
            AcsSetPppoe(
                device_id=device_id,
                wcd_index=desired.wan_pppoe_wcd_index,
                instance_index=target_inst,
                username=desired.wan_pppoe_username or "",
                password_ref=desired.wan_pppoe_password_ref or "",
                vlan=desired.wan_vlan,
            )
        )
        drifts.append(
            Drift(
                field="wan_pppoe_username",
                surface="acs",
                desired=desired.wan_pppoe_username,
                observed=observed.acs.acs_observed_pppoe_username,
                repairable=True,
            )
        )


# ── Diff helpers ────────────────────────────────────────────────────────────


def _observed_differs(observed_value, desired_value) -> bool:
    """A field is in drift when the observed value is set AND differs.

    Crucially: an observed value of ``None`` means "we didn't read this
    field" — not "the field is empty on the device". Treat None-observed as
    "no drift signal" to avoid emitting writes against unknown state.

    Exception: fresh authorizations (where olt_present was False) get fully
    bootstrapped in ``_plan_olt_side`` regardless of this helper.
    """
    if observed_value is None:
        return False
    return observed_value != desired_value


def _wifi_changes(
    desired: OntDesiredState,
    observed: OntObservedState,
    *,
    proposed_fields: frozenset[str],
    force_proposed_writes: bool,
) -> _WifiChanges:
    """Return observable WiFi drift and values for one batched write."""
    acs = observed.acs
    root = acs.acs_data_model_root or desired.tr069_data_model_root
    desired_security = _normalise_wifi_security_mode(desired.wifi_security_mode, root)
    observed_security = _normalise_wifi_security_mode(
        acs.acs_observed_wifi_security_mode, root
    )
    candidates = (
        ("wifi_ssid", "ssid", desired.wifi_ssid, acs.acs_observed_ssid),
        (
            "wifi_enabled",
            "enabled",
            desired.wifi_enabled,
            acs.acs_observed_wifi_enabled,
        ),
        (
            "wifi_channel",
            "channel",
            desired.wifi_channel,
            acs.acs_observed_wifi_channel,
        ),
        (
            "wifi_security_mode",
            "security_mode",
            desired_security,
            observed_security,
        ),
    )
    values: dict[str, object] = {}
    drifts: list[tuple[str, object, object]] = []
    for field, action_key, desired_value, observed_value in candidates:
        if desired_value is None:
            continue
        if not is_deliverable(field, desired_value):
            # The value is unset, not intended — see ``reconcile.sentinels``.
            # No drift is recorded: an unknown desired value has no target to
            # converge on, and marking it would strand the ONT out_of_sync
            # forever. ``scripts/network/ont_sentinel_blast_radius.py`` is
            # where unmanaged fields become visible.
            continue
        forced = force_proposed_writes and field in proposed_fields
        differs = _observed_differs(observed_value, desired_value)
        # Bring-up push. The freshness signal is the OLT authorization, not
        # "the ACS has never seen this device": the latter guaranteed a 404
        # (nothing can be written to a device the ACS holds no document for)
        # and never converged. ``olt_present`` flips to True once authorized,
        # so this fires at most once per ONT.
        fresh = not observed.olt.olt_present
        if not (forced or differs or fresh):
            continue
        values[action_key] = desired_value
        drifts.append((field, desired_value, observed_value))
    enabled = values.get("enabled")
    ssid = values.get("ssid")
    channel = values.get("channel")
    security_mode = values.get("security_mode")
    return _WifiChanges(
        enabled=enabled if isinstance(enabled, bool) else None,
        ssid=ssid if isinstance(ssid, str) else None,
        channel=channel if isinstance(channel, int) else None,
        security_mode=security_mode if isinstance(security_mode, str) else None,
        drifts=tuple(drifts),
    )


def _normalise_wifi_security_mode(value: str | None, root: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if root != "InternetGatewayDevice":
        return text
    aliases = {
        "none": "None",
        "open": "None",
        "wep": "Basic",
        "basic": "Basic",
        "wpa": "WPA",
        "wpa-personal": "WPA",
        "wpapsk": "WPA",
        "wpa-psk": "WPA",
        "wpa2": "11i",
        "wpa2-personal": "11i",
        "wpa2psk": "11i",
        "wpa2-psk": "11i",
        "11i": "11i",
        "wpa-wpa2": "WPAand11i",
        "wpa-wpa2-personal": "WPAand11i",
        "wpa/wpa2": "WPAand11i",
        "wpa2/wpa": "WPAand11i",
        "wpaand11i": "WPAand11i",
        "mixed": "WPAand11i",
    }
    return aliases.get(text.lower(), text)


def _plan_remote_access(
    desired: OntDesiredState,
    observed: OntObservedState,
    device_id: str,
    actions: list[Action],
    drifts: list[Drift],
    *,
    proposed_fields: frozenset[str],
    force_proposed_writes: bool,
) -> None:
    """Plan one atomic SSH/Telnet support-access transaction."""
    acs = observed.acs
    explicit_toggle = "wan_remote_access_enabled" in proposed_fields
    strict_readback = desired.wan_remote_access_enabled or explicit_toggle
    force_toggle = force_proposed_writes and explicit_toggle

    ssh_differs = (
        acs.acs_observed_remote_ssh_enabled != desired.wan_remote_access_enabled
        if strict_readback
        else _observed_differs(
            acs.acs_observed_remote_ssh_enabled,
            desired.wan_remote_access_enabled,
        )
    )
    port_differs = desired.wan_remote_access_enabled and (
        acs.acs_observed_remote_ssh_port != desired.wan_remote_access_ssh_port
    )
    telnet_differs = (
        acs.acs_observed_remote_telnet_enabled is not False
        if strict_readback
        else acs.acs_observed_remote_telnet_enabled is True
    )

    paths = desired.remote_access_paths or _standard_remote_access_paths(
        acs.acs_data_model_root or desired.tr069_data_model_root
    )
    changes: list[tuple[str, object, object]] = []
    if force_toggle or ssh_differs:
        changes.append(
            (
                "wan_remote_access_enabled",
                desired.wan_remote_access_enabled,
                acs.acs_observed_remote_ssh_enabled,
            )
        )
    if port_differs:
        changes.append(
            (
                "wan_remote_access_ssh_port",
                desired.wan_remote_access_ssh_port,
                acs.acs_observed_remote_ssh_port,
            )
        )
    if telnet_differs:
        changes.append(
            (
                "wan_remote_telnet_disabled",
                False,
                acs.acs_observed_remote_telnet_enabled,
            )
        )
    for field, desired_value, observed_value in changes:
        drifts.append(
            Drift(
                field=field,
                surface="acs",
                desired=desired_value,
                observed=observed_value,
                repairable=paths is not None,
            )
        )
    if not changes or paths is None:
        return

    actions.append(
        AcsSetRemoteAccess(
            device_id=device_id,
            paths=paths,
            ssh_enabled=(
                desired.wan_remote_access_enabled
                if force_toggle or ssh_differs
                else None
            ),
            ssh_port=(desired.wan_remote_access_ssh_port if port_differs else None),
            # Enabling support access always carries the Telnet-off guard in
            # the same CWMP request, even when the cached value is already off.
            telnet_enabled=(
                False if telnet_differs or desired.wan_remote_access_enabled else None
            ),
        )
    )


def _standard_wifi_paths(root: str | None) -> Tr069WifiParameterPaths:
    if root == "Device":
        return Tr069WifiParameterPaths(
            enabled="Device.WiFi.SSID.1.Enable",
            ssid="Device.WiFi.SSID.1.SSID",
            psk_path="Device.WiFi.AccessPoint.1.Security.KeyPassphrase",
            channel="Device.WiFi.Radio.1.Channel",
            security_mode="Device.WiFi.AccessPoint.1.Security.ModeEnabled",
        )
    return Tr069WifiParameterPaths(
        enabled="InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Enable",
        ssid="InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID",
        psk_path=(
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1."
            "PreSharedKey.1.PreSharedKey"
        ),
        channel="InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Channel",
        security_mode=(
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.BeaconType"
        ),
    )


def _standard_remote_access_paths(
    root: str | None,
) -> Tr069RemoteAccessParameterPaths:
    prefix = "Device" if root == "Device" else "InternetGatewayDevice"
    base = f"{prefix}.X_HW_UserInterface"
    return Tr069RemoteAccessParameterPaths(
        ssh_enabled=f"{base}.SSHEnable",
        ssh_port=f"{base}.SSHPort",
        telnet_enabled=f"{base}.TelnetEnable",
        telnet_port=f"{base}.TelnetPort",
    )


def _iphost_differs(desired: OntDesiredState, observed: OntObservedState) -> bool:
    olt = observed.olt
    if olt.olt_mgmt_ip is not None and olt.olt_mgmt_ip != desired.mgmt_ip:
        return True
    if olt.olt_mgmt_vlan is not None and olt.olt_mgmt_vlan != desired.mgmt_vlan:
        return True
    # Fresh authorization always needs the iphost write — even when the OLT
    # reader hasn't populated mgmt_ip yet (None).
    if not olt.olt_present:
        return True
    return False


def _wan_ppp_differs(desired: OntDesiredState, observed: OntObservedState) -> bool:
    acs = observed.acs
    if _observed_differs(acs.acs_observed_pppoe_username, desired.wan_pppoe_username):
        return True
    if _observed_differs(acs.acs_observed_wan_vlan, desired.wan_vlan):
        return True
    if acs.acs_observed_pppoe_enable is False:
        return True
    # NOTE: there is deliberately no "ACS has nothing yet, push anyway" branch.
    # A device with no ACS document cannot be written to at all — every NBI
    # call against it is a 404 — so ``resolve_acs_device_id`` stops the plan
    # before this helper is consulted. Establishing PPP on a live-but-unconfigured
    # device is driven by the addObject/``created`` path in ``_plan_acs_wan_ppp``.
    return False


def _wan_ip_differs(desired: OntDesiredState, observed: OntObservedState) -> bool:
    acs = observed.acs
    expected_type = "DHCP" if desired.wan_mode == "dhcp" else "Static"
    strict = (
        desired.tr181_wan_paths is not None
        and (acs.acs_data_model_root or desired.tr069_data_model_root) == "Device"
    )

    def differs(observed_value, desired_value) -> bool:
        return (
            observed_value != desired_value
            if strict
            else _observed_differs(observed_value, desired_value)
        )

    if differs(acs.acs_observed_wan_ip_enable, True):
        return True
    if differs(acs.acs_observed_wan_addressing_type, expected_type):
        return True
    if differs(acs.acs_observed_wan_vlan, desired.wan_vlan):
        return True
    if differs(acs.acs_observed_nat_enabled, desired.nat_enabled):
        return True
    if desired.wan_mode == "static":
        return any(
            (
                differs(acs.acs_observed_wan_ip_address, desired.wan_static_ip),
                differs(acs.acs_observed_wan_subnet_mask, desired.wan_static_subnet),
                differs(acs.acs_observed_wan_gateway, desired.wan_static_gateway),
                differs(
                    _normalise_dns_servers(acs.acs_observed_wan_dns_servers),
                    _normalise_dns_servers(desired.wan_static_dns),
                ),
            )
        )
    return False


def _normalise_dns_servers(value: str | None) -> str | None:
    if value is None:
        return None
    servers = [item for item in re.split(r"[\s,]+", value) if item]
    return ",".join(servers) or None


def _ipv6_differs(desired: OntDesiredState, observed: OntObservedState) -> bool:
    acs = observed.acs
    values = [acs.acs_observed_ipv6_enabled]
    if desired.tr069_data_model_root == "Device" or acs.acs_data_model_root == "Device":
        values.extend(
            [
                acs.acs_observed_dhcpv6_enabled,
                acs.acs_observed_dhcpv6_request_prefixes,
                acs.acs_observed_ra_enabled,
            ]
        )
    return any(_observed_differs(value, desired.ipv6_enabled) for value in values)


def _observed_wan_ppp_locations(
    observed: OntObservedState,
) -> tuple[tuple[int, int], ...]:
    acs = observed.acs
    if acs.acs_observed_wan_ppp_locations:
        return acs.acs_observed_wan_ppp_locations
    if (
        acs.acs_observed_wan_wcd_index is not None
        and acs.acs_observed_wan_instance_index is not None
    ):
        return ((acs.acs_observed_wan_wcd_index, acs.acs_observed_wan_instance_index),)
    return ()


def _primary_observed_wan_ppp_location(
    observed: OntObservedState,
) -> tuple[int, int] | None:
    acs = observed.acs
    if (
        acs.acs_observed_wan_wcd_index is not None
        and acs.acs_observed_wan_instance_index is not None
    ):
        return (
            acs.acs_observed_wan_wcd_index,
            acs.acs_observed_wan_instance_index,
        )
    return None


def _desired_wan_ppp_instance(
    desired: OntDesiredState,
    observed: OntObservedState,
) -> int | None:
    primary = _primary_observed_wan_ppp_location(observed)
    if primary and primary[0] == desired.wan_pppoe_wcd_index:
        return primary[1]
    instances = [
        inst
        for wcd, inst in _observed_wan_ppp_locations(observed)
        if wcd == desired.wan_pppoe_wcd_index
    ]
    if not instances:
        return None
    # Prefer the lowest discovered instance when the reader did not expose a
    # primary location on the desired WCD. This avoids steering toward a
    # later duplicate child and keeps deleteObject disabled for ambiguous
    # layouts until we have a confirmed primary target.
    return min(instances)


def _target_wan_ppp_location(
    desired: OntDesiredState,
    observed: OntObservedState,
) -> tuple[int, int] | None:
    if desired.wan_pppoe_wcd_index is None:
        return None
    inst = _desired_wan_ppp_instance(desired, observed)
    if inst is None:
        return None
    return (desired.wan_pppoe_wcd_index, inst)


def _stale_wan_ppp_locations(
    desired: OntDesiredState,
    observed: OntObservedState,
) -> tuple[tuple[int, int], ...]:
    target = _target_wan_ppp_location(desired, observed)
    primary = _primary_observed_wan_ppp_location(observed)
    # Only prune duplicates when the target matches the same live child the
    # reader used for observed values. Otherwise the layout is ambiguous and
    # deleteObject risks removing the active session.
    if target is None or primary != target:
        return ()
    return tuple(
        pair for pair in _observed_wan_ppp_locations(observed) if pair != target
    )


def _wan_ppp_needs_heal(desired: OntDesiredState, observed: OntObservedState) -> bool:
    locations = _observed_wan_ppp_locations(observed)
    if not locations:
        return False
    if _desired_wan_ppp_instance(desired, observed) is None:
        return True
    return bool(_stale_wan_ppp_locations(desired, observed))


def _dhcp_differs(desired: OntDesiredState, observed: OntObservedState) -> bool:
    acs = observed.acs
    if _observed_differs(acs.acs_observed_dhcp_enabled, desired.dhcp_enabled):
        return True
    # DHCP pool min/max/mask are write-only on most HG8546M firmwares — once
    # set we accept what's on the device. The defensive enable on bring-up
    # covers the no-DHCP-by-default case from feedback_ont_setup_defaults; it
    # keys off the OLT authorization rather than ACS absence, because a device
    # the ACS has no document for cannot be written to at all.
    return not observed.olt.olt_present


def _management_server_differs(
    desired: OntDesiredState, observed: OntObservedState
) -> bool:
    acs = observed.acs
    if desired.acs_url and acs.acs_observed_url != desired.acs_url:
        return True
    if desired.acs_url and acs.acs_observed_username != (desired.acs_username or ""):
        return True
    if desired.acs_password_ref and not acs.acs_observed_password_set:
        return True
    if (
        acs.acs_observed_periodic_inform_interval_sec
        != desired.periodic_inform_interval_sec
    ):
        return True
    # Empty/missing/mismatched CR username drives every NBI write toward 202
    # (queued, not delivered) — always emit when not confirmed equal.
    if acs.acs_observed_cr_username != desired.cr_username:
        return True
    if not acs.acs_observed_cr_password_set:
        return True
    return False


def _should_push_wifi_password(
    desired: OntDesiredState,
    mode: ReconcileMode,
    observed: OntObservedState,
    proposed_fields: frozenset[str],
    force_proposed_writes: bool,
) -> bool:
    """Push at explicit desired-state change, fresh sync, and BOOTSTRAP.

    The sweeper never pushes the PSK — no observable value can confirm drift.
    In sync mode, an operator-proposed password change is an explicit write
    request and must emit exactly one ACS action on the apply pass. Verification
    calls omit ``proposed_fields`` so the write-only password is not re-emitted.

    An unset PSK is never pushed on any trigger: writing the empty sentinel
    would clear the pre-shared key and leave the customer's WLAN open. Because
    the field is write-only, nothing downstream could observe that it happened.
    """
    if not is_deliverable("wifi_password_ref", desired.wifi_password_ref):
        return False
    if mode == "bootstrap":
        return True
    if (
        force_proposed_writes
        and mode == "sync"
        and "wifi_password_ref" in proposed_fields
    ):
        return True
    if mode == "sync" and not observed.olt.olt_present:
        return True
    return False


def resolve_acs_device_id(
    desired: OntDesiredState,
    observed: OntObservedState,
) -> AcsIdentity:
    """Resolve the exact GenieACS ``_id`` this ONT's ACS writes must target.

    A GenieACS ``_id`` is ``{OUI}-{ProductClass}-{SerialNumber}``. This function
    NEVER composes one: both the OUI and the ProductClass are model-specific and
    the fleet is not one model. The only two admissible sources are

    1. ``desired.acs_device_id`` — the persisted ``Tr069CpeDevice`` record the
       TR-069 Inform handler owns (``app/services/tr069.py``). This is the
       authoritative identity.
    2. ``observed.acs.acs_observed_device_id`` — the ``_id`` the ACS itself
       returned for this serial on this pass. An observation of the external
       system, never an invention.

    Every other outcome fails closed with a wait reason and **no** ACS actions:

    * device absent from the ACS (``acs_present=False``) → ``ont_not_informing``
    * more than one ACS document matched the serial → ``acs_identity_unresolved``
    * neither source knows the id → ``acs_identity_unresolved``
    * the two sources disagree → ``acs_identity_unresolved`` (one of them names
      a different physical CPE; writing to either is unsafe)

    Release gate: no ACS plan synthesizes a device identifier.
    """
    persisted = (desired.acs_device_id or "").strip() or None
    acs = observed.acs
    seen = (acs.acs_observed_device_id or "").strip() or None

    if not acs.acs_present:
        return AcsIdentity(
            device_id=None,
            wait_reason=ReconcileFailureReason.ONT_NOT_INFORMING,
            detail=(
                f"ONT {desired.serial_number} has no ACS device document; "
                "ACS configuration waits for the next Inform."
            ),
        )
    if acs.acs_observed_device_match_count > 1:
        return AcsIdentity(
            device_id=None,
            wait_reason=ReconcileFailureReason.ACS_IDENTITY_UNRESOLVED,
            detail=(
                f"{acs.acs_observed_device_match_count} ACS devices match serial "
                f"{desired.serial_number}; refusing to guess which one to write."
            ),
        )
    if persisted and seen and persisted != seen:
        return AcsIdentity(
            device_id=None,
            wait_reason=ReconcileFailureReason.ACS_IDENTITY_UNRESOLVED,
            detail=(
                "Recorded ACS device id disagrees with the id the ACS reports "
                f"for serial {desired.serial_number}; refusing to write to "
                "either until the CPE record is repaired."
            ),
        )
    device_id = persisted or seen
    if not device_id:
        return AcsIdentity(
            device_id=None,
            wait_reason=ReconcileFailureReason.ACS_IDENTITY_UNRESOLVED,
            detail=(
                f"No recorded GenieACS device id for serial {desired.serial_number} "
                "and the ACS read returned none."
            ),
        )
    return AcsIdentity(device_id=device_id, wait_reason=None, detail="")


__all__ = ("Plan", "compute_plan")
