"""Pre-change backup of a subscriber's connectivity state — capture + restore.

Snapshots the external RADIUS rows (``radcheck``/``radreply``/``radusergroup``), the internal
credential/radius-user active flags, and the IP state (served columns + active
``IPAssignment`` rows) for one subscriber *before* a destructive connectivity
mutation. The reconciler/enforcement paths can then mutate with a way back.

Design rules:
- **Capture never breaks the mutation it guards.** ``capture_connectivity_state``
  swallows its own errors and returns ``None`` on failure (the caller also wraps
  it). A missing RADIUS DB degrades to a partial backup, not an exception.
- **It is a backup, not a source of truth.** The reconciler never reads these
  rows to make decisions; only an explicit ``restore`` consumes them.
- **Restore defaults to dry-run.** Applying a backup re-materializes the captured
  RADIUS rows and flips the local flags/IP back — an operator action, gated.

RADIUS targets are resolved from DB configuration. Restore requests the sole
``access.radius_projection`` owner; this module never writes auth tables.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Column, Integer, String, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.catalog import AccessCredential, Subscription
from app.models.connectivity_backup import ConnectivityStateBackup
from app.models.network import (
    IPAssignment,
    IPv4Address,
    IPv6Address,
    IPVersion,
)
from app.models.radius import RadiusUser
from app.services.audit_adapter import stage_audit_event
from app.services.common import coerce_uuid
from app.services.external_radius_targets import (
    active_external_radius_targets,
    external_radius_table,
    get_external_engine,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reads (best-effort, no mutation)
# ---------------------------------------------------------------------------


def _subscriber_usernames(db: Session, subscriber_id: Any) -> list[str]:
    """Every RADIUS username that belongs to this subscriber: credential
    usernames ∪ radius_user usernames ∪ subscription logins. Deduped, non-empty,
    stable order."""
    sid = coerce_uuid(subscriber_id)
    names: list[str] = []
    seen: set[str] = set()

    def _add(value: str | None) -> None:
        v = (value or "").strip()
        if v and v not in seen:
            seen.add(v)
            names.append(v)

    for username in db.scalars(
        select(AccessCredential.username).where(AccessCredential.subscriber_id == sid)
    ).all():
        _add(username)
    for username in db.scalars(
        select(RadiusUser.username).where(RadiusUser.subscriber_id == sid)
    ).all():
        _add(username)
    for login in db.scalars(
        select(Subscription.login).where(Subscription.subscriber_id == sid)
    ).all():
        _add(login)
    return names


def _read_radius_rows(
    db: Session,
    usernames: list[str],
) -> tuple[list[dict] | None, str | None]:
    """Read all three owned tables from every configured projection target."""
    if not usernames:
        return [], None
    targets = active_external_radius_targets(db, capability="users")
    if not targets:
        return None, "radius_targets_unconfigured"
    snapshots: list[dict] = []
    failures: list[str] = []
    for target in targets:
        snapshot = {
            "target_id": target["target_id"],
            "target_name": target["target_name"],
            "target_fingerprint": target["target_fingerprint"],
            "usernames": list(usernames),
            "radcheck": [],
            "radreply": [],
            "radusergroup": [],
        }
        try:
            radcheck = external_radius_table(
                target["radcheck_table"],
                Column("username", String),
                Column("attribute", String),
                Column("op", String),
                Column("value", String),
            )
            radreply = external_radius_table(
                target["radreply_table"],
                Column("username", String),
                Column("attribute", String),
                Column("op", String),
                Column("value", String),
            )
            radusergroup = external_radius_table(
                target["radusergroup_table"],
                Column("username", String),
                Column("groupname", String),
                Column("priority", Integer),
            )
            engine = get_external_engine(target["db_url"])
            with engine.connect() as conn:
                snapshot["radcheck"] = [
                    dict(row._mapping)
                    for row in conn.execute(
                        select(
                            radcheck.c.username,
                            radcheck.c.attribute,
                            radcheck.c.op,
                            radcheck.c.value,
                        ).where(radcheck.c.username.in_(usernames))
                    ).all()
                ]
                snapshot["radreply"] = [
                    dict(row._mapping)
                    for row in conn.execute(
                        select(
                            radreply.c.username,
                            radreply.c.attribute,
                            radreply.c.op,
                            radreply.c.value,
                        ).where(radreply.c.username.in_(usernames))
                    ).all()
                ]
                snapshot["radusergroup"] = [
                    dict(row._mapping)
                    for row in conn.execute(
                        select(
                            radusergroup.c.username,
                            radusergroup.c.groupname,
                            radusergroup.c.priority,
                        ).where(radusergroup.c.username.in_(usernames))
                    ).all()
                ]
            snapshots.append(snapshot)
        except Exception as exc:  # best-effort: partial backup beats no mutation
            failures.append(f"{target['target_name']}:{type(exc).__name__}")
    return (snapshots or None), (",".join(failures) or None)


def _capture_credentials(db: Session, subscriber_id: Any) -> list[dict]:
    sid = coerce_uuid(subscriber_id)
    radius_active: dict[str, bool] = {}
    for username, is_active in db.execute(
        select(RadiusUser.username, RadiusUser.is_active).where(
            RadiusUser.subscriber_id == sid
        )
    ).all():
        radius_active[username] = bool(is_active)
    creds: list[dict] = []
    for cred in db.scalars(
        select(AccessCredential).where(AccessCredential.subscriber_id == sid)
    ).all():
        creds.append(
            {
                "credential_id": str(cred.id),
                "username": cred.username,
                "credential_active": bool(cred.is_active),
                "radius_user_active": radius_active.get(cred.username),
            }
        )
    return creds


def _capture_ip_state(db: Session, subscriber_id: Any) -> dict[str, Any]:
    sid = coerce_uuid(subscriber_id)
    subs = [
        {
            "id": str(s.id),
            "status": s.status.value if s.status is not None else None,
            "ipv4_address": s.ipv4_address,
            "ipv6_address": s.ipv6_address,
        }
        for s in db.scalars(
            select(Subscription).where(Subscription.subscriber_id == sid)
        ).all()
    ]
    assignments: list[dict] = []
    rows = db.execute(
        select(
            IPAssignment.id,
            IPAssignment.ip_version,
            IPAssignment.is_active,
            IPv4Address.allocation_type,
            IPv4Address.address,
            IPv6Address.address,
        )
        .select_from(IPAssignment)
        .outerjoin(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
        .outerjoin(IPv6Address, IPAssignment.ipv6_address_id == IPv6Address.id)
        .where(IPAssignment.subscriber_id == sid)
    ).all()
    for aid, ver, active, alloc, v4, v6 in rows:
        assignments.append(
            {
                "id": str(aid),
                "ip_version": ver.value if ver is not None else None,
                "address": v4 if ver == IPVersion.ipv4 else v6,
                "is_active": bool(active),
                "allocation_type": alloc,
            }
        )
    return {"subscriptions": subs, "assignments": assignments}


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def capture_connectivity_state(
    db: Session,
    subscriber_id: Any,
    *,
    reason: str,
    captured_by: str | None = None,
    include_radius: bool = True,
) -> ConnectivityStateBackup | None:
    """Snapshot a subscriber's connectivity state into a backup row.

    Best-effort: returns ``None`` (logging) on failure rather than raising, so it
    is safe to call inline before a destructive mutation. The row is added to the
    caller's session and flushed (so it shares the mutation's transaction — if
    the mutation rolls back, so does the unneeded backup), but NOT committed here.
    """
    try:
        sid = coerce_uuid(subscriber_id)
        usernames = _subscriber_usernames(db, sid)
        radius_targets = None
        radcheck = radreply = None
        radius_error = None
        if include_radius:
            radius_targets, radius_error = _read_radius_rows(db, usernames)
            if radius_targets and len(radius_targets) == 1:
                radcheck = radius_targets[0]["radcheck"]
                radreply = radius_targets[0]["radreply"]
        credentials = _capture_credentials(db, sid)
        ip_state = _capture_ip_state(db, sid)

        backup = ConnectivityStateBackup(
            subscriber_id=sid,
            reason=reason,
            captured_by=captured_by,
            radcheck=radcheck,
            radreply=radreply,
            radius_targets=radius_targets,
            credentials=credentials,
            ip_state=ip_state,
        )
        db.add(backup)
        db.flush()

        stage_audit_event(
            db,
            actor_type=AuditActorType.system,
            actor_id="connectivity_backup",
            action="connectivity_backup.capture",
            entity_type="subscriber",
            entity_id=str(sid),
            metadata={
                "backup_id": str(backup.id),
                "reason": reason,
                "usernames": len(usernames),
                "radcheck_rows": len(radcheck) if radcheck is not None else None,
                "radreply_rows": len(radreply) if radreply is not None else None,
                "radius_targets": len(radius_targets or []),
                "credentials": len(credentials),
                "radius_error": radius_error,
            },
        )
        return backup
    except Exception as exc:  # never break the guarded mutation
        logger.warning(
            "connectivity backup: capture failed for subscriber=%s reason=%s: %s",
            subscriber_id,
            reason,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Restore (operator rollback — dry-run by default)
# ---------------------------------------------------------------------------


def restore_connectivity_state(
    db: Session,
    backup_id: Any,
    *,
    dry_run: bool = True,
    restored_by: str | None = None,
    include_radius: bool = True,
) -> dict[str, Any]:
    """Restore a captured backup. **Dry-run by default** — returns the plan and
    writes nothing. With ``dry_run=False`` it flips local credential/IP flags and
    subscription IP columns back to the captured values and (``include_radius``)
    re-materializes the captured radcheck/radreply rows."""
    backup = db.get(ConnectivityStateBackup, coerce_uuid(backup_id))
    if backup is None:
        return {"ok": False, "reason": "backup_not_found"}

    plan: dict[str, Any] = {
        "ok": True,
        "backup_id": str(backup.id),
        "subscriber_id": str(backup.subscriber_id),
        "dry_run": dry_run,
        "credentials": len(backup.credentials or []),
        "assignments": len((backup.ip_state or {}).get("assignments", [])),
        "subscriptions": len((backup.ip_state or {}).get("subscriptions", [])),
        "radcheck_rows": len(backup.radcheck or []) if backup.radcheck else 0,
        "radreply_rows": len(backup.radreply or []) if backup.radreply else 0,
        "radius_targets": len(backup.radius_targets or []),
        "applied": False,
    }
    if dry_run:
        return plan

    # Local flags: AccessCredential.is_active / RadiusUser.is_active.
    by_username_cred = {c["username"]: c for c in (backup.credentials or [])}
    for cred in db.scalars(
        select(AccessCredential).where(
            AccessCredential.subscriber_id == backup.subscriber_id
        )
    ).all():
        snap = by_username_cred.get(cred.username)
        if snap is not None and snap.get("credential_active") is not None:
            cred.is_active = bool(snap["credential_active"])
    for ru in db.scalars(
        select(RadiusUser).where(RadiusUser.subscriber_id == backup.subscriber_id)
    ).all():
        snap = by_username_cred.get(ru.username)
        if snap is not None and snap.get("radius_user_active") is not None:
            ru.is_active = bool(snap["radius_user_active"])

    # IP state: subscription served columns + IPAssignment.is_active.
    ip_state = backup.ip_state or {}
    sub_snap = {s["id"]: s for s in ip_state.get("subscriptions", [])}
    for sub in db.scalars(
        select(Subscription).where(Subscription.subscriber_id == backup.subscriber_id)
    ).all():
        snap = sub_snap.get(str(sub.id))
        if snap is not None:
            sub.ipv4_address = snap.get("ipv4_address")
            sub.ipv6_address = snap.get("ipv6_address")
    assign_snap = {a["id"]: a for a in ip_state.get("assignments", [])}
    for assign in db.scalars(
        select(IPAssignment).where(IPAssignment.subscriber_id == backup.subscriber_id)
    ).all():
        snap = assign_snap.get(str(assign.id))
        if snap is not None and snap.get("is_active") is not None:
            assign.is_active = bool(snap["is_active"])

    radius_counts: dict[str, object] = {}
    if include_radius and (
        backup.radius_targets
        or backup.radcheck is not None
        or backup.radreply is not None
    ):
        from app.services.radius_population import restore_projection_snapshot

        snapshots = backup.radius_targets
        if not snapshots:
            snapshots = [
                {
                    "usernames": [
                        item["username"]
                        for item in (backup.credentials or [])
                        if item.get("username")
                    ],
                    "radcheck": backup.radcheck or [],
                    "radreply": backup.radreply or [],
                    "radusergroup": [],
                }
            ]
        radius_counts = restore_projection_snapshot(db, snapshots)

    from datetime import UTC, datetime

    backup.restored_at = datetime.now(UTC)
    backup.restored_by = restored_by
    stage_audit_event(
        db,
        actor_type=AuditActorType.system,
        actor_id="connectivity_backup",
        action="connectivity_backup.restore",
        entity_type="subscriber",
        entity_id=str(backup.subscriber_id),
        metadata={"backup_id": str(backup.id), **radius_counts},
    )
    db.commit()

    plan["applied"] = True
    plan["radius"] = radius_counts
    return plan
