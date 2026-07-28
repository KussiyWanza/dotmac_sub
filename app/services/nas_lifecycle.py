"""Cross-domain NAS lifecycle and subscription access-path reconciliation SOT."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter, defaultdict
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, NasDeviceStatus, Subscription
from app.models.network_monitoring import AlertSeverity, NetworkDevice
from app.models.radius_active_session import RadiusActiveSession
from app.services.audit_adapter import stage_audit_event
from app.services.catalog.subscriptions import Subscriptions
from app.services.nas.devices import NasDevices
from app.services.network.radius_sessions import list_all_active_sessions
from app.services.observability import (
    Finding,
    StateObservation,
    publish_state_snapshot,
    record_finding,
    resolve_findings,
)
from app.services.radius import (
    apply_radius_nas_lifecycle,
    radius_nas_lifecycle_states,
)

_MONITORING_FRESHNESS = timedelta(minutes=15)
_FINDING_PREFIX = "network:nas-lifecycle:"
logger = logging.getLogger(__name__)


class NasLifecycleAction(StrEnum):
    reactivate = "reactivate"
    relink_and_decommission = "relink_and_decommission"
    decommission = "decommission"
    reconcile_radius_active = "reconcile_radius_active"
    manual_review = "manual_review"


@dataclass(frozen=True)
class SubscriptionNasRelink:
    subscription_id: str
    target_nas_device_id: str

    def digest_value(self) -> dict[str, str]:
        return {
            "subscription_id": self.subscription_id,
            "target_nas_device_id": self.target_nas_device_id,
        }


@dataclass(frozen=True)
class NasLifecyclePlanItem:
    nas_device_id: str
    nas_name: str
    action: NasLifecycleAction
    current_is_active: bool
    current_status: str
    nonterminal_subscriptions: int
    current_live_sessions: int
    monitoring_up: bool
    monitoring_state: str
    internal_radius_clients: int
    external_radius_present: bool
    client_ip_present: bool
    local_secret_present: bool
    relinks: tuple[SubscriptionNasRelink, ...] = ()
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.action == NasLifecycleAction.manual_review

    def digest_value(self) -> dict[str, object]:
        return {
            "nas_device_id": self.nas_device_id,
            "action": self.action.value,
            "current_is_active": self.current_is_active,
            "current_status": self.current_status,
            "nonterminal_subscriptions": self.nonterminal_subscriptions,
            "current_live_sessions": self.current_live_sessions,
            "monitoring_up": self.monitoring_up,
            "monitoring_state": self.monitoring_state,
            "internal_radius_clients": self.internal_radius_clients,
            "external_radius_present": self.external_radius_present,
            "client_ip_present": self.client_ip_present,
            "local_secret_present": self.local_secret_present,
            "relinks": [relink.digest_value() for relink in self.relinks],
            "reason": self.reason,
        }

    def public_detail(self) -> dict[str, object]:
        return {
            "nas_device_id": self.nas_device_id,
            "nas_name": self.nas_name,
            "action": self.action.value,
            "reason": self.reason,
            "current_is_active": self.current_is_active,
            "current_status": self.current_status,
            "nonterminal_subscriptions": self.nonterminal_subscriptions,
            "current_live_sessions": self.current_live_sessions,
            "monitoring_state": self.monitoring_state,
            "internal_radius_clients": self.internal_radius_clients,
            "external_radius_present": self.external_radius_present,
            "relink_count": len(self.relinks),
            "target_nas_device_ids": sorted(
                {relink.target_nas_device_id for relink in self.relinks}
            ),
        }


@dataclass(frozen=True)
class NasLifecyclePlan:
    items: tuple[NasLifecyclePlanItem, ...]
    generated_at: datetime

    @property
    def action_counts(self) -> dict[str, int]:
        counts = Counter(item.action.value for item in self.items)
        return {key: int(counts[key]) for key in sorted(counts)}

    @property
    def blocked(self) -> int:
        return sum(item.blocked for item in self.items)

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            [item.digest_value() for item in self.items],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def as_dict(self, *, include_details: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "plan_digest": self.digest,
            "items": len(self.items),
            "blocked": self.blocked,
            "actions": self.action_counts,
            "generated_at": self.generated_at.isoformat(),
        }
        if include_details:
            payload["details"] = [item.public_detail() for item in self.items]
        return payload


@dataclass(frozen=True)
class NasLifecycleResult:
    status: str
    execute: bool
    plan: NasLifecyclePlan
    nas_transitions: int = 0
    subscription_relinks: int = 0
    internal_radius_changes: int = 0
    external_radius_changes: int = 0
    reason: str | None = None

    def as_dict(self, *, include_details: bool = False) -> dict[str, object]:
        return {
            "status": self.status,
            "execute": self.execute,
            **self.plan.as_dict(include_details=include_details),
            "nas_transitions": self.nas_transitions,
            "subscription_relinks": self.subscription_relinks,
            "internal_radius_changes": self.internal_radius_changes,
            "external_radius_changes": self.external_radius_changes,
            "reason": self.reason,
        }


def _enum_value(value) -> str:
    return str(getattr(value, "value", value) or "")


def _fresh(timestamp: datetime | None, now: datetime) -> bool:
    if timestamp is None:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return now - timestamp <= _MONITORING_FRESHNESS


def _monitoring_evidence(
    node: NetworkDevice | None,
    *,
    now: datetime,
) -> tuple[bool, str]:
    if node is None or not node.is_active:
        return False, "status_retry_pending"
    from app.services.topology.live_status import live_status_observed_at

    live = str(node.live_status or "").strip().lower()
    observation_at = live_status_observed_at(
        node,
        now=now,
        stale_after_seconds=int(_MONITORING_FRESHNESS.total_seconds()),
    )
    if live == "up" and _fresh(observation_at, now):
        return True, "live_status_up"
    if node.last_ping_ok is True and _fresh(node.last_ping_at, now):
        return True, "ping_up"
    if live == "down" and _fresh(observation_at, now):
        return False, "live_status_down"
    if node.last_ping_ok is False and _fresh(node.last_ping_at, now):
        return False, "ping_down"
    return False, "stale_or_unknown"


def _network_nodes_by_nas(
    db: Session,
    nas_devices: list[NasDevice],
) -> dict[object, NetworkDevice]:
    nas_ids = {device.id for device in nas_devices}
    direct_ids = {
        device.network_device_id
        for device in nas_devices
        if device.network_device_id is not None
    }
    nodes = db.scalars(
        select(NetworkDevice).where(
            or_(
                NetworkDevice.id.in_(direct_ids),
                (
                    (NetworkDevice.matched_device_type == "nas")
                    & (NetworkDevice.matched_device_id.in_(nas_ids))
                ),
            )
        )
    ).all()
    by_id = {node.id: node for node in nodes}
    by_nas: dict[object, NetworkDevice] = {}
    for device in nas_devices:
        direct = (
            by_id.get(device.network_device_id)
            if device.network_device_id is not None
            else None
        )
        if direct is not None:
            by_nas[device.id] = direct
    for node in nodes:
        if node.matched_device_type == "nas" and node.matched_device_id in nas_ids:
            by_nas.setdefault(node.matched_device_id, node)
    return by_nas


def _session_ids_by_nas(
    sessions: Sequence[RadiusActiveSession],
    nas_devices: list[NasDevice],
) -> dict[object, set[object]]:
    by_device_id: dict[object, set[object]] = defaultdict(set)
    by_ip: dict[str, set[object]] = defaultdict(set)
    for session in sessions:
        if session.nas_device_id is not None:
            by_device_id[session.nas_device_id].add(session.id)
        if session.nas_ip_address:
            by_ip[str(session.nas_ip_address).strip()].add(session.id)
    result: dict[object, set[object]] = {}
    for device in nas_devices:
        ids = set(by_device_id.get(device.id, set()))
        for value in (device.nas_ip, device.management_ip, device.ip_address):
            if value:
                ids.update(by_ip.get(str(value).strip(), set()))
        result[device.id] = ids
    return result


def _relinks_for_subscriptions(
    subscriptions: list[Subscription],
    *,
    sessions_by_subscription: dict[object, set[object]],
    active_nas_ids: AbstractSet[object],
    current_nas_id: object,
) -> tuple[SubscriptionNasRelink, ...]:
    relinks: list[SubscriptionNasRelink] = []
    for subscription in subscriptions:
        candidates = {
            nas_id
            for nas_id in sessions_by_subscription.get(subscription.id, set())
            if nas_id in active_nas_ids and nas_id != current_nas_id
        }
        if len(candidates) != 1:
            return ()
        relinks.append(
            SubscriptionNasRelink(
                subscription_id=str(subscription.id),
                target_nas_device_id=str(next(iter(candidates))),
            )
        )
    return tuple(sorted(relinks, key=lambda item: item.subscription_id))


def build_nas_lifecycle_plan(
    db: Session,
    *,
    now: datetime | None = None,
    lock: bool = False,
) -> NasLifecyclePlan:
    """Build a deterministic plan from lifecycle, service, session, and NOC truth."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    nas_devices = NasDevices.list_for_lifecycle(db, lock=lock)
    if not nas_devices:
        return NasLifecyclePlan(items=(), generated_at=now)

    nas_ids = {device.id for device in nas_devices}
    subscriptions = Subscriptions.list_nonterminal_for_nas_devices(db, nas_ids)
    subscriptions_by_nas: dict[object, list[Subscription]] = defaultdict(list)
    for subscription in subscriptions:
        subscriptions_by_nas[subscription.provisioning_nas_device_id].append(
            subscription
        )

    sessions = list_all_active_sessions(db)
    session_ids_by_nas = _session_ids_by_nas(sessions, nas_devices)
    sessions_by_subscription: dict[object, set[object]] = defaultdict(set)
    for session in sessions:
        if session.subscription_id is not None and session.nas_device_id is not None:
            sessions_by_subscription[session.subscription_id].add(session.nas_device_id)

    active_nas_ids = {
        device.id
        for device in nas_devices
        if device.is_active and device.status == NasDeviceStatus.active
    }
    nodes_by_nas = _network_nodes_by_nas(db, nas_devices)
    radius_states = radius_nas_lifecycle_states(db, nas_devices)
    items: list[NasLifecyclePlanItem] = []

    for device in nas_devices:
        status = _enum_value(device.status)
        device_subscriptions = subscriptions_by_nas.get(device.id, [])
        current_sessions = len(session_ids_by_nas.get(device.id, set()))
        monitoring_up, monitoring_state = _monitoring_evidence(
            nodes_by_nas.get(device.id), now=now
        )
        radius_state = radius_states[device.id]
        can_rebuild_radius = bool(radius_state.client_ip and device.shared_secret)
        usable_radius_identity = bool(
            radius_state.client_ip
            and (device.shared_secret or radius_state.external_present)
        )
        relinks = _relinks_for_subscriptions(
            device_subscriptions,
            sessions_by_subscription=sessions_by_subscription,
            active_nas_ids=active_nas_ids,
            current_nas_id=device.id,
        )
        action: NasLifecycleAction | None = None
        reason = ""

        if not device.is_active:
            if status == NasDeviceStatus.decommissioned.value:
                if current_sessions:
                    action = NasLifecycleAction.manual_review
                    reason = "decommissioned_nas_has_live_sessions"
                elif device_subscriptions and relinks:
                    action = NasLifecycleAction.relink_and_decommission
                    reason = "all_service_paths_proven_on_active_nas"
                elif device_subscriptions:
                    action = NasLifecycleAction.manual_review
                    reason = "decommissioned_nas_has_service_dependencies"
                elif (
                    radius_state.internal_active_clients
                    or radius_state.external_present
                ):
                    action = NasLifecycleAction.decommission
                    reason = "decommissioned_nas_has_radius_state"
            elif current_sessions or monitoring_up:
                if usable_radius_identity:
                    action = NasLifecycleAction.reactivate
                    reason = (
                        "live_session_proves_active"
                        if current_sessions
                        else "fresh_monitoring_proves_active"
                    )
                else:
                    action = NasLifecycleAction.manual_review
                    reason = "active_evidence_without_usable_radius_identity"
            elif device_subscriptions and relinks:
                action = NasLifecycleAction.relink_and_decommission
                reason = "all_service_paths_proven_on_active_nas"
            elif device_subscriptions:
                action = NasLifecycleAction.manual_review
                reason = "inactive_nas_has_unresolved_service_dependencies"
            else:
                action = NasLifecycleAction.decommission
                reason = "inactive_nas_has_no_service_or_session_dependency"
        elif status == NasDeviceStatus.decommissioned.value:
            action = NasLifecycleAction.manual_review
            reason = "active_flag_conflicts_with_decommissioned_status"
        elif status == NasDeviceStatus.active.value:
            radius_complete = bool(
                radius_state.internal_active_clients > 0
                and radius_state.external_present
            )
            if not radius_complete and can_rebuild_radius:
                action = NasLifecycleAction.reconcile_radius_active
                reason = "active_nas_radius_projection_incomplete"
            elif not radius_complete and (device_subscriptions or current_sessions):
                action = NasLifecycleAction.manual_review
                reason = "active_nas_has_dependencies_without_usable_radius_identity"

        if action is None:
            continue
        items.append(
            NasLifecyclePlanItem(
                nas_device_id=str(device.id),
                nas_name=device.name,
                action=action,
                current_is_active=bool(device.is_active),
                current_status=status,
                nonterminal_subscriptions=len(device_subscriptions),
                current_live_sessions=current_sessions,
                monitoring_up=monitoring_up,
                monitoring_state=monitoring_state,
                internal_radius_clients=radius_state.internal_active_clients,
                external_radius_present=radius_state.external_present,
                client_ip_present=bool(radius_state.client_ip),
                local_secret_present=bool(device.shared_secret),
                relinks=relinks,
                reason=reason,
            )
        )

    items.sort(key=lambda item: (item.nas_device_id, item.action.value))
    return NasLifecyclePlan(items=tuple(items), generated_at=now)


def _publish_plan(db: Session, plan: NasLifecyclePlan, status: str) -> None:
    observations = [
        StateObservation(
            signal="plan_items", scope="all", value=float(len(plan.items))
        ),
        StateObservation(
            signal="manual_review", scope="all", value=float(plan.blocked)
        ),
    ]
    observations.extend(
        StateObservation(signal="action", scope=action, value=float(count))
        for action, count in plan.action_counts.items()
    )
    try:
        publish_state_snapshot(
            "nas_lifecycle",
            observations,
            status="error" if plan.blocked else ("degraded" if plan.items else "ok"),
            now=plan.generated_at,
        )
    except Exception:
        logger.exception("nas_lifecycle_state_snapshot_failed")

    active: set[str] = set()
    if plan.blocked:
        fingerprint = f"{_FINDING_PREFIX}manual-review"
        active.add(fingerprint)
        record_finding(
            db,
            Finding(
                fingerprint=fingerprint,
                domain="network",
                source="nas_lifecycle",
                severity=AlertSeverity.warning,
                title="NAS lifecycle records require review",
                summary=f"{plan.blocked} NAS lifecycle record(s) require review.",
                details={"actions": plan.action_counts, "status": status},
                target_url="/admin/network/core-devices",
            ),
        )
    resolve_findings(
        db,
        managed_prefix=_FINDING_PREFIX,
        active_fingerprints=active,
    )


def reconcile_nas_lifecycle(
    db: Session,
    *,
    execute: bool = False,
    confirm_plan_digest: str | None = None,
) -> NasLifecycleResult:
    plan = build_nas_lifecycle_plan(db, lock=execute)
    if plan.blocked:
        result = NasLifecycleResult(
            status="blocked",
            execute=execute,
            plan=plan,
            reason="manual_review_required",
        )
        _publish_plan(db, plan, result.status)
        return result
    if not execute:
        result = NasLifecycleResult(status="dry_run", execute=False, plan=plan)
        _publish_plan(db, plan, result.status)
        return result
    if not confirm_plan_digest or confirm_plan_digest != plan.digest:
        result = NasLifecycleResult(
            status="confirmation_required",
            execute=True,
            plan=plan,
            reason="plan_digest_mismatch",
        )
        _publish_plan(db, plan, result.status)
        return result

    nas_transitions = 0
    subscription_relinks = 0
    internal_radius_changes = 0
    external_radius_changes = 0
    for item in plan.items:
        device = db.get(NasDevice, item.nas_device_id)
        if device is None:
            raise RuntimeError("NAS lifecycle target disappeared after planning")
        desired_active = item.action in {
            NasLifecycleAction.reactivate,
            NasLifecycleAction.reconcile_radius_active,
        }
        if item.action == NasLifecycleAction.reactivate:
            device = NasDevices.stage_lifecycle_state(
                db,
                item.nas_device_id,
                is_active=True,
                status=NasDeviceStatus.active,
            )
            nas_transitions += 1
        elif item.action == NasLifecycleAction.relink_and_decommission:
            for relink in item.relinks:
                Subscriptions.stage_provisioning_nas_assignment(
                    db,
                    relink.subscription_id,
                    relink.target_nas_device_id,
                )
                subscription_relinks += 1
            device = NasDevices.stage_lifecycle_state(
                db,
                item.nas_device_id,
                is_active=False,
                status=NasDeviceStatus.decommissioned,
            )
            nas_transitions += 1
        elif item.action == NasLifecycleAction.decommission:
            device = NasDevices.stage_lifecycle_state(
                db,
                item.nas_device_id,
                is_active=False,
                status=NasDeviceStatus.decommissioned,
            )
            nas_transitions += 1

        radius_result = apply_radius_nas_lifecycle(
            db,
            device,
            active=desired_active,
        )
        internal_radius_changes += radius_result.internal_clients_changed
        external_radius_changes += radius_result.external_clients_changed
        stage_audit_event(
            db,
            action="nas_lifecycle_reconcile",
            entity_type="NasDevice",
            entity_id=item.nas_device_id,
            metadata={
                "action": item.action.value,
                "reason": item.reason,
                "subscription_relinks": len(item.relinks),
                "plan_digest": plan.digest,
            },
        )

    db.commit()
    after = build_nas_lifecycle_plan(db)
    result = NasLifecycleResult(
        status="completed" if not after.items else "incomplete",
        execute=True,
        plan=after,
        nas_transitions=nas_transitions,
        subscription_relinks=subscription_relinks,
        internal_radius_changes=internal_radius_changes,
        external_radius_changes=external_radius_changes,
        reason=None if not after.items else "lifecycle_drift_remains",
    )
    _publish_plan(db, after, result.status)
    return result
