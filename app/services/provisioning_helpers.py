"""Helper functions for provisioning service flows."""

import ipaddress
import logging
from typing import cast
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.connector import ConnectorConfig
from app.models.domain_settings import SettingDomain
from app.models.network import (
    IPAssignment,
    IpPool,
    IPv4Address,
    IPv6Address,
    IPVersion,
)
from app.models.provisioning import (
    ProvisioningVendor,
    ProvisioningWorkflow,
    ServiceOrder,
)
from app.schemas.network import IPAssignmentCreate
from app.services import network as network_service
from app.services import settings_spec
from app.services.common import (
    coerce_uuid,
    validate_enum,
)
from app.services.provisioning_context import extend_provisioning_context
from app.services.secrets import resolve_secret

logger = logging.getLogger(__name__)


def _resolve_connector_context(db: Session, config: dict | None) -> dict | None:
    if not config:
        return None
    connector_id = config.get("connector_config_id") or config.get("connector_id")
    connector_name = config.get("connector_name")
    connector = None
    if connector_id:
        connector = db.get(ConnectorConfig, connector_id)
    elif connector_name:
        connector = (
            db.query(ConnectorConfig)
            .filter(ConnectorConfig.name == connector_name)
            .first()
        )
    if not connector:
        return None
    auth_config = dict(connector.auth_config or {})
    for key, value in auth_config.items():
        if isinstance(value, str):
            auth_config[key] = resolve_secret(value)
    base_url = connector.base_url
    host = auth_config.get("host")
    port = auth_config.get("port")
    if base_url and not host:
        parsed = urlparse(base_url)
        host = parsed.hostname or base_url
        port = port or parsed.port
    return {
        "base_url": base_url,
        "headers": connector.headers,
        "timeout_sec": connector.timeout_sec,
        "auth_config": {
            **auth_config,
            "host": host,
            "port": port,
        },
    }


def _parse_ip_value(
    value: str, label: str
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        return ipaddress.ip_address(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{label} must be a valid IP address."
        ) from exc


def _pool_prefix_length(pool: IpPool | None) -> int | None:
    if not pool or not pool.cidr:
        return None
    try:
        return ipaddress.ip_network(pool.cidr, strict=False).prefixlen
    except ValueError:
        return None


def _resolve_pool_for_version(
    db: Session,
    ip_version: IPVersion,
    pool_id: str | None,
    nas_device_id: str | None = None,
) -> IpPool | None:
    nas = None
    allowed_pool_ids: set[str] = set()
    nas_uuid = None
    if nas_device_id:
        from app.models.catalog import NasDevice
        from app.services.nas import radius_pool_ids_from_tags

        try:
            nas_uuid = coerce_uuid(nas_device_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="Invalid NAS device ID."
            ) from exc
        nas = db.get(NasDevice, nas_uuid)
        if nas is None:
            raise HTTPException(status_code=404, detail="NAS device not found.")
        allowed_pool_ids = set(radius_pool_ids_from_tags(nas.tags))

    if pool_id:
        try:
            pool_uuid = coerce_uuid(pool_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid pool_id.") from exc
        pool = cast(IpPool | None, db.get(IpPool, pool_uuid))
        if not pool or pool.ip_version != ip_version:
            raise HTTPException(status_code=404, detail="IP pool not found.")
        if nas_uuid is not None and not (
            str(pool.id) in allowed_pool_ids or pool.nas_device_id == nas_uuid
        ):
            raise HTTPException(
                status_code=400,
                detail="IP pool is not assigned to the subscription's BNG.",
            )
        return pool

    if nas_uuid is not None:
        # NAS tags are the admin UI's many-to-many RADIUS pool assignment.
        # The direct pool FK remains supported for legacy records. Never fall
        # through to a global pool for a BNG-bound subscription.
        ownership = [IpPool.nas_device_id == nas_uuid]
        if allowed_pool_ids:
            ownership.append(IpPool.id.in_([coerce_uuid(v) for v in allowed_pool_ids]))
        nas_pool = cast(
            IpPool | None,
            db.query(IpPool)
            .filter(IpPool.ip_version == ip_version)
            .filter(IpPool.is_active.is_(True))
            .filter(or_(*ownership))
            .order_by(IpPool.name.asc())
            .first(),
        )
        return nas_pool

    # Fallback: first active pool (alphabetical)
    return cast(
        IpPool | None,
        (
            db.query(IpPool)
            .filter(IpPool.ip_version == ip_version)
            .filter(IpPool.is_active.is_(True))
            .order_by(IpPool.name.asc())
            .first()
        ),
    )


def _get_or_create_address_by_value(
    db: Session, ip_version: IPVersion, value: str, pool: IpPool | None
) -> IPv4Address | IPv6Address:
    model = IPv4Address if ip_version == IPVersion.ipv4 else IPv6Address
    address = cast(
        IPv4Address | IPv6Address | None,
        db.query(model).filter(model.address == value).first(),
    )
    if address:
        return address
    address = model(address=value, pool_id=pool.id if pool else None)
    db.add(address)
    db.commit()
    db.refresh(address)
    return address


def _get_address_by_id(
    db: Session, ip_version: IPVersion, address_id: str
) -> IPv4Address | IPv6Address:
    try:
        address_uuid = coerce_uuid(address_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid address_id.") from exc
    model = IPv4Address if ip_version == IPVersion.ipv4 else IPv6Address
    address = cast(IPv4Address | IPv6Address | None, db.get(model, address_uuid))
    if not address:
        raise HTTPException(status_code=404, detail="IP address not found.")
    return address


def _find_available_address(
    db: Session, ip_version: IPVersion, pool_id: str
) -> IPv4Address | IPv6Address | None:
    # An address is free when it has no *active* assignment — a released
    # (is_active=false) assignment stays for history but must not block reuse
    # (the partial-unique index permits a fresh active row alongside it). The
    # active-only outerjoin is the read side of the asymmetric-release fix.
    # ``with_for_update(skip_locked=True)`` makes two concurrent provisionings pick
    # *different* free rows instead of racing onto the same lowest address. (No-op
    # on SQLite, which the test suite uses; real protection on Postgres.)
    if ip_version == IPVersion.ipv4:
        return cast(
            IPv4Address | None,
            (
                db.query(IPv4Address)
                .outerjoin(
                    IPAssignment,
                    and_(
                        IPAssignment.ipv4_address_id == IPv4Address.id,
                        IPAssignment.is_active.is_(True),
                    ),
                )
                .filter(IPv4Address.pool_id == pool_id)
                .filter(IPv4Address.is_reserved.is_(False))
                .filter(IPAssignment.id.is_(None))
                .order_by(IPv4Address.address.asc())
                .with_for_update(skip_locked=True, of=IPv4Address)
                .first()
            ),
        )
    return cast(
        IPv6Address | None,
        (
            db.query(IPv6Address)
            .outerjoin(
                IPAssignment,
                and_(
                    IPAssignment.ipv6_address_id == IPv6Address.id,
                    IPAssignment.is_active.is_(True),
                ),
            )
            .filter(IPv6Address.pool_id == pool_id)
            .filter(IPv6Address.is_reserved.is_(False))
            .filter(IPAssignment.id.is_(None))
            .order_by(IPv6Address.address.asc())
            .with_for_update(skip_locked=True, of=IPv6Address)
            .first()
        ),
    )


def _active_ipv4_assignment_exists(db: Session, address: IPv4Address) -> bool:
    """True if an *active* IPAssignment references this address.

    Suspended/blocked customers keep their assignment ACTIVE (only terminal
    release, admin release, ONT removal and the repair tools deactivate), so an
    inactive assignment means the address was released and is reusable. Only an
    active assignment blocks reuse.
    """
    return (
        db.query(IPAssignment.id)
        .filter(IPAssignment.ipv4_address_id == address.id)
        .filter(IPAssignment.is_active.is_(True))
        .first()
        is not None
    )


def _reactivation_address_is_valid(db: Session, address: IPv4Address | None) -> bool:
    """True if a subscriber's previously-released IPv4 address is still safe to
    reactivate. Guards the ``inactive -> active`` reuse so a long-dead assignment
    is not resurrected onto an address whose role changed (now reserved, a
    management/ONT address, or swallowed by an active routed block).
    """
    if address is None:
        return False
    if address.is_reserved or address.ont_unit_id is not None:
        return False
    if (address.allocation_type or "") == "management":
        return False
    # Don't resurrect onto an address already re-allocated to someone else — the
    # partial-unique index would reject a second active row anyway.
    if _active_ipv4_assignment_exists(db, address):
        return False
    try:
        ip_obj = ipaddress.ip_address(str(address.address))
    except ValueError:
        return False
    return not any(ip_obj in net for net in _active_additional_route_networks(db))


def _active_additional_route_networks(db: Session) -> list[ipaddress.IPv4Network]:
    """Active routed IP blocks (``SubscriberAdditionalRoute``).

    A routed block's hosts belong to a customer via ``Framed-Route``, NOT an
    ``IPAssignment`` — so without this guard on-demand allocation could hand a
    routed customer's IP (e.g. a routed /30's hosts) to someone else.
    """
    from app.models.network import SubscriberAdditionalRoute

    nets: list[ipaddress.IPv4Network] = []
    for (cidr,) in (
        db.query(SubscriberAdditionalRoute.cidr)
        .filter(SubscriberAdditionalRoute.is_active.is_(True))
        .all()
    ):
        try:
            net = ipaddress.ip_network(str(cidr), strict=False)
        except ValueError:
            continue
        if isinstance(net, ipaddress.IPv4Network):
            nets.append(net)
    return nets


def _pool_candidate_ipv4_cidrs(pool: IpPool) -> list[str]:
    """Ranges to draw on-demand IPv4 hosts from: active blocks first, then the
    pool CIDR as a fallback.

    UI availability rows are block-CIDR based (``web_network_ip._build_ipv4_range_rows``),
    so blocks are the authoritative ranges when a pool defines them; ``pool.cidr``
    is only used when there are none.
    """
    cidrs = [
        str(block.cidr)
        for block in (getattr(pool, "blocks", None) or [])
        if getattr(block, "is_active", True) and getattr(block, "cidr", None)
    ]
    if not cidrs and getattr(pool, "cidr", None):
        cidrs = [str(pool.cidr)]
    return cidrs


# Cap how many hosts we materialize per CIDR so a wide fallback CIDR (e.g. a /16
# pool with no blocks) can't pull a huge candidate set into memory.
_ON_DEMAND_HOST_SCAN_CAP = 4096


def _allocate_ipv4_on_demand(db: Session, pool: IpPool) -> IPv4Address | None:
    """Create-on-demand fallback: the lowest-numbered safe, free IPv4 host in the
    pool, materialized as an ``IPv4Address`` row.

    Runs only when ``_find_available_address`` finds no existing free row. It
    closes the display/allocate asymmetry — the availability view computes hosts
    straight from the CIDR, but allocation can only hand out *materialized* rows,
    so a pool whose rows were never expanded shows IPs "free" yet fails to assign
    them ("No available addresses" → no Framed-IP → customer never comes online).

    Safety rules:
      * skip network/broadcast unless the pool opts in (``allow_network_broadcast``);
      * never touch reserved, management (``allocation_type='management'``), or
        ONT-management (``ont_unit_id`` set) rows — ``wan`` and other unassigned
        rows are reusable;
      * never reuse an address that already has ANY assignment (active or
        inactive);
      * never hand out a host inside an active routed block
        (``SubscriberAdditionalRoute``) — those belong to a customer via
        ``Framed-Route``, not an ``IPAssignment``;
      * reuse a safe, unassigned row when one already exists for the chosen IP —
        including a loose ``pool_id IS NULL`` row, which it attaches to the pool —
        rather than creating a duplicate;
      * guard the ``ipv4_addresses.address`` unique constraint with a SAVEPOINT
        and skip to the next host on a concurrent-create race.
    """
    from app.services.web_network_ip import _pool_allows_network_broadcast

    allow_network_broadcast = _pool_allows_network_broadcast(pool)
    route_nets = _active_additional_route_networks(db)

    for cidr in _pool_candidate_ipv4_cidrs(pool):
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if network.version != 4:
            continue

        host_strs: list[str] = []
        scanned = 0
        for host in network if allow_network_broadcast else network.hosts():
            # Bound iterations even when every host is route-excluded.
            scanned += 1
            if scanned > _ON_DEMAND_HOST_SCAN_CAP * 2:
                break
            if any(host in net for net in route_nets):
                continue  # routed block — owned via Framed-Route, not assignable
            host_strs.append(str(host))
            if len(host_strs) >= _ON_DEMAND_HOST_SCAN_CAP:
                break
        if not host_strs:
            continue

        # One round-trip for any existing rows at these IPs (in this pool OR
        # loose), and one for which of them already carry an assignment.
        existing_by_ip = {
            row.address: row
            for row in db.query(IPv4Address)
            .filter(IPv4Address.address.in_(host_strs))
            .all()
        }
        assigned_ids: set = set()
        if existing_by_ip:
            assigned_ids = {
                r[0]
                for r in db.query(IPAssignment.ipv4_address_id)
                .filter(
                    IPAssignment.ipv4_address_id.in_(
                        [row.id for row in existing_by_ip.values()]
                    )
                )
                # Only an ACTIVE assignment blocks reuse; a released (inactive)
                # one leaves the address allocatable.
                .filter(IPAssignment.is_active.is_(True))
                .all()
            }

        for ip in host_strs:  # ipaddress yields ascending numeric order
            existing = existing_by_ip.get(ip)
            if existing is not None:
                if (
                    existing.is_reserved
                    or existing.ont_unit_id is not None
                    or (existing.allocation_type or "") == "management"
                    or existing.pool_id not in (None, pool.id)
                    or existing.id in assigned_ids
                ):
                    continue
                if existing.pool_id is None:
                    existing.pool_id = pool.id
                    db.flush()
                return existing
            # No row yet — materialize it, guarding the unique(address) race.
            candidate = IPv4Address(address=ip, pool_id=pool.id, is_reserved=False)
            try:
                with db.begin_nested():
                    db.add(candidate)
                    db.flush()
            except IntegrityError:
                # Concurrent create/claim of this IP — try the next host.
                continue
            return candidate
    return None


def _ensure_ip_assignment_for_version(
    db: Session,
    subscription: Subscription,
    ip_version: IPVersion,
    context: dict,
) -> tuple[IPAssignment | None, IPv4Address | IPv6Address | None]:
    assignment = (
        db.query(IPAssignment)
        .filter(IPAssignment.subscriber_id == subscription.subscriber_id)
        .filter(IPAssignment.ip_version == ip_version)
        .filter(IPAssignment.is_active.is_(True))
        .first()
    )
    # If no active assignment, check for an inactive one (e.g. from suspension)
    # and reactivate it to preserve IP stability across suspend/resume cycles.
    if not assignment:
        inactive_assignment = (
            db.query(IPAssignment)
            .filter(IPAssignment.subscriber_id == subscription.subscriber_id)
            .filter(IPAssignment.ip_version == ip_version)
            .filter(IPAssignment.is_active.is_(False))
            .order_by(IPAssignment.updated_at.desc())
            .first()
        )
        if inactive_assignment:
            candidate_addr = (
                inactive_assignment.ipv4_address
                if ip_version == IPVersion.ipv4
                else inactive_assignment.ipv6_address
            )
            if ip_version == IPVersion.ipv4 and not _reactivation_address_is_valid(
                db, candidate_addr
            ):
                # Address changed role since release — don't resurrect onto it;
                # fall through to a fresh allocation below.
                logger.info(
                    "Not reactivating assignment %s for subscription %s — address "
                    "%s is no longer allocatable; allocating fresh",
                    inactive_assignment.id,
                    subscription.id,
                    getattr(candidate_addr, "address", None),
                )
            else:
                inactive_assignment.is_active = True
                assignment = inactive_assignment
                logger.info(
                    "Reactivated existing IP assignment %s for subscription %s",
                    assignment.id,
                    subscription.id,
                )

    version_key = ip_version.value
    override_address_id = context.get(f"{version_key}_address_id")
    override_address_value = context.get(f"{version_key}_address")
    subscription_address_value = getattr(subscription, f"{version_key}_address") or None
    override_pool_id = context.get(f"{version_key}_pool_id")

    if assignment:
        address = (
            assignment.ipv4_address
            if ip_version == IPVersion.ipv4
            else assignment.ipv6_address
        )
        if (
            override_address_id
            and address
            and str(address.id) != str(override_address_id)
        ):
            raise HTTPException(
                status_code=400,
                detail=f"{version_key} address override does not match existing assignment.",
            )
        if (
            override_address_value
            and address
            and address.address != override_address_value
        ):
            raise HTTPException(
                status_code=400,
                detail=f"{version_key} address override does not match existing assignment.",
            )
        if address:
            setattr(subscription, f"{version_key}_address", address.address)
        return assignment, address

    address = None
    nas_id = (
        str(subscription.provisioning_nas_device_id)
        if getattr(subscription, "provisioning_nas_device_id", None)
        else context.get("nas_device_id")
    )
    pool = _resolve_pool_for_version(
        db, ip_version, override_pool_id, nas_device_id=nas_id
    )

    if override_address_id:
        address = _get_address_by_id(db, ip_version, override_address_id)

    manual_value = override_address_value or subscription_address_value
    if manual_value:
        parsed = _parse_ip_value(manual_value, f"{version_key} address")
        if parsed.version != (6 if ip_version == IPVersion.ipv6 else 4):
            raise HTTPException(
                status_code=400,
                detail=f"{version_key} address override does not match IP version.",
            )
        if address and address.address != manual_value:
            raise HTTPException(
                status_code=400,
                detail=f"{version_key} address override does not match address_id.",
            )
        if not address:
            address = _get_or_create_address_by_value(
                db, ip_version, manual_value, pool
            )

    if not address:
        if not pool:
            raise HTTPException(
                status_code=400,
                detail=f"No active {version_key} pool available for assignment.",
            )
        address = _find_available_address(db, ip_version, str(pool.id))
        if not address and ip_version == IPVersion.ipv4:
            # No materialized free row — create-on-demand the lowest free host
            # from the pool's blocks/CIDR (closes the display/allocate gap that
            # leaves "free" pools unable to assign).
            address = _allocate_ipv4_on_demand(db, pool)
        if not address:
            raise HTTPException(
                status_code=400,
                detail=f"No available {version_key} addresses in pool {pool.name}.",
            )

    # ``address.assignment`` is active-preferring (see the model relationship), so
    # only treat it as a conflict when it is ACTIVE. An inactive (released) row —
    # possibly from a different, terminated subscriber — does not block reuse: a
    # fresh active assignment is created alongside it under the partial-unique
    # index.
    active_assignment = (
        address.assignment
        if (address.assignment and address.assignment.is_active)
        else None
    )
    if active_assignment:
        same_subscription = active_assignment.subscription_id == subscription.id
        legacy_same_subscriber = (
            active_assignment.subscription_id is None
            and active_assignment.subscriber_id == subscription.subscriber_id
        )
        if not same_subscription and not legacy_same_subscriber:
            raise HTTPException(
                status_code=400,
                detail=f"{version_key} address is already assigned.",
            )
        if legacy_same_subscriber:
            active_assignment.subscription_id = subscription.id

    if active_assignment:
        assignment = active_assignment
    else:
        assignment_payload = IPAssignmentCreate(
            subscriber_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            service_address_id=subscription.service_address_id,
            ip_version=ip_version,
            ipv4_address_id=address.id if ip_version == IPVersion.ipv4 else None,
            ipv6_address_id=address.id if ip_version == IPVersion.ipv6 else None,
            prefix_length=_pool_prefix_length(
                pool_to_use := (cast(IpPool | None, address.pool) or pool)
            ),
            gateway=pool_to_use.gateway if pool_to_use else None,
            dns_primary=pool_to_use.dns_primary if pool_to_use else None,
            dns_secondary=pool_to_use.dns_secondary if pool_to_use else None,
        )
        assignment = network_service.ip_assignments.create(db, assignment_payload)

    setattr(subscription, f"{version_key}_address", address.address)
    return assignment, address


def _ensure_ip_assignments(
    db: Session, subscription_id: str | None, context: dict
) -> dict:
    if not subscription_id:
        return {}
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    updates: dict[str, object] = {}
    for ip_version in (IPVersion.ipv4, IPVersion.ipv6):
        assignment, address = _ensure_ip_assignment_for_version(
            db, subscription, ip_version, context
        )
        if assignment and address:
            version_key = ip_version.value
            updates.update(
                {
                    f"{version_key}_address": address.address,
                    f"{version_key}_address_id": str(address.id),
                    f"{version_key}_gateway": assignment.gateway,
                    f"{version_key}_dns_primary": assignment.dns_primary,
                    f"{version_key}_dns_secondary": assignment.dns_secondary,
                    f"{version_key}_prefix_length": assignment.prefix_length,
                }
            )

    # IPv6 prefix delegation (flag-gated by IPV6_PD_ENABLED): allocate a delegated
    # prefix into the app's PD inventory; the RADIUS writers emit
    # Delegated-IPv6-Prefix from there. No-op when disabled or no PD pool resolves.
    from app.services import ipv6_pd

    ipv6_pd.provision_pd_for_subscription(db, subscription)

    db.commit()
    return updates


def ensure_ip_assignments_for_subscription(
    db: Session, subscription_id: str, context: dict | None = None
) -> dict:
    """Allocate IP assignments for a subscription using pool defaults."""
    context = context or {}
    return _ensure_ip_assignments(db, subscription_id, context)


def ensure_ipv4_assignment_for_subscription(
    db: Session, subscription_id: str, context: dict | None = None
) -> IPAssignment | None:
    """Allocate ONLY the IPv4 assignment, in the caller's transaction.

    Two differences from :func:`ensure_ip_assignments_for_subscription`, both
    required by callers that provision a static IPv4 inside their own loop:

    * It does not walk IPv6. ``_ensure_ip_assignments`` fails closed when
      EITHER version has no active pool, so an IPv4-only caller would inherit a
      hard dependency on IPv6 pool availability it has no reason to carry.
    * It flushes rather than commits, so the assignment lives or dies with the
      caller's unit of work instead of being stranded by a later failure.

    Returns the assignment so the caller can assert the served column it now
    carries is backed by IPAM rather than standing alone.
    """
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    assignment, _address = _ensure_ip_assignment_for_version(
        db, subscription, IPVersion.ipv4, context or {}
    )
    db.flush()
    return assignment


def _extend_provisioning_context(
    db: Session,
    subscription_id: str | None,
    context: dict,
) -> dict:
    return extend_provisioning_context(db, subscription_id, context)


def resolve_workflow_for_service_order(
    db: Session, service_order: ServiceOrder
) -> ProvisioningWorkflow | None:
    default_workflow_id = settings_spec.resolve_value(
        db, SettingDomain.provisioning, "default_workflow_id"
    )
    if default_workflow_id:
        try:
            workflow_uuid = coerce_uuid(default_workflow_id)
        except (TypeError, ValueError):
            logger.warning("Invalid provisioning default_workflow_id setting value.")
            workflow_uuid = None
        if workflow_uuid:
            workflow = cast(
                ProvisioningWorkflow | None, db.get(ProvisioningWorkflow, workflow_uuid)
            )
            if workflow and workflow.is_active:
                return workflow
            logger.warning(
                "Provisioning default_workflow_id %s not found or inactive.",
                default_workflow_id,
            )
    vendor_value = settings_spec.resolve_value(
        db, SettingDomain.provisioning, "default_vendor"
    )
    vendor = None
    if vendor_value:
        try:
            vendor = validate_enum(vendor_value, ProvisioningVendor, "vendor")
        except HTTPException:
            logger.warning("Invalid provisioning default_vendor setting value.")
            vendor = None
    query = db.query(ProvisioningWorkflow).filter(
        ProvisioningWorkflow.is_active.is_(True)
    )
    if vendor:
        query = query.filter(ProvisioningWorkflow.vendor == vendor)
    return cast(
        ProvisioningWorkflow | None,
        query.order_by(ProvisioningWorkflow.created_at.asc()).first(),
    )
