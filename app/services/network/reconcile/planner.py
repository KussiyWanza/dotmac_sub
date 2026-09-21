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

Service-port index is a symmetric OLT-side precondition
=========================================================

There is no allocator anywhere in this package: a service-port index either
arrives from operator input (``desired.mgmt_service_port_index`` /
``wan_service_port_index``) or stays ``None``. When both are ``None`` and
every currently observed service port would otherwise be planned for
deletion — none of them protected by a VLAN+GEM match against an unindexed
desired slot — the plan fails closed there too: it withholds every
``OltDeleteServicePort`` action and sets ``Plan.olt_wait_reason`` to
``SERVICE_PORT_INDEX_UNALLOCATED`` instead of deleting the ONT's entire
service-port set with nothing to recreate it at. The same class of refusal
applies narrower: when only ONE of the two indices is unallocated, a
specific observed port correlated (by VLAN) to that one slot is withheld
from deletion the same way, even though the other slot's real index means
the rest of the set is still repaired normally.

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
    # Every planned service-port CREATE (never observable as "drift" at plan
    # time — the port doesn't exist yet, so there's no divergent value to
    # diff, just an absence) records ONE entry here as well as in ``drifts``.
    # ``reconcile_ont`` re-checks this set (not just ``drifts``) on the
    # post-apply verify pass, so a create the OLT silently no-op'd cannot
    # re-plan as "another driftless create" and slip past the no-drift
    # convergence gate. Deliberately NOT ``verify_plan.actions`` — bootstrap
    # mode always re-plans the WiFi/PSK push regardless of whether anything
    # is actually wrong, which would turn every bootstrap pass into a false
    # mismatch.
    verification_debt: tuple[Drift, ...] = ()
    # Set when the ACS half of this plan could not be built because the device
    # has no ACS document or no unambiguous GenieACS ``_id``. The plan then
    # carries OLT actions ONLY. ``reconcile_ont`` still applies those, then
    # reports this reason instead of claiming convergence — the ACS half waits
    # for the CPE's next Inform. It never means "push anyway to a guessed id".
    acs_wait_reason: str | None = None
    acs_wait_detail: str = ""
    # Set when the OLT half of this plan withheld one or more
    # ``OltDeleteServicePort`` actions for this ONT because the mgmt and/or
    # WAN service-port index is unallocated (``None``) and a correlated
    # observed service port would otherwise have been planned for deletion,
    # with nothing to recreate it at — either every observed port (both
    # indices unallocated, nothing preserved) or just the port(s) correlated
    # to the one unallocated slot (the other slot carries a real index).
    # Unindexed ports still matched by ``_matches_unindexed_desired_slot``
    # remain preserved and do not trigger this —
    # see ``ReconcileFailureReason.SERVICE_PORT_INDEX_UNALLOCATED``.
    olt_wait_reason: str | None = None
    olt_wait_detail: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.actions

    @property
    def waiting_for_acs(self) -> bool:
        return self.acs_wait_reason is not None

    @property
    def waiting_for_olt(self) -> bool:
        return self.olt_wait_reason is not None


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
_WAN_ONLY_FIELDS = frozenset(
    {
        "wan_mode",
        "wan_pppoe_username",
        "wan_pppoe_password_ref",
        "wan_pppoe_wcd_index",
        "wan_static_ip",
        "wan_static_subnet",
        "wan_static_gateway",
        "wan_static_dns",
        "wan_static_ip_is_public",
        "ipv6_enabled",
    }
)
_ACS_ENDPOINT_FIELDS = frozenset({"acs_url", "acs_username", "acs_password_ref"})
_TR069_PROFILE_ONLY_FIELDS = frozenset({"tr069_profile_id"})


def _is_wifi_only_change(mode: ReconcileMode, proposed_fields: frozenset[str]) -> bool:
    # ``bootstrap`` counts here as well as ``sync``. Setting a WiFi password
    # forces bootstrap mode (the password is write-only, so observed state can
    # never prove drift), and restricting this to ``sync`` meant every SSID+PSK
    # edit fell through to a full OLT-side plan — one that can emit
    # OltDeleteServicePort against a live customer service port. A BOOTSTRAP
    # event from GenieACS carries no proposed fields, so it still gets the full
    # plan via the ``bool(proposed_fields)`` guard.
    return (
        mode in ("sync", "sweep", "bootstrap")
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


def _is_wan_only_change(mode: ReconcileMode, proposed_fields: frozenset[str]) -> bool:
    return (
        mode == "sync" and bool(proposed_fields) and proposed_fields <= _WAN_ONLY_FIELDS
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
    verification_debt: list[Drift] = []
    proposed_fields = proposed_fields or frozenset()
    wifi_only_change = _is_wifi_only_change(mode, proposed_fields)
    remote_only_change = _is_remote_access_only_change(mode, proposed_fields)
    wan_only_change = _is_wan_only_change(mode, proposed_fields)
    tr069_profile_only_change = _is_tr069_profile_only_change(mode, proposed_fields)
    olt_wait_reason: str | None = None

    # Physical identity is a hard precondition for every OLT action (Astra
    # Bug 1): an unresolved or mismatched fsp/olt_ont_id target means no
    # coordinate pair is safe to authorize or modify against. This gate runs
    # BEFORE the mode branches below and, when it fires, skips all of them —
    # ``wifi_only_change``/``remote_only_change`` never touch the OLT side
    # regardless, so they are the only ones exempt.
    olt_identity_gate = _olt_identity_gate(desired, observed)
    if olt_identity_gate is not None and not (wifi_only_change or remote_only_change):
        drifts.append(_olt_identity_drift(desired, observed))
        olt_wait_reason = olt_identity_gate
        omci_wan_planned = False
    elif tr069_profile_only_change:
        _plan_tr069_profile_only(desired, observed, actions, drifts)
        omci_wan_planned = False
    elif wifi_only_change or remote_only_change:
        omci_wan_planned = False
    elif wan_only_change:
        _plan_wan_service_port(desired, observed, actions, drifts, verification_debt)
        omci_wan_planned = _plan_olt_omci_wan(desired, observed, mode, actions, drifts)
        _append_reset_if_needed(desired, actions)
    else:
        olt_wait_reason = _plan_olt_side(
            desired, observed, mode, actions, drifts, verification_debt
        )
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

    olt_wait_detail = ""
    if olt_wait_reason == ReconcileFailureReason.OLT_IDENTITY_UNRESOLVED:
        olt_wait_detail = (
            f"ONT {desired.serial_number}: desired state has no fsp/"
            "olt_ont_id target (or the OLT registration found by serial "
            "could not be checked against one); refusing every OLT action "
            "until an owner assigns a real physical target."
        )
    elif olt_wait_reason == ReconcileFailureReason.OLT_IDENTITY_MISMATCH:
        olt_wait_detail = (
            f"ONT {desired.serial_number}: the OLT reports this serial "
            f"registered somewhere other than the desired fsp={desired.fsp!r} "
            f"olt_ont_id={desired.olt_ont_id!r} target; refusing every OLT "
            "action rather than writing to either coordinate pair."
        )
    elif olt_wait_reason is not None:
        olt_wait_detail = (
            f"ONT {desired.serial_number}: one or more OLT service-port "
            "indices (mgmt and/or WAN) are unallocated while "
            f"{len(observed.olt.olt_service_ports or ())} service port(s) are "
            "observed; refusing to delete the correlated port(s) with no "
            "target to recreate them."
        )

    required_surfaces = frozenset(a.surface for a in actions)
    return Plan(
        actions=tuple(actions),
        drifts=tuple(drifts),
        required_surfaces=required_surfaces,
        verification_debt=tuple(verification_debt),
        acs_wait_reason=acs_identity.wait_reason,
        acs_wait_detail=acs_identity.detail,
        olt_wait_reason=olt_wait_reason,
        olt_wait_detail=olt_wait_detail,
    )


# ── OLT-side planning ───────────────────────────────────────────────────────


def _olt_identity_gate(
    desired: OntDesiredState, observed: OntObservedState
) -> str | None:
    """Whether this pass's OLT read confirmed the STORED target's identity.

    Returns ``None`` when bound (safe to plan OLT actions against
    ``desired.fsp``/``desired.olt_ont_id``), else the
    ``ReconcileFailureReason`` naming why every OLT action is withheld this
    pass.

    Two independent signals, checked in order:

    1. ``is_deliverable("olt_ont_id", ...)``/``is_deliverable("fsp", ...)`` —
       the STORED target itself is a registered inadmissible sentinel
       (``olt_ont_id is None`` / ``fsp == ""``). Checked directly against
       ``desired`` rather than trusting ``observed.olt.olt_identity_status``
       alone, so a caller that builds ``OntObservedState`` without correctly
       threading identity_status (e.g. a cached fallback that defaults to
       ``"bound"``) cannot accidentally let an unresolved target through.
    2. ``observed.olt.olt_identity_status`` — set by ``readers.olt_reader``
       after checking a found registration's fsp+onu_id against the desired
       target (or noting there was no target to check).
    """
    if not is_deliverable("olt_ont_id", desired.olt_ont_id) or not is_deliverable(
        "fsp", desired.fsp
    ):
        return ReconcileFailureReason.OLT_IDENTITY_UNRESOLVED
    status = observed.olt.olt_identity_status
    if status == "bound":
        return None
    if status == "mismatch":
        return ReconcileFailureReason.OLT_IDENTITY_MISMATCH
    return ReconcileFailureReason.OLT_IDENTITY_UNRESOLVED


def _olt_identity_drift(desired: OntDesiredState, observed: OntObservedState) -> Drift:
    """Unrepairable drift recorded when the identity gate withholds every
    OLT action. Keeps ``plan.drifts`` honest about the withheld state instead
    of reporting a clean plan for a device whose physical target could not
    be confirmed."""
    return Drift(
        field="olt_identity",
        surface="olt",
        desired=(desired.fsp, desired.olt_ont_id),
        observed=observed.olt.olt_identity_status,
        repairable=False,
    )


def _plan_tr069_profile_only(
    desired: OntDesiredState,
    observed: OntObservedState,
    actions: list[Action],
    drifts: list[Drift],
) -> None:
    """Plan only the requested OLT TR-069 profile binding."""
    if desired.olt_ont_id is None:
        # ``compute_plan`` never reaches this function while the identity
        # gate is open; this guard exists only to narrow the type for mypy
        # and as a second line of defense.
        return
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
    verification_debt: list[Drift],
) -> str | None:
    """Plan the OLT half; returns a wait reason when it was withheld.

    The only current wait reason is
    ``ReconcileFailureReason.SERVICE_PORT_INDEX_UNALLOCATED``, produced by
    ``_plan_service_ports`` and passed through unchanged — every other
    OLT-side action in this function still plans normally even when it
    fires.
    """
    if desired.olt_ont_id is None:
        # ``compute_plan`` never reaches this function while the identity
        # gate is open; this guard exists only to narrow the type for mypy
        # and as a second line of defense.
        return None
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
    service_port_wait_reason = _plan_service_ports(
        desired, observed, actions, drifts, verification_debt
    )

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

    return service_port_wait_reason


def _sp_int(sp: object, *names: str) -> int | None:
    """Coerce a named field of an observed service-port dict to ``int``."""
    if not isinstance(sp, dict):
        return None
    for name in names:
        value = sp.get(name)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _service_port_matches(
    sp: object,
    *,
    index: int,
    vlan: int,
    gem_index: int,
    ont_id: int,
    fsp: str,
) -> bool:
    """Whether an observed service-port entry IS the desired slot.

    Full identity — index, VLAN, GEM, ONT-ID, and fsp — not just the global
    index. Astra Bug 2: comparing on index alone means a create whose write
    silently no-ops (or a port some other process repointed to a different
    VLAN/GEM) still reads back "index present" and is reported converged.
    ``ont_id``/``fsp`` are compared only when the observed entry actually
    carries them (older/partial readers may not populate every field);
    their absence is not itself a mismatch.
    """
    if not isinstance(sp, dict):
        return False
    if _sp_int(sp, "index") != index:
        return False
    if _sp_int(sp, "vlan_id", "vlan") != vlan:
        return False
    if _sp_int(sp, "gem_index", "gem") != gem_index:
        return False
    observed_ont_id = _sp_int(sp, "ont_id")
    if observed_ont_id is not None and observed_ont_id != ont_id:
        return False
    observed_fsp = sp.get("fsp")
    if observed_fsp not in (None, "", fsp):
        return False
    return True


def _plan_service_ports(
    desired: OntDesiredState,
    observed: OntObservedState,
    actions: list[Action],
    drifts: list[Drift],
    verification_debt: list[Drift],
) -> str | None:
    """Repair OLT service ports; returns a wait reason when deletes are withheld.

    Returns ``ReconcileFailureReason.SERVICE_PORT_INDEX_UNALLOCATED`` in two
    cases, both withholding only the affected delete(s) rather than the
    whole OLT plan:

    1. **Both slots unallocated.** ``mgmt_service_port_index`` and
       ``wan_service_port_index`` are both ``None`` AND every observed
       service port would be planned for deletion — i.e. none of them is
       protected by ``_matches_unindexed_desired_slot`` (a VLAN+GEM match
       against a still-unallocated desired slot). The desired set is empty
       and nothing preserves any observed port, so the delete loop would
       remove the ONT's entire service-port set with no way to recreate
       them (the create branch below requires a real index).
    2. **One slot unallocated.** Only ``mgmt_service_port_index`` (or only
       ``wan_service_port_index``) is ``None``. This is narrower and does
       not require the whole set to be at risk: a specific observed port
       whose VLAN correlates to that one still-unallocated slot (the same
       correlation ``_matches_unindexed_desired_slot`` uses, just not
       protected by it — e.g. a non-PPPoE WAN slot, which that helper never
       protects regardless of index) would otherwise be deleted with
       nothing to recreate it. Only that specific port's delete is
       withheld; an unrelated stale port in the same batch is still
       deleted normally.

    A port that *does* match an unindexed desired slot via
    ``_matches_unindexed_desired_slot`` is preserved exactly as before this
    guard existed. Every withheld port also gets an unrepairable ``Drift``
    entry, so ``plan.drifts`` reflects the withheld state instead of
    looking clean.

    A port that occupies the desired index but with the WRONG VLAN/GEM/ONT-ID
    (Astra Bug 2) is never auto delete+recreated — that is a live customer
    port and repairing it is a separate, human-owned decision. It is recorded
    as unrepairable drift instead, and no create is planned for that slot
    (the index is occupied, just not correctly).
    """
    if desired.olt_ont_id is None:
        # ``compute_plan`` never reaches this function while the identity
        # gate is open; this guard exists only to narrow the type for mypy
        # and as a second line of defense.
        return None
    if observed.olt.olt_service_ports is None:
        # Astra Bug 2 follow-on: the enumeration itself failed (SSH
        # error/exception), so the OLT's real service-port set is UNKNOWN —
        # never "confirmed empty". Treating it as empty would plan a CREATE
        # for an index that may already hold a live port, and protect every
        # genuinely stale port from deletion by accident. Record the gap as
        # drift (blocks the no-drift-tolerance convergence check) and plan
        # no service-port action at all this pass.
        drifts.append(
            Drift(
                field="olt_service_ports",
                surface="olt",
                desired="known",
                observed=None,
                repairable=False,
            )
        )
        return None
    fsp = desired.fsp
    ont_id = desired.olt_ont_id

    def _matches_unindexed_desired_slot(sp: dict) -> bool:
        if desired.mgmt_service_port_index is None and desired.mgmt_vlan is not None:
            if (
                _sp_int(sp, "vlan_id", "vlan") == int(desired.mgmt_vlan)
                and _sp_int(sp, "gem_index", "gem") == 2
            ):
                return True
        if (
            desired.wan_service_port_index is None
            and desired.wan_vlan is not None
            and desired.wan_mode == "pppoe"
        ):
            if _sp_int(sp, "vlan_id", "vlan") == int(desired.wan_vlan) and _sp_int(
                sp, "gem_index", "gem"
            ) == int(desired.wan_gem_index or 1):
                return True
        return False

    desired_indices = {
        desired.mgmt_service_port_index,
        desired.wan_service_port_index,
    }
    desired_indices.discard(None)

    # Ports occupying a desired index but not matching it exactly — recorded
    # as unrepairable drift below, never deleted or recreated.
    mismatched_at_index: dict[str, dict] = {}

    # Build the candidate delete list first without mutating ``actions``/
    # ``drifts`` yet, so the "every observed port would be deleted with
    # nothing protecting any of them" hazard can be detected before
    # committing to it.
    indexed_observed_count = 0
    delete_candidates: list[tuple[int, str, dict]] = []
    for sp in observed.olt.olt_service_ports:
        idx = _sp_int(sp, "index")
        if idx is None or not isinstance(sp, dict):
            continue
        indexed_observed_count += 1
        if idx == desired.mgmt_service_port_index:
            if desired.mgmt_vlan is not None and _service_port_matches(
                sp,
                index=idx,
                vlan=desired.mgmt_vlan,
                gem_index=2,
                ont_id=ont_id,
                fsp=fsp,
            ):
                continue
            mismatched_at_index["mgmt"] = sp
            continue
        if idx == desired.wan_service_port_index:
            if desired.wan_vlan is not None and _service_port_matches(
                sp,
                index=idx,
                vlan=desired.wan_vlan,
                gem_index=desired.wan_gem_index or 1,
                ont_id=ont_id,
                fsp=fsp,
            ):
                continue
            mismatched_at_index["wan"] = sp
            continue
        if _matches_unindexed_desired_slot(sp):
            continue
        # Recover the slot from the observed VLAN. A stale port matches neither
        # desired index, so the desired state cannot name it; the VLAN is the
        # only evidence of what it was for. Unrecoverable stays "unknown",
        # which PPP delivery authorization treats as fail-closed.
        observed_vlan = _sp_int(sp, "vlan_id", "vlan")
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
        delete_candidates.append((int(idx), slot, sp))

    for slot, sp in mismatched_at_index.items():
        expected_vlan = desired.mgmt_vlan if slot == "mgmt" else desired.wan_vlan
        expected_gem = 2 if slot == "mgmt" else (desired.wan_gem_index or 1)
        drifts.append(
            Drift(
                field=f"olt_service_ports[{slot}]",
                surface="olt",
                desired={"vlan": expected_vlan, "gem": expected_gem},
                observed=sp,
                repairable=False,
            )
        )

    if (
        not desired_indices
        and indexed_observed_count > 0
        and len(delete_candidates) == indexed_observed_count
    ):
        # Neither slot has an allocated index, and nothing observed matches an
        # unindexed desired slot either — every observed port would be
        # planned for deletion with nothing left standing and nothing to
        # recreate them at. Refuse instead of deleting the ONT's entire
        # service-port set. See
        # ``ReconcileFailureReason.SERVICE_PORT_INDEX_UNALLOCATED``.
        for idx, slot, sp in delete_candidates:
            drifts.append(
                Drift(
                    field=f"olt_service_ports[{idx}]",
                    surface="olt",
                    desired=None,
                    observed=sp,
                    repairable=False,
                )
            )
        return ReconcileFailureReason.SERVICE_PORT_INDEX_UNALLOCATED

    # Narrower per-slot case: one slot carries a real, operator-supplied
    # index (so the desired state is meaningfully populated) but a specific
    # observed port's VLAN positively correlates to the OTHER, still-
    # unallocated slot, and nothing protects it (`_matches_unindexed_desired_
    # slot` only protects the WAN slot in PPPoE mode and the mgmt slot when
    # a VLAN is set — a non-PPPoE WAN slot is never protected regardless of
    # index). Deleting it would strand that slot: nothing recreates it (the
    # create branch below requires a real index) and nothing protected it.
    # Withhold just that candidate; a port unrelated to either slot (`slot ==
    # "unknown"`) is unaffected and still deleted below.
    slot_index_unallocated = False
    for idx, slot, sp in delete_candidates:
        if (slot == "mgmt" and desired.mgmt_service_port_index is None) or (
            slot == "wan" and desired.wan_service_port_index is None
        ):
            slot_index_unallocated = True
            drifts.append(
                Drift(
                    field=f"olt_service_ports[{idx}]",
                    surface="olt",
                    desired=None,
                    observed=sp,
                    repairable=False,
                )
            )
            continue
        actions.append(OltDeleteServicePort(service_port_index=idx, slot=slot))
        drifts.append(
            Drift(
                field=f"olt_service_ports[{idx}]",
                surface="olt",
                desired=None,
                observed=sp,
                repairable=True,
            )
        )

    # Create missing service-ports — but only when a real index is set AND
    # nothing (matched or mismatched) already occupies it. There is no
    # allocator: an index either arrives from operator input
    # (``desired.mgmt_service_port_index``/``wan_service_port_index``, see
    # ``adapters.py``) or stays ``None``. A ``None`` index here plans no
    # create action; the both-slots-unallocated case already returned above,
    # and the per-slot case leaves ``slot_index_unallocated`` set so the
    # caller still learns nothing was recreated for the withheld slot.
    observed_indices = {_sp_int(sp, "index") for sp in observed.olt.olt_service_ports}
    observed_indices.discard(None)
    if (
        desired.mgmt_service_port_index is not None
        and desired.mgmt_service_port_index not in observed_indices
        and desired.mgmt_vlan is not None
    ):
        _plan_service_port_create(
            desired,
            actions,
            drifts,
            verification_debt,
            slot="mgmt",
            index=desired.mgmt_service_port_index,
            vlan=desired.mgmt_vlan,
            gem_index=2,
        )
    _plan_wan_service_port(desired, observed, actions, drifts, verification_debt)
    return (
        ReconcileFailureReason.SERVICE_PORT_INDEX_UNALLOCATED
        if slot_index_unallocated
        else None
    )


def _plan_service_port_create(
    desired: OntDesiredState,
    actions: list[Action],
    drifts: list[Drift],
    verification_debt: list[Drift],
    *,
    slot: str,
    index: int,
    vlan: int,
    gem_index: int,
) -> None:
    """Emit an ``OltCreateServicePort`` plus its verification-debt ``Drift``.

    A create is not observable as drift the way a value mismatch is — the
    port simply doesn't exist yet, so there's nothing to diff. The debt entry
    is what lets ``reconcile_ont``'s post-apply verify pass detect a silent
    no-op: if the create didn't really take, the SAME comparison
    (``_service_port_matches``) fails again on the fresh read and this
    function runs again, reproducing the debt entry instead of a clean plan.
    """
    assert desired.olt_ont_id is not None  # guaranteed by every caller
    actions.append(
        OltCreateServicePort(
            fsp=desired.fsp,
            ont_id=desired.olt_ont_id,
            service_port_index=index,
            vlan=vlan,
            gem_index=gem_index,
            slot=slot,
        )
    )
    debt = Drift(
        field=f"olt_service_ports[{slot}]",
        surface="olt",
        desired={"index": index, "vlan": vlan, "gem": gem_index},
        observed=None,
        repairable=True,
    )
    drifts.append(debt)
    verification_debt.append(debt)


def _plan_wan_service_port(
    desired: OntDesiredState,
    observed: OntObservedState,
    actions: list[Action],
    drifts: list[Drift],
    verification_debt: list[Drift],
) -> None:
    """Create a missing WAN port without treating other ports as disposable."""
    if desired.olt_ont_id is None:
        # ``compute_plan`` never reaches this function while the identity
        # gate is open; this guard exists only to narrow the type for mypy
        # and as a second line of defense.
        return
    if observed.olt.olt_service_ports is None:
        # Reachable directly from ``compute_plan``'s ``wan_only_change``
        # branch, which never goes through ``_plan_service_ports``'s own
        # guard — see that function's identical check for why ``None`` must
        # never be treated as "no ports".
        drifts.append(
            Drift(
                field="olt_service_ports",
                surface="olt",
                desired="known",
                observed=None,
                repairable=False,
            )
        )
        return
    if (
        desired.wan_service_port_index is not None
        and desired.wan_vlan is not None
        and is_deliverable("wan_vlan", desired.wan_vlan)
        and desired.wan_mode == "pppoe"
    ):
        gem_index = desired.wan_gem_index or 1
        # A create is planned only when NOTHING occupies the index —
        # matched or mismatched. A mismatched occupant is a live customer
        # port and is flagged as unrepairable drift by the main per-port
        # loop in ``_plan_service_ports``, never auto delete+recreated here.
        occupied = any(
            _sp_int(sp, "index") == desired.wan_service_port_index
            for sp in observed.olt.olt_service_ports
        )
        if not occupied:
            _plan_service_port_create(
                desired,
                actions,
                drifts,
                verification_debt,
                slot="wan",
                index=desired.wan_service_port_index,
                vlan=desired.wan_vlan,
                gem_index=gem_index,
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
    if desired.olt_ont_id is None:
        # ``compute_plan`` never reaches this function while the identity
        # gate is open; this guard exists only to narrow the type for mypy
        # and as a second line of defense.
        return False
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
    if desired.olt_ont_id is None:
        # No OLT action could have been planned without a resolved identity
        # (every action-emitting function guards the same way), so there is
        # nothing here that could ``requires_reset``. Guard exists only to
        # narrow the type for mypy.
        return
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
    wan_only_change = _is_wan_only_change(mode, proposed_fields)

    # TR-069 WAN PPP — skipped when OMCI owns the WAN.
    narrow_feature_change = wifi_only_change or remote_only_change
    if (
        not narrow_feature_change
        and desired.wan_mode == "pppoe"
        and not omci_wan_planned
    ):
        _plan_acs_wan_ppp(
            desired,
            observed,
            device_id,
            actions,
            drifts,
            proposed_fields=proposed_fields,
            force_proposed_writes=force_proposed_writes,
        )

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
        force_wan_ip_write = force_proposed_writes and bool(
            proposed_fields
            & {
                "wan_mode",
                "wan_pppoe_wcd_index",
                "wan_static_ip",
                "wan_static_subnet",
                "wan_static_gateway",
                "wan_static_dns",
            }
        )
        if force_wan_ip_write or fresh_bring_up or _wan_ip_differs(desired, observed):
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
    if not (remote_only_change or wan_only_change):
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

    if not (wifi_only_change or wan_only_change):
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

    if wan_only_change:
        return identity

    # DHCP server — push the whole block when any field differs.
    force_dhcp_write = force_proposed_writes and bool(
        proposed_fields
        & {
            "dhcp_enabled",
            "dhcp_pool_min",
            "dhcp_pool_max",
            "dhcp_subnet_mask",
            "lan_gateway_ip",
        }
    )
    if force_dhcp_write or _dhcp_differs(desired, observed):
        actions.append(
            AcsSetDhcpServer(
                device_id=device_id,
                enabled=desired.dhcp_enabled,
                pool_min=desired.dhcp_pool_min,
                pool_max=desired.dhcp_pool_max,
                subnet_mask=desired.dhcp_subnet_mask,
                gateway_ip=desired.lan_gateway_ip,
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
    *,
    proposed_fields: frozenset[str],
    force_proposed_writes: bool,
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
    force_pppoe_write = force_proposed_writes and bool(
        proposed_fields
        & {
            "wan_mode",
            "wan_pppoe_username",
            "wan_pppoe_password_ref",
            "wan_pppoe_wcd_index",
        }
    )
    if (
        created
        or force_pppoe_write
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
    # When a firmware does expose these values, enforce them before falling
    # back to "not exposed" behavior.
    optional_readback_pairs = (
        (acs.acs_observed_dhcp_pool_min, desired.dhcp_pool_min),
        (acs.acs_observed_dhcp_pool_max, desired.dhcp_pool_max),
        (acs.acs_observed_dhcp_subnet_mask, desired.dhcp_subnet_mask),
        (acs.acs_observed_lan_gateway_ip, desired.lan_gateway_ip),
    )
    if any(
        observed_value is not None
        and desired_value is not None
        and _observed_differs(observed_value, desired_value)
        for observed_value, desired_value in optional_readback_pairs
    ):
        return True
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
        and mode in ("sync", "sweep")
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
