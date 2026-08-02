"""Reconciler state types.

The reconciler is the only subsystem in the codebase that writes to the OLT (SSH)
and the ACS (GenieACS NBI). Its truth lives across four surfaces:

  - ``OntDesiredState``  — operator/system intent, persisted on ``OntUnit``
                           (structural columns + the ``desired_config`` JSON blob).
  - ``OntObservedState`` — last-seen live values from OLT + ACS, persisted on a
                           1:1 ``OntObservation`` row (added by a follow-up
                           migration; this module declares only the in-memory
                           shape).
  - OLT                  — Huawei MA5608T / MA5800-X2, accessed via SSH.
  - ACS                  — GenieACS NBI, accessed via HTTP.

Reconciliation flows: desired vs observed → ordered Plan of OLT and ACS Actions
→ Apply → re-read → commit. Failure modes are enumerated in
``ReconcileFailureReason`` and surfaced via ``ReconcileResult.failure`` so the
sync HTTP path can translate them into specific 4xx/5xx responses.

This module holds dataclasses and string constants only. No I/O, no SQLAlchemy
bindings, no protocol code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

# ── Public string-literal types ─────────────────────────────────────────────

SyncStatus = Literal["synced", "reconciling", "out_of_sync"]
"""Per-ONT reconciler status, persisted on ``OntUnit.sync_status``.

* ``synced``         — last reconcile produced an empty drift list.
* ``reconciling``    — a reconcile is in flight. Held under SELECT FOR UPDATE
                       on the OntUnit row so concurrent reconciles serialize.
* ``out_of_sync``    — last reconcile failed to converge. Blocks ``mode=sync``
                       writes; sweep mode keeps retrying.
"""

WanMode = Literal["pppoe", "dhcp", "static", "bridge"]
PppoeProvisioningMethod = Literal["omci", "tr069", "auto"]
WriteSurface = Literal["olt", "acs"]
ObserveSurface = Literal["olt", "acs"]
ReconcileMode = Literal["sync", "sweep", "bootstrap"]


# ── Desired state ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Tr181WanParameterPaths:
    """Resolved model-specific CWMP paths for one TR-181 WAN instance."""

    ip_enable: str
    dhcp_enable: str
    ip_address: str
    subnet_mask: str
    gateway: str
    dns_primary: str
    dns_secondary: str
    nat_enable: str
    vlan_enable: str
    vlan_id: str


@dataclass(frozen=True)
class Tr069WifiParameterPaths:
    """Resolved model-specific CWMP paths for the managed WiFi instance."""

    enabled: str
    ssid: str
    psk_path: str
    channel: str
    security_mode: str


@dataclass(frozen=True)
class Tr069RemoteAccessParameterPaths:
    """Resolved model-specific CWMP paths for WAN support access."""

    ssh_enabled: str
    ssh_port: str
    telnet_enabled: str
    telnet_port: str


@dataclass(frozen=True)
class OntDesiredState:
    """What this ONT should be.

    Every field here is operator-mutable through ``reconcile_ont`` with a
    ``proposed_change`` dict. Mutation is durable only when the corresponding
    OLT/ACS write succeeds end-to-end. Fields marked "immutable" reject any
    proposed change after first creation (see ``validator.validate_desired``).

    Persistence: structural fields (``olt_id``, ``fsp``, ``olt_ont_id``, etc.)
    map to typed columns on ``OntUnit``; configuration knobs map to the
    ``OntUnit.desired_config`` JSON blob under the sections the existing model
    already uses (``management``, ``wan``, ``wifi``, ``dhcp``, ``acs``).
    """

    # Identity (immutable post-creation)
    ont_unit_id: str
    serial_number: str
    # NOTE: ``acs_device_id`` is declared with the other defaulted fields at the
    # bottom of this dataclass (dataclass ordering), but it is *identity*, not
    # configuration. See the field comment there.

    # OLT binding
    olt_id: str
    fsp: str
    olt_ont_id: int
    line_profile_id: int
    service_profile_id: int
    description: str

    # Management plane
    mgmt_vlan: int | None
    mgmt_ip: str | None
    mgmt_subnet_mask: str | None
    mgmt_gateway: str | None
    mgmt_dns_primary: str | None
    mgmt_dns_secondary: str | None
    mgmt_iphost_priority: int

    # TR-069 / ACS
    tr069_profile_id: int
    acs_server_id: str | None
    cr_username: str | None
    cr_password_ref: str | None
    periodic_inform_interval_sec: int

    # WAN
    wan_mode: WanMode
    wan_vlan: int | None
    wan_gem_index: int | None
    wan_pppoe_username: str | None
    wan_pppoe_password_ref: str | None
    wan_pppoe_provisioning_method: PppoeProvisioningMethod
    wan_pppoe_wcd_index: int
    wan_pppoe_instance_index: int
    wan_config_profile_id: int | None
    wan_internet_config_ip_index: int | None
    nat_enabled: bool
    ipv6_enabled: bool

    # LAN / DHCP
    dhcp_enabled: bool
    dhcp_pool_min: str
    dhcp_pool_max: str
    dhcp_subnet_mask: str

    # WiFi
    wifi_ssid: str
    wifi_password_ref: str
    wifi_password_pushed_at: datetime | None

    # Service-port indices — allocator outputs, immutable after first allocation.
    mgmt_service_port_index: int | None
    wan_service_port_index: int | None

    # Forward-compat fields for the in-app Subscriber/Service migration.
    # All nullable today; populated once in-app RADIUS lands.
    subscriber_external_id: str | None
    wan_uprate_kbps: int | None
    wan_downrate_kbps: int | None

    # Huawei models span TR-098 and TR-181. The root selects model-specific
    # ACS paths for settings such as IPv6.
    tr069_data_model_root: str | None = None
    wan_static_ip: str | None = None
    wan_static_subnet: str | None = None
    wan_static_gateway: str | None = None
    wan_static_dns: str | None = None
    wan_static_ip_is_public: bool | None = None
    tr181_wan_paths: Tr181WanParameterPaths | None = None
    acs_url: str | None = None
    acs_username: str | None = None
    acs_password_ref: str | None = None
    wifi_enabled: bool | None = None
    wifi_channel: int | None = None
    wifi_security_mode: str | None = None
    wifi_paths: Tr069WifiParameterPaths | None = None
    wan_remote_access_enabled: bool = False
    wan_remote_access_expires_at: datetime | None = None
    wan_remote_access_source_cidrs: tuple[str, ...] = ()
    wan_remote_access_ssh_port: int = 22
    remote_access_paths: Tr069RemoteAccessParameterPaths | None = None

    # ── ACS device identity ────────────────────────────────────────────────
    # The GenieACS ``_id`` (``{OUI}-{ProductClass}-{SerialNumber}``) of this
    # ONT's CPE, copied from the persisted ``Tr069CpeDevice`` row that the
    # TR-069 Inform handler owns. It is NEVER constructed from a guessed OUI or
    # ProductClass: the fleet runs several Huawei models (HG8546M, EG8145V5,
    # …) and a fabricated identifier is a permanent 404 against the NBI.
    #
    # ``None`` means "we do not know this device's ACS identity". The planner
    # treats that as a fail-closed wait-for-Inform condition and emits no ACS
    # actions at all — see ``planner.resolve_acs_device_id``.
    acs_device_id: str | None = None


# ── Observed state ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OltObservedFields:
    """OLT-side observations from ``display ont info / ipconfig / optical-info``.

    Sourced exclusively from SSH reads. Never authoritative for desired state.
    """

    olt_present: bool
    olt_match_state: Literal["match", "mismatch", "initial"] | None
    olt_run_state: Literal["online", "offline", "los"] | None
    olt_distance_m: int | None
    olt_rx_dbm: float | None
    olt_tx_dbm: float | None
    olt_temperature_c: int | None
    olt_description: str | None
    olt_mgmt_ip: str | None
    olt_mgmt_vlan: int | None
    olt_line_profile_id: int | None
    olt_service_profile_id: int | None
    # Each service-port entry: {index, vlan, gem, state}. Tuple to keep frozen.
    olt_service_ports: tuple[dict[str, Any], ...]
    olt_tr069_profile_id: int | None = None


@dataclass(frozen=True)
class AcsObservedFields:
    """ACS-side observations from the GenieACS CWMP cache.

    Captured at the device's last Inform — staleness is bounded by
    ``periodic_inform_interval_sec``. The reconciler trusts cache values without
    forcing a ``refreshObject``; ``VERIFICATION_MISMATCH`` catches the rare
    stale case post-write.

    ``acs_observed_cr_username`` and
    ``acs_observed_periodic_inform_interval_sec`` are value-verified from the
    CWMP cache. ``acs_observed_cr_password_set`` remains presence-only because
    the password itself is not safely readable.

    ``acs_present=False`` means the ACS has no document for this serial at all.
    Nothing can be delivered to such a device — every NBI write against it is a
    404 — so the planner emits no ACS actions and the reconcile reports
    ``ONT_NOT_INFORMING`` until the device's next Inform creates the document.
    """

    acs_present: bool
    acs_last_inform_at: datetime | None
    acs_last_boot_at: datetime | None
    acs_last_bootstrap_at: datetime | None
    acs_observed_software_version: str | None
    acs_observed_pppoe_username: str | None
    acs_observed_pppoe_enable: bool | None
    acs_observed_wan_vlan: int | None
    acs_observed_wan_external_ip: str | None
    acs_observed_wan_connection_status: str | None
    acs_observed_nat_enabled: bool | None
    acs_observed_dhcp_enabled: bool | None
    acs_observed_ssid: str | None
    acs_observed_periodic_inform_interval_sec: int | None
    acs_observed_cr_username: str | None
    acs_observed_cr_username_set: bool | None
    acs_observed_cr_password_set: bool | None
    acs_observed_wan_wcd_index: int | None
    acs_observed_wan_instance_index: int | None
    acs_observed_wan_ppp_locations: tuple[tuple[int, int], ...]
    acs_data_model_root: str | None = None
    acs_observed_ipv6_enabled: bool | None = None
    acs_observed_wan_ip_enable: bool | None = None
    acs_observed_wan_addressing_type: str | None = None
    acs_observed_wan_ip_address: str | None = None
    acs_observed_wan_subnet_mask: str | None = None
    acs_observed_wan_gateway: str | None = None
    acs_observed_wan_dns_servers: str | None = None
    acs_observed_dhcpv6_enabled: bool | None = None
    acs_observed_dhcpv6_request_prefixes: bool | None = None
    acs_observed_ra_enabled: bool | None = None
    acs_observed_url: str | None = None
    acs_observed_username: str | None = None
    acs_observed_password_set: bool | None = None
    acs_observed_wifi_enabled: bool | None = None
    acs_observed_wifi_channel: int | None = None
    acs_observed_wifi_security_mode: str | None = None
    acs_observed_wifi_instance_index: int | None = None
    acs_observed_remote_ssh_enabled: bool | None = None
    acs_observed_remote_ssh_port: int | None = None
    acs_observed_remote_telnet_enabled: bool | None = None
    acs_observed_remote_telnet_port: int | None = None
    # The ``_id`` GenieACS actually returned for this serial. This is an
    # observation of the external system, not a value we may invent: the reader
    # copies it verbatim from the matched device document.
    acs_observed_device_id: str | None = None
    # How many ACS devices matched the trailing-serial query. ``0`` means the
    # device has not informed; ``>1`` means the identity is ambiguous and no
    # write may be targeted at any of them.
    acs_observed_device_match_count: int = 0


@dataclass(frozen=True)
class OntObservedState:
    """Last-seen reality as of ``last_reconciled_at``.

    Sweeper and sync reconciles both write this. Never authoritative for further
    writes — the planner compares ``OntDesiredState`` to ``OntObservedState`` to
    decide what to push.

    ``consecutive_sweep_unreachable`` is incremented by the sweeper when the
    reachability fast-check fails; it drives alert escalation (no exponential
    backoff — sweep keeps trying, the alert is what surfaces persistence).
    """

    last_reconciled_at: datetime
    last_reconcile_duration_ms: int
    mgmt_ip_pingable: bool
    consecutive_sweep_unreachable: int
    olt: OltObservedFields
    acs: AcsObservedFields


# ── Diff and action records ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Drift:
    """A field whose desired value differs from what was observed.

    ``repairable=False`` flags fields the planner *knows* cannot be verified by
    a subsequent read (notably ``wifi_password`` on HG8546M, which is write-only
    in the firmware data model). Drift on unrepairable fields is recorded for
    visibility but does not by itself trigger an action.
    """

    field: str
    surface: ObserveSurface
    desired: Any
    observed: Any
    repairable: bool


@dataclass(frozen=True)
class AppliedAction:
    """One write the reconciler performed during an apply pass.

    ``old_value`` reflects the observed value at the start of the reconcile;
    ``new_value`` reflects the desired value at apply time. Captured for the
    audit log and for surfacing in the per-ONT reconcile history view.
    """

    field: str
    surface: WriteSurface
    old_value: Any
    new_value: Any
    duration_ms: int
    evidence: dict[str, Any] | None = None


# ── Failure reasons ─────────────────────────────────────────────────────────


class ReconcileFailureReason:
    """String constants enumerating why a reconcile failed.

    Stored verbatim on ``OntUnit.last_error`` and emitted in
    ``ReconcileResult.failure.reason``. The sync HTTP handler maps these to
    specific 4xx/5xx responses so operators see the actual failure mode rather
    than a generic ``500``.

    Reachability failures (``*_UNREACHABLE``, ``ONT_OFFLINE``) are detected
    before any write is attempted — they imply the DB is unchanged.

    ``ONT_NOT_INFORMING`` and ``ACS_IDENTITY_UNRESOLVED`` are ACS *delivery*
    preconditions: the ONT's OLT-side plan may still have been applied, but no
    ACS write was attempted because the device has no ACS document, or because
    its GenieACS ``_id`` is unknown/ambiguous. Both are wait-for-Inform states,
    not device faults — the sweeper retries and they clear on their own once the
    CPE informs. Neither is ever "resolved" by inventing a device identifier.

    Apply-time failures (``OLT_WRITE_REJECTED``, ``ACS_WRITE_FAULTED``,
    ``ACS_CR_FAILED``, ``VERIFICATION_MISMATCH``) imply zero or more actions
    were applied before the failure; the ``ReconcileResult.actions_applied``
    list describes which ones.
    """

    # Preflight / reachability — no writes attempted
    OLT_UNREACHABLE = "olt_unreachable"
    ACS_UNREACHABLE = "acs_unreachable"
    ONT_OFFLINE = "ont_offline"
    ONT_NOT_INFORMING = "ont_not_informing"
    ACS_IDENTITY_UNRESOLVED = "acs_identity_unresolved"
    BLOCKED_OUT_OF_SYNC = "blocked_out_of_sync"
    INVALID_CHANGE = "invalid_change"

    # Apply time — writes may have partially completed
    OLT_WRITE_REJECTED = "olt_write_rejected"
    ACS_WRITE_FAULTED = "acs_write_faulted"
    ACS_CR_FAILED = "acs_cr_failed"
    VERIFICATION_MISMATCH = "verification_mismatch"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ReconcileFailure:
    """Why a reconcile failed.

    ``reason`` is one of the ``ReconcileFailureReason`` constants. ``message``
    is human-readable detail safe to surface in operator UI / API responses
    (does not contain secrets).
    """

    reason: str
    message: str
    evidence: dict[str, Any] | None = None


# ── Final reconcile result ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of a single reconcile pass.

    On success: ``actions_applied`` describes what was written, ``observed_after``
    is the fresh ``OntObservedState`` the caller persists, ``failure`` is None,
    and ``sync_status`` is ``synced`` -- UNLESS the pass withheld work by
    policy.

    Success with residual drift. PPP actions refused by delivery authorization
    are residual drift, so such a pass succeeds (nothing was rejected, and
    everything unrelated converged) while reporting ``sync_status ==
    "out_of_sync"`` with ``failure`` None and ``drift_after`` naming what was
    withheld. ``sync_status`` mirrors the persisted ``OntUnit.sync_status``
    rather than being re-derived from ``success``; the two must not disagree.

    Distinguishing this from a hard failure is ``OntUnit.last_error``: real
    failures record one, policy-withheld drift does not. The ``mode="sync"``
    guard keys off that, so a device whose PPP is deliberately blocked still
    accepts ordinary management, Wi-Fi and LAN convergence on later passes.

    On failure: ``actions_applied`` may be non-empty (partial — the reconciler
    completed some writes before the failing one). ``observed_after`` may be
    ``None`` if the post-apply re-read failed. ``sync_status`` is
    ``out_of_sync``. ``failure`` describes what went wrong and is surfaced to
    the operator.

    ``drift_before`` is computed from observed vs desired at the start; useful
    for the per-ONT reconcile history. ``drift_after`` is empty on success;
    on ``VERIFICATION_MISMATCH`` it lists the fields that still diverged after
    apply.
    """

    success: bool
    sync_status: SyncStatus
    actions_applied: tuple[AppliedAction, ...]
    drift_before: tuple[Drift, ...]
    drift_after: tuple[Drift, ...]
    observed_after: OntObservedState | None
    failure: ReconcileFailure | None
    duration_ms: int
    reconciled_at: datetime
