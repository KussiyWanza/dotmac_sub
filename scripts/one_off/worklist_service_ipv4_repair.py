#!/usr/bin/env python
"""Read-only worklist for the exact-service IPv4 ledger repair.

Prerequisite for the served-IP authority cutover documented in
``docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md``: RADIUS cannot be flipped to
consume the exact-service assignment until every projected subscription HAS one
and it agrees with what is being served. This enumerates that work. It applies
nothing.

**It does not classify anything itself.** Every row carries the decision and
fingerprint returned by
``ip_assignment_lifecycle.preview_service_ipv4_assignment_repair`` — the owner
of that judgement. "Free in IPAM" is not a decision: an address can be
unassigned and still be reserved, an ONT or device host, inside a routed block,
in an inactive or infrastructure pool, or not materialised at all. Only the
owner's preview accounts for those, so only the owner's preview is reported.

Two populations, deliberately kept apart because they need different decisions:

MISSING — the served column names an address, but no active exact-service
assignment backs it. Grouped by the owner's decision:

  ready_link              a same-subscriber legacy assignment already holds the
                          address; the repair links it to the exact service
  ready_create            materialised, unowned, in an active customer pool,
                          and passes every serviceability exclusion
  active_owner_conflict   another service actively owns it
  materialization_debt    the address falls inside an active pool CIDR but has
                          no IPv4Address row to assign
  outside_pool            no active pool contains it
  non_serviceable         reserved, management, ONT/device host, routed block,
                          or an inactive/infrastructure pool
  other                   any remaining owner decision, reported verbatim

MISMATCH — the column and the active assignment disagree. Four sources are
shown SIDE BY SIDE: served column, exact assignment, external RADIUS
Framed-IP, and the fresh live-session address. **No winner is inferred.** Three
sources agreeing is not evidence: the column is what feeds RADIUS, and the
session is what RADIUS handed out, so those three move together by construction
and outvoting the assignment would simply re-elect the drift. Each row carries
the owner's preview for BOTH candidate targets so a reviewer can see what each
choice would do before choosing.

Usage (inside the app container so the DB resolves):

    docker compose exec app python scripts/one_off/worklist_service_ipv4_repair.py
    ... --json > worklist.json

Exit status is non-zero when any work remains.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Column, String, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.catalog import Subscription
from app.models.network import IPAssignment, IpPool, IPv4Address, IPVersion
from app.models.radius_active_session import RadiusActiveSession
from app.services.ip_assignment_lifecycle import (
    IPv4AssignmentRepairDecision,
    preview_service_ipv4_assignment_repair,
)
from app.services.network.radius_sessions import ACTIVE_SESSION_FRESHNESS
from app.services.radius_access_state import ACTIVE_STATUSES, BLOCKED_STATUSES

_NON_SERVICEABLE = {
    IPv4AssignmentRepairDecision.target_address_not_serviceable,
    IPv4AssignmentRepairDecision.target_address_in_routed_block,
    IPv4AssignmentRepairDecision.target_address_is_device_host,
}


def _norm(value: object) -> str:
    if not value:
        return ""
    text = str(value).strip().split("/", 1)[0]
    if not text:
        return ""
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text


def _active_pool_networks(db: Session) -> list[tuple[str, Any]]:
    pools: list[tuple[str, Any]] = []
    for name, cidr in db.execute(
        select(IpPool.name, IpPool.cidr)
        .where(IpPool.is_active.is_(True))
        .where(IpPool.ip_version == IPVersion.ipv4)
    ).all():
        try:
            pools.append((name, ipaddress.ip_network(str(cidr), strict=False)))
        except ValueError:
            continue
    return pools


def _containing_pool(pools: list[tuple[str, Any]], address: str) -> str:
    try:
        host = ipaddress.ip_address(address)
    except ValueError:
        return ""
    for name, network in pools:
        if host in network:
            return name
    return ""


def _address_ids(db: Session) -> dict[str, UUID]:
    return {
        _norm(address): row_id
        for row_id, address in db.execute(
            select(IPv4Address.id, IPv4Address.address)
        ).all()
        if _norm(address)
    }


def _external_framed_ips(db: Session, logins: list[str]) -> dict[str, str]:
    """login -> radreply Framed-IP, read from every configured RADIUS target."""
    from app.services.radius import (
        _active_external_sync_configs,
        _external_radius_table,
        _get_external_engine,
    )

    framed: dict[str, str] = {}
    for config in _active_external_sync_configs(db):
        try:
            engine = _get_external_engine(config["db_url"])
            radreply = _external_radius_table(
                config.get("radreply_table", "radreply"),
                Column("username", String),
                Column("attribute", String),
                Column("value", String),
            )
            with engine.connect() as conn:
                for chunk_start in range(0, len(logins), 500):
                    chunk = logins[chunk_start : chunk_start + 500]
                    for username, value in conn.execute(
                        select(radreply.c.username, radreply.c.value)
                        .where(radreply.c.username.in_(chunk))
                        .where(radreply.c.attribute == "Framed-IP-Address")
                    ).all():
                        norm = _norm(value)
                        if norm:
                            framed.setdefault(username, norm)
        except Exception as exc:  # noqa: BLE001 - reported, never guessed around
            framed.setdefault("__error__", str(exc))
    return framed


def _fresh_session_ips(db: Session) -> dict[str, str]:
    cutoff = datetime.now(UTC) - ACTIVE_SESSION_FRESHNESS
    fresh: dict[str, str] = {}
    for session in db.scalars(select(RadiusActiveSession)).all():
        seen = session.last_update or session.session_start
        if seen is None:
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
        if seen < cutoff:
            continue
        norm = _norm(session.framed_ip_address)
        if norm:
            fresh.setdefault(session.username, norm)
    return fresh


def _preview(db: Session, subscription_id: UUID, address_id: UUID | None) -> Any:
    return preview_service_ipv4_assignment_repair(
        db,
        subscription_id=subscription_id,
        desired_address_id=address_id,
    )


def _group_for(
    decision: IPv4AssignmentRepairDecision,
    address: str,
    materialised: bool,
    pool: str,
) -> str:
    if decision is IPv4AssignmentRepairDecision.ready_link:
        return "ready_link"
    if decision is IPv4AssignmentRepairDecision.ready_create:
        return "ready_create"
    if decision is IPv4AssignmentRepairDecision.target_owned_by_other_service:
        return "active_owner_conflict"
    if decision in _NON_SERVICEABLE:
        return "non_serviceable"
    if decision is IPv4AssignmentRepairDecision.target_address_not_found:
        # The owner cannot distinguish "never materialised inside a pool we own"
        # from "not our address at all"; pool containment is the supplementary
        # evidence that separates repairable debt from out-of-scope.
        return "materialization_debt" if pool else "outside_pool"
    if not materialised and pool:
        return "materialization_debt"
    return "other"


def build_worklist(db: Session) -> dict[str, Any]:
    pools = _active_pool_networks(db)
    address_ids = _address_ids(db)

    subscriptions = (
        db.execute(
            select(Subscription).where(
                Subscription.status.in_(ACTIVE_STATUSES | BLOCKED_STATUSES),
                Subscription.login.is_not(None),
            )
        )
        .unique()
        .scalars()
        .all()
    )

    assigned: dict[str, list[tuple[str, str]]] = {}
    for subscription_id, assignment_id, address in db.execute(
        select(IPAssignment.subscription_id, IPAssignment.id, IPv4Address.address)
        .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ip_version == IPVersion.ipv4)
        .where(IPAssignment.subscription_id.is_not(None))
    ).all():
        assigned.setdefault(str(subscription_id), []).append(
            (str(assignment_id), _norm(address))
        )

    logins = sorted({(s.login or "").strip() for s in subscriptions if s.login})
    framed_by_login = _external_framed_ips(db, logins)
    radius_error = framed_by_login.pop("__error__", "")
    session_by_login = _fresh_session_ips(db)

    missing: dict[str, list[dict[str, Any]]] = {}
    mismatch: list[dict[str, Any]] = []

    for subscription in subscriptions:
        sub_id = str(subscription.id)
        login = (subscription.login or "").strip()
        column_ip = _norm(subscription.ipv4_address)
        if column_ip == "0.0.0.0":  # nosec B104  # noqa: S104
            column_ip = ""
        owned = assigned.get(sub_id, [])

        if not column_ip and not owned:
            continue  # purely dynamic, not repair work

        if not owned and column_ip:
            address_id = address_ids.get(column_ip)
            preview = _preview(db, subscription.id, address_id)
            pool = _containing_pool(pools, column_ip)
            group = _group_for(
                preview.decision, column_ip, address_id is not None, pool
            )
            missing.setdefault(group, []).append(
                {
                    "login": login,
                    "subscription_id": sub_id,
                    "subscriber_id": str(subscription.subscriber_id),
                    "column_ip": column_ip,
                    "ipv4_address_id": str(address_id) if address_id else None,
                    "containing_active_pool": pool or None,
                    "owner_decision": preview.decision.value,
                    "owner_applicable": preview.applicable,
                    "preview_fingerprint": preview.fingerprint,
                    "radius_framed_ip": framed_by_login.get(login, ""),
                    "fresh_session_ip": session_by_login.get(login, ""),
                }
            )
            continue

        owned_addresses = sorted({address for _, address in owned})
        if column_ip and owned_addresses and column_ip not in owned_addresses:
            column_address_id = address_ids.get(column_ip)
            assignment_address_id = address_ids.get(owned_addresses[0])
            column_preview = _preview(db, subscription.id, column_address_id)
            assignment_preview = _preview(db, subscription.id, assignment_address_id)
            mismatch.append(
                {
                    "login": login,
                    "subscription_id": sub_id,
                    "subscriber_id": str(subscription.subscriber_id),
                    "assignment_ids": [aid for aid, _ in owned],
                    # Four sources, side by side, no winner implied.
                    "served_column": column_ip,
                    "exact_assignment": ",".join(owned_addresses),
                    "external_radius": framed_by_login.get(login, ""),
                    "fresh_session": session_by_login.get(login, ""),
                    "if_column_wins": {
                        "decision": column_preview.decision.value,
                        "applicable": column_preview.applicable,
                        "fingerprint": column_preview.fingerprint,
                    },
                    "if_assignment_wins": {
                        "decision": assignment_preview.decision.value,
                        "applicable": assignment_preview.applicable,
                        "fingerprint": assignment_preview.fingerprint,
                    },
                }
            )

    counts = {group: len(rows) for group, rows in sorted(missing.items())}
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "external_radius_error": radius_error or None,
        "subscriptions_considered": len(subscriptions),
        "missing_counts": counts,
        "missing_total": sum(counts.values()),
        "mismatch_total": len(mismatch),
        "missing": missing,
        "mismatch": mismatch,
    }


def _print_human(result: dict[str, Any]) -> None:
    print(f"Generated {result['generated_at']} (READ-ONLY — nothing applied)")
    if result["external_radius_error"]:
        print(f"!! external RADIUS read incomplete: {result['external_radius_error']}")
    print(f"\nMISSING exact-service assignment: {result['missing_total']}")
    for group, count in result["missing_counts"].items():
        print(f"  {group:24} {count}")
    for group, rows in result["missing"].items():
        print(f"\n--- {group} ---")
        for row in rows:
            print(
                f"  {row['login']:11} {row['column_ip']:16} "
                f"pool={row['containing_active_pool'] or '-':20} "
                f"decision={row['owner_decision']:32} fp={row['preview_fingerprint']}"
            )
            print(f"    sub={row['subscription_id']}  addr_id={row['ipv4_address_id']}")
    print(f"\nMISMATCH column vs assignment: {result['mismatch_total']}")
    print("  (four sources side by side — no winner inferred)")
    print(
        f"  {'login':11} {'column':16} {'assignment':16} {'radius':16} {'session':16}"
    )
    for row in result["mismatch"]:
        print(
            f"  {row['login']:11} {row['served_column']:16} "
            f"{row['exact_assignment']:16} {row['external_radius'] or '-':16} "
            f"{row['fresh_session'] or '-':16}"
        )
        print(
            f"    sub={row['subscription_id']}  "
            f"if_column={row['if_column_wins']['decision']}"
            f"/{row['if_column_wins']['fingerprint']}  "
            f"if_assignment={row['if_assignment_wins']['decision']}"
            f"/{row['if_assignment_wins']['fingerprint']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        result = build_worklist(session)
    finally:
        session.close()

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_human(result)
    return 0 if not (result["missing_total"] or result["mismatch_total"]) else 1


if __name__ == "__main__":
    sys.exit(main())
