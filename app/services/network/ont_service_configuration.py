"""Lifecycle-safe ONT customer-service configuration coordinator.

Owner: ``network.ont_service_configuration``. HTTP and task modules are
adapters around the typed commands in this module.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeAlias

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.network import (
    CPEDevice,
    OLTDevice,
    OntAssignment,
    OntAuthorizationStatus,
    OntProvisioningEvent,
    OntUnit,
    OntWanServiceInstance,
    OntWanServiceLifecycle,
    PonPort,
    WanConnectionType,
)
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationType,
)
from app.models.ont_observation import OntObservation
from app.models.ont_service_configuration import (
    OntServiceConfigurationHead,
    OntServiceConfigurationPhase,
    OntServiceConfigurationRevision,
)
from app.models.tr069 import Tr069CpeDevice
from app.services.audit_adapter import stage_audit_event
from app.services.credential_crypto import encrypt_credential, get_encryption_key
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.ont_desired_config import set_desired_config_values
from app.services.network.ont_lan_block_choices import OPERATOR_LAN_BLOCK_PREFIXES
from app.services.network.ont_management_ipam import (
    allocate_ont_management_ip,
    release_ont_management_ip,
)
from app.services.network.ont_provisioning.result import StepResult
from app.services.network.ont_wan_service_intent import (
    WanServiceIntentSpec,
    active_primary_internet_intent,
    ensure_active_wan_service_intent_in_transaction,
)
from app.services.network.provisioning_events import (
    ProvisioningLifecycleIdentity,
    record_ont_provisioning_event,
)
from app.services.network.subscriber_wan_ipam import ensure_wan_static_ip_available
from app.services.network_catalog_ip_block_bridge import IpBlockPrefix
from app.services.network_operation_dispatch import (
    NetworkOperationCommand,
    stage_dispatch,
)
from app.services.network_operations import (
    StartOntServiceConfigurationOperation,
    StartOntServiceConfigurationVerificationOperation,
    network_operations,
    start_ont_service_configuration_operation,
    start_ont_service_configuration_verification_operation,
)
from app.services.network_subscriber_bridge import (
    AssignmentSubscriptionSnapshot,
    assignment_subscription_snapshot,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

if TYPE_CHECKING:
    from app.services.network.reconcile.state import (
        OntWifiDeliveryScope,
        ReconcileResult,
    )

OWNER = "network.ont_service_configuration"
_COORDINATION_CONCERN = "atomic ONT service configuration coordination"
_REPAIR_CONCERN = "reviewed ONT configuration lifecycle drift repair"

_CONFIGURE = OwnerCommandDefinition(
    owner=OWNER,
    concern=_COORDINATION_CONCERN,
    name="configure_ont_service",
)
_CONFIGURE_CUSTOMER_WIFI = OwnerCommandDefinition(
    owner=OWNER,
    concern=_COORDINATION_CONCERN,
    name="configure_customer_wifi",
)
_CONFIGURE_CPE_WIFI = OwnerCommandDefinition(
    owner=OWNER,
    concern=_COORDINATION_CONCERN,
    name="configure_cpe_wifi",
)
_EXECUTE = OwnerCommandDefinition(
    owner=OWNER,
    concern=_COORDINATION_CONCERN,
    name="execute_ont_service_configuration",
)
_RETRY = OwnerCommandDefinition(
    owner=OWNER,
    concern=_COORDINATION_CONCERN,
    name="retry_ont_service_configuration",
)
_VERIFY = OwnerCommandDefinition(
    owner=OWNER,
    concern=_COORDINATION_CONCERN,
    name="verify_ont_service_configuration_readback",
)
_REPAIR = OwnerCommandDefinition(
    owner=OWNER,
    concern=_REPAIR_CONCERN,
    name="repair_ont_service_configuration_drift",
)

_MAX_IDEMPOTENCY_LENGTH = 160
_MAX_READBACK_ATTEMPTS = 3
SERVICE_CONFIGURATION_RECONCILE_TIMEOUT_SECONDS = 120


class OntConfigurationSection(StrEnum):
    wan = "wan"
    lan = "lan"
    wifi = "wifi"
    management = "management"
    advanced = "advanced"


class WanVlanSource(StrEnum):
    config_pack = "config_pack"
    service_intent = "service_intent"
    reviewed_override = "reviewed_override"


class OntConfigurationNextAction(StrEnum):
    wait = "wait"
    retry_current_configuration = "retry_current_configuration"
    submit_configuration = "submit_configuration"
    none = "none"


@dataclass(frozen=True, slots=True)
class WanConfigurationChange:
    mode: str | None
    ip_protocol: str | None
    static_ip: str | None
    static_subnet: str | None
    static_gateway: str | None
    static_dns: str | None
    pppoe_wcd_index: int | None = None
    wan_service_port_index: int | None = None


@dataclass(frozen=True, slots=True)
class LanConfigurationChange:
    gateway_ip: str | None
    block_prefix: IpBlockPrefix | None
    dhcp_enabled: bool
    dhcp_start: str | None
    dhcp_end: str | None


@dataclass(frozen=True, slots=True)
class WifiConfigurationChange:
    enabled: bool
    ssid: str | None
    channel: str | None
    security_mode: str | None
    password: str | None = None


@dataclass(frozen=True, slots=True)
class CustomerWifiConfigurationChange:
    ssid: str
    password: str | None = None


@dataclass(frozen=True, slots=True)
class CpeWifiConfigurationChange:
    """Sparse CPE-detail WiFi patch applied under the owner's row locks."""

    ssid: str | None = None
    password: str | None = None


@dataclass(frozen=True, slots=True)
class ManagementConfigurationChange:
    ip_mode: str | None
    ip_address: str | None
    remote_access: bool
    wcd_index: int | None = None


@dataclass(frozen=True, slots=True)
class AdvancedConfigurationChange:
    pppoe_wcd_index: int | None = None
    management_wcd_index: int | None = None
    voip_wcd_index: int | None = None
    management_service_port_index: int | None = None
    wan_service_port_index: int | None = None


OntConfigurationChange: TypeAlias = (
    WanConfigurationChange
    | LanConfigurationChange
    | WifiConfigurationChange
    | ManagementConfigurationChange
    | AdvancedConfigurationChange
)


@dataclass(frozen=True, slots=True)
class ConfigureOntServiceCommand:
    context: CommandContext
    ont_unit_id: uuid.UUID
    permission_granted: bool
    section: OntConfigurationSection
    change: OntConfigurationChange


@dataclass(frozen=True, slots=True)
class ConfigureCustomerWifiCommand:
    context: CommandContext
    subscriber_id: uuid.UUID
    subscription_id: uuid.UUID
    change: CustomerWifiConfigurationChange


@dataclass(frozen=True, slots=True)
class ConfigureCpeWifiCommand:
    context: CommandContext
    cpe_device_id: uuid.UUID
    expected_tr069_device_id: uuid.UUID
    expected_ont_unit_id: uuid.UUID
    expected_assignment_id: uuid.UUID
    permission_granted: bool
    object_scope_granted: bool
    change: CpeWifiConfigurationChange


@dataclass(frozen=True, slots=True)
class ConfigureOntServiceOutcome:
    ont_unit_id: uuid.UUID
    assignment_id: uuid.UUID
    configuration_head_id: uuid.UUID
    revision: int
    operation_id: uuid.UUID
    phase: OntServiceConfigurationPhase
    replayed: bool
    message: str


@dataclass(frozen=True, slots=True)
class RetryOntServiceConfigurationCommand:
    context: CommandContext
    ont_unit_id: uuid.UUID
    expected_head_id: uuid.UUID
    expected_revision: int


@dataclass(frozen=True, slots=True)
class VerifyOntServiceConfigurationReadbackCommand:
    """Reconcile a terminally failed delivery record against live reality.

    Unlike ``RetryOntServiceConfigurationCommand`` (re-sends the write), this
    never re-issues ``setParameterValues``/an OLT write. It admits a
    readback-only re-attempt of exactly the referenced failed operation,
    linked to it via ``NetworkOperation.redrive_of_id`` — the original
    failure record is never overwritten or deleted.
    """

    context: CommandContext
    ont_unit_id: uuid.UUID
    expected_head_id: uuid.UUID
    expected_revision: int
    failed_operation_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ExecuteOntServiceConfigurationCommand:
    context: CommandContext
    ont_unit_id: uuid.UUID
    operation_id: uuid.UUID
    configuration_head_id: uuid.UUID
    revision: int
    verification_attempt: int = 0
    explicit_repair: bool = False
    # Set only by the readback-verification worker task. When True the
    # underlying ``reconcile_ont`` call is unconditionally readback-only,
    # regardless of ``verification_attempt`` — see ``_execution_locked``.
    force_readback_only: bool = False


@dataclass(frozen=True, slots=True)
class ExecuteOntServiceConfigurationOutcome:
    operation_id: uuid.UUID
    phase: OntServiceConfigurationPhase
    executed: bool
    stale: bool
    message: str


@dataclass(frozen=True, slots=True)
class ConfigurationEventView:
    event_id: uuid.UUID
    status: str
    step_name: str
    message: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class OntServiceConfigurationProjection:
    ont_unit_id: uuid.UUID
    assignment_id: uuid.UUID | None
    configuration_head_id: uuid.UUID | None
    revision: int | None
    section: OntConfigurationSection | None
    operation_id: uuid.UUID | None
    phase: OntServiceConfigurationPhase | None
    waiting_reason: str | None
    failure_code: str | None
    failure_message: str | None
    last_verified_at: datetime | None
    last_observation_at: datetime | None
    effective_customer_vlan: int | None
    vlan_source: WanVlanSource | None
    masked_pppoe_username: str | None
    pppoe_provenance: str | None
    next_action: OntConfigurationNextAction
    current_events: tuple[ConfigurationEventView, ...]
    historical_events: tuple[ConfigurationEventView, ...]


@dataclass(frozen=True, slots=True)
class OntServiceConfigurationEligibility:
    """Owner-backed capability disclosure for the admin Configure tab."""

    routed_wan_configurable: bool
    bridge_mode_configurable: bool
    nat_toggle_configurable: bool
    nat_default_enabled: bool
    lan_dhcp_configurable: bool
    retain_config_on_move_supported: bool
    routed_wan_message: str
    bridge_mode_message: str
    nat_message: str
    lan_dhcp_message: str
    move_message: str


@dataclass(frozen=True, slots=True)
class OntConfigurationSectionDeliveryProjection:
    ont_unit_id: uuid.UUID
    assignment_id: uuid.UUID
    section: OntConfigurationSection
    revision: int
    operation_id: uuid.UUID
    phase: OntServiceConfigurationPhase
    failure_code: str | None
    failure_message: str | None


@dataclass(frozen=True, slots=True)
class RepairOntServiceConfigurationDriftCommand:
    context: CommandContext
    ont_unit_ids: tuple[uuid.UUID, ...]
    reviewed_evidence: str


@dataclass(frozen=True, slots=True)
class OntServiceConfigurationDrift:
    ont_unit_id: uuid.UUID
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepairOntServiceConfigurationDriftOutcome:
    examined: int
    repaired: int
    findings: tuple[OntServiceConfigurationDrift, ...]


class OntServiceConfigurationError(DomainError):
    pass


def _error(
    suffix: str, message: str, **details: object
) -> OntServiceConfigurationError:
    return OntServiceConfigurationError(
        code=f"{OWNER}.{suffix}", message=message, details=details
    )


def _require_idempotency(context: CommandContext) -> str:
    value = str(context.idempotency_key or "").strip()
    if not value:
        raise _error(
            "idempotency_required", "Configuration requires an idempotency key."
        )
    if len(value) > _MAX_IDEMPOTENCY_LENGTH:
        raise _error("invalid_idempotency_key", "Idempotency key is too long.")
    return value


def _json_default(value: object) -> object:
    if isinstance(value, (uuid.UUID, StrEnum)):
        return str(value)
    raise TypeError(f"Unsupported fingerprint value: {type(value).__name__}")


def _command_fingerprint(
    command: (
        ConfigureOntServiceCommand
        | ConfigureCustomerWifiCommand
        | ConfigureCpeWifiCommand
    ),
) -> str:
    key = get_encryption_key()
    if key is None:
        raise _error(
            "fingerprint_key_unavailable",
            "Credential encryption key is required for safe request fingerprinting.",
        )
    section = (
        command.section
        if isinstance(command, ConfigureOntServiceCommand)
        else OntConfigurationSection.wifi
    )
    if isinstance(command, ConfigureOntServiceCommand):
        target: object = command.ont_unit_id
    elif isinstance(command, ConfigureCustomerWifiCommand):
        target = {
            "subscriber_id": command.subscriber_id,
            "subscription_id": command.subscription_id,
        }
    else:
        target = {
            "cpe_device_id": command.cpe_device_id,
            "tr069_device_id": command.expected_tr069_device_id,
            "ont_unit_id": command.expected_ont_unit_id,
            "assignment_id": command.expected_assignment_id,
        }
    material = json.dumps(
        {
            "target": target,
            "section": section,
            "change": asdict(command.change),
        },
        default=_json_default,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(
        key, b"ont-service-configuration-v1\0" + material, hashlib.sha256
    ).hexdigest()


def _text(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _positive_override(value: int | None, field: str) -> int | None:
    if value is None or value == 0:
        return None
    if value < 0:
        raise _error("invalid_change", f"{field} cannot be negative.", field=field)
    return int(value)


def _ip(value: str | None, field: str) -> str | None:
    normalized = _text(value)
    if normalized is None:
        return None
    try:
        return str(ipaddress.ip_address(normalized))
    except ValueError as exc:
        raise _error(
            "invalid_change", f"{field} is not a valid IP address.", field=field
        ) from exc


def _change_updates(
    db: Session,
    ont: OntUnit,
    _subscriber_id: uuid.UUID | None,
    section: OntConfigurationSection,
    change: OntConfigurationChange,
) -> tuple[dict[str, object], dict[str, object]]:
    updates: dict[str, object] = {}
    evidence: dict[str, object] = {}
    if section is OntConfigurationSection.wan and isinstance(
        change, WanConfigurationChange
    ):
        mode = _text(change.mode)
        if mode not in {None, "pppoe", "dhcp", "static_ip"}:
            raise _error(
                "invalid_change",
                "The Configure UI supports routed PPPoE, DHCP, or static WAN only.",
            )
        updates = {
            "wan.mode": mode,
            "wan.ip_protocol": _text(change.ip_protocol),
            "wan.static_ip": ensure_wan_static_ip_available(
                db, ont=ont, requested_ip=_text(change.static_ip)
            )
            if change.static_ip
            else None,
            "wan.static_subnet": _text(change.static_subnet),
            "wan.static_gateway": _ip(change.static_gateway, "static_gateway"),
            "wan.static_dns": _text(change.static_dns),
            "wan.pppoe_wcd_index": _positive_override(
                change.pppoe_wcd_index, "pppoe_wcd_index"
            ),
            "olt.wan_service_port_index": _positive_override(
                change.wan_service_port_index, "wan_service_port_index"
            ),
        }
        evidence = {key: value for key, value in updates.items()}
    elif section is OntConfigurationSection.lan and isinstance(
        change, LanConfigurationChange
    ):
        if (
            change.block_prefix is None
            or change.block_prefix not in OPERATOR_LAN_BLOCK_PREFIXES
        ):
            raise _error(
                "invalid_lan_block_prefix",
                "Select a supported LAN block size.",
                block_prefix=(
                    change.block_prefix.value if change.block_prefix else None
                ),
            )
        gateway = _ip(change.gateway_ip, "lan_gateway_ip")
        dhcp_start = _ip(change.dhcp_start, "lan_dhcp_start")
        dhcp_end = _ip(change.dhcp_end, "lan_dhcp_end")
        if gateway is None:
            raise _error(
                "lan_address_required",
                "The ONT LAN address is required for the selected IP block.",
            )
        if change.dhcp_enabled:
            if dhcp_start is None or dhcp_end is None:
                raise _error(
                    "invalid_dhcp_pool",
                    "Gateway, DHCP start, and DHCP end are required when DHCP "
                    "is enabled.",
                )
            network = ipaddress.IPv4Network(
                f"{gateway}/{change.block_prefix.prefix_length}", strict=False
            )
            start_address = ipaddress.IPv4Address(dhcp_start)
            end_address = ipaddress.IPv4Address(dhcp_end)
            gateway_address = ipaddress.IPv4Address(gateway)
            unavailable = {
                network.network_address,
                network.broadcast_address,
                gateway_address,
            }
            if (
                start_address not in network
                or end_address not in network
                or start_address in unavailable
                or end_address in unavailable
                or int(start_address) > int(end_address)
                or any(
                    int(start_address) <= int(item) <= int(end_address)
                    for item in unavailable
                )
            ):
                raise _error(
                    "invalid_dhcp_pool",
                    "The DHCP range must fit inside the selected block and exclude "
                    "network, broadcast, and gateway addresses.",
                )
        updates = {
            "lan.ip": gateway,
            "lan.subnet": change.block_prefix.subnet_mask,
            "lan.block_prefix": change.block_prefix.value,
            "lan.dhcp_enabled": bool(change.dhcp_enabled),
            "lan.dhcp_start": dhcp_start,
            "lan.dhcp_end": dhcp_end,
        }
        evidence = dict(updates)
    elif section is OntConfigurationSection.wifi and isinstance(
        change, WifiConfigurationChange
    ):
        updates = {
            "wifi.enabled": bool(change.enabled),
            "wifi.ssid": _text(change.ssid),
            "wifi.channel": _text(change.channel),
            "wifi.security_mode": _text(change.security_mode),
        }
        evidence = dict(updates)
        if change.password:
            updates["wifi.password"] = encrypt_credential(change.password)
            evidence["wifi.password"] = "changed"
    elif section is OntConfigurationSection.management and isinstance(
        change, ManagementConfigurationChange
    ):
        mode = _text(change.ip_mode)
        if mode not in {None, "inactive", "dhcp", "static_ip"}:
            raise _error("invalid_change", "Management IP mode is invalid.")
        address = _ip(change.ip_address, "management_ip_address")
        if mode == "static_ip":
            allocation = allocate_ont_management_ip(db, ont=ont, requested_ip=address)
            address = allocation.address
            updates.update(
                {
                    "management.subnet": allocation.subnet,
                    "management.gateway": allocation.gateway,
                }
            )
        elif mode in {"inactive", "dhcp"}:
            release_ont_management_ip(db, ont=ont, mode=mode)
            updates.update({"management.subnet": None, "management.gateway": None})
        updates.update(
            {
                "management.ip_mode": mode,
                "management.ip_address": address,
                "access.mgmt_remote": bool(change.remote_access),
                "management.wcd_index": _positive_override(
                    change.wcd_index, "management_wcd_index"
                ),
            }
        )
        evidence = dict(updates)
    elif section is OntConfigurationSection.advanced and isinstance(
        change, AdvancedConfigurationChange
    ):
        updates = {
            "wan.pppoe_wcd_index": _positive_override(
                change.pppoe_wcd_index, "pppoe_wcd_index"
            ),
            "management.wcd_index": _positive_override(
                change.management_wcd_index, "management_wcd_index"
            ),
            "voip.wcd_index": _positive_override(
                change.voip_wcd_index, "voip_wcd_index"
            ),
            "olt.mgmt_service_port_index": _positive_override(
                change.management_service_port_index, "management_service_port_index"
            ),
            "olt.wan_service_port_index": _positive_override(
                change.wan_service_port_index, "wan_service_port_index"
            ),
        }
        evidence = dict(updates)
    else:
        raise _error(
            "section_mismatch", "Configuration section and typed change do not match."
        )
    return updates, evidence


def _customer_wifi_change_updates(
    change: CustomerWifiConfigurationChange,
) -> tuple[dict[str, object], dict[str, object]]:
    ssid = change.ssid.strip()
    password = change.password.strip() if change.password else None
    if not 1 <= len(ssid) <= 32:
        raise _error("invalid_change", "WiFi name must be 1-32 characters.")
    if password is not None and not 8 <= len(password) <= 63:
        raise _error("invalid_change", "WiFi password must be 8-63 characters.")
    updates: dict[str, object] = {"wifi.ssid": ssid}
    evidence: dict[str, object] = {"wifi.ssid": ssid}
    if password is not None:
        updates["wifi.password"] = encrypt_credential(password)
        evidence["wifi.password"] = "changed"
    return updates, evidence


def _cpe_wifi_change_updates(
    change: CpeWifiConfigurationChange,
) -> tuple[dict[str, object], dict[str, object]]:
    """Validate one sparse CPE-detail field without snapshotting its siblings."""
    if (change.ssid is None) == (change.password is None):
        raise _error(
            "invalid_change",
            "A CPE WiFi command must change exactly one of name or password.",
        )
    updates: dict[str, object] = {}
    evidence: dict[str, object] = {}
    if change.ssid is not None:
        ssid = change.ssid.strip()
        if not 1 <= len(ssid) <= 32:
            raise _error("invalid_change", "WiFi name must be 1-32 characters.")
        updates["wifi.ssid"] = ssid
        evidence["wifi.ssid"] = ssid
    if change.password is not None:
        password = change.password.strip()
        if not 8 <= len(password) <= 63:
            raise _error("invalid_change", "WiFi password must be 8-63 characters.")
        updates["wifi.password"] = encrypt_credential(password)
        evidence["wifi.password"] = "changed"
    return updates, evidence


def _load_admission_scope(
    db: Session,
    *,
    context: CommandContext,
    ont_unit_id: uuid.UUID,
    permission_granted: bool,
) -> tuple[
    OntUnit,
    OntAssignment,
    AssignmentSubscriptionSnapshot,
    OLTDevice,
    PonPort,
]:
    if context.scope != "network:ont:write" or not permission_granted:
        raise _error(
            "permission_denied", "ONT service configuration requires network:ont:write."
        )
    ont = db.scalar(select(OntUnit).where(OntUnit.id == ont_unit_id).with_for_update())
    if ont is None:
        raise _error("ont_not_found", "ONT was not found.")
    assignments = list(
        db.scalars(
            select(OntAssignment)
            .where(OntAssignment.ont_unit_id == ont.id, OntAssignment.active.is_(True))
            .order_by(OntAssignment.id)
            .with_for_update()
        )
    )
    if len(assignments) != 1:
        raise _error(
            "active_assignment_required" if not assignments else "ambiguous_assignment",
            "ONT configuration requires one exact active assignment.",
        )
    assignment = assignments[0]
    if (
        assignment.subscription_id is None
        or assignment.subscriber_id is None
        or assignment.pon_port_id is None
    ):
        raise _error(
            "assignment_incomplete",
            "The active assignment lacks exact service or PON identity.",
        )
    subscription = assignment_subscription_snapshot(
        db, assignment.subscription_id, for_update=True
    )
    if subscription is None:
        raise _error(
            "subscription_missing",
            "The assigned subscription record could not be resolved.",
        )
    # Staff-initiated configuration is desired-state staging, not service
    # delivery: it deliberately does not gate on subscription lifecycle
    # status (active/suspended/blocked/...). Delivery authorization is a
    # separate concern owned by radius_access_state.py (PPPoE RADIUS
    # access) and ppp_delivery_authorization.py (PPP delivery
    # authorization) — those refuse actual service delivery for a
    # non-serviceable subscription independently of this admission check.
    if subscription.subscriber_id != assignment.subscriber_id:
        raise _error(
            "assignment_identity_conflict",
            "Assignment and subscription customer identity disagree.",
        )
    pon = db.scalar(
        select(PonPort).where(PonPort.id == assignment.pon_port_id).with_for_update()
    )
    if pon is None or not pon.is_active:
        raise _error("pon_not_ready", "The assignment PON is missing or inactive.")
    olt = db.scalar(
        select(OLTDevice).where(OLTDevice.id == pon.olt_id).with_for_update()
    )
    if olt is None or not olt.is_active:
        raise _error("olt_not_ready", "The assignment OLT is missing or inactive.")
    if ont.olt_device_id != olt.id or ont.pon_port_id != pon.id:
        raise _error(
            "assignment_topology_conflict",
            "ONT inventory and assignment PON identity disagree.",
        )
    if ont.authorization_status is not OntAuthorizationStatus.authorized:
        raise _error(
            "commissioning_not_ready", "ONT management commissioning is not ready."
        )
    return ont, assignment, subscription, olt, pon


def _load_customer_wifi_admission_scope(
    db: Session, command: ConfigureCustomerWifiCommand
) -> tuple[
    OntUnit,
    OntAssignment,
    AssignmentSubscriptionSnapshot,
    OLTDevice,
    PonPort,
]:
    if command.context.scope != "customer:device:wifi":
        raise _error(
            "customer_scope_denied",
            "Customer WiFi configuration requires customer device scope.",
        )
    candidates = list(
        db.scalars(
            select(OntAssignment)
            .where(
                OntAssignment.subscriber_id == command.subscriber_id,
                OntAssignment.subscription_id == command.subscription_id,
                OntAssignment.active.is_(True),
            )
            .order_by(OntAssignment.id)
        )
    )
    if not candidates:
        raise _error(
            "customer_active_assignment_required",
            "No supported active device is linked to this service.",
            subscriber_id=str(command.subscriber_id),
            subscription_id=str(command.subscription_id),
            candidate_count=0,
        )
    if len(candidates) > 1:
        raise _error(
            "customer_ambiguous_assignment",
            "Multiple active devices are linked to this service; contact support.",
            subscriber_id=str(command.subscriber_id),
            subscription_id=str(command.subscription_id),
            candidate_count=len(candidates),
            candidate_assignment_ids=[str(item.id) for item in candidates],
        )
    candidate = candidates[0]
    ont = db.scalar(
        select(OntUnit).where(OntUnit.id == candidate.ont_unit_id).with_for_update()
    )
    if ont is None:
        raise _error(
            "customer_assigned_ont_missing",
            "The device linked to this service could not be found.",
            subscriber_id=str(command.subscriber_id),
            subscription_id=str(command.subscription_id),
            assignment_id=str(candidate.id),
            ont_unit_id=str(candidate.ont_unit_id),
        )
    assignments = list(
        db.scalars(
            select(OntAssignment)
            .where(
                OntAssignment.ont_unit_id == ont.id,
                OntAssignment.active.is_(True),
            )
            .order_by(OntAssignment.id)
            .with_for_update()
        )
    )
    if (
        len(assignments) != 1
        or assignments[0].id != candidate.id
        or assignments[0].subscriber_id != command.subscriber_id
        or assignments[0].subscription_id != command.subscription_id
    ):
        raise _error(
            "customer_assignment_inconsistent",
            "The device's active assignment disagrees with this service's "
            "assignment; contact support.",
            subscriber_id=str(command.subscriber_id),
            subscription_id=str(command.subscription_id),
            candidate_assignment_id=str(candidate.id),
            ont_unit_id=str(ont.id),
            ont_active_assignment_ids=[str(item.id) for item in assignments],
        )
    assignment = assignments[0]
    if assignment.pon_port_id is None:
        raise _error(
            "assignment_incomplete",
            "The active assignment lacks exact service or PON identity.",
        )
    subscription = assignment_subscription_snapshot(
        db, command.subscription_id, for_update=True
    )
    if (
        subscription is None
        or subscription.status != "active"
        or subscription.subscriber_id != command.subscriber_id
    ):
        raise _error(
            "customer_subscription_not_found",
            "No supported active device is linked to this service.",
            subscriber_id=str(command.subscriber_id),
            subscription_id=str(command.subscription_id),
            subscription_status=(
                subscription.status if subscription is not None else None
            ),
            subscription_subscriber_id=(
                str(subscription.subscriber_id) if subscription is not None else None
            ),
        )
    pon = db.scalar(
        select(PonPort).where(PonPort.id == assignment.pon_port_id).with_for_update()
    )
    if pon is None or not pon.is_active:
        raise _error("pon_not_ready", "The assignment PON is missing or inactive.")
    olt = db.scalar(
        select(OLTDevice).where(OLTDevice.id == pon.olt_id).with_for_update()
    )
    if olt is None or not olt.is_active:
        raise _error("olt_not_ready", "The assignment OLT is missing or inactive.")
    if ont.olt_device_id != olt.id or ont.pon_port_id != pon.id:
        raise _error(
            "assignment_topology_conflict",
            "ONT inventory and assignment PON identity disagree.",
        )
    if ont.authorization_status is not OntAuthorizationStatus.authorized:
        raise _error(
            "commissioning_not_ready", "ONT management commissioning is not ready."
        )
    if ont.uisp_device_id:
        raise _error(
            "customer_device_unsupported",
            "Self-service device commands are not supported for this device.",
        )
    return ont, assignment, subscription, olt, pon


@dataclass(frozen=True, slots=True)
class CpeWifiAdmissionScope:
    """The exact, unambiguous ONT this admin CPE-detail WiFi action may touch.

    Resolved by ``resolve_cpe_wifi_admission_scope`` from the CPE side of the
    TR-069 identity chain (``cpe_device_id`` -> ``Tr069CpeDevice`` ->
    ``OntUnit`` -> active ``OntAssignment``) so the admin CPE-detail WiFi
    routes can delegate to ``configure_cpe_wifi`` — a typed owner-command
    path beside the customer self-care WiFi flow — instead of writing to
    GenieACS directly. This never falls back to serial-number matching or a
    default ACS server (``resolve_genieacs_for_cpe_with_reason`` tiers 2/3):
    an admin mutation targets one exact device or it is refused.
    """

    cpe_device_id: uuid.UUID
    ont_unit_id: uuid.UUID
    tr069_device_id: uuid.UUID
    assignment_id: uuid.UUID


def resolve_cpe_wifi_admission_scope(
    db: Session, cpe_device_id: uuid.UUID, *, for_update: bool = False
) -> CpeWifiAdmissionScope:
    """Resolve exactly one active TR-069 CPE row to its eligible owning ONT.

    Refuses, with a distinct typed ``OntServiceConfigurationError`` code per
    cause, rather than falling through to a looser resolution strategy:

    - ``cpe_device_not_found``: no active ``Tr069CpeDevice`` row is keyed to
      this exact ``cpe_device_id``.
    - ``cpe_device_ambiguous``: more than one active ``Tr069CpeDevice`` row is
      keyed to this exact ``cpe_device_id``.
    - ``cpe_ont_not_linked``: the resolved TR-069 row has no ``ont_unit_id``.
    - ``cpe_ont_missing``: ``ont_unit_id`` is set but the referenced
      ``OntUnit`` row cannot be found.
    - ``cpe_assignment_inactive``: the linked ONT has no active
      ``OntAssignment``.
    - ``cpe_assignment_ambiguous``: the linked ONT has more than one active
      ``OntAssignment``.
    - ``cpe_assignment_identity_conflict``: the CPE customer (and, when set,
      service) identity disagrees with the active assignment.

    The web preflight uses read-only lookups. ``configure_cpe_wifi`` invokes
    this resolver again with row locks, compares every resolved identity with
    the submitted preflight evidence, and then rechecks subscription, PON,
    OLT, commissioning, permission, and object scope before staging a change.
    """
    cpe_statement = select(CPEDevice).where(CPEDevice.id == cpe_device_id)
    if for_update:
        cpe_statement = cpe_statement.with_for_update()
    cpe = db.scalar(cpe_statement)
    if cpe is None:
        raise _error(
            "cpe_device_not_found",
            "The CPE inventory record could not be found.",
            cpe_device_id=str(cpe_device_id),
        )
    candidate_statement = (
        select(Tr069CpeDevice)
        .where(
            Tr069CpeDevice.cpe_device_id == cpe_device_id,
            Tr069CpeDevice.is_active.is_(True),
        )
        .order_by(Tr069CpeDevice.id)
    )
    if for_update:
        candidate_statement = candidate_statement.with_for_update()
    candidates = list(db.scalars(candidate_statement))
    if not candidates:
        raise _error(
            "cpe_device_not_found",
            "No active TR-069 device is linked to this CPE.",
            cpe_device_id=str(cpe_device_id),
        )
    if len(candidates) > 1:
        raise _error(
            "cpe_device_ambiguous",
            "Multiple active TR-069 devices are linked to this CPE; contact support.",
            cpe_device_id=str(cpe_device_id),
            candidate_count=len(candidates),
            candidate_tr069_device_ids=[str(item.id) for item in candidates],
        )
    tr069_device = candidates[0]
    if tr069_device.ont_unit_id is None:
        raise _error(
            "cpe_ont_not_linked",
            "This CPE is not linked to an ONT.",
            cpe_device_id=str(cpe_device_id),
            tr069_device_id=str(tr069_device.id),
        )
    ont_statement = select(OntUnit).where(OntUnit.id == tr069_device.ont_unit_id)
    if for_update:
        ont_statement = ont_statement.with_for_update()
    ont = db.scalar(ont_statement)
    if ont is None:
        raise _error(
            "cpe_ont_missing",
            "The ONT linked to this CPE could not be found.",
            cpe_device_id=str(cpe_device_id),
            tr069_device_id=str(tr069_device.id),
            ont_unit_id=str(tr069_device.ont_unit_id),
        )
    assignment_statement = (
        select(OntAssignment)
        .where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
        .order_by(OntAssignment.id)
    )
    if for_update:
        assignment_statement = assignment_statement.with_for_update()
    assignments = list(db.scalars(assignment_statement))
    if not assignments:
        raise _error(
            "cpe_assignment_inactive",
            "The ONT linked to this CPE has no active assignment.",
            cpe_device_id=str(cpe_device_id),
            ont_unit_id=str(ont.id),
        )
    if len(assignments) > 1:
        raise _error(
            "cpe_assignment_ambiguous",
            "The ONT linked to this CPE has multiple active assignments; "
            "contact support.",
            cpe_device_id=str(cpe_device_id),
            ont_unit_id=str(ont.id),
            candidate_assignment_ids=[str(item.id) for item in assignments],
        )
    assignment = assignments[0]
    subscription_conflict = (
        cpe.subscription_id is not None
        and assignment.subscription_id != cpe.subscription_id
    )
    if assignment.subscriber_id != cpe.subscriber_id or subscription_conflict:
        raise _error(
            "cpe_assignment_identity_conflict",
            "The CPE customer identity disagrees with the ONT's active assignment; "
            "contact support.",
            cpe_device_id=str(cpe_device_id),
            cpe_subscriber_id=str(cpe.subscriber_id),
            cpe_subscription_id=(
                str(cpe.subscription_id) if cpe.subscription_id is not None else None
            ),
            assignment_id=str(assignment.id),
            assignment_subscriber_id=(
                str(assignment.subscriber_id)
                if assignment.subscriber_id is not None
                else None
            ),
            assignment_subscription_id=(
                str(assignment.subscription_id)
                if assignment.subscription_id is not None
                else None
            ),
        )
    return CpeWifiAdmissionScope(
        cpe_device_id=cpe.id,
        ont_unit_id=ont.id,
        tr069_device_id=tr069_device.id,
        assignment_id=assignment.id,
    )


def _load_cpe_wifi_admission_scope(
    db: Session, command: ConfigureCpeWifiCommand
) -> tuple[
    OntUnit,
    OntAssignment,
    AssignmentSubscriptionSnapshot,
    OLTDevice,
    PonPort,
]:
    """Lock and revalidate the complete CPE-origin identity chain."""
    if command.context.scope != "network:ont:write" or not command.permission_granted:
        raise _error(
            "permission_denied", "ONT service configuration requires network:ont:write."
        )
    if not command.object_scope_granted:
        raise _error(
            "cpe_scope_denied",
            "The current user cannot manage the ONT linked to this CPE.",
        )
    scope = resolve_cpe_wifi_admission_scope(db, command.cpe_device_id, for_update=True)
    expected = (
        command.cpe_device_id,
        command.expected_tr069_device_id,
        command.expected_ont_unit_id,
        command.expected_assignment_id,
    )
    observed = (
        scope.cpe_device_id,
        scope.tr069_device_id,
        scope.ont_unit_id,
        scope.assignment_id,
    )
    if observed != expected:
        raise _error(
            "cpe_identity_changed",
            "The CPE, ONT, or assignment link changed; refresh and retry.",
            expected_cpe_device_id=str(command.cpe_device_id),
            expected_tr069_device_id=str(command.expected_tr069_device_id),
            expected_ont_unit_id=str(command.expected_ont_unit_id),
            expected_assignment_id=str(command.expected_assignment_id),
            observed_cpe_device_id=str(scope.cpe_device_id),
            observed_tr069_device_id=str(scope.tr069_device_id),
            observed_ont_unit_id=str(scope.ont_unit_id),
            observed_assignment_id=str(scope.assignment_id),
        )
    ont, assignment, subscription, olt, pon = _load_admission_scope(
        db,
        context=command.context,
        ont_unit_id=scope.ont_unit_id,
        permission_granted=command.permission_granted,
    )
    if assignment.id != scope.assignment_id:
        raise _error(
            "cpe_identity_changed",
            "The CPE's active assignment changed; refresh and retry.",
            expected_assignment_id=str(scope.assignment_id),
            observed_assignment_id=str(assignment.id),
        )
    return ont, assignment, subscription, olt, pon


def _head_for_assignment(
    db: Session, ont: OntUnit, assignment: OntAssignment
) -> OntServiceConfigurationHead:
    head = db.scalar(
        select(OntServiceConfigurationHead)
        .where(OntServiceConfigurationHead.assignment_id == assignment.id)
        .with_for_update()
    )
    if head is not None:
        if head.ont_unit_id != ont.id:
            raise _error(
                "configuration_head_conflict",
                "Configuration head points to another ONT.",
            )
        if head.phase is OntServiceConfigurationPhase.retired:
            raise _error(
                "configuration_head_retired",
                "The active assignment has a retired configuration head.",
            )
        return head
    head = OntServiceConfigurationHead(
        ont_unit_id=ont.id,
        assignment_id=assignment.id,
        current_revision=0,
        phase=OntServiceConfigurationPhase.saved,
    )
    db.add(head)
    db.flush()
    return head


def _connection_type(mode: object) -> str:
    normalized = str(mode or "").strip().lower()
    if normalized == "pppoe":
        return "pppoe"
    if normalized == "static_ip":
        return "static"
    if normalized == "dhcp":
        return "dhcp"
    raise _error("wan_mode_unresolved", "Effective routed WAN mode is not configured.")


def _admit(
    db: Session,
    command: (
        ConfigureOntServiceCommand
        | ConfigureCustomerWifiCommand
        | ConfigureCpeWifiCommand
    ),
) -> ConfigureOntServiceOutcome:
    idempotency_key = _require_idempotency(command.context)
    fingerprint = _command_fingerprint(command)
    if isinstance(command, ConfigureCustomerWifiCommand):
        section = OntConfigurationSection.wifi
        ont, assignment, _subscription, _olt, _pon = (
            _load_customer_wifi_admission_scope(db, command)
        )
    elif isinstance(command, ConfigureCpeWifiCommand):
        section = OntConfigurationSection.wifi
        ont, assignment, _subscription, _olt, _pon = _load_cpe_wifi_admission_scope(
            db, command
        )
    else:
        section = command.section
        ont, assignment, _subscription, _olt, _pon = _load_admission_scope(
            db,
            context=command.context,
            ont_unit_id=command.ont_unit_id,
            permission_granted=command.permission_granted,
        )
    head = _head_for_assignment(db, ont, assignment)
    replay = db.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.head_id == head.id,
            OntServiceConfigurationRevision.idempotency_key == idempotency_key,
        )
    )
    if replay is not None:
        if replay.command_fingerprint != fingerprint:
            raise _error(
                "idempotency_conflict",
                "Idempotency key was reused with different configuration input.",
            )
        return ConfigureOntServiceOutcome(
            ont_unit_id=ont.id,
            assignment_id=assignment.id,
            configuration_head_id=head.id,
            revision=replay.revision,
            operation_id=replay.operation_id,
            phase=replay.phase,
            replayed=True,
            message="Configuration request replayed.",
        )
    if isinstance(command, ConfigureCustomerWifiCommand):
        updates, evidence = _customer_wifi_change_updates(command.change)
    elif isinstance(command, ConfigureCpeWifiCommand):
        updates, evidence = _cpe_wifi_change_updates(command.change)
    else:
        updates, evidence = _change_updates(
            db,
            ont,
            assignment.subscriber_id,
            section,
            command.change,
        )
    current_revision = db.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.head_id == head.id,
            OntServiceConfigurationRevision.revision == head.current_revision,
        )
    )
    if (
        head.phase is OntServiceConfigurationPhase.failed
        and current_revision is not None
        and current_revision.command_fingerprint == fingerprint
    ):
        raise _error(
            "repair_required",
            "The current revision failed. Use Retry current configuration or submit a material change.",
            configuration_head_id=str(head.id),
            revision=head.current_revision,
        )

    set_desired_config_values(ont, updates)
    wan_intent_id: uuid.UUID | None = None
    effective_vlan: int | None = None
    vlan_source: WanVlanSource | None = None
    masked_username: str | None = None
    if (
        not isinstance(command, ConfigureCustomerWifiCommand)
        and section is OntConfigurationSection.wan
    ):
        resolved_effective = resolve_effective_ont_config(db, ont, olt=_olt)
        effective = resolved_effective if isinstance(resolved_effective, dict) else {}
        config_pack = effective.get("config_pack")
        raw_values = effective.get("values")
        values: dict[str, object] = raw_values if isinstance(raw_values, dict) else {}
        if config_pack is None:
            raise _error(
                "config_pack_missing",
                "The assignment OLT has no effective ONT config pack.",
            )
        connection_type = _connection_type(values.get("wan_mode"))
        subscription_id = assignment.subscription_id
        if subscription_id is None:
            raise _error(
                "assignment_incomplete",
                "The active assignment lost its exact subscription identity.",
            )
        existing_intent = active_primary_internet_intent(
            db, ont_id=ont.id, subscription_id=subscription_id, for_update=True
        )
        intent_vlan = existing_intent.s_vlan if existing_intent is not None else None
        pack_vlan = values.get("wan_vlan")
        try:
            effective_vlan = int(str(intent_vlan or pack_vlan or 0))
        except ValueError as exc:
            raise _error(
                "customer_vlan_missing",
                "Effective customer VLAN is missing or invalid.",
            ) from exc
        if not 1 <= effective_vlan <= 4094:
            raise _error(
                "customer_vlan_missing",
                "Effective customer VLAN is missing or invalid.",
            )
        vlan_source = (
            WanVlanSource.service_intent
            if intent_vlan is not None
            else WanVlanSource.config_pack
        )
        intent = ensure_active_wan_service_intent_in_transaction(
            db,
            spec=WanServiceIntentSpec(
                ont_id=ont.id,
                subscription_id=subscription_id,
                service_type="internet",
                connection_type=connection_type,
                is_primary=True,
                name="Primary Internet",
                priority=1,
                s_vlan=effective_vlan,
            ),
            context=command.context,
        )
        wan_intent_id = intent.instance_id
        if connection_type == "pppoe":
            from app.services.cpe_dialer_credential_reconcile import (
                ProjectCpeDialerCredential,
                project_cpe_dialer_credential_for_configuration,
            )

            try:
                credential = project_cpe_dialer_credential_for_configuration(
                    db,
                    ProjectCpeDialerCredential(
                        ont_unit_id=ont.id,
                        subscription_id=subscription_id,
                    ),
                )
            except ValueError as exc:
                raise _error(
                    "authoritative_credential_unavailable",
                    "The assigned subscriber has no usable authoritative "
                    "access credential.",
                ) from exc
            masked_username = credential.masked_username
        else:
            from app.services.cpe_dialer_credential_reconcile import (
                clear_cpe_dialer_projection_for_non_ppp,
            )

            clear_cpe_dialer_projection_for_non_ppp(db, ont_unit_id=ont.id)

    next_revision = int(head.current_revision) + 1
    if current_revision is not None and current_revision.phase not in {
        OntServiceConfigurationPhase.retired,
        OntServiceConfigurationPhase.superseded,
    }:
        current_revision.phase = OntServiceConfigurationPhase.superseded
    correlation_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
    operation = start_ont_service_configuration_operation(
        db,
        StartOntServiceConfigurationOperation(
            ont_unit_id=ont.id,
            assignment_id=assignment.id,
            configuration_head_id=head.id,
            configuration_revision=next_revision,
            section=section.value,
            command_fingerprint=fingerprint,
            correlation_key=f"ont-service-config:{assignment.id}:{correlation_hash}",
            initiated_by=command.context.actor,
            wan_intent_id=wan_intent_id,
            effective_customer_vlan=effective_vlan,
            vlan_source=vlan_source.value if vlan_source is not None else None,
        ),
    )
    if wan_intent_id is not None:
        evidence.update(
            {
                "effective_customer_vlan": effective_vlan,
                "vlan_source": vlan_source.value if vlan_source is not None else None,
                "wan_intent_id": str(wan_intent_id),
                "pppoe_username_masked": masked_username,
                "pppoe_provenance": (
                    "authoritative_access_credential" if masked_username else None
                ),
            }
        )
    revision = OntServiceConfigurationRevision(
        head_id=head.id,
        assignment_id=assignment.id,
        revision=next_revision,
        section=section.value,
        command_fingerprint=fingerprint,
        idempotency_key=idempotency_key,
        desired_change_evidence=evidence,
        operation_id=operation.id,
        phase=OntServiceConfigurationPhase.queued,
    )
    db.add(revision)
    head.current_revision = next_revision
    head.latest_operation_id = operation.id
    head.phase = OntServiceConfigurationPhase.queued
    head.waiting_reason = "awaiting_dispatch"
    head.failure_code = None
    head.failure_message = None
    stage_dispatch(db, operation, NetworkOperationCommand.ont_service_config_apply_v1)
    stage_audit_event(
        db,
        action="network.ont_service_configuration.queued",
        entity_type="ont_service_configuration_head",
        entity_id=str(head.id),
        actor_type=AuditActorType.user,
        actor_id=command.context.actor,
        metadata={
            "ont_unit_id": str(ont.id),
            "assignment_id": str(assignment.id),
            "revision": next_revision,
            "operation_id": str(operation.id),
            "section": section.value,
            "vlan_source": vlan_source.value if vlan_source is not None else None,
        },
    )
    emit_event(
        db,
        EventType.ont_service_configuration_queued,
        {
            "ont_unit_id": str(ont.id),
            "assignment_id": str(assignment.id),
            "configuration_head_id": str(head.id),
            "revision": next_revision,
            "operation_id": str(operation.id),
            "section": section.value,
        },
        actor=command.context.actor,
        subscriber_id=assignment.subscriber_id,
    )
    db.flush()
    return ConfigureOntServiceOutcome(
        ont_unit_id=ont.id,
        assignment_id=assignment.id,
        configuration_head_id=head.id,
        revision=next_revision,
        operation_id=operation.id,
        phase=OntServiceConfigurationPhase.queued,
        replayed=False,
        message="Configuration queued.",
    )


def configure_ont_service(
    db: Session, command: ConfigureOntServiceCommand
) -> ConfigureOntServiceOutcome:
    """Atomically admit desired state, intent, operation, and durable dispatch."""

    try:
        return execute_owner_command(
            db,
            definition=_CONFIGURE,
            context=command.context,
            operation=lambda: _admit(db, command),
        )
    except IntegrityError as exc:
        raise _error(
            "concurrent_configuration",
            "Configuration changed concurrently; refresh and retry.",
        ) from exc


def configure_customer_wifi(
    db: Session, command: ConfigureCustomerWifiCommand
) -> ConfigureOntServiceOutcome:
    """Atomically save and queue a customer-scoped WiFi configuration."""

    try:
        return execute_owner_command(
            db,
            definition=_CONFIGURE_CUSTOMER_WIFI,
            context=command.context,
            operation=lambda: _admit(db, command),
        )
    except IntegrityError as exc:
        raise _error(
            "concurrent_configuration",
            "Configuration changed concurrently; refresh and retry.",
        ) from exc


def configure_cpe_wifi(
    db: Session, command: ConfigureCpeWifiCommand
) -> ConfigureOntServiceOutcome:
    """Atomically revalidate CPE ownership and queue one sparse WiFi change."""

    try:
        return execute_owner_command(
            db,
            definition=_CONFIGURE_CPE_WIFI,
            context=command.context,
            operation=lambda: _admit(db, command),
        )
    except IntegrityError as exc:
        raise _error(
            "concurrent_configuration",
            "Configuration changed concurrently; refresh and retry.",
        ) from exc


def _record_execution_event(
    db: Session,
    *,
    ont: OntUnit,
    assignment: OntAssignment,
    head: OntServiceConfigurationHead,
    revision: OntServiceConfigurationRevision,
    operation_id: uuid.UUID,
    phase: OntServiceConfigurationPhase,
    message: str,
    success: bool,
    waiting: bool = False,
    extra_data: dict[str, Any] | None = None,
) -> None:
    data = {"phase": phase.value, "revision": revision.revision}
    if extra_data:
        data.update(extra_data)
    record_ont_provisioning_event(
        db,
        ont,
        "ont_service_configuration",
        StepResult(
            step_name="ont_service_configuration",
            success=success,
            waiting=waiting,
            message=message,
            data=data,
        ),
        action="configuration_phase_changed",
        lifecycle=ProvisioningLifecycleIdentity(
            assignment_id=assignment.id,
            configuration_head_id=head.id,
            configuration_revision=revision.revision,
            operation_id=operation_id,
        ),
    )


def _wifi_delivery_scope(
    revision: OntServiceConfigurationRevision,
) -> OntWifiDeliveryScope | None:
    """Recover typed delivery intent from immutable, redacted revision evidence."""

    from app.services.network.reconcile.state import (
        OntWifiDeliveryField,
        OntWifiDeliveryScope,
    )

    if revision.section != OntConfigurationSection.wifi.value:
        return None
    evidence = revision.desired_change_evidence or {}
    field_by_evidence_key: dict[str, OntWifiDeliveryField] = {
        "wifi.enabled": "wifi_enabled",
        "wifi.ssid": "wifi_ssid",
        "wifi.channel": "wifi_channel",
        "wifi.security_mode": "wifi_security_mode",
        "wifi.password": "wifi_password_ref",
    }
    changed_fields = frozenset(
        field
        for evidence_key, field in field_by_evidence_key.items()
        if evidence_key in evidence
    )
    return OntWifiDeliveryScope(changed_fields=changed_fields)


def _force_lan_delivery(revision: OntServiceConfigurationRevision) -> bool:
    """Whether this exact admitted revision requires the write-only LAN block."""

    return revision.section == OntConfigurationSection.lan.value and bool(
        set(revision.desired_change_evidence or {})
        & {
            "lan.ip",
            "lan.subnet",
            "lan.dhcp_enabled",
            "lan.dhcp_start",
            "lan.dhcp_end",
        }
    )


def _only_ppp_delivery_residual_drift(result: ReconcileResult) -> bool:
    if not result.drift_after:
        return True
    return all(
        str(getattr(drift, "field", "") or "").startswith("ppp_delivery[")
        for drift in result.drift_after
    )


def _connection_request_queued_pending(result: ReconcileResult) -> bool:
    """Whether GenieACS accepted this revision's write but the immediate
    Connection Request failed, leaving the task queued for the device's next
    Inform.

    This GenieACS behavior (``ACS_CR_FAILED``) is identical regardless of
    which section (LAN, WiFi, WAN, management) issued the write — the
    ``setParameterValues`` batch is accepted and queued either way. This used
    to be gated to ``_force_lan_delivery`` (LAN's forced write-only-block
    predicate), which routed every WiFi/WAN/management occurrence of this
    exact GenieACS delivery outcome into terminal failure instead of the
    queued/readback-pending recovery LAN already received.
    """
    failure = result.failure
    return failure is not None and failure.reason == "acs_cr_failed"


def _connection_request_drain_still_pending(
    *,
    waiting_reason: str | None,
    result: ReconcileResult,
) -> bool:
    """Whether a prior queued-CR-failure wait is still undrained.

    Section-agnostic for the same reason as
    ``_connection_request_queued_pending`` — the ``awaiting_acs_task_drain``
    wait state and the GenieACS failure it watches for are identical across
    sections.
    """
    failure = result.failure
    if waiting_reason != "awaiting_acs_task_drain" or failure is None:
        return False
    if failure.reason == "acs_cr_failed":
        return True
    if failure.reason != "blocked_out_of_sync":
        return False
    message = str(failure.message or "").lower()
    return (
        "connection request failed" in message or "setparametervalues queued" in message
    )


def _lan_exact_readback_available(
    revision: OntServiceConfigurationRevision, result: ReconcileResult
) -> bool:
    observed_after = getattr(result, "observed_after", None)
    if not _force_lan_delivery(revision) or observed_after is None:
        return False
    acs = observed_after.acs
    evidence = revision.desired_change_evidence or {}
    if "lan.ip" in evidence and acs.acs_observed_lan_gateway_ip is None:
        return False
    if "lan.subnet" in evidence and acs.acs_observed_dhcp_subnet_mask is None:
        return False
    if "lan.dhcp_enabled" in evidence and acs.acs_observed_dhcp_enabled is None:
        return False
    if bool(evidence.get("lan.dhcp_enabled")):
        return (
            acs.acs_observed_dhcp_pool_min is not None
            and acs.acs_observed_dhcp_pool_max is not None
        )
    return True


def _execution_locked(
    db: Session, command: ExecuteOntServiceConfigurationCommand
) -> ExecuteOntServiceConfigurationOutcome:
    ont = db.scalar(
        select(OntUnit).where(OntUnit.id == command.ont_unit_id).with_for_update()
    )
    head = db.scalar(
        select(OntServiceConfigurationHead)
        .where(OntServiceConfigurationHead.id == command.configuration_head_id)
        .with_for_update()
    )
    operation = db.scalar(
        select(NetworkOperation)
        .where(NetworkOperation.id == command.operation_id)
        .with_for_update()
    )
    if ont is None or head is None or operation is None:
        raise _error(
            "execution_target_missing", "Configuration execution identity is missing."
        )
    assignment = db.scalar(
        select(OntAssignment)
        .where(OntAssignment.id == head.assignment_id)
        .with_for_update()
    )
    revision = db.scalar(
        select(OntServiceConfigurationRevision)
        .where(
            OntServiceConfigurationRevision.head_id == head.id,
            OntServiceConfigurationRevision.revision == command.revision,
        )
        .with_for_update()
    )
    stale = (
        assignment is None
        or not assignment.active
        or assignment.ont_unit_id != ont.id
        or head.ont_unit_id != ont.id
        or head.current_revision != command.revision
        or head.latest_operation_id != operation.id
        or revision is None
        or revision.operation_id != operation.id
        or operation.target_id != ont.id
        or operation.operation_type is not NetworkOperationType.ont_service_config
    )
    if stale:
        if operation.status in {
            NetworkOperationStatus.pending,
            NetworkOperationStatus.running,
            NetworkOperationStatus.waiting,
        }:
            network_operations.mark_canceled(db, str(operation.id))
        if revision is not None:
            revision.phase = OntServiceConfigurationPhase.superseded
        db.flush()
        return ExecuteOntServiceConfigurationOutcome(
            operation_id=operation.id,
            phase=OntServiceConfigurationPhase.superseded,
            executed=False,
            stale=True,
            message="Configuration was superseded before device delivery.",
        )
    assert assignment is not None and revision is not None
    prior_waiting_reason = head.waiting_reason
    head.phase = OntServiceConfigurationPhase.applying
    revision.phase = OntServiceConfigurationPhase.applying
    head.waiting_reason = None
    network_operations.mark_running(db, str(operation.id))

    from app.services.network.reconcile.core import reconcile_ont
    from app.services.network.reconcile.lifecycle import ReconcileLifecycleBinding

    wifi_scope = _wifi_delivery_scope(revision)
    # ``force_readback_only`` is set exclusively by the readback-verification
    # worker task (``app.tasks.ont_service_configuration.verify_readback``)
    # and is never derived from anything in the dispatch payload — see that
    # task and ``_ont_service_config_verify_invocation`` for why no caller
    # input can flip this back to a write.
    readback_only = command.verification_attempt > 0 or command.force_readback_only
    result = reconcile_ont(
        db,
        ont.id,
        mode="sweep" if command.explicit_repair else "sync",
        wifi_delivery_scope=wifi_scope,
        force_lan_config=_force_lan_delivery(revision),
        lifecycle_binding=ReconcileLifecycleBinding(
            ont_unit_id=ont.id,
            assignment_id=assignment.id,
            configuration_head_id=head.id,
            desired_revision=revision.revision,
            operation_id=operation.id,
        ),
        readback_only=readback_only,
        timeout_sec=SERVICE_CONFIGURATION_RECONCILE_TIMEOUT_SECONDS,
    )
    delivered_without_readback = (
        result.success
        and _force_lan_delivery(revision)
        and not _lan_exact_readback_available(revision, result)
        and (
            (result.sync_status == "synced" and not result.drift_after)
            or _only_ppp_delivery_residual_drift(result)
        )
    )
    # TR-069 WiFi passwords are write-only: no observed field ever confirms
    # the value actually landed, so a readback-only pass that finds "no
    # drift" has proven everything EXCEPT the password. Reusing the write
    # path's own success signal (an accepted setParameterValues call) is
    # fine for a real apply, but a readback-only re-check must not silently
    # promote that gap to ``verified``.
    wifi_password_unverifiable_on_readback = (
        readback_only
        and result.success
        and result.sync_status == "synced"
        and not result.drift_after
        and wifi_scope is not None
        and "wifi_password_ref" in wifi_scope.changed_fields
    )
    if delivered_without_readback:
        head.phase = OntServiceConfigurationPhase.delivered_unverified
        revision.phase = OntServiceConfigurationPhase.delivered_unverified
        head.waiting_reason = None
        head.failure_code = "exact_lan_readback_unavailable"
        head.failure_message = (
            "The LAN configuration was delivered, but this ONT firmware does not "
            "expose the subnet and DHCP range for exact readback."
        )
        network_operations.mark_succeeded(
            db,
            str(operation.id),
            output_payload={
                "configuration_head_id": str(head.id),
                "configuration_revision": revision.revision,
                "phase": OntServiceConfigurationPhase.delivered_unverified.value,
                "verification": "unavailable",
            },
        )
        _record_execution_event(
            db,
            ont=ont,
            assignment=assignment,
            head=head,
            revision=revision,
            operation_id=operation.id,
            phase=OntServiceConfigurationPhase.delivered_unverified,
            message=head.failure_message,
            success=True,
        )
        phase = OntServiceConfigurationPhase.delivered_unverified
        message = "Configuration delivered; exact LAN readback is unavailable."
    elif wifi_password_unverifiable_on_readback:
        head.phase = OntServiceConfigurationPhase.delivered_unverified
        revision.phase = OntServiceConfigurationPhase.delivered_unverified
        head.waiting_reason = None
        head.failure_code = "wifi_password_write_only_unverifiable"
        head.failure_message = (
            "WiFi SSID and other readable settings converged, but the WiFi "
            "password is a write-only TR-069 parameter and cannot be "
            "confirmed by readback."
        )
        network_operations.mark_succeeded(
            db,
            str(operation.id),
            output_payload={
                "configuration_head_id": str(head.id),
                "configuration_revision": revision.revision,
                "phase": OntServiceConfigurationPhase.delivered_unverified.value,
                "verification": "password_write_only",
            },
        )
        _record_execution_event(
            db,
            ont=ont,
            assignment=assignment,
            head=head,
            revision=revision,
            operation_id=operation.id,
            phase=OntServiceConfigurationPhase.delivered_unverified,
            message=head.failure_message,
            success=True,
        )
        phase = OntServiceConfigurationPhase.delivered_unverified
        message = (
            "Readable configuration verified; WiFi password cannot be "
            "confirmed by readback."
        )
    elif result.success and result.sync_status == "synced" and not result.drift_after:
        now = datetime.now(UTC)
        head.phase = OntServiceConfigurationPhase.verified
        revision.phase = OntServiceConfigurationPhase.verified
        revision.verified_at = now
        head.waiting_reason = None
        head.failure_code = None
        head.failure_message = None
        network_operations.mark_succeeded(
            db,
            str(operation.id),
            output_payload={
                "configuration_head_id": str(head.id),
                "configuration_revision": revision.revision,
                "phase": OntServiceConfigurationPhase.verified.value,
                "verified_at": now.isoformat(),
            },
        )
        _record_execution_event(
            db,
            ont=ont,
            assignment=assignment,
            head=head,
            revision=revision,
            operation_id=operation.id,
            phase=OntServiceConfigurationPhase.verified,
            message="Current configuration revision verified by device readback.",
            success=True,
        )
        phase = OntServiceConfigurationPhase.verified
        message = "Configuration verified."
    else:
        failure = result.failure
        failure_code = failure.reason if failure is not None else "residual_drift"
        failure_message = (
            failure.message
            if failure is not None
            else "Device still has residual drift."
        )
        readback_pending = bool(
            failure is not None
            and isinstance(failure.evidence, dict)
            and failure.evidence.get("readback_pending")
        )
        connection_request_queued_pending = _connection_request_queued_pending(
            result
        ) or _connection_request_drain_still_pending(
            waiting_reason=prior_waiting_reason,
            result=result,
        )
        if command.force_readback_only and (
            readback_pending or connection_request_queued_pending
        ):
            # A readback-only verification (``verify_ont_service_configuration_
            # readback``) must never auto re-dispatch. The retry loop below
            # re-stages ``ont_service_config_apply_v1`` with a ``verify:N``
            # dispatch key, which only STAYS readback-only because
            # ``_ont_service_config_invocation`` parses that key into
            # ``verification_attempt`` and this function's own
            # ``readback_only`` check honours it — a flag check deep in
            # shared dispatch code, exactly the shape this command was built
            # to avoid relying on structurally. It also carries no
            # ``explicit_repair`` marker, so that re-entry runs in
            # ``mode="sync"`` while the ONT is still ``out_of_sync``, gets
            # refused by the ``BLOCKED_OUT_OF_SYNC`` guard ~30s later, and
            # overwrites this accurate "still not converged" diagnosis with a
            # misleading one. Report the mismatch directly instead; an
            # operator or scheduler re-runs the verify command explicitly.
            head.phase = OntServiceConfigurationPhase.failed
            revision.phase = OntServiceConfigurationPhase.failed
            head.failure_code = "verification_mismatch"
            head.failure_message = failure_message
            head.waiting_reason = None
            network_operations.mark_failed(
                db,
                str(operation.id),
                failure_message,
                output_payload={
                    "configuration_head_id": str(head.id),
                    "configuration_revision": revision.revision,
                    "phase": OntServiceConfigurationPhase.failed.value,
                    "failure_code": "verification_mismatch",
                    "verification": "still_unverified",
                },
            )
            _record_execution_event(
                db,
                ont=ont,
                assignment=assignment,
                head=head,
                revision=revision,
                operation_id=operation.id,
                phase=OntServiceConfigurationPhase.failed,
                message=failure_message,
                success=False,
            )
            phase = OntServiceConfigurationPhase.failed
            message = (
                "still_unverified: readback did not converge. Re-run "
                "verification once the device has informed again."
            )
        elif readback_pending and command.verification_attempt < _MAX_READBACK_ATTEMPTS:
            next_attempt = command.verification_attempt + 1
            head.phase = OntServiceConfigurationPhase.readback_pending
            revision.phase = OntServiceConfigurationPhase.readback_pending
            head.waiting_reason = "awaiting_fresh_device_readback"
            head.failure_code = None
            head.failure_message = None
            network_operations.mark_waiting(db, str(operation.id), head.waiting_reason)
            stage_dispatch(
                db,
                operation,
                NetworkOperationCommand.ont_service_config_apply_v1,
                dispatch_key=f"verify:{next_attempt}",
                not_before=datetime.now(UTC) + timedelta(seconds=30 * next_attempt),
            )
            _record_execution_event(
                db,
                ont=ont,
                assignment=assignment,
                head=head,
                revision=revision,
                operation_id=operation.id,
                phase=OntServiceConfigurationPhase.readback_pending,
                message=failure_message,
                success=False,
                waiting=True,
            )
            phase = OntServiceConfigurationPhase.readback_pending
            message = "Configuration applied; fresh readback is pending."
        elif (
            connection_request_queued_pending
            and command.verification_attempt < _MAX_READBACK_ATTEMPTS
        ):
            next_attempt = command.verification_attempt + 1
            pending_message = (
                f"The {revision.section} configuration was accepted by ACS, but "
                "the ONT rejected the immediate Connection Request. The queued "
                "ACS task will drain on the next Inform or after an OLT ONT reset."
            )
            head.phase = OntServiceConfigurationPhase.readback_pending
            revision.phase = OntServiceConfigurationPhase.readback_pending
            head.waiting_reason = "awaiting_acs_task_drain"
            head.failure_code = None
            head.failure_message = None
            network_operations.mark_waiting(db, str(operation.id), head.waiting_reason)
            stage_dispatch(
                db,
                operation,
                NetworkOperationCommand.ont_service_config_apply_v1,
                dispatch_key=f"verify:{next_attempt}",
                not_before=datetime.now(UTC) + timedelta(seconds=30 * next_attempt),
            )
            _record_execution_event(
                db,
                ont=ont,
                assignment=assignment,
                head=head,
                revision=revision,
                operation_id=operation.id,
                phase=OntServiceConfigurationPhase.readback_pending,
                message=pending_message,
                success=False,
                waiting=True,
                extra_data={
                    "acs_task_queued_despite_cr_failure": True,
                    "section": revision.section,
                    "failure_reason": failure.reason if failure is not None else None,
                    "failure_message": (
                        failure.message if failure is not None else None
                    ),
                    "confirmation_path": "readback_only_verification_after_fresh_inform",
                    "verification_attempt": next_attempt,
                },
            )
            phase = OntServiceConfigurationPhase.readback_pending
            message = pending_message
        else:
            if connection_request_queued_pending:
                failure_code = "acs_cr_failed"
                failure_message = (
                    f"The {revision.section} configuration is queued in ACS, but "
                    "the ONT still rejects or misses the GenieACS Connection "
                    "Request. Fix the ONT connection request credentials or force "
                    "an OLT ONT reset, then retry."
                )
            head.phase = OntServiceConfigurationPhase.failed
            revision.phase = OntServiceConfigurationPhase.failed
            head.failure_code = failure_code
            head.failure_message = failure_message
            head.waiting_reason = None
            network_operations.mark_failed(
                db,
                str(operation.id),
                failure_message,
                output_payload={
                    "configuration_head_id": str(head.id),
                    "configuration_revision": revision.revision,
                    "phase": OntServiceConfigurationPhase.failed.value,
                    "failure_code": failure_code,
                },
            )
            _record_execution_event(
                db,
                ont=ont,
                assignment=assignment,
                head=head,
                revision=revision,
                operation_id=operation.id,
                phase=OntServiceConfigurationPhase.failed,
                message=failure_message,
                success=False,
            )
            phase = OntServiceConfigurationPhase.failed
            message = failure_message
    emit_event(
        db,
        EventType.ont_service_configuration_phase_changed,
        {
            "ont_unit_id": str(ont.id),
            "assignment_id": str(assignment.id),
            "configuration_head_id": str(head.id),
            "revision": revision.revision,
            "operation_id": str(operation.id),
            "phase": phase.value,
        },
        actor=command.context.actor,
        subscriber_id=assignment.subscriber_id,
    )
    db.flush()
    return ExecuteOntServiceConfigurationOutcome(
        operation_id=operation.id,
        phase=phase,
        executed=True,
        stale=False,
        message=message,
    )


def execute_ont_service_configuration(
    db: Session, command: ExecuteOntServiceConfigurationCommand
) -> ExecuteOntServiceConfigurationOutcome:
    return execute_owner_command(
        db,
        definition=_EXECUTE,
        context=command.context,
        operation=lambda: _execution_locked(db, command),
    )


def _retry_locked(
    db: Session, command: RetryOntServiceConfigurationCommand
) -> ConfigureOntServiceOutcome:
    idempotency_key = _require_idempotency(command.context)
    ont = db.scalar(
        select(OntUnit).where(OntUnit.id == command.ont_unit_id).with_for_update()
    )
    head = db.scalar(
        select(OntServiceConfigurationHead)
        .where(OntServiceConfigurationHead.id == command.expected_head_id)
        .with_for_update()
    )
    if ont is None or head is None or head.ont_unit_id != ont.id:
        raise _error(
            "configuration_head_not_found",
            "Current configuration lifecycle was not found.",
        )
    assignment = db.scalar(
        select(OntAssignment)
        .where(OntAssignment.id == head.assignment_id)
        .with_for_update()
    )
    revision = db.scalar(
        select(OntServiceConfigurationRevision)
        .where(
            OntServiceConfigurationRevision.head_id == head.id,
            OntServiceConfigurationRevision.revision == command.expected_revision,
        )
        .with_for_update()
    )
    if (
        assignment is None
        or not assignment.active
        or head.current_revision != command.expected_revision
        or revision is None
    ):
        raise _error(
            "stale_configuration",
            "Configuration lifecycle changed; refresh before retrying.",
        )
    latest_configuration_operation = (
        db.get(NetworkOperation, head.latest_operation_id)
        if head.latest_operation_id is not None
        else None
    )
    interrupted_current_revision_for_retry = bool(
        latest_configuration_operation is not None
        and latest_configuration_operation.status
        in {NetworkOperationStatus.failed, NetworkOperationStatus.canceled}
        and head.phase
        in {
            OntServiceConfigurationPhase.queued,
            OntServiceConfigurationPhase.applying,
            OntServiceConfigurationPhase.readback_pending,
        }
    )
    if (
        head.phase is not OntServiceConfigurationPhase.failed
        and not interrupted_current_revision_for_retry
    ):
        raise _error(
            "retry_not_eligible",
            "Only the owner's current failed revision may be retried.",
        )
    retry_fingerprint = hashlib.sha256(
        f"{head.id}:{head.current_revision}:{command.context.reason}".encode()
    ).hexdigest()
    if (
        head.last_retry_idempotency_key == idempotency_key
        and head.last_retry_operation_id is not None
    ):
        prior = db.get(NetworkOperation, head.last_retry_operation_id)
        prior_fingerprint = (
            str((prior.input_payload or {}).get("retry_fingerprint") or "")
            if prior
            else ""
        )
        if prior_fingerprint != retry_fingerprint:
            raise _error(
                "idempotency_conflict",
                "Retry key was reused with different reviewed input.",
            )
        return ConfigureOntServiceOutcome(
            ont_unit_id=ont.id,
            assignment_id=assignment.id,
            configuration_head_id=head.id,
            revision=head.current_revision,
            operation_id=head.last_retry_operation_id,
            phase=head.phase,
            replayed=True,
            message="Configuration repair replayed.",
        )
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
    operation = start_ont_service_configuration_operation(
        db,
        StartOntServiceConfigurationOperation(
            ont_unit_id=ont.id,
            assignment_id=assignment.id,
            configuration_head_id=head.id,
            configuration_revision=head.current_revision,
            section=revision.section,
            command_fingerprint=revision.command_fingerprint,
            correlation_key=f"ont-service-config-repair:{head.id}:{key_hash}",
            initiated_by=command.context.actor,
            explicit_repair=True,
            retry_fingerprint=retry_fingerprint,
        ),
    )
    revision.operation_id = operation.id
    revision.phase = OntServiceConfigurationPhase.queued
    revision.failure_code = None
    revision.failure_message = None
    head.latest_operation_id = operation.id
    head.last_retry_idempotency_key = idempotency_key
    head.last_retry_operation_id = operation.id
    head.phase = OntServiceConfigurationPhase.queued
    head.failure_code = None
    head.failure_message = None
    head.waiting_reason = "awaiting_dispatch"
    stage_dispatch(db, operation, NetworkOperationCommand.ont_service_config_apply_v1)
    db.flush()
    return ConfigureOntServiceOutcome(
        ont_unit_id=ont.id,
        assignment_id=assignment.id,
        configuration_head_id=head.id,
        revision=head.current_revision,
        operation_id=operation.id,
        phase=OntServiceConfigurationPhase.queued,
        replayed=False,
        message="Configuration repair queued.",
    )


def retry_ont_service_configuration(
    db: Session, command: RetryOntServiceConfigurationCommand
) -> ConfigureOntServiceOutcome:
    return execute_owner_command(
        db,
        definition=_RETRY,
        context=command.context,
        operation=lambda: _retry_locked(db, command),
    )


def _verify_locked(
    db: Session, command: VerifyOntServiceConfigurationReadbackCommand
) -> ConfigureOntServiceOutcome:
    idempotency_key = _require_idempotency(command.context)
    ont = db.scalar(
        select(OntUnit).where(OntUnit.id == command.ont_unit_id).with_for_update()
    )
    head = db.scalar(
        select(OntServiceConfigurationHead)
        .where(OntServiceConfigurationHead.id == command.expected_head_id)
        .with_for_update()
    )
    if ont is None or head is None or head.ont_unit_id != ont.id:
        raise _error(
            "configuration_head_not_found",
            "Current configuration lifecycle was not found.",
        )
    assignment = db.scalar(
        select(OntAssignment)
        .where(OntAssignment.id == head.assignment_id)
        .with_for_update()
    )
    revision = db.scalar(
        select(OntServiceConfigurationRevision)
        .where(
            OntServiceConfigurationRevision.head_id == head.id,
            OntServiceConfigurationRevision.revision == command.expected_revision,
        )
        .with_for_update()
    )
    failed_operation = db.scalar(
        select(NetworkOperation)
        .where(NetworkOperation.id == command.failed_operation_id)
        .with_for_update()
    )
    if (
        assignment is None
        or not assignment.active
        or head.current_revision != command.expected_revision
        or revision is None
        or failed_operation is None
    ):
        raise _error(
            "stale_configuration",
            "Configuration lifecycle changed; refresh before verifying.",
        )
    if (
        head.phase is not OntServiceConfigurationPhase.failed
        or revision.phase is not OntServiceConfigurationPhase.failed
    ):
        raise _error(
            "verification_not_eligible",
            "Only the owner's current terminally failed revision may be verified.",
        )
    if (
        head.latest_operation_id != failed_operation.id
        or revision.operation_id != failed_operation.id
    ):
        # A caller could otherwise name an unrelated, stale or superseded
        # operation id and have this command "verify" the wrong attempt.
        raise _error(
            "verification_operation_mismatch",
            "The referenced operation is not this revision's current tracked failure.",
        )
    if failed_operation.status is not NetworkOperationStatus.failed:
        raise _error(
            "verification_operation_not_failed",
            "The referenced operation did not terminate in a failed state.",
        )

    # ── Fresh-Inform gate ────────────────────────────────────────────────
    # A readback that only reflects the same stale cache the failed attempt
    # already saw proves nothing. Require positive evidence the device has
    # contacted ACS again since the failure, rather than trusting that the
    # desired value happens to already match a cached document. Neither
    # "still unverified" outcome below mutates the head/revision/operation —
    # nothing is asserted success or newly failed.
    failure_at = failed_operation.completed_at
    last_inform_at = ont.acs_last_inform_at
    if failure_at is None or last_inform_at is None or last_inform_at <= failure_at:
        return ConfigureOntServiceOutcome(
            ont_unit_id=ont.id,
            assignment_id=assignment.id,
            configuration_head_id=head.id,
            revision=head.current_revision,
            operation_id=failed_operation.id,
            phase=head.phase,
            replayed=False,
            message=(
                "still_unverified: no fresh ACS Inform has been observed since "
                "the failed delivery attempt; retry once the device has "
                "informed again."
            ),
        )

    verify_idempotency_key = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
    operation, replayed = start_ont_service_configuration_verification_operation(
        db,
        StartOntServiceConfigurationVerificationOperation(
            ont_unit_id=ont.id,
            assignment_id=assignment.id,
            configuration_head_id=head.id,
            configuration_revision=head.current_revision,
            source_operation=failed_operation,
            correlation_key=(
                f"ont-service-config-verify:{head.id}:{verify_idempotency_key}"
            ),
            initiated_by=command.context.actor,
            reason=command.context.reason,
            idempotency_key=verify_idempotency_key,
        ),
    )
    if not replayed:
        # Preserve the original failed operation exactly as it is: only the
        # pointers (head.latest_operation_id / revision.operation_id) move to
        # the new redrive-linked attempt, mirroring ``_retry_locked``. The
        # failed operation row itself is never mutated here.
        revision.operation_id = operation.id
        revision.phase = OntServiceConfigurationPhase.queued
        head.latest_operation_id = operation.id
        head.phase = OntServiceConfigurationPhase.queued
        head.waiting_reason = "awaiting_dispatch"
        stage_dispatch(
            db, operation, NetworkOperationCommand.ont_service_config_verify_v1
        )
        db.flush()
    return ConfigureOntServiceOutcome(
        ont_unit_id=ont.id,
        assignment_id=assignment.id,
        configuration_head_id=head.id,
        revision=head.current_revision,
        operation_id=operation.id,
        phase=head.phase,
        replayed=replayed,
        message=(
            "Readback verification replayed."
            if replayed
            else "Readback verification queued."
        ),
    )


def verify_ont_service_configuration_readback(
    db: Session, command: VerifyOntServiceConfigurationReadbackCommand
) -> ConfigureOntServiceOutcome:
    return execute_owner_command(
        db,
        definition=_VERIFY,
        context=command.context,
        operation=lambda: _verify_locked(db, command),
    )


def _event_view(event: OntProvisioningEvent) -> ConfigurationEventView:
    return ConfigurationEventView(
        event_id=event.id,
        status=str(getattr(event.status, "value", event.status)),
        step_name=event.step_name,
        message=event.message,
        occurred_at=event.created_at,
    )


def get_ont_service_configuration_projection(
    db: Session, *, ont_unit_id: uuid.UUID
) -> OntServiceConfigurationProjection:
    ont = db.get(OntUnit, ont_unit_id)
    if ont is None:
        raise _error("ont_not_found", "ONT was not found.")
    assignments = list(
        db.scalars(
            select(OntAssignment).where(
                OntAssignment.ont_unit_id == ont.id, OntAssignment.active.is_(True)
            )
        )
    )
    assignment = assignments[0] if len(assignments) == 1 else None
    head = (
        db.scalar(
            select(OntServiceConfigurationHead).where(
                OntServiceConfigurationHead.assignment_id == assignment.id
            )
        )
        if assignment is not None
        else None
    )
    revision = (
        db.scalar(
            select(OntServiceConfigurationRevision).where(
                OntServiceConfigurationRevision.head_id == head.id,
                OntServiceConfigurationRevision.revision == head.current_revision,
            )
        )
        if head is not None and head.current_revision > 0
        else None
    )
    current_events: tuple[ConfigurationEventView, ...] = ()
    if revision is not None and head is not None:
        current_events = tuple(
            _event_view(event)
            for event in db.scalars(
                select(OntProvisioningEvent)
                .where(
                    OntProvisioningEvent.configuration_head_id == head.id,
                    OntProvisioningEvent.configuration_revision == revision.revision,
                )
                .order_by(OntProvisioningEvent.created_at.desc())
                .limit(12)
            )
        )
    history_stmt = select(OntProvisioningEvent).where(
        OntProvisioningEvent.ont_unit_id == ont.id
    )
    if revision is not None and head is not None:
        history_stmt = history_stmt.where(
            or_(
                OntProvisioningEvent.configuration_head_id.is_(None),
                OntProvisioningEvent.configuration_revision.is_(None),
                OntProvisioningEvent.configuration_head_id != head.id,
                OntProvisioningEvent.configuration_revision != revision.revision,
            )
        )
    historical_events = tuple(
        _event_view(event)
        for event in db.scalars(
            history_stmt.order_by(OntProvisioningEvent.created_at.desc()).limit(25)
        )
    )
    evidence = revision.desired_change_evidence if revision is not None else {}
    observation = db.scalar(
        select(OntObservation).where(OntObservation.ont_unit_id == ont.id)
    )
    observation_is_current = bool(
        head is not None
        and revision is not None
        and assignment is not None
        and ont.reconcile_configuration_head_id == head.id
        and ont.reconcile_assignment_id == assignment.id
        and ont.reconcile_desired_revision == revision.revision
        and ont.reconcile_operation_id == head.latest_operation_id
    )
    latest_operation = (
        db.get(NetworkOperation, head.latest_operation_id)
        if head is not None and head.latest_operation_id is not None
        else None
    )
    interrupted_current_revision = bool(
        head is not None
        and latest_operation is not None
        and latest_operation.status
        in {NetworkOperationStatus.failed, NetworkOperationStatus.canceled}
        and head.phase
        in {
            OntServiceConfigurationPhase.queued,
            OntServiceConfigurationPhase.applying,
            OntServiceConfigurationPhase.readback_pending,
        }
    )
    projected_phase = (
        OntServiceConfigurationPhase.failed
        if interrupted_current_revision
        else (head.phase if head is not None else None)
    )
    projected_waiting_reason = (
        None
        if interrupted_current_revision
        else (head.waiting_reason if head is not None else None)
    )
    projected_failure_code = (
        "operation_interrupted"
        if interrupted_current_revision
        else (head.failure_code if head is not None else None)
    )
    projected_failure_message = (
        str(
            latest_operation.error
            or (latest_operation.output_payload or {}).get("message")
            or "Configuration worker stopped before recording a lifecycle result."
        )
        if interrupted_current_revision and latest_operation is not None
        else (head.failure_message if head is not None else None)
    )
    if head is None:
        next_action = (
            OntConfigurationNextAction.submit_configuration
            if assignment
            else OntConfigurationNextAction.none
        )
    elif projected_phase is OntServiceConfigurationPhase.failed:
        next_action = OntConfigurationNextAction.retry_current_configuration
    elif projected_phase in {
        OntServiceConfigurationPhase.queued,
        OntServiceConfigurationPhase.applying,
        OntServiceConfigurationPhase.readback_pending,
    }:
        next_action = OntConfigurationNextAction.wait
    else:
        next_action = OntConfigurationNextAction.submit_configuration
    vlan_source_raw = str(evidence.get("vlan_source") or "")
    vlan_source = WanVlanSource(vlan_source_raw) if vlan_source_raw else None
    return OntServiceConfigurationProjection(
        ont_unit_id=ont.id,
        assignment_id=assignment.id if assignment is not None else None,
        configuration_head_id=head.id if head is not None else None,
        revision=revision.revision if revision is not None else None,
        section=(
            OntConfigurationSection(revision.section) if revision is not None else None
        ),
        operation_id=head.latest_operation_id if head is not None else None,
        phase=projected_phase,
        waiting_reason=projected_waiting_reason,
        failure_code=projected_failure_code,
        failure_message=projected_failure_message,
        last_verified_at=revision.verified_at if revision is not None else None,
        last_observation_at=(
            observation.last_reconciled_at
            if observation is not None and observation_is_current
            else None
        ),
        effective_customer_vlan=(
            int(evidence["effective_customer_vlan"])
            if evidence.get("effective_customer_vlan") is not None
            else None
        ),
        vlan_source=vlan_source,
        masked_pppoe_username=_text(str(evidence.get("pppoe_username_masked") or "")),
        pppoe_provenance=_text(str(evidence.get("pppoe_provenance") or "")),
        next_action=next_action,
        current_events=current_events,
        historical_events=historical_events,
    )


def get_ont_service_configuration_eligibility(
    db: Session, *, ont_unit_id: uuid.UUID
) -> OntServiceConfigurationEligibility:
    ont = db.get(OntUnit, ont_unit_id)
    if ont is None:
        raise _error("ont_not_found", "ONT was not found.")
    active_assignments = list(
        db.scalars(
            select(OntAssignment).where(
                OntAssignment.ont_unit_id == ont.id,
                OntAssignment.active.is_(True),
            )
        )
    )
    has_one_active_assignment = len(active_assignments) == 1
    effective = resolve_effective_ont_config(db, ont)
    config_pack_ready = bool(
        isinstance(effective, dict) and effective.get("config_pack") is not None
    )
    has_olt_assignment = bool(getattr(ont, "olt_device_id", None))
    routed_wan_configurable = bool(
        has_one_active_assignment and has_olt_assignment and config_pack_ready
    )

    return OntServiceConfigurationEligibility(
        routed_wan_configurable=routed_wan_configurable,
        bridge_mode_configurable=False,
        nat_toggle_configurable=False,
        nat_default_enabled=True,
        lan_dhcp_configurable=has_one_active_assignment,
        retain_config_on_move_supported=False,
        routed_wan_message=(
            "Routed DHCP, PPPoE, and static WAN changes are owner-backed."
            if routed_wan_configurable
            else "Routed WAN changes require one active assignment, an OLT assignment, "
            "and an effective config pack."
        ),
        bridge_mode_message=(
            "Bridge/routing conversion is not available from ONT Configure yet; "
            "it needs a dedicated WAN-mode transition owner."
        ),
        nat_message=(
            "NAT defaults to enabled for routed WAN. Disabling NAT is not available "
            "until NAT is added to the typed WAN intent and readback contract."
        ),
        lan_dhcp_message=(
            "LAN gateway, DHCP state, pool, and block size are owner-backed."
            if has_one_active_assignment
            else "LAN DHCP edits require one active assignment."
        ),
        move_message=(
            "Moving an ONT must use the inventory move flow, preserve logical desired "
            "configuration, and re-resolve OLT-local service ports and profile bindings "
            "on the target OLT; ONT Configure does not perform moves."
        ),
    )


def get_latest_ont_configuration_section_delivery(
    db: Session,
    *,
    ont_unit_id: uuid.UUID,
    section: OntConfigurationSection,
) -> OntConfigurationSectionDeliveryProjection | None:
    """Return the newest delivery revision for one active assignment section."""

    assignments = list(
        db.scalars(
            select(OntAssignment).where(
                OntAssignment.ont_unit_id == ont_unit_id,
                OntAssignment.active.is_(True),
            )
        )
    )
    if len(assignments) != 1:
        return None
    assignment = assignments[0]
    head = db.scalar(
        select(OntServiceConfigurationHead).where(
            OntServiceConfigurationHead.assignment_id == assignment.id
        )
    )
    if head is None:
        return None
    revision = db.scalar(
        select(OntServiceConfigurationRevision)
        .where(
            OntServiceConfigurationRevision.head_id == head.id,
            OntServiceConfigurationRevision.section == section.value,
        )
        .order_by(OntServiceConfigurationRevision.revision.desc())
        .limit(1)
    )
    if revision is None:
        return None
    is_current = revision.revision == head.current_revision
    return OntConfigurationSectionDeliveryProjection(
        ont_unit_id=ont_unit_id,
        assignment_id=assignment.id,
        section=section,
        revision=revision.revision,
        operation_id=revision.operation_id,
        phase=revision.phase,
        failure_code=head.failure_code if is_current else revision.failure_code,
        failure_message=(
            head.failure_message if is_current else revision.failure_message
        ),
    )


def inspect_ont_service_configuration_drift(
    db: Session, *, ont_unit_ids: tuple[uuid.UUID, ...] | None = None
) -> tuple[OntServiceConfigurationDrift, ...]:
    stmt = select(OntUnit).order_by(OntUnit.id)
    if ont_unit_ids:
        stmt = stmt.where(OntUnit.id.in_(ont_unit_ids))
    findings: list[OntServiceConfigurationDrift] = []
    for ont in db.scalars(stmt):
        reasons: list[str] = []
        active_assignments = list(
            db.scalars(
                select(OntAssignment).where(
                    OntAssignment.ont_unit_id == ont.id,
                    OntAssignment.active.is_(True),
                )
            )
        )
        active_assignment = (
            active_assignments[0] if len(active_assignments) == 1 else None
        )
        head = (
            db.scalar(
                select(OntServiceConfigurationHead).where(
                    OntServiceConfigurationHead.assignment_id == active_assignment.id
                )
            )
            if active_assignment is not None
            else None
        )
        if ont.last_error and ont.reconcile_assignment_id is None:
            reasons.append("legacy_unbound_out_of_sync_error")
        if not active_assignments and ont.last_error:
            reasons.append("inventory_ont_with_failure_projection")
        if active_assignment is not None and ont.reconcile_assignment_id not in {
            None,
            active_assignment.id,
        }:
            reasons.append("new_assignment_inheriting_old_failure_projection")
        if active_assignment is not None and head is None:
            reasons.append("active_assignment_without_configuration_head")
        if (
            head is not None
            and head.current_revision > 0
            and head.latest_operation_id is None
        ):
            reasons.append("configuration_head_without_tracked_operation")
        if head is not None and head.phase is OntServiceConfigurationPhase.retired:
            reasons.append("retired_configuration_presented_as_current")
        wan_intents = list(
            db.scalars(
                select(OntWanServiceInstance).where(
                    OntWanServiceInstance.ont_id == ont.id
                )
            )
        )
        if any(
            intent.lifecycle_state is OntWanServiceLifecycle.active
            and intent.connection_type is WanConnectionType.pppoe
            and intent.is_primary
            for intent in wan_intents
        ) and (head is None or head.latest_operation_id is None):
            reasons.append(
                "active_pppoe_intent_without_tracked_configuration_operation"
            )
        if any(
            intent.lifecycle_state is OntWanServiceLifecycle.retired
            and intent.is_active
            for intent in wan_intents
        ):
            reasons.append("retired_intent_incorrectly_presented_as_current")
        if reasons:
            findings.append(
                OntServiceConfigurationDrift(ont_unit_id=ont.id, reasons=tuple(reasons))
            )
    return tuple(findings)


def _repair_locked(
    db: Session, command: RepairOntServiceConfigurationDriftCommand
) -> RepairOntServiceConfigurationDriftOutcome:
    _require_idempotency(command.context)
    if not command.ont_unit_ids:
        raise _error("exact_ont_ids_required", "Repair requires exact ONT IDs.")
    if not command.reviewed_evidence.strip():
        raise _error("reviewed_evidence_required", "Repair requires reviewed evidence.")
    findings = inspect_ont_service_configuration_drift(
        db, ont_unit_ids=command.ont_unit_ids
    )
    from app.services.network.reconcile.lifecycle import (
        RetireOntReconcileProjectionForInventory,
        retire_ont_reconcile_projection_for_inventory,
    )

    repaired = 0
    for finding in findings:
        ont = db.scalar(
            select(OntUnit).where(OntUnit.id == finding.ont_unit_id).with_for_update()
        )
        if ont is None:
            continue
        # Only clear a projection proven not to belong to the active assignment.
        # Missing active heads are reported for a fresh Configure submission;
        # repair does not invent revision zero as if it were accepted config.
        if (
            "inventory_ont_with_failure_projection" in finding.reasons
            or "new_assignment_inheriting_old_failure_projection" in finding.reasons
            or "legacy_unbound_out_of_sync_error" in finding.reasons
        ):
            retire_ont_reconcile_projection_for_inventory(
                db,
                RetireOntReconcileProjectionForInventory(
                    ont_unit_id=ont.id,
                    assignment_ids=(),
                    actor=command.context.actor,
                    reason=command.context.reason,
                ),
            )
            repaired += 1
        stage_audit_event(
            db,
            action="network.ont_service_configuration.reviewed_repair",
            entity_type="ont_unit",
            entity_id=str(ont.id),
            actor_type=AuditActorType.user,
            actor_id=command.context.actor,
            metadata={
                "reasons": list(finding.reasons),
                "reviewed_evidence": command.reviewed_evidence,
                "idempotency_key": command.context.idempotency_key,
            },
        )
    db.flush()
    return RepairOntServiceConfigurationDriftOutcome(
        examined=len(command.ont_unit_ids), repaired=repaired, findings=findings
    )


def repair_ont_service_configuration_drift(
    db: Session, command: RepairOntServiceConfigurationDriftCommand
) -> RepairOntServiceConfigurationDriftOutcome:
    return execute_owner_command(
        db,
        definition=_REPAIR,
        context=command.context,
        operation=lambda: _repair_locked(db, command),
    )


__all__ = (
    "AdvancedConfigurationChange",
    "ConfigureCpeWifiCommand",
    "ConfigureCustomerWifiCommand",
    "ConfigureOntServiceCommand",
    "ConfigureOntServiceOutcome",
    "CustomerWifiConfigurationChange",
    "CpeWifiAdmissionScope",
    "CpeWifiConfigurationChange",
    "ExecuteOntServiceConfigurationCommand",
    "ExecuteOntServiceConfigurationOutcome",
    "LanConfigurationChange",
    "ManagementConfigurationChange",
    "OntConfigurationNextAction",
    "OntConfigurationSectionDeliveryProjection",
    "OntConfigurationChange",
    "OntConfigurationSection",
    "OntServiceConfigurationError",
    "OntServiceConfigurationEligibility",
    "OntServiceConfigurationProjection",
    "RepairOntServiceConfigurationDriftCommand",
    "RepairOntServiceConfigurationDriftOutcome",
    "RetryOntServiceConfigurationCommand",
    "VerifyOntServiceConfigurationReadbackCommand",
    "WanConfigurationChange",
    "WanVlanSource",
    "WifiConfigurationChange",
    "configure_cpe_wifi",
    "configure_customer_wifi",
    "configure_ont_service",
    "execute_ont_service_configuration",
    "get_ont_service_configuration_eligibility",
    "get_ont_service_configuration_projection",
    "get_latest_ont_configuration_section_delivery",
    "inspect_ont_service_configuration_drift",
    "repair_ont_service_configuration_drift",
    "resolve_cpe_wifi_admission_scope",
    "retry_ont_service_configuration",
    "verify_ont_service_configuration_readback",
)
