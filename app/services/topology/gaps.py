"""Topology-gaps report and customer-path match rate.

Surfaces what the reconcile could not resolve so it can be fixed instead of
silently lost:
- Reconciled nodes with no confident provisioning-device match (unmatched or
  ambiguous both land as ``matched_device_id IS NULL``).
- Active subscriptions whose resolve_customer_path returns a gap.

The match-rate (% of active subscriptions resolving to a complete
ONT → device → basestation path) is the reconciliation cutover metric and doubles as
the empirical answer to "how many customers have no resolvable path" (open
decision C re: wireless).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import (
    CPEDevice,
    DeviceStatus,
    OLTDevice,
    OntAssignment,
    OntUnit,
)
from app.models.network_monitoring import DeviceProjection, NetworkDevice, PopSite
from app.models.radius_active_session import RadiusActiveSession
from app.services.topology.customer_path import (
    GAP_NO_BASESTATION,
    GAP_NO_NODE,
    GAP_NO_ONT,
)

DEFAULT_TABLE_PER_PAGE = 50

# Inventory-vs-projection drift classes. Authority-neutral by construction:
# every one of them is derived by comparing the authoritative device tables
# against the materialised projection, and none of them keys on a provenance
# string. (The previous drift filter required ``source == 'zabbix_reconcile'``,
# the stamp of an importer removed on 2026-07-10, so it reported only on rows
# predating that deletion and was silent about everything newer.)
DRIFT_MISSING_PROJECTION = "missing_projection"
DRIFT_ORPHAN_PROJECTION = "orphan_projection"
DRIFT_STALE_PROJECTION = "stale_projection"

# The projection reconciler is beat-scheduled every 60s. Rows older than this
# mean the reconciler itself has stopped converging.
PROJECTION_STALE_AFTER = timedelta(minutes=15)


@dataclass
class TopologyGaps:
    unmatched_nodes: list[NetworkDevice] = field(default_factory=list)
    unmatched_node_total_count: int = 0
    unmatched_node_page: int = 1
    unmatched_node_per_page: int = DEFAULT_TABLE_PER_PAGE
    subscription_gaps: list[dict] = field(default_factory=list)  # {id, gap}
    subscription_gap_total_count: int = 0
    subscription_gap_page: int = 1
    subscription_gap_per_page: int = DEFAULT_TABLE_PER_PAGE
    active_subscriptions: int = 0
    resolved_complete: int = 0
    # Inventory-vs-projection drift: [{id, name, drift}], newest report only.
    projection_drift: list[dict] = field(default_factory=list)
    projection_drift_total_count: int = 0

    @property
    def unmatched_node_count(self) -> int:
        return self.unmatched_node_total_count

    @property
    def projection_drift_count(self) -> int:
        return self.projection_drift_total_count

    @property
    def unmatched_node_total_pages(self) -> int:
        return _total_pages(
            self.unmatched_node_total_count, self.unmatched_node_per_page
        )

    @property
    def subscription_gap_total_pages(self) -> int:
        return _total_pages(
            self.subscription_gap_total_count, self.subscription_gap_per_page
        )

    @property
    def subscription_gap_count(self) -> int:
        return self.subscription_gap_total_count

    @property
    def subscription_gap_display_count(self) -> int:
        return len(self.subscription_gaps)

    @property
    def subscription_gaps_truncated(self) -> bool:
        return self.subscription_gap_total_count > len(self.subscription_gaps)

    @property
    def match_rate(self) -> float:
        if not self.active_subscriptions:
            return 0.0
        return self.resolved_complete / self.active_subscriptions


def _clamp_page(page: int) -> int:
    return max(page, 1)


def _clamp_per_page(per_page: int) -> int:
    return min(max(per_page, 10), 200)


def _total_pages(total: int, per_page: int) -> int:
    if total <= 0:
        return 1
    return ((total - 1) // per_page) + 1


def _page_slice(rows: list, *, page: int, per_page: int) -> list:
    offset = (page - 1) * per_page
    return rows[offset : offset + per_page]


def _active_assignment_query():
    return (
        select(
            OntAssignment.subscriber_id,
            OntAssignment.service_address_id,
            OntAssignment.ont_unit_id,
        )
        .where(
            OntAssignment.subscriber_id.is_not(None),
            OntAssignment.active.is_(True),
        )
        .order_by(OntAssignment.id)
    )


def _device_node_state(
    db: Session, *, device_type: str, device_ids: set
) -> dict[object, tuple[bool, bool]]:
    """Return {device_id: (has_node, has_node_with_existing_pop_site)}."""
    if not device_ids:
        return {}

    rows = db.execute(
        select(
            NetworkDevice.matched_device_id,
            NetworkDevice.pop_site_id,
        ).where(
            NetworkDevice.matched_device_type == device_type,
            NetworkDevice.matched_device_id.in_(device_ids),
        )
    ).all()
    pop_site_ids = {row.pop_site_id for row in rows if row.pop_site_id is not None}
    existing_pop_site_ids = set()
    if pop_site_ids:
        existing_pop_site_ids = set(
            db.execute(select(PopSite.id).where(PopSite.id.in_(pop_site_ids))).scalars()
        )

    state: dict[object, tuple[bool, bool]] = {}
    for row in rows:
        has_complete_node = row.pop_site_id in existing_pop_site_ids
        has_node, has_complete = state.get(row.matched_device_id, (False, False))
        state[row.matched_device_id] = (
            True,
            has_complete or has_complete_node,
        )
    return state


def _wireless_subscriber_state(
    db: Session, subscriber_ids: set
) -> dict[object, tuple[bool, bool]]:
    """{subscriber_id: (has_node, has_node_with_existing_pop_site)} for the
    wireless arm — the batched mirror of customer_path._active_wireless_cpe.

    Per subscriber, the SAME radio the canonical resolver would pick (active
    CPE with a parent AP, not UISP-vanished; most recently uisp-synced first,
    id tie-break) decides the verdict. A subscriber whose selected radio's
    parent node row is missing is omitted entirely, matching the resolver's
    fall-through to the NAS arm.
    """
    if not subscriber_ids:
        return {}
    cpe_rows = db.execute(
        select(CPEDevice.subscriber_id, CPEDevice.parent_network_device_id)
        .where(
            CPEDevice.subscriber_id.in_(subscriber_ids),
            CPEDevice.parent_network_device_id.is_not(None),
            CPEDevice.status == DeviceStatus.active,
            or_(
                CPEDevice.last_uisp_status.is_(None),
                CPEDevice.last_uisp_status != "vanished",
            ),
        )
        .order_by(
            CPEDevice.uisp_synced_at.desc().nullslast(),
            CPEDevice.id,
        )
    ).all()
    # First row per subscriber = the radio _active_wireless_cpe would return.
    selected_parent: dict[object, object] = {}
    for row in cpe_rows:
        selected_parent.setdefault(row.subscriber_id, row.parent_network_device_id)
    if not selected_parent:
        return {}

    ap_pop_by_node = {
        row.id: row.pop_site_id
        for row in db.execute(
            select(NetworkDevice.id, NetworkDevice.pop_site_id).where(
                NetworkDevice.id.in_(set(selected_parent.values()))
            )
        )
    }
    pop_ids = {pid for pid in ap_pop_by_node.values() if pid is not None}
    existing_pop_ids = set()
    if pop_ids:
        existing_pop_ids = set(
            db.execute(select(PopSite.id).where(PopSite.id.in_(pop_ids))).scalars()
        )

    state: dict[object, tuple[bool, bool]] = {}
    for subscriber_id, node_id in selected_parent.items():
        if node_id not in ap_pop_by_node:
            continue  # node row gone: resolver falls through to the NAS arm
        state[subscriber_id] = (
            True,
            ap_pop_by_node[node_id] in existing_pop_ids,
        )
    return state


# Medium labels for the per-subscription classification. "unknown" is the
# no-access-device case (always GAP_NO_ONT): the sub has no ONT, no resolvable
# radio, and no provisioning NAS, so we cannot tell what medium it should be.
MEDIUM_FIBER = "fiber"
MEDIUM_WIRELESS = "wireless"
MEDIUM_NAS = "nas"
MEDIUM_UNKNOWN = "unknown"


def _live_nas_by_subscription(
    db: Session, active_subs: Sequence, subscriber_ids: set
) -> dict[object, object]:
    """{subscription_id: live nas_device_id} — the batched mirror of
    customer_path._live_session_nas_device_id.

    Applies the SAME sibling-subscription filter: a session explicitly bound to
    a *different* subscription of the same subscriber is excluded (only the
    subscription's own session, or a session with no subscription binding, is
    eligible). Per subscription, the SAME session the canonical resolver would
    pick decides the NAS: prefer this subscription's own binding, then freshest
    (last_update desc nulls-last, session_start desc, id). Reads only
    nas_device_id (a UUID FK) — no raw radacct/inet columns.
    """
    if not subscriber_ids:
        return {}
    rows = db.execute(
        select(
            RadiusActiveSession.subscriber_id,
            RadiusActiveSession.subscription_id,
            RadiusActiveSession.nas_device_id,
            RadiusActiveSession.last_update,
            RadiusActiveSession.session_start,
            RadiusActiveSession.id,
        ).where(
            RadiusActiveSession.subscriber_id.in_(subscriber_ids),
            RadiusActiveSession.nas_device_id.is_not(None),
        )
    ).all()
    if not rows:
        return {}
    by_subscriber: defaultdict[object, list] = defaultdict(list)
    for row in rows:
        by_subscriber[row.subscriber_id].append(row)

    def _order_key(session_row, sub_id):
        # Ascending sort => first element is the row the DB order_by would
        # return: own-subscription binding first, then last_update desc
        # (nulls last), then session_start desc, then id.
        last_update = session_row.last_update
        session_start = session_row.session_start
        return (
            0 if session_row.subscription_id == sub_id else 1,
            last_update is None,
            -last_update.timestamp() if last_update is not None else 0.0,
            -session_start.timestamp() if session_start is not None else 0.0,
            session_row.id,
        )

    result: dict[object, object] = {}
    for sub in active_subs:
        candidates = [
            row
            for row in by_subscriber.get(sub.subscriber_id, [])
            if row.subscription_id == sub.id or row.subscription_id is None
        ]
        if candidates:
            best = min(candidates, key=lambda row: _order_key(row, sub.id))
            result[sub.id] = best.nas_device_id
    return result


def classify_active_subscriptions(db: Session) -> list[dict]:
    # NOTE: This is a batched (set-based) reimplementation of the per-subscription
    # gap classification in ``resolve_customer_path`` (app/services/topology/
    # customer_path.py), avoiding an N+1 across all active subscriptions. The two
    # MUST stay in sync — ``resolve_customer_path`` remains the canonical reader
    # for a single subscription's path; this function must produce the same
    # GAP_NO_ONT / GAP_NO_NODE / GAP_NO_BASESTATION verdict in aggregate.
    #
    # Returns one row per ACTIVE subscription:
    #   {"id": <subscription id>, "medium": MEDIUM_*, "gap": GAP_* | None}
    # ``gap is None`` means the E2E path resolved completely. The gaps page and
    # the coverage-metrics exporter both consume this so the two can never
    # disagree.
    active_subs = db.execute(
        select(
            Subscription.id,
            Subscription.subscriber_id,
            Subscription.service_address_id,
            Subscription.provisioning_nas_device_id,
        )
        .where(Subscription.status == SubscriptionStatus.active)
        .order_by(Subscription.id)
    ).all()
    if not active_subs:
        return []

    subscriber_ids = {row.subscriber_id for row in active_subs}
    assignment_rows = db.execute(
        _active_assignment_query().where(
            OntAssignment.subscriber_id.in_(subscriber_ids)
        )
    ).all()
    assignments_by_subscriber: defaultdict[object, list] = defaultdict(list)
    assignments_by_subscriber_address: defaultdict[tuple[object, object], list] = (
        defaultdict(list)
    )
    ont_ids = set()
    for row in assignment_rows:
        assignments_by_subscriber[row.subscriber_id].append(row.ont_unit_id)
        if row.service_address_id is not None:
            assignments_by_subscriber_address[
                (row.subscriber_id, row.service_address_id)
            ].append(row.ont_unit_id)
        ont_ids.add(row.ont_unit_id)

    ont_to_olt: dict[object, object] = {}
    if ont_ids:
        ont_to_olt = {
            row.id: row.olt_device_id
            for row in db.execute(
                select(OntUnit.id, OntUnit.olt_device_id).where(OntUnit.id.in_(ont_ids))
            )
            if row.olt_device_id is not None
        }

    olt_ids = set(ont_to_olt.values())
    existing_olt_ids = set()
    if olt_ids:
        existing_olt_ids = set(
            db.execute(select(OLTDevice.id).where(OLTDevice.id.in_(olt_ids))).scalars()
        )
    # Live-session NAS per subscription (mirror of resolve_customer_path's
    # live arm). The NAS existence + node-state lookups below must cover BOTH
    # the static provisioning NAS ids and the live-session NAS ids.
    live_nas_by_sub = _live_nas_by_subscription(db, active_subs, subscriber_ids)
    nas_ids = {
        row.provisioning_nas_device_id
        for row in active_subs
        if row.provisioning_nas_device_id is not None
    } | set(live_nas_by_sub.values())
    existing_nas_ids = set()
    if nas_ids:
        existing_nas_ids = set(
            db.execute(select(NasDevice.id).where(NasDevice.id.in_(nas_ids))).scalars()
        )
    olt_node_state = _device_node_state(db, device_type="olt", device_ids=olt_ids)
    nas_node_state = _device_node_state(db, device_type="nas", device_ids=nas_ids)
    wireless_state = _wireless_subscriber_state(db, subscriber_ids)

    classified: list[dict] = []
    for row in active_subs:
        assignment_ont_ids = assignments_by_subscriber[row.subscriber_id]
        if row.service_address_id is not None:
            address_assignment_ids = assignments_by_subscriber_address[
                (row.subscriber_id, row.service_address_id)
            ]
            if address_assignment_ids:
                assignment_ont_ids = address_assignment_ids

        selected_ont_id = assignment_ont_ids[0] if assignment_ont_ids else None
        live_nas_id = live_nas_by_sub.get(row.id)
        live_nas_exists = live_nas_id is not None and live_nas_id in existing_nas_ids
        has_access_device = (
            selected_ont_id is not None
            or row.provisioning_nas_device_id in existing_nas_ids
            or live_nas_exists
        )
        has_node = False
        has_complete_path = False

        if selected_ont_id is not None:
            olt_id = ont_to_olt.get(selected_ont_id)
            if olt_id is None:
                has_access_device = True
            elif olt_id in existing_olt_ids:
                node_exists, complete_node = olt_node_state.get(olt_id, (False, False))
                has_node = node_exists
                has_complete_path = complete_node
            else:
                has_access_device = True

        # Wireless before NAS, mirroring resolve_customer_path: the radio ->
        # AP arm is finer than the coarse NAS fallback. The AP node IS the
        # topology node, so a resolvable radio grants access device + node.
        medium = MEDIUM_UNKNOWN
        if selected_ont_id is not None:
            medium = MEDIUM_FIBER
        elif row.subscriber_id in wireless_state:
            medium = MEDIUM_WIRELESS
            has_access_device = True
            has_node, has_complete_path = wireless_state[row.subscriber_id]
        else:
            # NAS arm, mirroring resolve_customer_path's live>static precedence:
            # the live-session NAS wins only when it resolves to a COMPLETE path
            # (node + basestation); otherwise fall back to the static
            # provisioning NAS; otherwise keep a live-only partial. This keeps
            # the batched classifier in sync with the canonical resolver so the
            # coverage/match-rate metric never disagrees.
            static_nas_id = row.provisioning_nas_device_id
            live_complete = (
                live_nas_exists and nas_node_state.get(live_nas_id, (False, False))[1]
            )
            if live_complete:
                medium = MEDIUM_NAS
                has_access_device = True
                has_node = True
                has_complete_path = True
            elif static_nas_id in existing_nas_ids:
                medium = MEDIUM_NAS
                has_access_device = True
                has_node, has_complete_path = nas_node_state.get(
                    static_nas_id, (False, False)
                )
            elif live_nas_exists:
                # Live NAS was the only access device (no static NAS) but did
                # not resolve completely: keep its partial node state.
                medium = MEDIUM_NAS
                has_access_device = True
                has_node, has_complete_path = nas_node_state.get(
                    live_nas_id, (False, False)
                )

        gap = None
        if not has_access_device:
            gap = GAP_NO_ONT
        elif not has_node:
            gap = GAP_NO_NODE
        elif not has_complete_path:
            gap = GAP_NO_BASESTATION
        classified.append({"id": row.id, "medium": medium, "gap": gap})

    return classified


def _subscription_gap_rows(db: Session) -> tuple[int, list[dict]]:
    """(active subscription count, [{id, gap}] for unresolved subs) — the
    shape the gaps page renders; derived from classify_active_subscriptions."""
    classified = classify_active_subscriptions(db)
    return len(classified), [
        {"id": row["id"], "gap": row["gap"]} for row in classified if row["gap"]
    ]


def _identity_keys(*values) -> set[str]:
    """Lowercased non-empty identity tokens used to match a device to a row."""
    return {
        text for text in (str(value or "").strip().lower() for value in values) if text
    }


def projection_drift_rows(db: Session, *, now: datetime | None = None) -> list[dict]:
    """Reconcile device inventory against the materialised device projection.

    Three drift classes, all derived by comparing the authoritative device
    tables to ``device_projections``:

    ``missing_projection``
        An inventory device with no projection row and no row that stands in
        for it. ``collect_devices`` deliberately folds a monitored device into
        the OLT/NAS/router row it duplicates, so a device is only "missing"
        when nothing in the projection shares any of its identity tokens.
    ``orphan_projection``
        A projected core row whose source inventory row is gone — the phantom
        the reconciler's prune is supposed to remove.
    ``stale_projection``
        Rows the reconciler has not refreshed within ``PROJECTION_STALE_AFTER``,
        i.e. the reconciler itself stopped converging.

    A report, not a decision: it never writes, and it names no owner's state.
    """
    current = now or datetime.now(UTC)
    devices = list(db.scalars(select(NetworkDevice)).all())
    rows = list(db.scalars(select(DeviceProjection)).all())

    core_source_ids = {r.source_id for r in rows if r.device_type == "core"}
    covered = set()
    for row in rows:
        covered |= _identity_keys(row.name, row.ip_address, row.serial_number)

    drift: list[dict] = []
    for device in devices:
        if str(device.id) in core_source_ids:
            continue
        if _identity_keys(device.name, device.mgmt_ip, device.hostname) & covered:
            continue  # folded into an OLT/NAS/router row by the derivation
        drift.append(
            {
                "id": str(device.id),
                "name": device.name,
                "drift": DRIFT_MISSING_PROJECTION,
            }
        )

    device_ids = {str(device.id) for device in devices}
    for row in rows:
        if row.device_type == "core" and row.source_id not in device_ids:
            drift.append(
                {
                    "id": row.source_id,
                    "name": row.name,
                    "drift": DRIFT_ORPHAN_PROJECTION,
                }
            )
            continue
        refreshed = row.refreshed_at
        if refreshed is None:
            continue
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=UTC)
        if current - refreshed > PROJECTION_STALE_AFTER:
            drift.append(
                {
                    "id": row.source_id,
                    "name": row.name,
                    "drift": DRIFT_STALE_PROJECTION,
                }
            )

    drift.sort(key=lambda row: (row["drift"], (row["name"] or "").lower()))
    return drift


def topology_gaps(
    db: Session,
    *,
    node_page: int = 1,
    node_per_page: int = DEFAULT_TABLE_PER_PAGE,
    gap_page: int = 1,
    gap_per_page: int = DEFAULT_TABLE_PER_PAGE,
) -> TopologyGaps:
    node_page = _clamp_page(node_page)
    gap_page = _clamp_page(gap_page)
    node_per_page = _clamp_per_page(node_per_page)
    gap_per_page = _clamp_per_page(gap_per_page)
    gaps = TopologyGaps(
        unmatched_node_page=node_page,
        unmatched_node_per_page=node_per_page,
        subscription_gap_page=gap_page,
        subscription_gap_per_page=gap_per_page,
    )

    # Authority-neutral: no provenance filter. This query used to require
    # ``source == 'zabbix_reconcile'`` — the stamp of an importer deleted on
    # 2026-07-10 — so the only drift report on the platform was structurally
    # blind to every device created since. Whether a device needs a
    # provisioning match is a property of the device, never of who filed it.
    unmatched_query = db.query(NetworkDevice).filter(
        NetworkDevice.matched_device_id.is_(None),
        NetworkDevice.is_active.is_(True),
    )
    gaps.unmatched_node_total_count = unmatched_query.count()
    node_offset = (node_page - 1) * node_per_page
    gaps.unmatched_nodes = (
        unmatched_query.order_by(NetworkDevice.name)
        .limit(node_per_page)
        .offset(node_offset)
        .all()
    )

    gaps.active_subscriptions, all_subscription_gaps = _subscription_gap_rows(db)
    gaps.subscription_gap_total_count = len(all_subscription_gaps)
    gaps.resolved_complete = max(
        gaps.active_subscriptions - gaps.subscription_gap_total_count, 0
    )
    gaps.subscription_gaps = _page_slice(
        all_subscription_gaps, page=gap_page, per_page=gap_per_page
    )

    all_drift = projection_drift_rows(db)
    gaps.projection_drift_total_count = len(all_drift)
    gaps.projection_drift = _page_slice(
        all_drift, page=node_page, per_page=node_per_page
    )

    return gaps
