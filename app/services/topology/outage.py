"""Outage incident management and customer-impact projection.

An outage is declared against a node, basestation, or FDH cabinet; the
affected subscriber count is snapshotted from affected_customers at declare
time. Declared either by an operator (manual console) or by the auto-detect
scan (retired; see ``outage_reconcile``), which marked incidents via ``declared_by ==
AUTO_DETECT_ACTOR`` + an ``AUTO_NOTE_PREFIX`` note — deliberately NOT a new
column (no migration needed; the model stays lean). No notification sending
here; every lifecycle transition stages its typed outage event (plus the
legacy ``network.alert`` webhook fan-out) atomically with the status write,
and the registered outage lifecycle projection handler applies cross-owner
consequences after commit (docs/designs/NETWORK_OUTAGE_RESPONSE_LIFECYCLE.md).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.network import FdhCabinet
from app.models.network_monitoring import (
    NetworkDevice,
    OutageIncident,
    OutageIncidentTicketLink,
    OutageScopeRevision,
    OutageScopeRevisionMember,
    PopSite,
)
from app.services.topology.affected import (
    _dist_to_core,
    downstream_nodes,
    forwarding_graph_projection,
)

logger = logging.getLogger(__name__)


# Status remains a free-form String column (not a DB enum — the enum migration
# collided in #876). This code-native vocabulary is the lifecycle source of
# truth for validation, filters, and read projections.
class OutageStatus(StrEnum):
    open = "open"
    suspected = "suspected"
    confirmed = "confirmed"
    clearing = "clearing"
    resolved = "resolved"
    discarded = "discarded"


OUTAGE_STATUS_VALUES = tuple(status.value for status in OutageStatus)
OPERATOR_OUTAGE_STATUSES = frozenset(
    {OutageStatus.open.value, OutageStatus.resolved.value}
)
# Actor stamped on classifier incidents — distinct from AUTO_DETECT_ACTOR so the
# two auto provenances never collide. The ``detection_source`` COLUMN (not
# ``declared_by``) is the authoritative operator/classifier discriminator.
CLASSIFIER_ACTOR = "system:outage-classifier"
CLASSIFIER_SOURCE = "classifier"
OPERATOR_SOURCE = "operator"
# Open classifier states: an incident still describing a live/settling outage.
# suspected (debouncing up), confirmed (declared), clearing (debouncing down).
CLASSIFIER_OPEN_STATUSES = (
    OutageStatus.suspected.value,
    OutageStatus.confirmed.value,
    OutageStatus.clearing.value,
)
CLASSIFIER_CUSTOMER_VISIBLE_STATUSES = (
    OutageStatus.confirmed.value,
    OutageStatus.clearing.value,
)
CLASSIFIER_TERMINAL_STATUSES = (
    OutageStatus.resolved.value,
    OutageStatus.discarded.value,
)
_CLASSIFIER_STATUSES = frozenset(CLASSIFIER_OPEN_STATUSES) | frozenset(
    CLASSIFIER_TERMINAL_STATUSES
)
# Statuses that keep an incident "live" for the open-incidents surfaces: operator
# open + every non-terminal classifier state (resolved/discarded excluded).
_LIVE_STATUSES = (OutageStatus.open.value,) + CLASSIFIER_OPEN_STATUSES
# ``declared_by`` sentinel marking auto-detected incidents (see module doc).
AUTO_DETECT_ACTOR = "system:outage-autodetect"
AUTO_NOTE_PREFIX = "AUTO-DETECTED:"
# An open incident older than this is surfaced for operator review — a lingering
# open incident keeps showing customers a false "known outage" banner. Manual
# only: it is flagged, never auto-resolved (auto-resolve would mis-fire on a
# flapping link).
STALE_OPEN_HOURS = 36


def set_outage_status(incident: OutageIncident, status: str) -> bool:
    """Guarded OPERATOR status writer (open/resolved). Returns True if it
    changed. Idempotent; stamps resolved_at on the open->resolved transition.

    Refuses classifier incidents: they carry a debounced lifecycle
    (suspected/confirmed/clearing/resolved) driven ONLY by the reconcile loop.
    Jumping one straight to ``resolved`` here would drop confirmed_at (breaking
    MTTR = resolved_at - confirmed_at) and let the next reconcile re-open a
    duplicate suspected incident for the still-dark node."""
    if status not in OPERATOR_OUTAGE_STATUSES:
        raise ValueError(f"invalid outage status: {status!r}")
    # detection_source is None only for an un-flushed operator row; classifier
    # incidents always carry CLASSIFIER_SOURCE from open_classifier_incident.
    if incident.detection_source not in (None, OPERATOR_SOURCE):
        raise ValueError(
            "set_outage_status is operator-only; classifier incidents transition "
            "via the reconcile lifecycle"
        )
    if incident.status == status:
        return False
    incident.status = status
    incident.resolved_at = (
        datetime.now(UTC) if status == OutageStatus.resolved.value else None
    )
    return True


def is_stale_open(incident: OutageIncident, *, now: datetime | None = None) -> bool:
    """True for an `open` incident that has lingered past STALE_OPEN_HOURS."""
    if incident.status != OutageStatus.open.value or incident.started_at is None:
        return False
    started = incident.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return (now - started) >= timedelta(hours=STALE_OPEN_HOURS)


def detection_source(incident: OutageIncident) -> str:
    """``auto`` for scanner-declared incidents, else ``manual``."""
    return "auto" if incident.declared_by == AUTO_DETECT_ACTOR else "manual"


def _match_timestamp_style(value: datetime, reference: datetime | None) -> datetime:
    if reference is not None and reference.tzinfo is None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _outage_event_payload(
    session: Session, incident: OutageIncident, kind: str
) -> dict:
    scope: dict = {"type": None, "id": None, "name": None}
    if incident.root_node_id is not None:
        node = session.get(NetworkDevice, incident.root_node_id)
        scope = {
            "type": "node",
            "id": str(incident.root_node_id),
            "name": getattr(node, "name", None),
        }
    elif incident.basestation_id is not None:
        pop = session.get(PopSite, incident.basestation_id)
        scope = {
            "type": "basestation",
            "id": str(incident.basestation_id),
            "name": getattr(pop, "name", None),
        }
    elif incident.fdh_cabinet_id is not None:
        fdh = session.get(FdhCabinet, incident.fdh_cabinet_id)
        scope = {
            "type": "fdh",
            "id": str(incident.fdh_cabinet_id),
            "name": getattr(fdh, "name", None),
        }
    return {
        "alert_type": kind,  # e.g. "outage.created" | "outage.resolved"
        "incident_id": str(incident.id),
        "status": incident.status,
        "detection_source": detection_source(incident),
        "provenance": incident.detection_source,
        "scope": scope,
        "severity": incident.severity,
        "affected_count": incident.affected_count,
        "started_at": incident.started_at.isoformat() if incident.started_at else None,
        "resolved_at": incident.resolved_at.isoformat()
        if incident.resolved_at
        else None,
    }


def _emit_outage_event(session: Session, incident: OutageIncident, kind: str) -> None:
    """Stage an incident lifecycle change as durable owner outputs.

    Two outputs commit atomically with the transition and dispatch after
    commit:

    - the typed lifecycle event (``outage.created``/``outage.confirmed``/…)
      that the registered outage lifecycle projection handler consumes to
      apply cross-owner consequences (operational owners/watchers, SLA
      escalation planning and cancellation) with durable retry;
    - the legacy ``network.alert`` fan-out with its unchanged payload, kept
      for external webhook subscribers (WebhookHandler → ``IntegrationDelivery``
      rows → HMAC delivery worker; CRM/mobile filter on ``alert_type``).

    No PII in the payload beyond counts; detail comes from the CRM outage
    API. A staging failure raises: the transition and its outputs are one
    atomic fact, never a warning log.
    """
    from app.services.events import emit_event
    from app.services.events.types import EventType

    payload = _outage_event_payload(session, incident, kind)
    actor = incident.declared_by or "system"
    emit_event(session, EventType(kind), payload, actor=actor)
    emit_event(session, EventType.network_alert, payload, actor=actor)


def _incident_scope_target(session: Session, incident: OutageIncident):
    """(scope_type, scope_id, affected_customers kwargs) for the current root.

    FDH wins over node over basestation, mirroring how a declared incident
    carries exactly one scope. An incident whose root was re-pointed to None
    is honestly ``unresolved`` with an empty audience.
    """

    if incident.fdh_cabinet_id is not None:
        fdh = session.get(FdhCabinet, incident.fdh_cabinet_id)
        return "fdh-cabinet", incident.fdh_cabinet_id, {"fdh": fdh}
    if incident.root_node_id is not None:
        node = session.get(NetworkDevice, incident.root_node_id)
        return "node", incident.root_node_id, {"node": node}
    if incident.basestation_id is not None:
        basestation = session.get(PopSite, incident.basestation_id)
        return "basestation", incident.basestation_id, {"basestation": basestation}
    return "unresolved", None, {}


def latest_scope_revision(session: Session, incident_id) -> OutageScopeRevision | None:
    return (
        session.query(OutageScopeRevision)
        .filter(OutageScopeRevision.incident_id == incident_id)
        .order_by(OutageScopeRevision.sequence.desc())
        .first()
    )


def list_scope_revisions(session: Session, incident_id) -> list[OutageScopeRevision]:
    """Full append-only scope history, oldest first."""

    return (
        session.query(OutageScopeRevision)
        .filter(OutageScopeRevision.incident_id == incident_id)
        .order_by(OutageScopeRevision.sequence.asc())
        .all()
    )


def revision_audience_subscription_ids(revision: OutageScopeRevision) -> set[str]:
    """The exact audience at one revision: entered + retained members."""

    return {
        str(member.subscription_id)
        for member in revision.members
        if member.membership in ("entered", "retained")
    }


def record_scope_revision(
    session: Session,
    incident: OutageIncident,
    *,
    reason: str,
    actor: str | None = None,
    effective_at: datetime | None = None,
) -> OutageScopeRevision | None:
    """Append the incident's current scope + exact audience as a revision.

    Idempotent by content: when neither the scope nor the exact audience
    membership changed since the latest revision, nothing is appended —
    reconcile reruns and repeated transitions cannot fork or pad history.
    History is append-only; rows are never updated or deleted
    (OUTAGE_SLA_SPINE §3).
    """

    from app.services.bulk_actions import membership_scope_token
    from app.services.topology.affected import affected_customers

    scope_type, scope_id, target_kwargs = _incident_scope_target(session, incident)
    subscription_ids: set[str] = set()
    target = next(iter(target_kwargs.values()), None)
    if target is not None:
        impact = affected_customers(session, **target_kwargs)
        subscription_ids = {
            str(subscription.id) for subscription in impact["subscriptions"]
        }
    token = membership_scope_token(f"{scope_type}:{scope_id}", sorted(subscription_ids))

    previous = latest_scope_revision(session, incident.id)
    if (
        previous is not None
        and previous.new_scope_type == scope_type
        and previous.new_scope_id == scope_id
        and previous.membership_token == token
    ):
        return None

    previous_audience: set[str] = (
        revision_audience_subscription_ids(previous) if previous is not None else set()
    )
    entered = subscription_ids - previous_audience
    left = previous_audience - subscription_ids
    retained = subscription_ids & previous_audience

    revision = OutageScopeRevision(
        incident_id=incident.id,
        sequence=(previous.sequence + 1) if previous is not None else 1,
        effective_at=effective_at or datetime.now(UTC),
        reason=reason,
        actor=actor or incident.declared_by,
        old_scope_type=previous.new_scope_type if previous is not None else None,
        old_scope_id=previous.new_scope_id if previous is not None else None,
        new_scope_type=scope_type,
        new_scope_id=scope_id,
        membership_token=token,
        member_count=len(subscription_ids),
        entered_count=len(entered),
        left_count=len(left),
    )
    session.add(revision)
    session.flush()
    for subscription_id, membership in (
        *((sid, "entered") for sid in sorted(entered)),
        *((sid, "retained") for sid in sorted(retained)),
        *((sid, "left") for sid in sorted(left)),
    ):
        session.add(
            OutageScopeRevisionMember(
                revision_id=revision.id,
                subscription_id=subscription_id,
                membership=membership,
            )
        )
    session.flush()
    return revision


def declare_outage(
    session: Session,
    *,
    node: NetworkDevice | None = None,
    basestation: PopSite | None = None,
    fdh: FdhCabinet | None = None,
    declared_by: str | None = None,
    note: str | None = None,
    severity: str | None = None,
    impact: dict | None = None,
) -> OutageIncident:
    """Open an incident against infrastructure, snapshotting affected_count.

    ``impact`` is an optional precomputed ``affected_customers`` result for
    the SAME scope — the auto-detect scan already resolves it for its
    threshold gate, so passing it in avoids a second full graph walk per
    incident. Omitted (manual console path), it is computed here.
    """
    if node is None and basestation is None and fdh is None:
        raise ValueError("declare_outage requires a node, basestation, or FDH cabinet")
    if impact is None:
        from app.services.network.outage_impact import (
            resolve_basestation_impact,
            resolve_fdh_impact,
            resolve_node_impact,
        )

        if node is not None:
            impact = resolve_node_impact(session, node).payload
        elif basestation is not None:
            impact = resolve_basestation_impact(session, basestation).payload
        else:
            assert fdh is not None
            impact = resolve_fdh_impact(session, fdh).payload
    incident = OutageIncident(
        root_node_id=node.id if node is not None else None,
        basestation_id=basestation.id if basestation is not None else None,
        fdh_cabinet_id=fdh.id if fdh is not None else None,
        declared_by=declared_by,
        note=note,
        severity=severity,
        affected_count=impact["count"],
        status=OutageStatus.open.value,
    )
    session.add(incident)
    session.flush()
    # The initial immutable scope revision snapshots the exact audience the
    # incident opened against (OUTAGE_SLA_SPINE §3).
    record_scope_revision(
        session, incident, reason="declared", effective_at=incident.started_at
    )
    # Operational owners/watchers and escalation planning are applied by the
    # outage lifecycle projection handler consuming this committed output.
    _emit_outage_event(session, incident, "outage.created")
    return incident


def resolve_outage(session: Session, incident_id) -> OutageIncident | None:
    incident = session.get(OutageIncident, incident_id)
    if incident is None:
        return None
    # Classifier incidents are resolved ONLY by the reconcile lifecycle — the
    # operator Resolve button is a no-op on them (never operator-terminated).
    if incident.detection_source not in (None, OPERATOR_SOURCE):
        return incident
    if set_outage_status(incident, OutageStatus.resolved.value):
        session.flush()
        # Escalation cancellation is applied by the outage lifecycle
        # projection handler consuming this committed output.
        _emit_outage_event(session, incident, "outage.resolved")
    return incident


# --- classifier-driven lifecycle (§7.6) -----------------------------------
#
# State machine (classifier incidents only; operator open/resolved is untouched):
#
#     suspected --W_confirm--> confirmed --recovery--> clearing --W_resolve--> resolved
#         |                                               ^  |
#     recovery (discard)                        re-darken |  | (stays until window)
#         v                                               |  v
#      discarded                                     confirmed (reopen)
#
# W_confirm is scaled by affected_count; W_resolve is fixed. Firing (customer /
# ticket notification) stays GATED — these helpers only persist the lifecycle
# and fan lifecycle events into the existing webhook machinery.


def open_classifier_incident(
    session: Session,
    *,
    root_node: NetworkDevice | None = None,
    basestation_id=None,
    affected_count: int = 0,
    confidence: float | None = None,
    classification: str | None = None,
    now: datetime,
) -> OutageIncident:
    """Open a fresh ``suspected`` classifier incident and emit its event."""
    incident = OutageIncident(
        root_node_id=root_node.id if root_node is not None else None,
        basestation_id=basestation_id,
        declared_by=CLASSIFIER_ACTOR,
        detection_source=CLASSIFIER_SOURCE,
        status=OutageStatus.suspected.value,
        affected_count=affected_count,
        confidence=confidence,
        classification=classification,
        started_at=now,
        suspected_at=now,
    )
    session.add(incident)
    session.flush()
    record_scope_revision(session, incident, reason="suspected", effective_at=now)
    _emit_outage_event(session, incident, "outage.suspected")
    return incident


def find_open_classifier_incident(
    session: Session,
    *,
    basestation_id=None,
    root_node_id=None,
    component_node_ids=None,
    exclude_ids=None,
) -> OutageIncident | None:
    """The OPEN classifier incident covering this outage (§7.6 decision 3).

    Identity is matched by ``basestation_id`` when present, else by an exact
    ``root_node_id``, else by the incident's root falling inside the current
    connected-dark ``component_node_ids`` (localization drift). Oldest match
    wins; ``exclude_ids`` skips incidents already claimed this pass.
    """
    exclude = set(exclude_ids or ())

    def _q():
        return (
            session.query(OutageIncident)
            .filter(
                OutageIncident.detection_source == CLASSIFIER_SOURCE,
                OutageIncident.status.in_(CLASSIFIER_OPEN_STATUSES),
            )
            .order_by(OutageIncident.started_at.asc())
        )

    def _pick(rows):
        for inc in rows:
            if inc.id not in exclude:
                return inc
        return None

    if basestation_id is not None:
        inc = _pick(_q().filter(OutageIncident.basestation_id == basestation_id).all())
        if inc is not None:
            return inc
    if root_node_id is not None:
        inc = _pick(_q().filter(OutageIncident.root_node_id == root_node_id).all())
        if inc is not None:
            return inc
    ids = [nid for nid in (component_node_ids or ()) if nid is not None]
    if ids:
        inc = _pick(_q().filter(OutageIncident.root_node_id.in_(ids)).all())
        if inc is not None:
            return inc
    return None


def update_classifier_snapshot(
    incident: OutageIncident,
    *,
    affected_count: int | None = None,
    confidence: float | None = None,
    classification: str | None = None,
) -> None:
    """Refresh the per-pass verdict fields on an open classifier incident."""
    if affected_count is not None:
        incident.affected_count = affected_count
    if confidence is not None:
        incident.confidence = confidence
    if classification is not None:
        incident.classification = classification


def repoint_root(
    session: Session, incident: OutageIncident, root_node: NetworkDevice | None
) -> bool:
    """Re-point an open classifier incident to the current deepest-dark node on
    localization drift (§7.6 decision 3) — no new incident. Emits a rerooted
    event and returns True when the root actually moved."""
    new_id = root_node.id if root_node is not None else None
    if incident.root_node_id == new_id:
        return False
    incident.root_node_id = new_id
    session.flush()
    record_scope_revision(session, incident, reason="rerooted")
    _emit_outage_event(session, incident, "outage.rerooted")
    return True


def confirm_incident(
    session: Session, incident: OutageIncident, *, now: datetime
) -> None:
    """suspected -> confirmed (debounce satisfied). Stamps confirmed_at."""
    incident.status = OutageStatus.confirmed.value
    incident.confirmed_at = now
    session.flush()
    _emit_outage_event(session, incident, "outage.confirmed")


def discard_incident(session: Session, incident: OutageIncident) -> None:
    """suspected -> discarded (recovered before W_confirm — a false positive).
    No confirmed event ever fires for a discarded incident."""
    incident.status = OutageStatus.discarded.value
    session.flush()
    _emit_outage_event(session, incident, "outage.discarded")


def start_clearing(
    session: Session, incident: OutageIncident, *, now: datetime
) -> None:
    """confirmed -> clearing (recovery observed). Stamps cleared_at; the
    W_resolve window must elapse sustained before this becomes resolved."""
    incident.status = OutageStatus.clearing.value
    incident.cleared_at = now
    session.flush()
    _emit_outage_event(session, incident, "outage.clearing")


def reopen_incident(session: Session, incident: OutageIncident) -> None:
    """clearing -> confirmed (re-darkened inside W_resolve — hysteresis). Clears
    cleared_at so the resolve window restarts on the next recovery."""
    incident.status = OutageStatus.confirmed.value
    incident.cleared_at = None
    session.flush()
    _emit_outage_event(session, incident, "outage.reopened")


def resolve_classifier_incident(
    session: Session, incident: OutageIncident, *, now: datetime
) -> None:
    """clearing -> resolved (recovery sustained past W_resolve). Stamps
    resolved_at; MTTR = resolved_at - confirmed_at."""
    incident.status = OutageStatus.resolved.value
    incident.resolved_at = _match_timestamp_style(now, incident.confirmed_at)
    session.flush()
    _emit_outage_event(session, incident, "outage.resolved")


def open_incident_for_path(
    session: Session,
    path,
    *,
    dist: dict | None = None,
    adjacency: dict | None = None,
    include_suspected_classifier: bool = False,
) -> OutageIncident | None:
    """The live incident covering a customer's path, if any.

    Customer-facing callers see operator ``open`` incidents plus debounced-real
    classifier incidents (``confirmed``/``clearing``). The auto-detect scanner can
    opt into suspected classifier incidents so it does not duplicate a candidate
    already owned by the debounce lifecycle.
    """
    if path is None:
        return None
    customer_node_ids = set()
    node = getattr(path, "node", None)
    customer_access_id = node.id if node is not None else None
    if customer_access_id is not None:
        customer_node_ids.add(customer_access_id)
    for hop in getattr(path, "upstream_chain", None) or []:
        customer_node_ids.add(hop.id)
    basestation = getattr(path, "basestation", None)
    basestation_id = basestation.id if basestation is not None else None

    classifier_statuses = (
        CLASSIFIER_OPEN_STATUSES
        if include_suspected_classifier
        else CLASSIFIER_CUSTOMER_VISIBLE_STATUSES
    )
    incidents = (
        session.query(OutageIncident)
        .filter(
            or_(
                and_(
                    OutageIncident.detection_source == OPERATOR_SOURCE,
                    OutageIncident.status == OutageStatus.open.value,
                ),
                and_(
                    OutageIncident.detection_source == CLASSIFIER_SOURCE,
                    OutageIncident.status.in_(classifier_statuses),
                ),
            )
        )
        .order_by(OutageIncident.started_at.desc())
        .all()
    )
    # Pass 1 (cheap): basestation match, or the incident root is on the
    # customer's own (hop-capped) path.
    for incident in incidents:
        if basestation_id is not None and incident.basestation_id == basestation_id:
            return incident
        if (
            incident.root_node_id is not None
            and incident.root_node_id in customer_node_ids
        ):
            return incident
    # Pass 2 (blast radius): the customer is downstream of an incident root that
    # lies beyond their hop-capped upstream chain. This keeps the read-side
    # membership in sync with the declare-side affected_count (both computed via
    # downstream_nodes), so a counted customer always sees the banner. Only
    # reached during an active outage that didn't already match cheaply.
    root_incidents = [i for i in incidents if i.root_node_id is not None]
    if customer_access_id is not None and root_incidents:
        # dist/adjacency are root-independent; compute the full-graph maps ONCE
        # and reuse them across incidents rather than recomputing inside each
        # downstream_nodes call (this runs on the customer connection-status
        # request path, possibly with many open incidents during a wide outage
        # — and once per candidate inside the auto-detect scan, which passes
        # its own precomputed maps in).
        if adjacency is None:
            graph = forwarding_graph_projection(session)
            adjacency = graph.adjacency
            if dist is None:
                dist = _dist_to_core(
                    session,
                    adjacency=adjacency,
                    root_ids=graph.root_device_ids,
                )
        if dist is None:
            dist = _dist_to_core(session, adjacency=adjacency)
        for incident in root_incidents:
            root = session.get(NetworkDevice, incident.root_node_id)
            if root is not None and customer_access_id in downstream_nodes(
                session, root, dist=dist, adjacency=adjacency
            ):
                return incident
    return None


def list_open_incidents(session: Session) -> list[OutageIncident]:
    """Live incidents across BOTH provenances: operator ``open`` plus the
    non-terminal classifier states (suspected/confirmed/clearing). Terminal
    states (resolved/discarded) are excluded. Consumers that must not offer an
    operator Resolve button on classifier rows use ``list_operator_open_incidents``.
    """
    return (
        session.query(OutageIncident)
        .filter(OutageIncident.status.in_(_LIVE_STATUSES))
        .order_by(OutageIncident.started_at.desc())
        .all()
    )


def list_classifier_incidents(
    session: Session, *, states: tuple[str, ...] = CLASSIFIER_OPEN_STATUSES
) -> list[OutageIncident]:
    """Persisted classifier incidents for the P4a console — the debounced truth
    (§7.6). Defaults to the OPEN lifecycle states (suspected/confirmed/clearing),
    newest first; terminal states (resolved/discarded) are excluded so the
    console shows only what is live. Live-compute is a separate secondary view."""
    return (
        session.query(OutageIncident)
        .filter(
            OutageIncident.detection_source == CLASSIFIER_SOURCE,
            OutageIncident.status.in_(states),
        )
        .order_by(OutageIncident.started_at.desc())
        .all()
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def mttr_seconds(incident: OutageIncident) -> int | None:
    """MTTR for a resolved incident: ``resolved_at - confirmed_at`` in seconds.
    None unless BOTH stamps are set (operator rows have no confirmed_at)."""
    confirmed = _aware(incident.confirmed_at)
    resolved = _aware(incident.resolved_at)
    if confirmed is None or resolved is None:
        return None
    return max(int((resolved - confirmed).total_seconds()), 0)


def mttr_so_far_seconds(
    incident: OutageIncident, *, now: datetime | None = None
) -> int | None:
    """Elapsed repair time on a still-live confirmed/clearing incident:
    ``now - confirmed_at``. None until the incident is confirmed."""
    confirmed = _aware(incident.confirmed_at)
    if confirmed is None:
        return None
    now = now or datetime.now(UTC)
    return max(int((now - confirmed).total_seconds()), 0)


def list_operator_open_incidents(session: Session) -> list[OutageIncident]:
    """Open OPERATOR incidents only — the manual console's listing. Classifier
    incidents are omitted: they are lifecycle-managed by the reconcile loop and
    must never be operator-resolved (see set_outage_status/resolve_outage)."""
    return (
        session.query(OutageIncident)
        .filter(
            OutageIncident.detection_source == OPERATOR_SOURCE,
            OutageIncident.status == OutageStatus.open.value,
        )
        .order_by(OutageIncident.started_at.desc())
        .all()
    )


def list_stale_open_incidents(
    session: Session, *, older_than_hours: int = STALE_OPEN_HOURS
) -> list[OutageIncident]:
    """Open incidents that have lingered past the threshold — likely forgotten,
    still showing customers a false outage banner. For operator review only."""
    cutoff = datetime.now(UTC) - timedelta(hours=older_than_hours)
    return (
        session.query(OutageIncident)
        .filter(
            OutageIncident.status == OutageStatus.open.value,
            OutageIncident.started_at < cutoff,
        )
        .order_by(OutageIncident.started_at.asc())
        .all()
    )


# --- receipted lifecycle-output consumption --------------------------------
#
# The registered OutageLifecycleProjectionHandler delivers this owner's
# committed outputs back to these commands. Each runs on a transaction-free
# owner-command session and wraps its cross-owner effect (operational
# escalation participants) in a unique ``(consumer, event_id)`` receipt via
# ``events.owner_outputs`` — the effect and receipt commit atomically, a
# redelivery is an exact no-op, and a raised failure leaves the delivery
# durably failed and retryable. The incident transitions above never apply
# these consequences inline.


# --- incident ticket links (OUTAGE_SLA_SPINE ticket slice) -----------------
# One canonical infrastructure ticket per incident, deduplicated complaint
# links, reconciliation state only: ticket transitions stay with the Support
# owner and network recovery never closes a Ticket or WorkOrder from here.

INFRASTRUCTURE_ROLE = "infrastructure"
COMPLAINT_ROLE = "complaint"
_RECONCILIATION_STATES = ("native", "pending", "synced", "drift")


def _current_revision_sequence(
    session: Session, incident: OutageIncident
) -> int | None:
    revision = latest_scope_revision(session, incident.id)
    return revision.sequence if revision is not None else None


def links_for_incident(session: Session, incident_id) -> list[OutageIncidentTicketLink]:
    return (
        session.query(OutageIncidentTicketLink)
        .filter(OutageIncidentTicketLink.incident_id == incident_id)
        .order_by(OutageIncidentTicketLink.linked_at)
        .all()
    )


def infrastructure_link_for(
    session: Session, incident_id
) -> OutageIncidentTicketLink | None:
    return (
        session.query(OutageIncidentTicketLink)
        .filter(
            OutageIncidentTicketLink.incident_id == incident_id,
            OutageIncidentTicketLink.role == INFRASTRUCTURE_ROLE,
        )
        .first()
    )


def link_infrastructure_ticket(
    session: Session,
    incident: OutageIncident,
    ticket_id,
    *,
    linked_by: str | None,
    source: str = "operator",
    external_ref: str | None = None,
) -> OutageIncidentTicketLink:
    """Bind the ONE canonical infrastructure ticket.

    Idempotent for the same ticket; a different ticket while one is already
    bound raises — replacing the canonical ticket is an explicit reviewed
    operation, never a silent overwrite.
    """

    existing = infrastructure_link_for(session, incident.id)
    if existing is not None:
        if existing.ticket_id == ticket_id:
            return existing
        raise ValueError(
            f"incident {incident.id} already has canonical infrastructure "
            f"ticket {existing.ticket_id}; unlink it explicitly before "
            "binding another"
        )
    link = OutageIncidentTicketLink(
        incident_id=incident.id,
        ticket_id=ticket_id,
        role=INFRASTRUCTURE_ROLE,
        linked_at=datetime.now(UTC),
        linked_by=linked_by,
        source=source,
        external_ref=external_ref,
        scope_revision_sequence=_current_revision_sequence(session, incident),
    )
    session.add(link)
    session.flush()
    return link


def link_complaint_ticket(
    session: Session,
    incident: OutageIncident,
    ticket_id,
    *,
    linked_by: str | None,
    source: str = "operator",
) -> OutageIncidentTicketLink:
    """Attach one customer complaint ticket; the pair is deduplicated."""

    existing = (
        session.query(OutageIncidentTicketLink)
        .filter(
            OutageIncidentTicketLink.incident_id == incident.id,
            OutageIncidentTicketLink.ticket_id == ticket_id,
        )
        .first()
    )
    if existing is not None:
        return existing
    link = OutageIncidentTicketLink(
        incident_id=incident.id,
        ticket_id=ticket_id,
        role=COMPLAINT_ROLE,
        linked_at=datetime.now(UTC),
        linked_by=linked_by,
        source=source,
        scope_revision_sequence=_current_revision_sequence(session, incident),
    )
    session.add(link)
    session.flush()
    return link


def mark_reconciliation(
    session: Session,
    link: OutageIncidentTicketLink,
    *,
    state: str,
    external_ref: str | None = None,
) -> None:
    """Record the external CRM reconciliation state for one link."""

    if state not in _RECONCILIATION_STATES:
        raise ValueError(
            f"unknown reconciliation state {state!r}; expected one of "
            f"{_RECONCILIATION_STATES}"
        )
    link.reconciliation_state = state
    if external_ref is not None:
        link.external_ref = external_ref
    session.flush()


def _consume_definition(name: str):
    from app.services.owner_commands import OwnerCommandDefinition

    return OwnerCommandDefinition(
        owner="network.outage_lifecycle",
        concern="committed outage output consumption",
        name=name,
    )


def consume_outage_activation(
    session: Session,
    *,
    incident_id,
    event_id,
    event_type: str,
    context,
) -> str | None:
    """Receipt one declared/confirmed output into operational consequences."""
    from app.services.events.owner_outputs import consume_owner_output
    from app.services.owner_commands import execute_owner_command

    def _effect() -> str:
        from app.services.topology.outage_operations import (
            ensure_outage_customer_watchers,
            ensure_outage_operations,
            plan_outage_escalations,
        )

        incident = session.get(OutageIncident, incident_id)
        if incident is None:
            logger.warning(
                "outage activation consequence: incident %s missing", incident_id
            )
            return "skipped_missing"
        # A stale replay after the incident already terminated must not plan
        # fresh escalations that nothing would cancel.
        if incident.status in CLASSIFIER_TERMINAL_STATUSES:
            return "skipped_terminal"
        ensure_outage_operations(session, incident)
        ensure_outage_customer_watchers(session, incident)
        plan_outage_escalations(session, incident, trigger=event_type)
        return "applied"

    return execute_owner_command(
        session,
        definition=_consume_definition("consume_outage_activation"),
        context=context,
        operation=lambda: consume_owner_output(
            session,
            consumer="network.outage_lifecycle",
            event_id=event_id,
            event_type=event_type,
            producer_owner="network.outage_lifecycle",
            context=context,
            operation=_effect,
        )[0],
    )


def consume_outage_termination(
    session: Session,
    *,
    incident_id,
    event_id,
    event_type: str,
    resolved_at: datetime | None,
    context,
) -> str | None:
    """Receipt one resolved/discarded output into escalation cancellation."""
    from app.models.operational_escalation import OperationalEntityType
    from app.services import operational_escalation
    from app.services.events.owner_outputs import consume_owner_output
    from app.services.owner_commands import execute_owner_command

    def _effect() -> str:
        incident = session.get(OutageIncident, incident_id)
        if incident is None:
            logger.warning(
                "outage termination consequence: incident %s missing", incident_id
            )
            return "skipped_missing"
        operational_escalation.cancel_entity_events(
            session,
            entity_type=OperationalEntityType.outage,
            entity_id=incident.id,
            reason=event_type,
            canceled_at=resolved_at,
        )
        return "applied"

    return execute_owner_command(
        session,
        definition=_consume_definition("consume_outage_termination"),
        context=context,
        operation=lambda: consume_owner_output(
            session,
            consumer="network.outage_lifecycle",
            event_id=event_id,
            event_type=event_type,
            producer_owner="network.outage_lifecycle",
            context=context,
            operation=_effect,
        )[0],
    )
