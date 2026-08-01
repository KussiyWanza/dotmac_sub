"""Populate radcheck + radreply from dotmac_sub authoritative joins.

Source of truth: dotmac_sub Postgres
  - Subscription (login, status, ipv4_address, offer_id, radius_profile_id)
  - AccessCredential (username, secret_hash — Fernet-encrypted)
  - CatalogOffer (speed_download_mbps, speed_upload_mbps)
  - RadiusProfile (mikrotik_rate_limit, idle_timeout, simultaneous_use)

No external BSS calls. No double-source. Idempotent (DELETE + INSERT per user).

Usage:
  docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
      python -m app.services.radius_population --execute
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import cast

import psycopg
from sqlalchemy import Boolean, Column, Integer, String, delete, insert, select, text
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.models.catalog import (
    AccessCredential,
    CatalogOffer,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.services.credential_crypto import (
    decrypt_credential_with_key,
    get_encryption_key,
)
from app.services.external_radius_targets import (
    active_external_radius_targets,
    assert_legacy_target_alignment,
    external_radius_table,
    get_external_engine,
)
from app.services.radius_access_state import ACTIVE_STATUSES, BLOCKED_STATUSES
from app.services.radius_address_lists import (
    DEFAULT_SUSPENDED_ADDRESS_LIST,
    suspended_address_list,
)
from app.services.radius_projection_planner import plan_login_radius_projections

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ACCT_INTERIM_SECONDS = 300  # 5 min Acct-Interim-Update cadence
SUSPENDED_ADDRESS_LIST = DEFAULT_SUSPENDED_ADDRESS_LIST

RadiusAttribute = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class RadiusProjectionWorkItem:
    """Typed per-login input consumed by the sole RADIUS projection writer."""

    username: str
    cleartext_password: str
    check_attrs: tuple[RadiusAttribute, ...]
    reply_attrs: tuple[RadiusAttribute, ...]
    blocked: bool
    status: SubscriptionStatus
    mode: str
    profile_group: str | None


def _result_count(result: Mapping[str, object], key: str) -> int:
    """Read an integer counter from a structured projection result."""
    value = result.get(key, 0)
    return value if isinstance(value, int) else 0


def _increment_result_count(
    result: dict[str, object], key: str, amount: int = 1
) -> None:
    result[key] = _result_count(result, key) + amount


def _captive_redirect_allowed(subscriber: object | None) -> bool:
    """Compatibility adapter to the canonical captive eligibility owner."""
    if subscriber is None:
        return False
    from app.models.subscriber import Subscriber
    from app.services.walled_garden_policy import captive_account_eligible

    return captive_account_eligible(cast(Subscriber, subscriber))


def _rate_limit(offer: CatalogOffer, profile: RadiusProfile | None) -> str | None:
    """Pick MikroTik rate-limit string: profile override > offer-derived > None."""
    if profile and profile.mikrotik_rate_limit:
        return profile.mikrotik_rate_limit
    if offer and offer.speed_download_mbps and offer.speed_upload_mbps:
        return f"{offer.speed_download_mbps}M/{offer.speed_upload_mbps}M"
    return None


def _effective_profile(
    cred: AccessCredential | None,
    subscription_profile: RadiusProfile | None,
    profiles_by_id: dict,
) -> RadiusProfile | None:
    """Resolve the RADIUS profile that should shape a credential's radreply.

    A dunning/FUP throttle is applied by setting ``AccessCredential.
    radius_profile_id``; that credential-level override must win over the
    subscription profile, otherwise the authoritative populate() sweep rebuilds
    radreply from the offer/subscription speed and silently reverts the throttle
    (SP-2). Mirrors the credential>subscription precedence in
    ``enforcement.resolve_radius_profile``. Falls back to the subscription
    profile when the credential carries no override (the normal, non-throttled
    case — behaviour unchanged), or when the referenced profile can't be found.
    """
    if cred is not None and cred.radius_profile_id:
        override = profiles_by_id.get(cred.radius_profile_id)
        if override is not None:
            return override
    return subscription_profile


def simultaneous_use_enforcement_enabled(db) -> bool:
    """Return the database-owned cutover gate for RADIUS concurrency checks."""
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    return (
        settings_spec.resolve_value(
            db,
            SettingDomain.radius,
            "simultaneous_use_enforcement_enabled",
        )
        is True
    )


def _simultaneous_use_value(profile: RadiusProfile | None) -> str:
    """Resolve the profile limit, preserving the historical default of one."""
    return str((profile.simultaneous_use if profile else None) or 1)


def _radcheck_policy_attrs(
    profile: RadiusProfile | None,
    *,
    simultaneous_use_enabled: bool,
) -> list[RadiusAttribute]:
    """Build FreeRADIUS check/control attributes for an authenticating login."""
    if not simultaneous_use_enabled:
        return []
    return [("Simultaneous-Use", ":=", _simultaneous_use_value(profile))]


def _radreply_attrs(
    sub: Subscription,
    offer: CatalogOffer,
    profile: RadiusProfile | None,
    subscriber_blocked: bool = False,
    captive_redirect_enabled: bool = False,
    additional_routes: list[tuple[str, int | None]] | None = None,
    framed_ipv4: str | None = None,
    framed_ipv6: str | None = None,
    delegated_ipv6: str | None = None,
    suspended_list_name: str = SUSPENDED_ADDRESS_LIST,
    simultaneous_use_enabled: bool = False,
) -> list[RadiusAttribute]:
    """Compute the list of (attribute, op, value) tuples for radreply.

    `subscriber_blocked`: customer-level block.
    Customer-level block dominates: even if subscription is active, the customer
    gets blocked RADIUS treatment.

    `captive_redirect_enabled`: per-customer opt-in for the soft walled-garden
    captive redirect. Only opted-in blocked subscribers get the
    Mikrotik-Address-List=suspended attribute; non-opted blocked subscribers are
    hard-rejected in radcheck (see populate()), so they get no captive radreply.

    `additional_routes`: extra routed IP blocks (subscriber_additional_routes) as
    (cidr, metric) tuples. Emitted as Framed-Route for non-walled-garden subs only
    — this is the authoritative single-writer, so the routes must be emitted here
    or the periodic sweep wipes what build_radius_reply_attributes wrote.

    `framed_ipv4`: the IP to emit as Framed-IP-Address. Defaults to
    `sub.ipv4_address`, but the caller passes an active-IPAssignment fallback so a
    stale/cleared `subscriptions.ipv4_address` does NOT silently drop Framed-IP
    (which de-IPs the customer and the BNG tears the session down). "0.0.0.0" is
    treated as no address.

    `framed_ipv6`: the prefix to emit as Framed-IPv6-Prefix; defaults to
    `sub.ipv6_address`. This authoritative sweep must emit it too — otherwise the
    sweep's `DELETE FROM radreply` wipes the Framed-IPv6-Prefix that
    build_radius_reply_attributes wrote on activation, so IPv6 RADIUS could never
    be durable (same wipe hazard the Framed-Route handling already guards against).

    `simultaneous_use_enabled`: cutover gate for the corrected FreeRADIUS
    check/control projection. Before cutover, retain the legacy radreply row so
    deployment alone does not silently change fleet-wide authentication policy.
    After cutover, `_radcheck_policy_attrs` owns the attribute and this reply
    builder omits it.
    """
    ipv4 = framed_ipv4 if framed_ipv4 is not None else sub.ipv4_address
    if ipv4 == "0.0.0.0":  # nosec B104  # noqa: S104 — IP-string compare, not a bind
        ipv4 = None
    ipv6 = (
        framed_ipv6 if framed_ipv6 is not None else getattr(sub, "ipv6_address", None)
    )
    ipv6 = (str(ipv6).strip() or None) if ipv6 else None

    attrs: list[RadiusAttribute] = [
        ("Service-Type", ":=", "Framed-User"),
        ("Framed-Protocol", ":=", "PPP"),
        ("Acct-Interim-Interval", ":=", str(ACCT_INTERIM_SECONDS)),
    ]

    if ipv4:
        attrs.append(("Framed-IP-Address", ":=", ipv4))
    if ipv6:
        attrs.append(("Framed-IPv6-Prefix", ":=", ipv6))
    delegated = (str(delegated_ipv6).strip() or None) if delegated_ipv6 else None
    if delegated:
        attrs.append(("Delegated-IPv6-Prefix", ":=", delegated))

    rate = _rate_limit(offer, profile)
    if rate:
        attrs.append(("Mikrotik-Rate-Limit", ":=", rate))

    if not simultaneous_use_enabled:
        attrs.append(("Simultaneous-Use", ":=", _simultaneous_use_value(profile)))

    if profile and profile.idle_timeout:
        attrs.append(("Idle-Timeout", ":=", str(profile.idle_timeout)))

    # Soft captive walled-garden — only for blocked subscribers who OPTED IN
    # (per-customer captive_redirect_enabled). Non-opted blocked subscribers get
    # a hard reject in radcheck instead, so they never reach this radreply.
    is_blocked = subscriber_blocked or sub.status in (
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
    )
    if is_blocked and captive_redirect_enabled:
        attrs.append(("Mikrotik-Address-List", ":=", suspended_list_name))
    elif additional_routes and not is_blocked:
        # Additional routed IP blocks -> one Framed-Route each (+= so multiple
        # coexist; gateway 0.0.0.0 = via this session, since primaries are CGNAT).
        # Not for walled-garden subs: a captive customer must not route extra IPs.
        primary_host = f"{ipv4}/32" if ipv4 else None
        seen: set[str] = set()
        for cidr, metric in additional_routes:
            cidr = (cidr or "").strip()
            if not cidr or cidr == primary_host or cidr in seen:
                continue
            seen.add(cidr)
            attrs.append(("Framed-Route", "+=", f"{cidr} 0.0.0.0 {metric or 1}"))

    return attrs


class RadiusProjectionIncomplete(RuntimeError):
    """At least one target failed; callers must not issue customer CoA."""

    def __init__(self, outcomes: list[dict[str, object]]) -> None:
        self.outcomes = outcomes
        failed = [str(item["target_name"]) for item in outcomes if not item["ok"]]
        super().__init__(
            "External RADIUS projection incomplete for target(s): " + ", ".join(failed)
        )


class RadiusProjectionUnbuildable(RuntimeError):
    """One or more desired active/captive logins could not be rebuilt."""


def require_complete_projection(result: Mapping[str, object]) -> None:
    if result.get("projection_complete") is not True:
        raise RadiusProjectionUnbuildable(
            "RADIUS projection contains one or more unbuildable logins"
        )


def _projection_rows_for_item(
    item: RadiusProjectionWorkItem,
    config: Mapping[str, object],
    *,
    access_groups: Mapping[str, str],
    access_group_priority: int,
    group_routing_enabled: bool,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Build the exact external rows used by both writer and comparator."""
    if item.mode == "reject":
        check_rows: list[dict[str, object]] = [
            {
                "username": item.username,
                "attribute": "Auth-Type",
                "op": ":=",
                "value": "Reject",
            }
        ]
        reply_rows: list[dict[str, object]] = []
    else:
        check_rows = [
            {
                "username": item.username,
                "attribute": config["password_attribute"],
                "op": config["password_op"],
                "value": item.cleartext_password,
            }
        ]
        check_rows.extend(
            {
                "username": item.username,
                "attribute": attribute,
                "op": op,
                "value": value,
            }
            for attribute, op, value in item.check_attrs
        )
        reply_rows = [
            {
                "username": item.username,
                "attribute": attribute,
                "op": op or config["default_reply_op"],
                "value": value,
            }
            for attribute, op, value in item.reply_attrs
        ]

    group_rows: list[dict[str, object]] = []
    if config["use_group"] and item.profile_group and item.mode == "active":
        group_rows.append(
            {
                "username": item.username,
                "groupname": item.profile_group,
                "priority": config["group_priority"],
            }
        )
    if group_routing_enabled:
        access_key = (
            "captive"
            if item.mode == "captive"
            else "suspended"
            if item.mode == "reject"
            else "active"
        )
        access_group = access_groups.get(access_key)
        if access_group:
            group_rows.append(
                {
                    "username": item.username,
                    "groupname": access_group,
                    "priority": access_group_priority,
                }
            )
    return check_rows, reply_rows, group_rows


def _projection_fingerprint(
    *,
    radcheck_rows: Iterable[Mapping[str, object]],
    radreply_rows: Iterable[Mapping[str, object]],
    radusergroup_rows: Iterable[Mapping[str, object]],
) -> str:
    """Return a keyed digest; password values never leave this owner."""

    def normalized(
        rows: Iterable[Mapping[str, object]], columns: tuple[str, ...]
    ) -> list[list[str]]:
        return sorted(
            [[str(row.get(column) or "") for column in columns] for row in rows]
        )

    payload = json.dumps(
        {
            "radcheck": normalized(
                radcheck_rows, ("username", "attribute", "op", "value")
            ),
            "radreply": normalized(
                radreply_rows, ("username", "attribute", "op", "value")
            ),
            "radusergroup": normalized(
                radusergroup_rows, ("username", "groupname", "priority")
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    key = get_encryption_key()
    if key is None:
        raise RuntimeError("Credential encryption key is required for RADIUS parity")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def fingerprint_observed_radius_rows(
    *,
    radcheck_rows: Iterable[Mapping[str, object]],
    radreply_rows: Iterable[Mapping[str, object]],
    radusergroup_rows: Iterable[Mapping[str, object]],
) -> dict[str, str]:
    """Fingerprint complete observed rows per login without exposing secrets."""
    checks: dict[str, list[Mapping[str, object]]] = {}
    replies: dict[str, list[Mapping[str, object]]] = {}
    groups: dict[str, list[Mapping[str, object]]] = {}
    for rows, target in (
        (radcheck_rows, checks),
        (radreply_rows, replies),
        (radusergroup_rows, groups),
    ):
        for row in rows:
            username = str(row.get("username") or "")
            if username:
                target.setdefault(username, []).append(row)
    usernames = set(checks) | set(replies) | set(groups)
    return {
        username: _projection_fingerprint(
            radcheck_rows=checks.get(username, ()),
            radreply_rows=replies.get(username, ()),
            radusergroup_rows=groups.get(username, ()),
        )
        for username in usernames
    }


def _projection_tables(config: dict):
    radcheck = external_radius_table(
        config["radcheck_table"],
        Column("username", String),
        Column("attribute", String),
        Column("op", String),
        Column("value", String),
    )
    radreply = external_radius_table(
        config["radreply_table"],
        Column("username", String),
        Column("attribute", String),
        Column("op", String),
        Column("value", String),
    )
    radusergroup = external_radius_table(
        config["radusergroup_table"],
        Column("username", String),
        Column("groupname", String),
        Column("priority", Integer),
    )
    return radcheck, radreply, radusergroup


def _write_radius_projection(
    conn,
    config: dict,
    work: Iterable[RadiusProjectionWorkItem],
    delete_usernames: Iterable[str],
    *,
    access_groups: dict[str, str],
    access_group_priority: int,
    group_routing_enabled: bool,
) -> dict[str, int]:
    """Idempotently project auth, reply, and owned group rows to one target."""
    if conn.dialect.name == "postgresql":
        conn.execute(text("SELECT pg_advisory_xact_lock(3281601275)"))
    radcheck, radreply, radusergroup = _projection_tables(config)
    delete_list = sorted({str(name) for name in delete_usernames if name})
    counts = {
        "radcheck_written": 0,
        "radreply_written": 0,
        "radusergroup_written": 0,
    }
    if delete_list:
        conn.execute(delete(radcheck).where(radcheck.c.username.in_(delete_list)))
        conn.execute(delete(radreply).where(radreply.c.username.in_(delete_list)))
        group_delete = delete(radusergroup).where(
            radusergroup.c.username.in_(delete_list)
        )
        delete_group_rows = True
        if not config["use_group"]:
            owned_names = sorted({name for name in access_groups.values() if name})
            if owned_names:
                group_delete = group_delete.where(
                    radusergroup.c.groupname.in_(owned_names)
                )
            else:
                delete_group_rows = False
        if delete_group_rows:
            conn.execute(group_delete)

    for item in work:
        check_rows, reply_rows, group_rows = _projection_rows_for_item(
            item,
            config,
            access_groups=access_groups,
            access_group_priority=access_group_priority,
            group_routing_enabled=group_routing_enabled,
        )
        if check_rows:
            conn.execute(insert(radcheck), check_rows)
            counts["radcheck_written"] += len(check_rows)
        if reply_rows:
            conn.execute(insert(radreply), reply_rows)
            counts["radreply_written"] += len(reply_rows)
        if group_rows:
            conn.execute(insert(radusergroup), group_rows)
            counts["radusergroup_written"] += len(group_rows)
    return counts


def _single_active_ipv4(
    candidates: set[str] | None,
) -> tuple[str | None, tuple[str, ...]]:
    """Resolve one active IPv4 from a candidate set, or refuse.

    Returns ``(address, ())`` when exactly one active assignment exists,
    ``(None, ())`` when there is none, and ``(None, sorted_candidates)`` when
    there is more than one.

    Refusing is the point. ``docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md``
    states that consumers fail closed on an ambiguous ledger: picking a winner
    -- by insertion order, lowest value, or any other deterministic tiebreak --
    is an ownership decision this projection does not hold. The caller preserves
    the login's existing RADIUS rows so an ambiguous ledger degrades into "no
    change, reported" rather than "a coin-flip address, silently served".
    """
    if not candidates:
        return None, ()
    if len(candidates) == 1:
        return next(iter(candidates)), ()
    return None, tuple(sorted(candidates))


def populate(
    dry_run: bool = True,
    only_usernames: set[str] | None = None,
    *,
    source_db=None,
    include_expected_fingerprints: bool = False,
) -> dict[str, object]:
    """Project the authoritative subscriber state to every configured target."""
    stats: dict[str, object] = {
        "subscriptions_considered": 0,
        "skipped_no_credential": 0,
        "skipped_no_password": 0,
        "skipped_decrypt_failed": 0,
        "radcheck_upserts": 0,
        "radreply_upserts": 0,
        "blocked_users_written": 0,
        "captive_ineligible_optins": 0,
        "skipped_ambiguous_ipv4_ledger": 0,
    }

    enc_key = get_encryption_key()
    db = source_db or SessionLocal()
    owns_db = source_db is None
    try:
        projection_targets = active_external_radius_targets(db, capability="users")
        if not projection_targets:
            raise RuntimeError("No DB-configured external RADIUS user target")
        alignment = assert_legacy_target_alignment(db)
        stats["projection_targets"] = len(projection_targets)
        stats["legacy_targets_verified"] = len(alignment)

        from app.models.domain_settings import SettingDomain
        from app.services import settings_spec
        from app.services.enforcement_event_policy import (
            resolve_group_routing_policy,
        )

        group_routing_enabled = resolve_group_routing_policy(db).enabled
        simultaneous_use_enabled = simultaneous_use_enforcement_enabled(db)
        stats["simultaneous_use_enforcement_enabled"] = simultaneous_use_enabled
        access_groups = {
            "active": str(
                settings_spec.resolve_value(
                    db, SettingDomain.radius, "active_group_name"
                )
                or "dotmac-active"
            ),
            "suspended": str(
                settings_spec.resolve_value(
                    db, SettingDomain.radius, "suspended_group_name"
                )
                or "dotmac-suspended"
            ),
            "captive": str(
                settings_spec.resolve_value(
                    db, SettingDomain.radius, "captive_group_name"
                )
                or "dotmac-captive"
            ),
        }
        access_group_priority = int(
            settings_spec.resolve_value(
                db, SettingDomain.radius, "access_group_priority"
            )
            or 0
        )
        suspended_list_name = suspended_address_list(db)
        from app.models.subscriber import Subscriber

        # Active, blocked, or suspended subs with a login — blocked/suspended
        # subs get a walled-garden radreply so suspension actually takes
        # effect at the BNG (hard-deleting their rows would fail-closed but
        # lose the captive pay-page treatment).
        rows = (
            db.execute(
                select(Subscription)
                .options(
                    joinedload(Subscription.offer),
                    joinedload(Subscription.radius_profile),
                    joinedload(Subscription.subscriber).joinedload(Subscriber.reseller),
                )
                .where(
                    Subscription.status.in_(ACTIVE_STATUSES | BLOCKED_STATUSES),
                    Subscription.login.isnot(None),
                )
            )
            .unique()
            .scalars()
            .all()
        )
        stats["subscriptions_considered"] = len(rows)
        logger.info(
            "considering %d active/blocked subscriptions with a login", len(rows)
        )
        login_projections = plan_login_radius_projections(db, rows)

        # Pre-fetch all AccessCredentials keyed by username
        creds_by_username: dict[str, AccessCredential] = {
            c.username: c
            for c in db.scalars(
                select(AccessCredential).where(AccessCredential.is_active.is_(True))
            ).all()
        }

        # Pre-fetch RadiusProfiles by id so a credential-level profile override
        # (a dunning/FUP throttle sets AccessCredential.radius_profile_id) can be
        # resolved in memory. Without this, populate() rebuilds radreply purely
        # from the subscription/offer profile and silently reverts every throttle
        # within one sweep — the throttle never reaches the router (SP-2). This
        # mirrors the credential>subscription precedence in
        # enforcement.resolve_radius_profile; the offer-derived fallback below is
        # unchanged, so a non-throttled customer's speed is untouched.
        radius_profiles_by_id: dict = {
            p.id: p for p in db.scalars(select(RadiusProfile)).all()
        }

        # Pre-fetch additional routed IP blocks,
        # keyed by subscriber_id, so each user's Framed-Routes are O(1) in the
        # sweep loop below.
        from app.models.network import SubscriberAdditionalRoute

        subscriber_service_counts: dict = {}
        for row in rows:
            subscriber_service_counts[row.subscriber_id] = (
                subscriber_service_counts.get(row.subscriber_id, 0) + 1
            )

        routes_by_subscription: dict = {}
        legacy_routes_by_subscriber: dict = {}
        for r in db.scalars(
            select(SubscriberAdditionalRoute).where(
                SubscriberAdditionalRoute.is_active.is_(True)
            )
        ).all():
            target = (
                routes_by_subscription.setdefault(r.subscription_id, [])
                if getattr(r, "subscription_id", None)
                else legacy_routes_by_subscriber.setdefault(r.subscriber_id, [])
            )
            target.append((r.cidr, r.metric))
        logger.info(
            "%d subscriptions with additional routed IP blocks",
            len(routes_by_subscription),
        )

        # Fallback IPv4 from the active IPAssignment so a stale/cleared
        # subscriptions.ipv4_address doesn't silently drop Framed-IP-Address (which
        # de-IPs the customer -> BNG teardown -> reconnect flap). One query, keyed
        # by subscription_id. Legacy unbound assignments are used only for a
        # subscriber with one projected subscription.
        from app.models.network import IPAssignment, IPv4Address, IPVersion

        # Collect ALL active addresses per key rather than first-wins. The
        # previous `setdefault` over an unordered query silently chose a winner
        # when a service carried more than one active assignment -- an ownership
        # decision this projection has no authority to make, and one that could
        # differ between two runs over identical data. Ambiguity is now resolved
        # by refusing to resolve: see `_single_active_ipv4` below.
        ipv4_candidates_by_subscription: dict = {}
        legacy_ipv4_candidates_by_subscriber: dict = {}
        for sid, subscription_id, addr in db.execute(
            select(
                IPAssignment.subscriber_id,
                IPAssignment.subscription_id,
                IPv4Address.address,
            )
            .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
            .where(IPAssignment.is_active.is_(True))
            .where(IPAssignment.ip_version == IPVersion.ipv4)
        ).all():
            if sid and addr:
                if subscription_id:
                    ipv4_candidates_by_subscription.setdefault(
                        subscription_id, set()
                    ).add(str(addr))
                else:
                    legacy_ipv4_candidates_by_subscriber.setdefault(sid, set()).add(
                        str(addr)
                    )

        # IPv6 PD: the subscription's assigned delegated prefix, emitted as
        # Delegated-IPv6-Prefix. Flag-gated (inert until IPv6 PD is turned on).
        pd_by_subscription: dict = {}
        legacy_pd_by_subscriber: dict = {}
        from app.services.ipv6_pd import pd_enabled

        if pd_enabled():
            from app.models.network import Ipv6DelegatedPrefix, Ipv6PrefixState

            for sid, subscription_id, prefix, plen in db.execute(
                select(
                    Ipv6DelegatedPrefix.subscriber_id,
                    Ipv6DelegatedPrefix.subscription_id,
                    Ipv6DelegatedPrefix.prefix,
                    Ipv6DelegatedPrefix.prefix_length,
                ).where(Ipv6DelegatedPrefix.state == Ipv6PrefixState.assigned)
            ).all():
                if sid and prefix:
                    value = f"{prefix}/{plen}"
                    if subscription_id:
                        pd_by_subscription.setdefault(subscription_id, value)
                    else:
                        legacy_pd_by_subscriber.setdefault(sid, value)

        # Compute the full work list in memory while the dotmac session is
        # alive, then release it BEFORE the radius writes — holding the read
        # transaction through the write phase trips the app's 120s
        # idle-in-transaction timeout on large fleets.
        by_login: dict[str, RadiusProjectionWorkItem] = {}
        preserve_usernames: set[str] = set()
        unbuildable_usernames: set[str] = set()
        for sub in rows:
            login = cast(str | None, sub.login)
            if not login:
                _increment_result_count(stats, "skipped_no_credential")
                continue
            selected_projection = login_projections.get(login)
            if (
                selected_projection is None
                or selected_projection.subscription_id != str(sub.id)
            ):
                continue
            projection = selected_projection.plan
            captive = projection.mode == "captive"
            if (
                getattr(sub.subscriber, "captive_redirect_enabled", False)
                and not captive
            ):
                _increment_result_count(stats, "captive_ineligible_optins")

            # A hard reject is a complete RADIUS projection in its own right and
            # does not need a customer password.  Resolve it before credential
            # lookup/decryption so a missing or unreadable secret can never
            # preserve an old permissive row for a blocked login.
            if projection.mode == "reject":
                by_login[login] = RadiusProjectionWorkItem(
                    username=login,
                    cleartext_password="",
                    check_attrs=(),
                    reply_attrs=(),
                    blocked=True,
                    status=sub.status,
                    mode=projection.mode,
                    profile_group=None,
                )
                continue

            cred = creds_by_username.get(login)
            if cred is None:
                _increment_result_count(stats, "skipped_no_credential")
                unbuildable_usernames.add(login)
                preserve_usernames.add(login)
                continue
            if not cred.secret_hash:
                _increment_result_count(stats, "skipped_no_password")
                unbuildable_usernames.add(login)
                preserve_usernames.add(login)
                continue
            try:
                cleartext = decrypt_credential_with_key(cred.secret_hash, enc_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("decrypt failed for %s: %s", login, exc)
                _increment_result_count(stats, "skipped_decrypt_failed")
                unbuildable_usernames.add(login)
                preserve_usernames.add(login)
                continue
            if not cleartext:
                _increment_result_count(stats, "skipped_no_password")
                unbuildable_usernames.add(login)
                preserve_usernames.add(login)
                continue

            sub_blocked = projection.blocked
            eff_ipv4 = sub.ipv4_address
            if not eff_ipv4 or eff_ipv4 == "0.0.0.0":  # nosec B104  # noqa: S104
                # Only reached when the served projection is empty, so this is
                # the one place the ledger actually has to be read. An ambiguous
                # ledger here cannot be resolved without inventing an owner, so
                # the login is preserved and reported instead of guessed at.
                eff_ipv4, ambiguous_addresses = _single_active_ipv4(
                    ipv4_candidates_by_subscription.get(sub.id)
                )
                if eff_ipv4 is None and not ambiguous_addresses:
                    if subscriber_service_counts.get(sub.subscriber_id) == 1:
                        eff_ipv4, ambiguous_addresses = _single_active_ipv4(
                            legacy_ipv4_candidates_by_subscriber.get(sub.subscriber_id)
                        )
                if ambiguous_addresses:
                    logger.error(
                        "ambiguous active IPv4 ledger for login %s (subscription "
                        "%s): %s. Preserving the existing RADIUS rows; the "
                        "assignment owner must adjudicate.",
                        login,
                        sub.id,
                        ", ".join(ambiguous_addresses),
                    )
                    _increment_result_count(stats, "skipped_ambiguous_ipv4_ledger")
                    unbuildable_usernames.add(login)
                    preserve_usernames.add(login)
                    continue
            delegated_ipv6 = pd_by_subscription.get(sub.id)
            if (
                delegated_ipv6 is None
                and subscriber_service_counts.get(sub.subscriber_id) == 1
            ):
                delegated_ipv6 = legacy_pd_by_subscriber.get(sub.subscriber_id)
            additional_routes = routes_by_subscription.get(sub.id)
            if (
                additional_routes is None
                and subscriber_service_counts.get(sub.subscriber_id) == 1
            ):
                additional_routes = legacy_routes_by_subscriber.get(sub.subscriber_id)
            # Credential-level profile (throttle/FUP) takes precedence over the
            # subscription profile, so an applied throttle actually shapes the
            # radreply instead of being reverted to full offer speed each sweep.
            effective_profile = _effective_profile(
                cred, sub.radius_profile, radius_profiles_by_id
            )
            attrs = _radreply_attrs(
                sub,
                sub.offer,
                effective_profile,
                sub_blocked,
                captive_redirect_enabled=captive,
                additional_routes=additional_routes,
                framed_ipv4=eff_ipv4,
                delegated_ipv6=delegated_ipv6,
                suspended_list_name=suspended_list_name,
                simultaneous_use_enabled=simultaneous_use_enabled,
            )
            blocked_flag = projection.blocked
            # Enforcement mode for the radcheck write: active subs and opted-in
            # blocked subs keep a usable password (captive subs are walled via
            # the radreply Address-List); non-opted blocked subs are hard
            # rejected (Auth-Type := Reject, offline).
            mode = projection.mode
            check_attrs = (
                _radcheck_policy_attrs(
                    effective_profile,
                    simultaneous_use_enabled=simultaneous_use_enabled,
                )
                if mode in {"active", "captive"}
                else []
            )
            by_login[login] = RadiusProjectionWorkItem(
                username=login,
                cleartext_password=cleartext,
                check_attrs=tuple(check_attrs),
                reply_attrs=tuple(attrs),
                blocked=blocked_flag,
                status=sub.status,
                mode=mode,
                profile_group=effective_profile.name if effective_profile else None,
            )

        active_usernames = {sub.login for sub in rows if sub.login}
    finally:
        if owns_db:
            db.close()

    work = list(by_login.values())
    # A scoped reconcile writes only the requested usernames. The projection is
    # still computed fleet-wide above, so the subscriber service-count and
    # duplicate-login dedup stay identical to the full sweep; only the write set
    # narrows. A requested username absent from `work` has no active/blocked
    # subscription and is deleted (removal), never reinserted.
    scoped = only_usernames is not None
    if only_usernames is not None:
        work = [item for item in work if item.username in only_usernames]
        stats["scoped_targets"] = len(only_usernames)
        unbuildable_usernames.intersection_update(only_usernames)
    stats["projected_logins"] = len(work)
    stats["unbuildable_logins"] = len(unbuildable_usernames)
    stats["projection_complete"] = not unbuildable_usernames
    stats["radcheck_upserts"] = len(work) * len(projection_targets)
    stats["radcheck_policy_upserts"] = sum(
        len(item.check_attrs) for item in work
    ) * len(projection_targets)
    stats["radreply_upserts"] = sum(len(item.reply_attrs) for item in work) * len(
        projection_targets
    )
    stats["blocked_users_written"] = sum(1 for item in work if item.blocked)
    stats["captive_users_written"] = sum(1 for item in work if item.mode == "captive")
    stats["rejected_users_written"] = sum(1 for item in work if item.mode == "reject")
    if include_expected_fingerprints:
        expected: dict[str, dict[str, str]] = {}
        for target in projection_targets:
            target_rows: dict[str, str] = {}
            for item in work:
                check_rows, reply_rows, group_rows = _projection_rows_for_item(
                    item,
                    target,
                    access_groups=access_groups,
                    access_group_priority=access_group_priority,
                    group_routing_enabled=group_routing_enabled,
                )
                target_rows[item.username] = _projection_fingerprint(
                    radcheck_rows=check_rows,
                    radreply_rows=reply_rows,
                    radusergroup_rows=group_rows,
                )
            expected[str(target["target_fingerprint"])] = target_rows
        stats["expected_projection_fingerprints"] = expected

    delete_usernames = (
        (set(only_usernames or set()) - preserve_usernames)
        if scoped
        else {item.username for item in work}
    )
    if dry_run:
        stats["target_outcomes"] = [
            {
                "target_name": target["target_name"],
                "target_fingerprint": target["target_fingerprint"],
                "ok": True,
                "dry_run": True,
            }
            for target in projection_targets
        ]
        logger.info("DRY RUN — no writes (orphan cleanup also skipped)")
        log_stats = {
            key: value
            for key, value in stats.items()
            if key != "expected_projection_fingerprints"
        }
        logger.info("done: %s", log_stats)
        return stats

    outcomes: list[dict[str, object]] = []
    for target in projection_targets:
        outcome: dict[str, object] = {
            "target_name": target["target_name"],
            "target_fingerprint": target["target_fingerprint"],
            "ok": False,
        }
        try:
            engine = get_external_engine(str(target["db_url"]))
            with engine.begin() as conn:
                counts = _write_radius_projection(
                    conn,
                    target,
                    work,
                    delete_usernames,
                    access_groups=access_groups,
                    access_group_priority=access_group_priority,
                    group_routing_enabled=group_routing_enabled,
                )
                if not scoped:
                    counts.update(
                        _reap_radius_orphans(
                            conn,
                            target,
                            active_usernames,
                            access_groups=access_groups,
                        )
                    )
                    counts["probe_identity_synced"] = _ensure_probe_identity(
                        conn, target
                    )
            outcome.update(counts)
            outcome["ok"] = True
        except Exception as exc:  # noqa: BLE001
            # Do not include exception text: DB errors can echo secret binds.
            outcome["error_type"] = type(exc).__name__
        outcomes.append(outcome)
    stats["target_outcomes"] = outcomes
    if any(not outcome["ok"] for outcome in outcomes):
        raise RadiusProjectionIncomplete(outcomes)

    log_stats = {
        key: value
        for key, value in stats.items()
        if key != "expected_projection_fingerprints"
    }
    logger.info("done: %s", log_stats)
    return stats


def reconcile_usernames(
    usernames: Iterable[str], dry_run: bool = True, *, source_db=None
) -> dict[str, object]:
    """Scoped `access.radius_projection` reconcile for a bounded username set.

    Computes the same fleet-wide projection as the full sweep — so the
    subscriber service-count and duplicate-login dedup stay identical — then
    writes only the requested usernames. A requested username with no active/
    blocked/suspended subscription is deleted (removal); one still present is
    rewritten. No global orphan reap and no probe sync.

    This is the entry point per-user callers (event-time enforcement, credential
    add/remove/block) request instead of writing radcheck/radreply directly.
    The full-fleet ``refresh_radius_from_subs`` remains the sweep. Thin wrapper
    over ``populate(only_usernames=...)`` so both share one write path and DSN.
    """
    targets = {u for u in usernames if u}
    if not targets:
        return {
            "scoped_targets": 0,
            "projected_logins": 0,
            "unbuildable_logins": 0,
            "projection_targets": 0,
            "projection_complete": True,
            "radcheck_upserts": 0,
            "radreply_upserts": 0,
        }
    return populate(dry_run=dry_run, only_usernames=targets, source_db=source_db)


def restore_projection_snapshot(db, snapshots: list[dict]) -> dict[str, object]:
    """Restore an operator-approved backup through the projection owner.

    This is intentionally separate from normal desired-state reconciliation:
    it consumes an auditable backup transport, but retains the same DB target
    resolution, cutover guard, per-target transaction, and ownership boundary.
    """
    targets = active_external_radius_targets(db, capability="users")
    if not targets:
        raise RuntimeError("No DB-configured external RADIUS user target")
    assert_legacy_target_alignment(db)
    by_id = {target["target_id"]: target for target in targets}
    outcomes: list[dict[str, object]] = []
    totals = {
        "radcheck_restored": 0,
        "radreply_restored": 0,
        "radusergroup_restored": 0,
    }
    for snapshot in snapshots:
        target_id = snapshot.get("target_id")
        fingerprint = snapshot.get("target_fingerprint")
        target = by_id.get(target_id) if target_id else None
        if target is None and fingerprint:
            matches = [
                candidate
                for candidate in targets
                if candidate["target_fingerprint"] == fingerprint
            ]
            target = matches[0] if len(matches) == 1 else None
        if target is None and len(targets) == 1 and len(snapshots) == 1:
            target = targets[0]
        if target is None:
            outcomes.append(
                {
                    "target_name": snapshot.get("target_name") or "unmatched",
                    "target_fingerprint": fingerprint or "legacy",
                    "ok": False,
                    "error_type": "TargetNotConfigured",
                }
            )
            continue
        outcome: dict[str, object] = {
            "target_name": target["target_name"],
            "target_fingerprint": target["target_fingerprint"],
            "ok": False,
        }
        try:
            restored_counts = {
                "radcheck_restored": 0,
                "radreply_restored": 0,
                "radusergroup_restored": 0,
            }
            radcheck, radreply, radusergroup = _projection_tables(target)
            usernames = sorted(
                {
                    str(username)
                    for username in snapshot.get("usernames", [])
                    if username
                }
                | {
                    str(row["username"])
                    for key in ("radcheck", "radreply", "radusergroup")
                    for row in snapshot.get(key, [])
                    if row.get("username")
                }
            )
            engine = get_external_engine(str(target["db_url"]))
            with engine.begin() as conn:
                if conn.dialect.name == "postgresql":
                    conn.execute(text("SELECT pg_advisory_xact_lock(3281601275)"))
                if usernames:
                    conn.execute(
                        delete(radcheck).where(radcheck.c.username.in_(usernames))
                    )
                    conn.execute(
                        delete(radreply).where(radreply.c.username.in_(usernames))
                    )
                    conn.execute(
                        delete(radusergroup).where(
                            radusergroup.c.username.in_(usernames)
                        )
                    )
                for key, table, columns in (
                    (
                        "radcheck",
                        radcheck,
                        ("username", "attribute", "op", "value"),
                    ),
                    (
                        "radreply",
                        radreply,
                        ("username", "attribute", "op", "value"),
                    ),
                    (
                        "radusergroup",
                        radusergroup,
                        ("username", "groupname", "priority"),
                    ),
                ):
                    rows = [
                        {column: row.get(column) for column in columns}
                        for row in snapshot.get(key, [])
                    ]
                    if rows:
                        conn.execute(insert(table), rows)
                    restored_counts[f"{key}_restored"] = len(rows)
            for key, value in restored_counts.items():
                totals[key] += value
                outcome[key] = value
            outcome["ok"] = True
        except Exception as exc:  # noqa: BLE001
            outcome["error_type"] = type(exc).__name__
        outcomes.append(outcome)
    if any(not outcome["ok"] for outcome in outcomes):
        raise RadiusProjectionIncomplete(outcomes)
    return {**totals, "target_outcomes": outcomes}


def _reap_radius_orphans(
    conn,
    config: dict,
    active_usernames: set[str],
    *,
    access_groups: dict[str, str],
) -> dict[str, int]:
    from app.services.radius_probe import probe_username

    radcheck, radreply, radusergroup = _projection_tables(config)
    radcheck_users = set(conn.scalars(select(radcheck.c.username).distinct()).all())
    orphans = sorted(radcheck_users - active_usernames - {probe_username()})
    counts = {
        "radcheck_orphans_deleted": 0,
        "radreply_orphans_deleted": 0,
        "radusergroup_orphans_deleted": 0,
    }
    if not orphans:
        return counts
    result = conn.execute(delete(radcheck).where(radcheck.c.username.in_(orphans)))
    counts["radcheck_orphans_deleted"] = result.rowcount or 0
    result = conn.execute(delete(radreply).where(radreply.c.username.in_(orphans)))
    counts["radreply_orphans_deleted"] = result.rowcount or 0
    group_delete = delete(radusergroup).where(radusergroup.c.username.in_(orphans))
    delete_group_rows = True
    if not config["use_group"]:
        owned_names = sorted({name for name in access_groups.values() if name})
        if owned_names:
            group_delete = group_delete.where(radusergroup.c.groupname.in_(owned_names))
        else:
            delete_group_rows = False
    if delete_group_rows:
        result = conn.execute(group_delete)
        counts["radusergroup_orphans_deleted"] = result.rowcount or 0
    return counts


def _ensure_probe_identity(conn, target: dict) -> int:
    """Upsert the synthetic auth-probe's radcheck user and nas client.

    Returns 1 when synced, 0 when the probe is unconfigured. Env-sourced
    (``RADIUS_PROBE_*``) — no secret lives in the app DB. The nas client is
    loaded by FreeRADIUS at startup (``read_clients``), so a first-time client
    add needs a FreeRADIUS restart; the user row takes effect immediately.
    """
    import os

    from app.services.radius_probe import probe_config, probe_username

    probe = probe_config()
    if not probe["configured"]:
        return 0
    username = probe_username()
    radcheck, _radreply, _radusergroup = _projection_tables(target)
    conn.execute(
        delete(radcheck).where(
            radcheck.c.username == username,
            radcheck.c.attribute == target["password_attribute"],
        )
    )
    conn.execute(
        insert(radcheck).values(
            username=username,
            attribute=target["password_attribute"],
            op=target["password_op"],
            value=probe["password"],
        )
    )

    # nas client covering the worker source range (compose bridge by default).
    client_subnet = os.getenv("RADIUS_PROBE_CLIENT_SUBNET", "172.20.0.0/16").strip()
    nas = external_radius_table(
        target["nas_table"],
        Column("nasname", String),
        Column("shortname", String),
        Column("type", String),
        Column("secret", String),
        Column("description", String),
        Column("require_message_authenticator", Boolean),
    )
    conn.execute(delete(nas).where(nas.c.shortname == username))
    conn.execute(
        insert(nas).values(
            nasname=client_subnet,
            shortname=username,
            type="other",
            secret=probe["secret"],
            description="synthetic health probe",
            require_message_authenticator=True,
        )
    )
    return 1


if __name__ == "__main__":
    if "--execute" in sys.argv:
        populate(dry_run=False)
    else:
        populate(dry_run=True)
        print("\nTo execute: python -m app.services.radius_population --execute")


# ---------------------------------------------------------------------------
# Staff device-login RADIUS projection
# ---------------------------------------------------------------------------

DEVICE_LOGIN_SYNC_STATUS_KEY = "device_login_last_sync"


def get_device_login_sync_status(db) -> dict | None:
    """Return the last staff router-login RADIUS sync status, if recorded."""
    from app.models.domain_settings import DomainSetting, SettingDomain

    row = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.radius)
        .filter(DomainSetting.key == DEVICE_LOGIN_SYNC_STATUS_KEY)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not row or not isinstance(row.value_json, dict):
        return None
    return row.value_json


def record_device_login_sync_status(
    db,
    *,
    status: str,
    result: dict | None = None,
    error: str | None = None,
) -> dict:
    """Persist the last staff router-login RADIUS sync result."""
    from datetime import UTC, datetime

    from app.models.domain_settings import DomainSetting, SettingDomain
    from app.models.subscription_engine import SettingValueType

    payload = {
        "status": status,
        "synced_at": datetime.now(UTC).isoformat(),
        "result": dict(result or {}),
    }
    if error:
        payload["error"] = error

    row = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.radius)
        .filter(DomainSetting.key == DEVICE_LOGIN_SYNC_STATUS_KEY)
        .first()
    )
    if row is None:
        row = DomainSetting(
            domain=SettingDomain.radius,
            key=DEVICE_LOGIN_SYNC_STATUS_KEY,
            value_type=SettingValueType.json,
            value_json=payload,
            is_active=True,
        )
        db.add(row)
    else:
        row.value_type = SettingValueType.json
        row.value_json = payload
        row.value_text = None
        row.is_secret = False
        row.is_active = True
    db.commit()
    return payload


def effective_roles(db, system_user_id) -> set[str]:
    """Return the set of active role names held by a SystemUser.

    Mirrors the SystemUser branch in auth_dependencies.has_permission:
    join system_user_roles → roles and collect Role.name where Role.is_active.
    """
    from app.models.rbac import Role, SystemUserRole

    rows = (
        db.query(Role.name)
        .join(SystemUserRole, SystemUserRole.role_id == Role.id)
        .filter(SystemUserRole.system_user_id == system_user_id)
        .filter(Role.is_active.is_(True))
        .all()
    )
    return {row[0] for row in rows}


def effective_perms(db, system_user_id) -> set[str]:
    """Return the set of active permission keys held by a SystemUser.

    Mirrors the SystemUser branch in auth_dependencies.has_permission:
    collects keys from (a) role-via-system_user_roles and (b) direct
    SystemUserPermission grants, both filtered by Permission.is_active.
    """
    from app.models.rbac import (
        Permission,
        Role,
        RolePermission,
        SystemUserPermission,
        SystemUserRole,
    )

    # (a) role-derived permissions
    role_perm_keys = (
        db.query(Permission.key)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, RolePermission.role_id == Role.id)
        .join(SystemUserRole, SystemUserRole.role_id == Role.id)
        .filter(SystemUserRole.system_user_id == system_user_id)
        .filter(Role.is_active.is_(True))
        .filter(Permission.is_active.is_(True))
        .all()
    )

    # (b) direct grants
    direct_perm_keys = (
        db.query(Permission.key)
        .join(SystemUserPermission, SystemUserPermission.permission_id == Permission.id)
        .filter(SystemUserPermission.system_user_id == system_user_id)
        .filter(Permission.is_active.is_(True))
        .all()
    )

    return {row[0] for row in role_perm_keys} | {row[0] for row in direct_perm_keys}


def populate_device_login(
    db,
    *,
    dry_run: bool = False,
    _conn_factory=None,
    _target_config=None,
) -> dict[str, object]:
    """Project device-login-enabled staff into the admin RADIUS auth set.

    Writes ONLY to radcheck_admin / radreply_admin — never touches radcheck /
    radreply (subscriber auth).

    Per-user DELETE+INSERT pattern (idempotent): same as populate().  Eligible
    users get:
      radcheck_admin:  Cleartext-Password := <decrypted secret>
      radreply_admin:  Mikrotik-Group := <tier>
                       Service-Type   := Administrative-User

    Args:
        db: SQLAlchemy session (dotmac_sub app DB — SystemUser side).
        dry_run: If True, compute stats and issue SQL but rollback; no writes.
        _conn_factory: Optional zero-arg callable that returns a DB-API 2
            connection to the RADIUS DB.  Used by tests to inject an in-memory
            SQLite connection in place of the real psycopg Postgres connection.
            When None (production), connects to the DB-configured authoritative
            accounting target.

    Returns:
        dict with keys: considered, radcheck_upserts, radreply_upserts,
        removed, skipped_ineligible.
    """
    from datetime import UTC, datetime

    from app.models.system_user import SystemUser
    from app.services.credential_crypto import decrypt_credential
    from app.services.device_login import derive_router_tier

    if _conn_factory is None and _target_config is None:
        targets = active_external_radius_targets(db, capability="users")
        if not targets:
            raise RuntimeError("No DB-configured external RADIUS user target")
        assert_legacy_target_alignment(db)
        aggregate: dict[str, object] = {
            "considered": 0,
            "radcheck_upserts": 0,
            "radreply_upserts": 0,
            "removed": 0,
            "skipped_ineligible": 0,
            "app_disabled": 0,
            "projection_targets": len(targets),
            "target_outcomes": [],
        }
        outcomes: list[dict[str, object]] = []
        for target_config in targets:

            def _factory(config=target_config):
                url = str(config["db_url"]).replace(
                    "postgresql+psycopg://", "postgresql://", 1
                )
                connection = psycopg.connect(url)
                connection.autocommit = False
                return connection

            try:
                result = populate_device_login(
                    db,
                    dry_run=dry_run,
                    _conn_factory=_factory,
                    _target_config=target_config,
                )
            except Exception as exc:  # noqa: BLE001
                outcomes.append(
                    {
                        "target_name": target_config["target_name"],
                        "target_fingerprint": target_config["target_fingerprint"],
                        "ok": False,
                        "error_type": type(exc).__name__,
                    }
                )
                continue
            for key in ("radcheck_upserts", "radreply_upserts", "removed"):
                aggregate[key] = _result_count(aggregate, key) + _result_count(
                    result, key
                )
            for key in ("considered", "skipped_ineligible"):
                aggregate[key] = max(
                    _result_count(aggregate, key), _result_count(result, key)
                )
            aggregate["app_disabled"] = _result_count(
                aggregate, "app_disabled"
            ) + _result_count(result, "app_disabled")
            outcomes.append(
                {
                    "target_name": target_config["target_name"],
                    "target_fingerprint": target_config["target_fingerprint"],
                    "ok": True,
                }
            )
        aggregate["target_outcomes"] = outcomes
        if any(not outcome["ok"] for outcome in outcomes):
            raise RadiusProjectionIncomplete(outcomes)
        return aggregate

    stats: dict[str, int] = {
        "considered": 0,
        "radcheck_upserts": 0,
        "radreply_upserts": 0,
        "removed": 0,
        "skipped_ineligible": 0,
        "app_disabled": 0,
    }

    # Fetch all active SystemUsers from the app DB while we still hold the
    # session; compute work list in memory before opening the RADIUS connection
    # (avoids holding the app transaction open during RADIUS writes).
    staff = db.query(SystemUser).filter(SystemUser.is_active.is_(True)).all()

    # Build work list: (username, cleartext|None, tier|None, reason)
    # tier=None + reason="inactive"                → removed
    # tier=None + reason="permission_ineligible"   → skipped + app flag cleanup
    # tier=None + reason="secret_unusable"         → skipped only
    # tier set                                     → upsert
    work: list[tuple[str, str | None, str | None, str]] = []
    permission_ineligible_user_ids = []
    for u in staff:
        stats["considered"] += 1
        eligible = bool(
            u.device_login_enabled
            and u.device_login_revoked_at is None
            and u.device_login_secret
        )
        if not eligible:
            work.append((u.email, None, None, "inactive"))
            continue

        roles = effective_roles(db, u.id)
        perms = effective_perms(db, u.id)
        tier = derive_router_tier(roles, perms)

        if tier is None:
            permission_ineligible_user_ids.append(u.id)
            work.append((u.email, None, None, "permission_ineligible"))
            continue

        try:
            cleartext = decrypt_credential(u.device_login_secret)
        except Exception:  # noqa: BLE001
            logger.warning(
                "populate_device_login: decrypt failed for %s — skipping", u.email
            )
            work.append((u.email, None, None, "secret_unusable"))
            continue

        if not cleartext:
            work.append((u.email, None, None, "secret_unusable"))
            continue

        work.append((u.email, cleartext, tier, "eligible"))

    # Open RADIUS connection
    target = _target_config
    if _conn_factory is not None:
        conn = _conn_factory()
    else:  # pragma: no cover - production fan-out above supplies a factory
        raise RuntimeError("RADIUS connection factory not configured")

    try:
        cur = conn.cursor()

        def _admin_query(template: str, *table_keys: str):
            defaults = {
                "radcheck_admin_table": "radcheck_admin",
                "radreply_admin_table": "radreply_admin",
            }
            names = [
                str(target[key]) if target else defaults[key] for key in table_keys
            ]
            if target is None:
                return template.format(*names)
            from psycopg import sql

            return sql.SQL(template).format(
                *(sql.Identifier(*name.split(".")) for name in names)
            )

        # Desired end-state: eligible active users with a derivable tier.
        desired = {
            uname: (cleartext, tier)
            for (uname, cleartext, tier, _reason) in work
            if tier is not None
        }
        # Eligible (device-login enabled, not revoked, secret present) but with
        # no usable tier — permission-ineligible, decrypt failure, or empty.
        stats["skipped_ineligible"] = sum(
            1
            for (_u, _c, tier, reason) in work
            if tier is None and reason != "inactive"
        )

        # Authoritative removal: delete ANY admin RADIUS row whose username is
        # not in the desired set. This is driven off the RADIUS side (not the
        # active work list) so it also revokes staff who were DEACTIVATED,
        # DELETED, or RENAMED (email change) after being projected — none of
        # which appear in `work` (which only scans active users). Without this,
        # router login can survive staff deactivation.
        cur.execute(
            _admin_query(
                "SELECT username FROM {} UNION SELECT username FROM {}",
                "radcheck_admin_table",
                "radreply_admin_table",
            )
        )
        existing = {row[0] for row in cur.fetchall()}
        for uname in existing - set(desired):
            cur.execute(
                _admin_query(
                    "DELETE FROM {} WHERE username=%s", "radcheck_admin_table"
                ),
                (uname,),
            )
            cur.execute(
                _admin_query(
                    "DELETE FROM {} WHERE username=%s", "radreply_admin_table"
                ),
                (uname,),
            )
            stats["removed"] += 1

        # Upsert desired users (DELETE+INSERT keeps it idempotent).
        for uname, (cleartext, tier) in desired.items():
            cur.execute(
                _admin_query(
                    "DELETE FROM {} WHERE username=%s", "radcheck_admin_table"
                ),
                (uname,),
            )
            cur.execute(
                _admin_query(
                    "DELETE FROM {} WHERE username=%s", "radreply_admin_table"
                ),
                (uname,),
            )
            cur.execute(
                _admin_query(
                    "INSERT INTO {} (username, attribute, op, value) "
                    "VALUES (%s, %s, %s, %s)",
                    "radcheck_admin_table",
                ),
                (
                    uname,
                    target["password_attribute"] if target else "Cleartext-Password",
                    target["password_op"] if target else ":=",
                    cleartext,
                ),
            )
            cur.execute(
                _admin_query(
                    "INSERT INTO {} (username, attribute, op, value) "
                    "VALUES (%s, 'Mikrotik-Group', ':=', %s)",
                    "radreply_admin_table",
                ),
                (uname, tier),
            )
            cur.execute(
                _admin_query(
                    "INSERT INTO {} (username, attribute, op, value) "
                    "VALUES (%s, 'Service-Type', ':=', 'Administrative-User')",
                    "radreply_admin_table",
                ),
                (uname,),
            )
            stats["radcheck_upserts"] += 1
            stats["radreply_upserts"] += 2

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            if permission_ineligible_user_ids:
                now = datetime.now(UTC)
                stale_users = (
                    db.query(SystemUser)
                    .filter(SystemUser.id.in_(permission_ineligible_user_ids))
                    .all()
                )
                for user in stale_users:
                    if user.device_login_enabled:
                        user.device_login_enabled = False
                        user.device_login_revoked_at = now
                        stats["app_disabled"] += 1
                if stats["app_disabled"]:
                    db.commit()
    finally:
        conn.close()

    logger.info("populate_device_login done (dry_run=%s): %s", dry_run, stats)
    return cast(dict[str, object], stats)


# -----------------------------------------------------------------------------
# TODO — Celery change-driven handler
#
# Replace the periodic resync with event-driven updates. When a subscription
# changes (status/login/ipv4/offer/profile) or its AccessCredential changes
# (password rotation), a small per-user upsert runs instead of a full scan.
#
# Sketch:
#
#   @celery_app.task(name="app.tasks.radius.upsert_one")
#   def upsert_one(subscription_id: str) -> None:
#       db = SessionLocal()
#       sub = db.get(Subscription, subscription_id)
#       if not sub or sub.login is None:
#           return
#       cred = db.scalar(select(AccessCredential)
#                        .where(AccessCredential.username == sub.login))
#       # ... same per-user logic from populate(), but for one sub
#
#   # In app/services/events/handlers/subscription.py:
#   def on_subscription_changed(event):
#       upsert_one.delay(str(event.subscription_id))
#
#   def on_access_credential_changed(event):
#       # find subs sharing this username, queue each
#       ...
#
# Should also handle delete: when a sub is canceled/terminated, DELETE the
# radcheck + radreply rows so freeradius rejects future auths.
# -----------------------------------------------------------------------------
