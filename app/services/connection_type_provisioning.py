"""Connection-type-specific provisioning logic.

Generates different RADIUS attributes and NAS commands based on the
subscription's connection type (PPPoE, IPoE/DHCP Option 82, DHCP,
Static, Hotspot).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import (
    ConnectionType,
    NasConnectionRule,
    NasDevice,
    NasVendor,
    RadiusProfile,
    Subscription,
)
from app.services.nas import local_secret_policy
from app.services.nas.local_secret_policy import LocalSecretAction

logger = logging.getLogger(__name__)

# This builder's free-text ``action`` argument, mapped onto the boundary owner's
# typed vocabulary. An action absent from this map cannot touch a local secret.
_LOCAL_SECRET_ACTIONS: dict[str, LocalSecretAction] = {
    "create": LocalSecretAction.create,
    "delete": LocalSecretAction.delete,
    "suspend": LocalSecretAction.suspend,
    "unsuspend": LocalSecretAction.unsuspend,
    "change_ip": LocalSecretAction.change_ip,
}


# ---------------------------------------------------------------------------
# Connection type → default RADIUS attributes
# ---------------------------------------------------------------------------

# Standard RADIUS attribute constants
_SERVICE_TYPE_FRAMED = "Framed-User"
_SERVICE_TYPE_LOGIN = "Login-User"
_FRAMED_PROTOCOL_PPP = "PPP"
_NAS_PORT_TYPE_VIRTUAL = "Virtual"
_NAS_PORT_TYPE_ETHERNET = "Ethernet"


def _base_pppoe_attributes(
    profile: RadiusProfile | None,
    subscription: Subscription,
) -> list[dict[str, str]]:
    """Generate RADIUS reply attributes for PPPoE connections."""
    attrs: list[dict[str, str]] = [
        {"attribute": "Service-Type", "op": ":=", "value": _SERVICE_TYPE_FRAMED},
        {"attribute": "Framed-Protocol", "op": ":=", "value": _FRAMED_PROTOCOL_PPP},
    ]
    if profile:
        if profile.ip_pool_name:
            attrs.append(
                {"attribute": "Framed-Pool", "op": ":=", "value": profile.ip_pool_name}
            )
        if profile.ipv6_pool_name:
            attrs.append(
                {
                    "attribute": "Delegated-IPv6-Prefix-Pool",
                    "op": ":=",
                    "value": profile.ipv6_pool_name,
                }
            )
        if profile.session_timeout:
            attrs.append(
                {
                    "attribute": "Session-Timeout",
                    "op": ":=",
                    "value": str(profile.session_timeout),
                }
            )
        if profile.idle_timeout:
            attrs.append(
                {
                    "attribute": "Idle-Timeout",
                    "op": ":=",
                    "value": str(profile.idle_timeout),
                }
            )
        if profile.simultaneous_use:
            attrs.append(
                {
                    "attribute": "Simultaneous-Use",
                    "op": ":=",
                    "value": str(profile.simultaneous_use),
                }
            )
    if subscription.ipv4_address:
        attrs.append(
            {
                "attribute": "Framed-IP-Address",
                "op": ":=",
                "value": subscription.ipv4_address,
            }
        )
    return attrs


def _base_dhcp_attributes(
    profile: RadiusProfile | None,
    subscription: Subscription,
) -> list[dict[str, str]]:
    """Generate RADIUS reply attributes for plain DHCP connections."""
    attrs: list[dict[str, str]] = [
        {"attribute": "Service-Type", "op": ":=", "value": _SERVICE_TYPE_FRAMED},
    ]
    if profile:
        if profile.ip_pool_name:
            attrs.append(
                {"attribute": "Framed-Pool", "op": ":=", "value": profile.ip_pool_name}
            )
        if profile.session_timeout:
            attrs.append(
                {
                    "attribute": "Session-Timeout",
                    "op": ":=",
                    "value": str(profile.session_timeout),
                }
            )
    if subscription.ipv4_address:
        attrs.append(
            {
                "attribute": "Framed-IP-Address",
                "op": ":=",
                "value": subscription.ipv4_address,
            }
        )
    if subscription.mac_address:
        attrs.append(
            {
                "attribute": "Calling-Station-Id",
                "op": ":=",
                "value": subscription.mac_address,
            }
        )
    return attrs


def _base_ipoe_attributes(
    profile: RadiusProfile | None,
    subscription: Subscription,
) -> list[dict[str, str]]:
    """Generate RADIUS reply attributes for IPoE/DHCP Option 82 connections.

    IPoE uses MAC-based or Option 82-based authentication instead of
    PPPoE login/password. The relay agent info (circuit-id, remote-id)
    is matched to identify the subscriber.
    """
    attrs: list[dict[str, str]] = [
        {"attribute": "Service-Type", "op": ":=", "value": _SERVICE_TYPE_FRAMED},
        {"attribute": "NAS-Port-Type", "op": ":=", "value": _NAS_PORT_TYPE_ETHERNET},
    ]
    if profile:
        if profile.ip_pool_name:
            attrs.append(
                {"attribute": "Framed-Pool", "op": ":=", "value": profile.ip_pool_name}
            )
        if profile.vlan_id:
            attrs.append({"attribute": "Tunnel-Type", "op": ":=", "value": "VLAN"})
            attrs.append(
                {"attribute": "Tunnel-Medium-Type", "op": ":=", "value": "IEEE-802"}
            )
            attrs.append(
                {
                    "attribute": "Tunnel-Private-Group-Id",
                    "op": ":=",
                    "value": str(profile.vlan_id),
                }
            )
        if profile.inner_vlan_id:
            attrs.append(
                {
                    "attribute": "Tunnel-Private-Group-Id",
                    "op": "+=",
                    "value": str(profile.inner_vlan_id),
                }
            )
        if profile.session_timeout:
            attrs.append(
                {
                    "attribute": "Session-Timeout",
                    "op": ":=",
                    "value": str(profile.session_timeout),
                }
            )
    if subscription.ipv4_address:
        attrs.append(
            {
                "attribute": "Framed-IP-Address",
                "op": ":=",
                "value": subscription.ipv4_address,
            }
        )
    if subscription.mac_address:
        attrs.append(
            {
                "attribute": "Calling-Station-Id",
                "op": ":=",
                "value": subscription.mac_address,
            }
        )
    return attrs


def _base_static_attributes(
    profile: RadiusProfile | None,
    subscription: Subscription,
) -> list[dict[str, str]]:
    """Generate RADIUS reply attributes for static IP connections."""
    attrs: list[dict[str, str]] = [
        {"attribute": "Service-Type", "op": ":=", "value": _SERVICE_TYPE_FRAMED},
    ]
    if subscription.ipv4_address:
        attrs.append(
            {
                "attribute": "Framed-IP-Address",
                "op": ":=",
                "value": subscription.ipv4_address,
            }
        )
    if subscription.ipv6_address:
        attrs.append(
            {
                "attribute": "Framed-IPv6-Prefix",
                "op": ":=",
                "value": subscription.ipv6_address,
            }
        )
    if profile:
        if profile.session_timeout:
            attrs.append(
                {
                    "attribute": "Session-Timeout",
                    "op": ":=",
                    "value": str(profile.session_timeout),
                }
            )
    return attrs


def _base_hotspot_attributes(
    profile: RadiusProfile | None,
    subscription: Subscription,
) -> list[dict[str, str]]:
    """Generate RADIUS reply attributes for hotspot connections.

    Hotspot connections use Service-Type=Login and include
    MikroTik-specific hotspot attributes for bandwidth limiting
    and session management.
    """
    attrs: list[dict[str, str]] = [
        {"attribute": "Service-Type", "op": ":=", "value": _SERVICE_TYPE_LOGIN},
    ]
    if profile:
        if profile.ip_pool_name:
            attrs.append(
                {"attribute": "Framed-Pool", "op": ":=", "value": profile.ip_pool_name}
            )
        if profile.session_timeout:
            attrs.append(
                {
                    "attribute": "Session-Timeout",
                    "op": ":=",
                    "value": str(profile.session_timeout),
                }
            )
        if profile.idle_timeout:
            attrs.append(
                {
                    "attribute": "Idle-Timeout",
                    "op": ":=",
                    "value": str(profile.idle_timeout),
                }
            )
        if profile.simultaneous_use:
            attrs.append(
                {
                    "attribute": "Simultaneous-Use",
                    "op": ":=",
                    "value": str(profile.simultaneous_use),
                }
            )
        # MikroTik hotspot-specific: advertise the profile name as the group
        if profile.name:
            attrs.append(
                {"attribute": "Mikrotik-Group", "op": ":=", "value": profile.name}
            )
    if subscription.ipv4_address:
        attrs.append(
            {
                "attribute": "Framed-IP-Address",
                "op": ":=",
                "value": subscription.ipv4_address,
            }
        )
    if subscription.mac_address:
        attrs.append(
            {
                "attribute": "Calling-Station-Id",
                "op": ":=",
                "value": subscription.mac_address,
            }
        )
    return attrs


# Map connection types to their base attribute generators
_CONNECTION_TYPE_ATTRS = {
    ConnectionType.pppoe: _base_pppoe_attributes,
    ConnectionType.dhcp: _base_dhcp_attributes,
    ConnectionType.ipoe: _base_ipoe_attributes,
    ConnectionType.static: _base_static_attributes,
    ConnectionType.hotspot: _base_hotspot_attributes,
}


def _append_vendor_attributes(
    attrs: list[dict[str, str]],
    profile: RadiusProfile | None,
    connection_type: ConnectionType,
) -> None:
    """Append vendor-specific RADIUS attributes (MikroTik, Huawei, etc.)."""
    if not profile:
        return
    # MikroTik Rate-Limit (applies to PPPoE and hotspot mainly)
    if connection_type in (
        ConnectionType.pppoe,
        ConnectionType.hotspot,
        ConnectionType.ipoe,
    ):
        from app.services.enforcement import _build_mikrotik_rate_limit

        rate_limit = _build_mikrotik_rate_limit(profile)
        if rate_limit:
            attrs.append(
                {"attribute": "Mikrotik-Rate-Limit", "op": ":=", "value": rate_limit}
            )

    if profile.mikrotik_address_list:
        attrs.append(
            {
                "attribute": "Mikrotik-Address-List",
                "op": ":=",
                "value": profile.mikrotik_address_list,
            }
        )

    # VLAN attributes for PPPoE (if specified on profile)
    if connection_type == ConnectionType.pppoe and profile.vlan_id:
        attrs.append({"attribute": "Tunnel-Type", "op": ":=", "value": "VLAN"})
        attrs.append(
            {"attribute": "Tunnel-Medium-Type", "op": ":=", "value": "IEEE-802"}
        )
        attrs.append(
            {
                "attribute": "Tunnel-Private-Group-Id",
                "op": ":=",
                "value": str(profile.vlan_id),
            }
        )


# ---------------------------------------------------------------------------
# Connection type resolution
# ---------------------------------------------------------------------------


def resolve_connection_type(
    db: Session,
    subscription: Subscription,
    nas_device: NasDevice | None = None,
) -> ConnectionType:
    """Resolve the effective connection type for a subscription.

    Resolution order:
    1. RADIUS profile's explicit connection_type
    2. NAS connection rule matching the subscription
    3. NAS device's default_connection_type
    4. Fallback to PPPoE
    """
    # 1. Check profile-level connection type
    if subscription.radius_profile_id:
        profile = db.get(RadiusProfile, subscription.radius_profile_id)
        if profile and profile.connection_type:
            return profile.connection_type

    # 2. Resolve NAS device
    if not nas_device and subscription.provisioning_nas_device_id:
        nas_device = db.get(NasDevice, subscription.provisioning_nas_device_id)

    if nas_device:
        # 3. Check NAS connection rules (ordered by priority)
        rules = (
            db.query(NasConnectionRule)
            .filter(NasConnectionRule.nas_device_id == nas_device.id)
            .filter(NasConnectionRule.is_active.is_(True))
            .order_by(NasConnectionRule.priority.asc())
            .all()
        )
        for rule in rules:
            if _rule_matches(rule, subscription):
                if rule.connection_type:
                    return rule.connection_type

        # 4. NAS device default
        if nas_device.default_connection_type:
            return nas_device.default_connection_type

    return ConnectionType.pppoe


def _rule_matches(rule: NasConnectionRule, subscription: Subscription) -> bool:
    """Check if a NAS connection rule matches a subscription.

    match_expression supports simple patterns:
    - "*" matches everything
    - "login:prefix_*" matches login starting with prefix_
    - "mac:AA:BB:*" matches MAC prefix
    """
    if not rule.match_expression:
        return True  # No expression = matches all
    expr = rule.match_expression.strip()
    if expr == "*":
        return True
    if expr.startswith("login:") and subscription.login:
        pattern = expr[6:]
        if pattern.endswith("*"):
            return subscription.login.startswith(pattern[:-1])
        return subscription.login == pattern
    if expr.startswith("mac:") and subscription.mac_address:
        pattern = expr[4:]
        if pattern.endswith("*"):
            return subscription.mac_address.upper().startswith(pattern[:-1].upper())
        return subscription.mac_address.upper() == pattern.upper()
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _append_option82_attributes(
    db: Session,
    attrs: list[dict[str, str]],
    subscription: Subscription,
    connection_type: ConnectionType,
) -> None:
    """Append DHCP Option 82 relay agent attributes for IPoE connections.

    Looks up the subscriber's access credentials for circuit-id and
    remote-id fields, then generates the corresponding RADIUS check
    attributes for relay agent matching.
    """
    if connection_type != ConnectionType.ipoe:
        return

    from app.models.catalog import AccessCredential

    credential = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscription.subscriber_id)
        .filter(AccessCredential.is_active.is_(True))
        .filter(
            (AccessCredential.circuit_id.isnot(None))
            | (AccessCredential.remote_id.isnot(None))
        )
        .first()
    )
    if not credential:
        return
    if credential.circuit_id:
        attrs.append(
            {
                "attribute": "Agent-Circuit-Id",
                "op": ":=",
                "value": credential.circuit_id,
            }
        )
    if credential.remote_id:
        attrs.append(
            {
                "attribute": "Agent-Remote-Id",
                "op": ":=",
                "value": credential.remote_id,
            }
        )


def _append_additional_routes(
    db: Session,
    attrs: list[dict[str, str]],
    subscription: Subscription,
) -> None:
    """Append one Framed-Route per active additional routed block.

    Reproduces the legacy route format from ``services_internet.ipv4_route``: the
    BNG installs each as a route to this session's PPP interface and tears it
    down on disconnect. The gateway is ``0.0.0.0`` ("via this session") rather
    than the customer's primary, because primaries are CGNAT and can't be
    recursed through. ``+=`` so multiple blocks coexist on one Access-Accept.

    Emitted only for ``active`` subscribers — blocked/disabled/suspended get a
    captive-portal reply instead, and their routes must not leak into it. The
    rows still exist, so re-activation releases them automatically.
    """
    from app.models.network import SubscriberAdditionalRoute
    from app.models.subscriber import Subscriber, SubscriberStatus

    if subscription.subscriber_id is None:
        return

    subscriber = subscription.subscriber or db.get(
        Subscriber, subscription.subscriber_id
    )
    if subscriber is None or subscriber.status != SubscriberStatus.active:
        return

    primary = (subscription.ipv4_address or "").strip()
    primary_host = f"{primary}/32" if primary else None

    routes = (
        db.query(SubscriberAdditionalRoute)
        .filter(
            SubscriberAdditionalRoute.subscription_id == subscription.id,
            SubscriberAdditionalRoute.is_active.is_(True),
        )
        .all()
    )
    if not routes:
        from sqlalchemy import func

        from app.models.catalog import SubscriptionStatus

        service_count = (
            db.query(func.count(Subscription.id))
            .filter(
                Subscription.subscriber_id == subscription.subscriber_id,
                Subscription.status.in_(
                    (SubscriptionStatus.pending, SubscriptionStatus.active)
                ),
            )
            .scalar()
            or 0
        )
        if service_count == 1:
            routes = (
                db.query(SubscriberAdditionalRoute)
                .filter(
                    SubscriberAdditionalRoute.subscriber_id
                    == subscription.subscriber_id,
                    SubscriberAdditionalRoute.subscription_id.is_(None),
                    SubscriberAdditionalRoute.is_active.is_(True),
                )
                .all()
            )

    seen: set[str] = set()
    for route in routes:
        cidr = (route.cidr or "").strip()
        # Skip blanks, the primary /32 (defence in depth vs a mis-tagged row),
        # and duplicates.
        if not cidr or cidr == primary_host or cidr in seen:
            continue
        seen.add(cidr)
        attrs.append(
            {
                "attribute": "Framed-Route",
                "op": "+=",
                "value": f"{cidr} 0.0.0.0 {route.metric or 1}",
            }
        )


# Interim-accounting cadence — kept in sync with
# radius_population.ACCT_INTERIM_SECONDS so both writers agree.
_ACCT_INTERIM_SECONDS = "300"


def _offer_rate_limit_fallback(db: Session, subscription: Subscription) -> str | None:
    """Offer-derived MikroTik rate-limit ('{down}M/{up}M'), mirroring the
    authoritative sweep's fallback when the profile defines no rate-limit."""
    offer_id = getattr(subscription, "offer_id", None)
    if not offer_id:
        return None
    from app.models.catalog import CatalogOffer

    offer = db.get(CatalogOffer, offer_id)
    down = getattr(offer, "speed_download_mbps", None) if offer else None
    up = getattr(offer, "speed_upload_mbps", None) if offer else None
    if down and up:
        return f"{down}M/{up}M"
    return None


def _active_ipassignment_ipv4(db: Session, subscription: Subscription) -> str | None:
    """The subscriber's active IPAM IPv4 address, or None.

    Used as a fallback when ``subscription.ipv4_address`` is empty/stale, mirroring
    the authoritative ``radius_population`` sweep.
    """
    subscriber_id = getattr(subscription, "subscriber_id", None)
    if not subscriber_id:
        return None
    from app.models.network import IPAssignment, IPv4Address, IPVersion

    row = db.execute(
        select(IPv4Address.address)
        .join(IPAssignment, IPAssignment.ipv4_address_id == IPv4Address.id)
        .where(IPAssignment.subscriber_id == subscriber_id)
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ip_version == IPVersion.ipv4)
        .limit(1)
    ).scalar()
    return str(row) if row else None


def _ensure_framed_ip(
    db: Session, attrs: list[dict[str, str]], subscription: Subscription
) -> None:
    """Guarantee a usable Framed-IP-Address — never a stale/empty/0.0.0.0 one.

    The per-connection-type builders emit Framed-IP-Address straight from
    ``subscription.ipv4_address``, so a cleared column drops the attribute and a
    "0.0.0.0" column emits a bogus one — either way the customer is de-IP'd and the
    BNG tears the session down. Mirror the authoritative sweep: prefer the column
    when usable, else fall back to the active IPAM assignment, and never emit
    0.0.0.0.
    """
    column_ip = (subscription.ipv4_address or "").strip()
    if column_ip == "0.0.0.0":  # nosec B104  # noqa: S104 — IP-string compare
        column_ip = ""
    effective = column_ip or (_active_ipassignment_ipv4(db, subscription) or "")

    framed = [a for a in attrs if a["attribute"] == "Framed-IP-Address"]
    if effective:
        if framed:
            for a in framed:
                a["value"] = effective
        else:
            attrs.append(
                {"attribute": "Framed-IP-Address", "op": ":=", "value": effective}
            )
    else:
        # No usable IP anywhere — drop any 0.0.0.0 a builder added.
        attrs[:] = [
            a
            for a in attrs
            if not (
                a["attribute"] == "Framed-IP-Address" and a["value"] == "0.0.0.0"  # nosec B104  # noqa: S104 — IP-compare
            )
        ]


def build_radius_reply_attributes(
    db: Session,
    subscription: Subscription,
    profile: RadiusProfile | None = None,
    nas_device: NasDevice | None = None,
) -> list[dict[str, str]]:
    """Build the full set of RADIUS reply attributes for a subscription.

    Generates connection-type-specific base attributes, then appends
    vendor-specific, Option 82, and profile-level custom attributes.

    Args:
        db: Database session
        subscription: The subscription to build attributes for
        profile: Optional explicit profile (resolved automatically if None)
        nas_device: Optional NAS device (resolved from subscription if None)

    Returns:
        List of dicts with keys: attribute, op, value
    """
    if not profile and subscription.radius_profile_id:
        profile = db.get(RadiusProfile, subscription.radius_profile_id)

    connection_type = resolve_connection_type(db, subscription, nas_device)

    # Get base attributes for this connection type
    attr_fn = _CONNECTION_TYPE_ATTRS.get(connection_type, _base_pppoe_attributes)
    attrs = attr_fn(profile, subscription)

    # Parity with the authoritative radius_population sweep: never let a stale or
    # 0.0.0.0 ipv4_address column drop Framed-IP (BNG teardown), and emit the same
    # interim-accounting cadence so the two writers don't disagree.
    _ensure_framed_ip(db, attrs, subscription)
    if not any(a["attribute"] == "Acct-Interim-Interval" for a in attrs):
        attrs.append(
            {
                "attribute": "Acct-Interim-Interval",
                "op": ":=",
                "value": _ACCT_INTERIM_SECONDS,
            }
        )

    # IPv6 PD: emit the subscriber's delegated prefix from the app's PD inventory
    # (the app — not FreeRADIUS dynamic pools — is the source of truth). Flag-gated
    # and additive (only when the subscriber has an assigned prefix).
    if not any(a["attribute"] == "Delegated-IPv6-Prefix" for a in attrs):
        from app.services.ipv6_pd import (
            active_delegated_prefix_for_subscription,
            pd_enabled,
        )

        if pd_enabled():
            pd = active_delegated_prefix_for_subscription(
                db,
                subscription.id,
                subscriber_id=subscription.subscriber_id,
            )
            if pd:
                attrs.append(
                    {"attribute": "Delegated-IPv6-Prefix", "op": ":=", "value": pd}
                )

    # Append vendor-specific attributes
    _append_vendor_attributes(attrs, profile, connection_type)

    # Parity: when the profile defines no rate-limit, fall back to the offer's
    # speeds (the authoritative sweep does this) so a rate-limited connection type
    # isn't left unthrottled until the next 15-min sweep corrects it.
    if connection_type in (
        ConnectionType.pppoe,
        ConnectionType.hotspot,
        ConnectionType.ipoe,
    ) and not any(a["attribute"] == "Mikrotik-Rate-Limit" for a in attrs):
        offer_rate = _offer_rate_limit_fallback(db, subscription)
        if offer_rate:
            attrs.append(
                {"attribute": "Mikrotik-Rate-Limit", "op": ":=", "value": offer_rate}
            )

    # Append Option 82 relay agent attributes for IPoE
    _append_option82_attributes(db, attrs, subscription, connection_type)

    # Append Framed-Route per additional routed IP block.
    _append_additional_routes(db, attrs, subscription)

    # Append any custom attributes from the profile
    if profile:
        from app.models.catalog import RadiusAttribute

        custom_attrs = (
            db.query(RadiusAttribute)
            .filter(RadiusAttribute.profile_id == profile.id)
            .all()
        )
        seen = {a["attribute"].lower() for a in attrs}
        for attr in custom_attrs:
            if attr.attribute.lower() not in seen:
                attrs.append(
                    {
                        "attribute": attr.attribute,
                        "op": attr.operator or ":=",
                        "value": attr.value,
                    }
                )
                seen.add(attr.attribute.lower())

    logger.debug(
        "Built %d RADIUS attributes for subscription %s (type=%s).",
        len(attrs),
        subscription.id,
        connection_type.value,
    )
    return attrs


def build_nas_provisioning_commands(
    db: Session,
    subscription: Subscription,
    nas_device: NasDevice,
    profile: RadiusProfile | None = None,
    action: str = "create",
) -> list[str]:
    """Build NAS CLI commands for provisioning a subscription.

    Generates vendor-specific commands based on connection type.

    Args:
        db: Database session
        subscription: The subscription to provision
        nas_device: Target NAS device
        profile: RADIUS profile (resolved if None)
        action: create, delete, suspend, unsuspend

    Returns:
        List of CLI command strings
    """
    from app.models.catalog import NasVendor

    if not profile and subscription.radius_profile_id:
        profile = db.get(RadiusProfile, subscription.radius_profile_id)

    connection_type = resolve_connection_type(db, subscription, nas_device)
    commands: list[str] = []

    if nas_device.vendor == NasVendor.mikrotik:
        commands = _mikrotik_commands(subscription, profile, connection_type, action)
    else:
        # Generic / other vendors: log and return empty (use provisioning templates)
        logger.debug(
            "No built-in commands for vendor %s, use provisioning templates.",
            nas_device.vendor.value,
        )

    return commands


def _mikrotik_commands(
    subscription: Subscription,
    profile: RadiusProfile | None,
    connection_type: ConnectionType,
    action: str,
) -> list[str]:
    """Generate MikroTik RouterOS commands for provisioning."""
    from app.services.enforcement import _sanitize_routeros_value

    login = _sanitize_routeros_value(subscription.login or "")
    ip = _sanitize_routeros_value(subscription.ipv4_address or "")
    mac = _sanitize_routeros_value(subscription.mac_address or "")
    prof_name = _sanitize_routeros_value(profile.name if profile else "")
    commands: list[str] = []

    if connection_type == ConnectionType.pppoe:
        # No per-subscriber local secret is ever emitted. A RouterOS /ppp secret
        # shadows the RADIUS projection rather than supplementing it, so the NAS
        # carries no customer record at all — see
        # app/services/nas/local_secret_policy.py and
        # docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md. Session disconnect on
        # suspend belongs to access.session_enforcement (CoA), not to a local
        # secret toggle; removal of a pre-existing secret belongs to the
        # reviewed cleanup operation.
        ruling = local_secret_policy.decide(
            vendor=NasVendor.mikrotik,
            connection_type=connection_type,
            action=_LOCAL_SECRET_ACTIONS.get(action),
        )
        if not ruling.emits_commands:
            logger.info("nas_local_secret_suppressed", extra=ruling.as_log_extra())
        return []

    if connection_type == ConnectionType.dhcp:
        if action == "create" and ip:
            parts = [f"/ip dhcp-server lease add address={ip}"]
            if mac:
                parts.append(f"mac-address={mac}")
            if profile and profile.mikrotik_rate_limit:
                from app.services.enforcement import _build_mikrotik_rate_limit

                rate = _build_mikrotik_rate_limit(profile)
                if rate:
                    parts.append(f"rate-limit={_sanitize_routeros_value(rate)}")
            commands.append(" ".join(parts))
        elif action == "delete" and ip:
            commands.append(f"/ip dhcp-server lease remove [find address={ip}]")
        elif action == "suspend" and ip:
            commands.append(
                f"/ip dhcp-server lease set [find address={ip}] disabled=yes"
            )
        elif action == "unsuspend" and ip:
            commands.append(
                f"/ip dhcp-server lease set [find address={ip}] disabled=no"
            )

    elif connection_type == ConnectionType.hotspot:
        if action == "create":
            parts = [f'/ip hotspot user add name="{login}"']
            if prof_name:
                parts.append(f'profile="{prof_name}"')
            if ip:
                parts.append(f"address={ip}")
            commands.append(" ".join(parts))
        elif action == "delete":
            commands.append(f'/ip hotspot user remove [find name="{login}"]')
        elif action == "suspend":
            commands.append(f'/ip hotspot user set [find name="{login}"] disabled=yes')
            commands.append(f'/ip hotspot active remove [find user="{login}"]')
        elif action == "unsuspend":
            commands.append(f'/ip hotspot user set [find name="{login}"] disabled=no')

    elif connection_type == ConnectionType.static:
        if action == "suspend" and ip:
            # Conditional add so a repeated suspend (e.g. duplicate event) is a
            # no-op instead of a "already have such entry" error — matching the
            # enforcement address-list helper.
            commands.append(
                f":if ([:len [/ip firewall address-list find "
                f'list="blocked-subscribers" address="{ip}"]] = 0) '
                f"do={{/ip firewall address-list add "
                f'list="blocked-subscribers" address="{ip}"}}'
            )
        elif action == "unsuspend" and ip:
            commands.append(
                f"/ip firewall address-list remove [find list=blocked-subscribers address={ip}]"
            )

    elif connection_type == ConnectionType.ipoe:
        if action == "create" and ip:
            parts = [f"/ip dhcp-server lease add address={ip}"]
            if mac:
                parts.append(f"mac-address={mac}")
            parts.append("use-src-mac=yes")
            commands.append(" ".join(parts))
        elif action == "delete" and ip:
            commands.append(f"/ip dhcp-server lease remove [find address={ip}]")

    return commands
