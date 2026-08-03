"""Customer-facing outage communications.

Owner: ``network.outage_communications`` (OUTAGE_SLA_SPINE §3 — "communications
fire on a material customer impact-state change"). It decides **whether a
customer is owed a message, which message, and when**. It owns no other
concern: the audience and impact word come from ``network.service_impact``,
the incident facts from ``network.outage_lifecycle``, the exact downtime from
``network.customer_outage_accrual``, and every send goes out through
``communication_intents`` — which keeps channel selection, opt-out and
delivery.

Why this exists next to ``outage_notifications``
------------------------------------------------

The legacy path (ADR 0004) tells customers an outage started and then never
speaks again. In 180 days of CRM traffic, 1,877 conversations were pure "any
update?" chasing — the cost of an opening message with no closing one. It also
predates the spine: it groups by ``connection_status.assess`` rather than the
resolver, so its audience can disagree with the ledger that will later justify
a compensation claim, and it messages **per subscription**, so a customer with
two services behind one splitter gets two emails about one fault.

This owner supersedes it. Arming ``outage_customer_comms_enabled`` disarms the
legacy automatic and manual send paths (see ``superseded_by``): two customer
outage senders must never be live at once.

The four rules that shape the code
----------------------------------

1. **Exposure is not a message.** Only ``confirmed_unavailable`` opens a
   conversation. ``potentially_affected``, ``unknown`` and a ``suspected``
   incident say nothing — a false "your area is down" is worse than silence.
2. **The recovery cohort comes from lineage, never from the current
   audience.** Restoration goes to exactly the customers who received an
   opening or update message for this incident: mid-incident joiners were
   never promised anything, and a customer who left the audience still deserves
   the all-clear. ``OutageCustomerNotice`` rows with a real
   ``communication_intent_id`` are that record.
3. **One customer, one message.** Grouping happens before policy evaluation,
   so multi-service customers are told once and their affected services are
   named in the body.
4. **Episodes, not incidents.** clearing → reopened is one continuous fault to
   the ledger but two conversations to a customer who was already told it was
   fixed. Sequence numbers make a second opening message possible without ever
   duplicating the first.

Transaction: planning is read-only. ``send_incident_notices`` stages notices,
intents and its breadcrumb event in the caller's transaction — the receipted
consumer and the operator command own the commit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.domain_settings import SettingDomain
from app.models.network_monitoring import (
    CustomerOutageInterval,
    OutageCustomerNotice,
    OutageIncident,
)
from app.models.notification import NotificationChannel
from app.services import communication_intents, settings_spec
from app.services.customer_notification_policy import (
    CustomerNotificationPolicyCandidate,
    CustomerNotificationPolicyCohortQuery,
    evaluate_bulk_customer_notification_policy,
)
from app.services.network.service_impact import resolve_incident_impacts
from app.services.service_impact_contracts import (
    SLA_CALENDAR_TIMEZONE,
    ImpactState,
)
from app.services.topology.outage import OutageStatus

logger = logging.getLogger(__name__)

EVENT_TYPE = "outage_customer_notice"
CATEGORY = "service"

#: Incident states that end the conversation. ``discarded`` closes it too: a
#: customer told about an outage that turned out to be a false positive is
#: still owed the all-clear, and silence reads as an unfixed fault.
_TERMINAL_STATUSES = frozenset(
    {OutageStatus.resolved.value, OutageStatus.discarded.value}
)


class NoticeStage(StrEnum):
    """The three customer-facing moments of an incident."""

    opened = "opened"
    update = "update"
    restored = "restored"


class NoticeStatus(StrEnum):
    """What became of one decided message."""

    queued = "queued"
    suppressed = "suppressed"
    skipped_no_recipient = "skipped_no_recipient"
    skipped_cap = "skipped_cap"
    planned_dry_run = "planned_dry_run"


#: Only a queued message with delivery lineage counts as "we told them" — the
#: recovery cohort is built from these and nothing else.
_DELIVERED_STATUSES = frozenset({NoticeStatus.queued.value})

#: Dedupe-key namespaces. Only a real send claims the canonical key; a dry-run
#: plan and a blocked recipient are recorded under their own namespaces so
#: neither can mute a later genuine message.
_KEY_SENT = "outage-notice"
_KEY_DRY = "outage-notice-dry"
_KEY_BLOCKED = "outage-notice-blocked"


class OutageCommunicationsError(ValueError):
    """Invalid send input (adapter: HTTP 400)."""


class OutageCommunicationsDriftError(RuntimeError):
    """Audience or content changed between preview and confirm (HTTP 409)."""


@dataclass(frozen=True, slots=True)
class NoticeCandidate:
    """One customer's decided message for one incident stage."""

    subscriber_id: UUID
    subscription_ids: tuple[str, ...]
    name: str
    email: str | None
    stage: NoticeStage
    sequence: int
    impact_state: str
    scope_revision_sequence: int | None
    subject: str
    body: str
    dedupe_key: str
    eligible: bool
    reason_code: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class IncidentNoticePlan:
    """What a confirmed send would do for one incident.

    ``eligible`` counts candidates that would actually be queued; the
    per-stage counts make "who are we opening with vs closing out" legible
    before anybody clicks send.
    """

    incident_id: str
    incident_status: str
    dry_run: bool
    gated_reason: str | None
    opened: int
    updates: int
    restored: int
    eligible: int
    suppressed: int
    missing_email: int
    impact_token: str
    candidates: tuple[NoticeCandidate, ...]

    @property
    def total(self) -> int:
        return len(self.candidates)


@dataclass(frozen=True, slots=True)
class IncidentNoticeResult:
    incident_id: str
    dispatched: bool
    reason: str
    queued: int = 0
    suppressed: int = 0
    deduplicated: int = 0
    planned: int = 0
    intent_ids: tuple[UUID, ...] = ()


# --- gates -----------------------------------------------------------------
#
# Database-authoritative (settings_spec), for the same reason ADR 0004 gave:
# an operator must be able to disarm or re-tighten anything that contacts
# customers from the admin UI mid-incident, without a deploy.


def _gate(session: Session, key: str, fallback: object) -> Any:
    # Any, not object: settings_spec.resolve_value is itself Any and every
    # caller immediately coerces to bool/int. Typing it as object only forces
    # a `type: ignore` at each of those coercions.
    value = settings_spec.resolve_value(session, SettingDomain.network_monitoring, key)
    return fallback if value is None else value


def is_armed(session: Session) -> bool:
    """True when this owner is the canonical customer outage sender."""

    return bool(_gate(session, "outage_customer_comms_enabled", False))


def _dry_run(session: Session) -> bool:
    return bool(_gate(session, "outage_customer_comms_dry_run", True))


def _settle_period(session: Session) -> timedelta:
    return timedelta(
        minutes=int(_gate(session, "outage_customer_comms_settle_minutes", 15))
    )


def _min_affected(session: Session) -> int:
    return int(_gate(session, "outage_customer_comms_min_affected", 5))


def _update_interval(session: Session) -> timedelta:
    return timedelta(
        hours=int(_gate(session, "outage_customer_comms_update_interval_hours", 6))
    )


def _max_recipients(session: Session) -> int:
    return int(_gate(session, "outage_customer_comms_max_recipients_per_run", 500))


def _customer_cooldown(session: Session) -> timedelta:
    return timedelta(
        hours=int(_gate(session, "outage_customer_comms_customer_cooldown_hours", 2))
    )


# --- helpers ---------------------------------------------------------------


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _local(value: datetime | None) -> str:
    """Customer-facing timestamp in the operating calendar timezone."""

    aware = _utc(value)
    if aware is None:
        return "an unrecorded time"
    return aware.astimezone(ZoneInfo(SLA_CALENDAR_TIMEZONE)).strftime("%d %b %Y, %H:%M")


def _humanize(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} hour{'s' if hours != 1 else ''} {minutes} minute{'s' if minutes != 1 else ''}"
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _notice_key(
    namespace: str,
    incident_id,
    stage: NoticeStage,
    sequence: int,
    subscriber_id: UUID,
) -> str:
    return f"{namespace}:{incident_id}:{stage.value}:{sequence}:{subscriber_id}"


def _notices_for_incident(
    db: Session, incident_id
) -> dict[UUID, list[OutageCustomerNotice]]:
    """Prior notice history per customer, oldest first."""

    rows = (
        db.query(OutageCustomerNotice)
        .filter(OutageCustomerNotice.incident_id == incident_id)
        .order_by(OutageCustomerNotice.created_at, OutageCustomerNotice.id)
        .all()
    )
    grouped: dict[UUID, list[OutageCustomerNotice]] = {}
    for row in rows:
        if row.subscriber_id is None:
            continue
        grouped.setdefault(row.subscriber_id, []).append(row)
    return grouped


@dataclass(frozen=True, slots=True)
class _History:
    """One customer's conversation state on one incident.

    ``told_in_episode`` is the whole point: it resets after a restoration
    message so a reopened incident can legitimately open a second
    conversation, while never re-sending the first one.
    """

    told_in_episode: bool
    last_told_at: datetime | None
    opened_count: int
    update_count_in_episode: int
    restored_count: int
    closed: bool


def _history(rows: list[OutageCustomerNotice]) -> _History:
    delivered = [row for row in rows if row.status in _DELIVERED_STATUSES]
    last_restored_at: datetime | None = None
    for row in delivered:
        if row.stage == NoticeStage.restored.value:
            created = _utc(row.created_at)
            if created is not None and (
                last_restored_at is None or created > last_restored_at
            ):
                last_restored_at = created

    def _in_episode(row: OutageCustomerNotice) -> bool:
        if last_restored_at is None:
            return True
        created = _utc(row.created_at)
        return created is not None and created > last_restored_at

    told = [
        row
        for row in delivered
        if row.stage in (NoticeStage.opened.value, NoticeStage.update.value)
        and _in_episode(row)
    ]
    told_at = [stamp for stamp in (_utc(row.created_at) for row in told) if stamp]
    last_told_at = max(told_at, default=None)
    return _History(
        told_in_episode=bool(told),
        last_told_at=last_told_at,
        opened_count=sum(
            1 for row in delivered if row.stage == NoticeStage.opened.value
        ),
        update_count_in_episode=sum(
            1 for row in told if row.stage == NoticeStage.update.value
        ),
        restored_count=len(
            [row for row in delivered if row.stage == NoticeStage.restored.value]
        ),
        closed=last_restored_at is not None and not told,
    )


def _recent_other_incident_notice(
    db: Session, subscriber_id: UUID, incident_id, *, since: datetime
) -> bool:
    """True when this customer heard about a *different* incident recently.

    Merge and split commands do not exist yet, so one physical fault can
    surface as two incidents. Without this, that customer gets told twice.
    A cooldown is the honest suppression available today; it is deliberately
    scoped to opening messages, never to a restoration.
    """

    return (
        db.query(OutageCustomerNotice.id)
        .filter(
            OutageCustomerNotice.subscriber_id == subscriber_id,
            OutageCustomerNotice.incident_id != incident_id,
            OutageCustomerNotice.stage == NoticeStage.opened.value,
            OutageCustomerNotice.status.in_(tuple(_DELIVERED_STATUSES)),
            OutageCustomerNotice.created_at >= since,
        )
        .first()
        is not None
    )


# --- message composition ---------------------------------------------------
#
# Customer-safe by contract: no device names, no blame, no internal
# classification. The incident reference is short and quotable so a support
# agent can tie a caller to the right fault.


def _reference(incident: OutageIncident) -> str:
    return str(incident.id)[:8].upper()


def _service_lines(subscription_ids: tuple[str, ...]) -> str:
    if len(subscription_ids) <= 1:
        return "your service"
    return f"your {len(subscription_ids)} services"


def _compose(
    incident: OutageIncident,
    stage: NoticeStage,
    subscription_ids: tuple[str, ...],
    *,
    name: str,
    downtime: timedelta | None,
    now: datetime,
) -> tuple[str, str]:
    services = _service_lines(subscription_ids)
    reference = _reference(incident)
    greeting = f"Hi {name}," if name and name != "Customer" else "Hello,"
    started = _local(incident.confirmed_at or incident.started_at)

    if stage is NoticeStage.opened:
        subject = "Service interruption in your area"
        body = (
            f"{greeting}\n\n"
            f"We've identified a network fault affecting {services} in your "
            f"area. It began around {started}.\n\n"
            "Our engineers are already working on it. You don't need to do "
            "anything — there's no fault with your equipment, and rebooting "
            "your router won't restore service while this is ongoing.\n\n"
            "We'll message you again as soon as service is restored.\n\n"
            f"Reference: {reference}"
        )
        return subject, body

    if stage is NoticeStage.update:
        elapsed = _humanize(now - (_utc(incident.started_at) or now))
        subject = "Update: we're still working on the interruption"
        body = (
            f"{greeting}\n\n"
            f"The network fault affecting {services} is still being worked on. "
            f"It has now been ongoing for about {elapsed}.\n\n"
            "We're sorry for the disruption. Our engineers remain on it and "
            "we'll confirm as soon as service is back.\n\n"
            f"Reference: {reference}"
        )
        return subject, body

    subject = "Your service is back"
    measured = (
        f"The interruption lasted about {_humanize(downtime)}.\n\n"
        if downtime is not None
        else ""
    )
    body = (
        f"{greeting}\n\n"
        f"The network fault affecting {services} has been resolved and your "
        f"service is back.\n\n"
        f"{measured}"
        "If you're still not connected, please restart your router once. If "
        "that doesn't help, reply to this message and we'll look at your "
        "connection specifically.\n\n"
        "Thank you for your patience.\n\n"
        f"Reference: {reference}"
    )
    return subject, body


def _downtime_for(
    db: Session, incident_id, subscription_ids: tuple[str, ...]
) -> timedelta | None:
    """Measured downtime from the ledger — never recomputed here.

    ``network.customer_outage_accrual`` is the authority; if it has no exact
    interval for this customer the message simply omits a duration rather
    than inventing one.
    """

    if not subscription_ids:
        return None
    rows = (
        db.query(CustomerOutageInterval)
        .filter(
            CustomerOutageInterval.incident_id == incident_id,
            CustomerOutageInterval.subscription_id.in_(list(subscription_ids)),
            CustomerOutageInterval.quality == "exact",
        )
        .all()
    )
    spans = [
        (_utc(row.started_at), _utc(row.ended_at))
        for row in rows
        if row.started_at is not None and row.ended_at is not None
    ]
    if not spans:
        return None
    # The customer experienced one outage, not one per service: union the
    # windows rather than summing them.
    ordered = sorted((start, end) for start, end in spans if start and end)
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    total = sum((end - start for start, end in merged), timedelta())
    return total if total > timedelta() else None


# --- planning --------------------------------------------------------------


def _customer_rows(
    db: Session, subscription_ids: set[str]
) -> dict[UUID, tuple[list[str], str, str | None]]:
    """Group affected subscriptions by distinct customer.

    Grouping precedes policy evaluation so a multi-service customer is
    evaluated — and messaged — once.
    """

    if not subscription_ids:
        return {}
    subscriptions = (
        db.query(Subscription).filter(Subscription.id.in_(list(subscription_ids))).all()
    )
    grouped: dict[UUID, tuple[list[str], str, str | None]] = {}
    for subscription in subscriptions:
        subscriber_id = subscription.subscriber_id
        if subscriber_id is None:
            continue
        subscriber = subscription.subscriber
        name = (
            " ".join(
                part
                for part in (
                    getattr(subscriber, "first_name", None),
                    getattr(subscriber, "last_name", None),
                )
                if part
            ).strip()
            or "Customer"
        )
        email = getattr(subscriber, "email", None)
        existing = grouped.get(subscriber_id)
        if existing is None:
            grouped[subscriber_id] = ([str(subscription.id)], name, email)
        else:
            existing[0].append(str(subscription.id))
    return grouped


def _stage_for(
    *,
    history: _History,
    terminal: bool,
    impact_state: ImpactState | None,
    now: datetime,
    update_interval: timedelta,
) -> tuple[NoticeStage, int] | None:
    """The one message this customer is owed right now, if any."""

    if terminal:
        if history.told_in_episode:
            return NoticeStage.restored, history.restored_count + 1
        return None

    if impact_state is ImpactState.confirmed_unavailable:
        if not history.told_in_episode:
            return NoticeStage.opened, history.opened_count + 1
        if (
            history.last_told_at is not None
            and now - history.last_told_at >= update_interval
        ):
            return NoticeStage.update, history.update_count_in_episode + 1
        return None

    if impact_state is ImpactState.restored and history.told_in_episode:
        # Partial restoration: this customer is back even though the incident
        # is still open for others. Close their conversation now.
        return NoticeStage.restored, history.restored_count + 1

    # potentially_affected / unknown / degraded say nothing. Exposure is not a
    # message, and a customer we cannot see is not a customer we can promise
    # anything to.
    #
    # This also covers a told customer who has left the audience entirely
    # (``impact_state`` is None): leaving is often a reroot artefact rather
    # than proven recovery, so their conversation stays open and closes at
    # incident termination, where they are still in the lineage cohort.
    return None


def _customer_impact(states: set[ImpactState]) -> ImpactState | None:
    """One word for a customer holding several affected services.

    Worst-first: any confirmed-unavailable service means the customer is
    down. Restoration needs *every* service back, so a partial recovery does
    not close the conversation early.
    """

    if ImpactState.confirmed_unavailable in states:
        return ImpactState.confirmed_unavailable
    if states and states <= {ImpactState.restored}:
        return ImpactState.restored
    if ImpactState.degraded in states:
        return ImpactState.degraded
    return None


def _impact_token(
    incident: OutageIncident, candidates: tuple[NoticeCandidate, ...]
) -> str:
    """Bind incident state, stages and every recipient disposition.

    Mirrors the cabinet-notice token: membership alone is not enough — a
    customer who became suppressed, or an incident that resolved, between
    preview and confirm must invalidate the confirmation.
    """

    payload = {
        "incident_id": str(incident.id),
        "status": incident.status,
        "candidates": sorted(
            [
                str(candidate.subscriber_id),
                candidate.stage.value,
                str(candidate.sequence),
                candidate.reason_code or "",
                "eligible" if candidate.eligible else "blocked",
            ]
            for candidate in candidates
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def plan_incident_notices(
    db: Session,
    incident: OutageIncident,
    *,
    now: datetime | None = None,
    dry_run: bool | None = None,
) -> IncidentNoticePlan:
    """Decide every message this incident owes right now. Read-only."""

    evaluated_at = now or datetime.now(UTC)
    terminal = incident.status in _TERMINAL_STATUSES
    dry = _dry_run(db) if dry_run is None else dry_run
    gated_reason: str | None = None

    impacts = (
        () if terminal else resolve_incident_impacts(db, incident, now=evaluated_at)
    )
    per_subscription: dict[str, ImpactState] = {
        impact.subscription_id: impact.state for impact in impacts
    }
    revision_sequence = next(
        (impact.scope_revision_sequence for impact in impacts), None
    )

    history_rows = _notices_for_incident(db, incident.id)

    # The audience to consider: everyone currently impacted, plus everyone we
    # already told (they are owed a close-out even after leaving the audience).
    candidate_subscription_ids = set(per_subscription)
    told_subscribers = {
        subscriber_id
        for subscriber_id, rows in history_rows.items()
        if _history(rows).told_in_episode
    }
    for rows in history_rows.values():
        for row in rows:
            if row.subscriber_id in told_subscribers and row.subscription_ids:
                candidate_subscription_ids.update(str(x) for x in row.subscription_ids)

    customers = _customer_rows(db, candidate_subscription_ids)

    # Opening gates. A settling window keeps a blip that self-clears away from
    # customers; a minimum affected count keeps "your area is down" honest.
    # Neither ever blocks a restoration — a promise already made is kept.
    visible_at = _utc(incident.confirmed_at) or _utc(incident.started_at)
    settled = visible_at is not None and (
        evaluated_at - visible_at >= _settle_period(db)
    )
    confirmed_total = sum(
        1
        for state in per_subscription.values()
        if state is ImpactState.confirmed_unavailable
    )
    opening_allowed = settled and confirmed_total >= _min_affected(db)
    if not terminal and not opening_allowed:
        gated_reason = "not_settled" if not settled else "below_min_affected"

    cooldown_since = evaluated_at - _customer_cooldown(db)
    candidates: list[NoticeCandidate] = []
    pending: list[tuple[UUID, str]] = []

    for subscriber_id, (subscription_ids, name, email) in customers.items():
        rows = history_rows.get(subscriber_id, [])
        history = _history(rows)
        states = {
            per_subscription[sid] for sid in subscription_ids if sid in per_subscription
        }
        decision = _stage_for(
            history=history,
            terminal=terminal,
            impact_state=_customer_impact(states),
            now=evaluated_at,
            update_interval=_update_interval(db),
        )
        if decision is None:
            continue
        stage, sequence = decision
        if stage is NoticeStage.opened and not opening_allowed:
            continue
        if stage is NoticeStage.opened and _recent_other_incident_notice(
            db, subscriber_id, incident.id, since=cooldown_since
        ):
            candidates.append(
                _blocked(
                    incident,
                    subscriber_id,
                    subscription_ids,
                    name,
                    email,
                    stage,
                    sequence,
                    states,
                    revision_sequence,
                    reason_code="recent_other_incident",
                    reason=(
                        "Customer was told about another incident inside the "
                        "cooldown window"
                    ),
                    dry=dry,
                    db=db,
                    now=evaluated_at,
                )
            )
            continue
        if not email:
            candidates.append(
                _blocked(
                    incident,
                    subscriber_id,
                    subscription_ids,
                    name,
                    email,
                    stage,
                    sequence,
                    states,
                    revision_sequence,
                    reason_code="missing_email",
                    reason="Customer has no email address on file",
                    dry=dry,
                    db=db,
                    now=evaluated_at,
                )
            )
            continue
        pending.append((subscriber_id, email))
        candidates.append(
            _candidate(
                incident,
                subscriber_id,
                subscription_ids,
                name,
                email,
                stage,
                sequence,
                states,
                revision_sequence,
                eligible=True,
                dry=dry,
                db=db,
                now=evaluated_at,
            )
        )

    if pending:
        decisions = evaluate_bulk_customer_notification_policy(
            db,
            CustomerNotificationPolicyCohortQuery(
                candidates=tuple(
                    CustomerNotificationPolicyCandidate(
                        subscriber_id=subscriber_id, recipient=email
                    )
                    for subscriber_id, email in pending
                ),
                channel=NotificationChannel.email,
                category=CATEGORY,
                event_type=EVENT_TYPE,
                evaluated_at=evaluated_at,
            ),
        )
        blocked = {
            decision.candidate.key: decision
            for decision in decisions.decisions
            if not decision.allowed
        }
        if blocked:
            # Only candidates that carried an address were evaluated, so a
            # missing one simply cannot match a policy decision.
            candidates = [
                (
                    _reblock(candidate, refusal)
                    if candidate.email
                    and (
                        refusal := blocked.get(
                            (candidate.subscriber_id, candidate.email)
                        )
                    )
                    else candidate
                )
                for candidate in candidates
            ]

    ordered = tuple(sorted(candidates, key=lambda c: str(c.subscriber_id)))
    return IncidentNoticePlan(
        incident_id=str(incident.id),
        incident_status=incident.status,
        dry_run=dry,
        gated_reason=gated_reason,
        opened=sum(1 for c in ordered if c.stage is NoticeStage.opened),
        updates=sum(1 for c in ordered if c.stage is NoticeStage.update),
        restored=sum(1 for c in ordered if c.stage is NoticeStage.restored),
        eligible=sum(1 for c in ordered if c.eligible),
        suppressed=sum(
            1 for c in ordered if not c.eligible and c.reason_code != "missing_email"
        ),
        missing_email=sum(1 for c in ordered if c.reason_code == "missing_email"),
        impact_token=_impact_token(incident, ordered),
        candidates=ordered,
    )


def _candidate(
    incident: OutageIncident,
    subscriber_id: UUID,
    subscription_ids: list[str],
    name: str,
    email: str | None,
    stage: NoticeStage,
    sequence: int,
    states: set[ImpactState],
    revision_sequence: int | None,
    *,
    eligible: bool,
    dry: bool,
    db: Session,
    now: datetime,
    reason_code: str | None = None,
    reason: str | None = None,
) -> NoticeCandidate:
    ids = tuple(sorted(subscription_ids))
    customer_impact = _customer_impact(states)
    downtime = (
        _downtime_for(db, incident.id, ids) if stage is NoticeStage.restored else None
    )
    subject, body = _compose(
        incident, stage, ids, name=name, downtime=downtime, now=now
    )
    namespace = _KEY_DRY if dry else (_KEY_SENT if eligible else _KEY_BLOCKED)
    return NoticeCandidate(
        subscriber_id=subscriber_id,
        subscription_ids=ids,
        name=name,
        email=email,
        stage=stage,
        sequence=sequence,
        impact_state=(customer_impact.value if customer_impact else "terminal"),
        scope_revision_sequence=revision_sequence,
        subject=subject,
        body=body,
        dedupe_key=_notice_key(namespace, incident.id, stage, sequence, subscriber_id),
        eligible=eligible,
        reason_code=reason_code,
        reason=reason,
    )


def _blocked(
    incident: OutageIncident,
    subscriber_id: UUID,
    subscription_ids: list[str],
    name: str,
    email: str | None,
    stage: NoticeStage,
    sequence: int,
    states: set[ImpactState],
    revision_sequence: int | None,
    *,
    reason_code: str,
    reason: str,
    dry: bool,
    db: Session,
    now: datetime,
) -> NoticeCandidate:
    return _candidate(
        incident,
        subscriber_id,
        subscription_ids,
        name,
        email,
        stage,
        sequence,
        states,
        revision_sequence,
        eligible=False,
        dry=dry,
        db=db,
        now=now,
        reason_code=reason_code,
        reason=reason,
    )


def _key_exists(db: Session, dedupe_key: str) -> bool:
    return (
        db.query(OutageCustomerNotice.id)
        .filter(OutageCustomerNotice.dedupe_key == dedupe_key)
        .first()
        is not None
    )


def _requalify(
    candidate: NoticeCandidate,
    namespace: str,
    *,
    eligible: bool | None = None,
    reason_code: str | None = None,
    reason: str | None = None,
) -> NoticeCandidate:
    """Copy a candidate onto a different dedupe-key namespace.

    Overrides are named rather than splatted: only the disposition fields are
    ever re-decided after planning, and naming them keeps the frozen contract
    checkable instead of degrading to an untyped bag.
    """

    rest = candidate.dedupe_key.split(":", 1)[1]
    return replace(
        candidate,
        dedupe_key=f"{namespace}:{rest}",
        eligible=candidate.eligible if eligible is None else eligible,
        reason_code=candidate.reason_code if reason_code is None else reason_code,
        reason=candidate.reason if reason is None else reason,
    )


def _deferred(candidate: NoticeCandidate) -> NoticeCandidate:
    """A capped candidate: audited now, still sendable next pass."""

    return _requalify(
        candidate,
        _KEY_BLOCKED,
        reason_code="per_run_cap",
        reason="Deferred to the next pass by the per-run recipient cap",
    )


def _reblock(candidate: NoticeCandidate, decision) -> NoticeCandidate:
    """Re-key a candidate the notification policy refused.

    A dry-run plan stays in its own namespace — a suppression noticed during
    a plan must not consume the blocked key a real pass would write.
    """

    prefix = candidate.dedupe_key.split(":", 1)[0]
    return _requalify(
        candidate,
        _KEY_DRY if prefix == _KEY_DRY else _KEY_BLOCKED,
        eligible=False,
        reason_code=decision.reason_code,
        reason=decision.reason,
    )


# --- sending ---------------------------------------------------------------


def send_incident_notices(
    db: Session,
    incident: OutageIncident,
    *,
    actor: str | None,
    now: datetime | None = None,
    dry_run: bool | None = None,
    expected_impact_token: str | None = None,
) -> IncidentNoticeResult:
    """Queue every message this incident owes, once.

    Stages notices, intents and the breadcrumb event in the caller's
    transaction — the receipted consumer and the operator command own the
    commit, so a failure cannot leave notice rows that would suppress a
    message nobody received.

    ``expected_impact_token`` is the operator preview→confirm guard; the
    automatic path omits it because the plan is recomputed from the same
    committed transition.
    """

    evaluated_at = now or datetime.now(UTC)
    plan = plan_incident_notices(db, incident, now=evaluated_at, dry_run=dry_run)

    if expected_impact_token is not None and not hmac.compare_digest(
        expected_impact_token, plan.impact_token
    ):
        raise OutageCommunicationsDriftError(
            "The incident audience, stage or recipient impact changed after "
            "preview. Review the updated plan before confirming again."
        )
    if not plan.candidates:
        return IncidentNoticeResult(
            incident_id=str(incident.id),
            dispatched=False,
            reason=plan.gated_reason or "nothing_owed",
        )

    cap = _max_recipients(db)
    queued = suppressed = deduplicated = planned = 0
    intent_ids: list[UUID] = []
    sent = 0

    for candidate in plan.candidates:
        if _key_exists(db, candidate.dedupe_key):
            deduplicated += 1
            continue

        if plan.dry_run:
            _record(
                db,
                incident,
                candidate,
                NoticeStatus.planned_dry_run,
                actor=actor,
                now=evaluated_at,
            )
            planned += 1
            continue
        if not candidate.eligible:
            status = (
                NoticeStatus.skipped_no_recipient
                if candidate.reason_code == "missing_email"
                else NoticeStatus.suppressed
            )
            _record(db, incident, candidate, status, actor=actor, now=evaluated_at)
            suppressed += 1
            continue
        if sent >= cap:
            # Deferred, not cancelled: the cap bounds one pass, so this
            # customer must still be reachable on the next one. Recording
            # under the canonical key would consume it and silently drop them,
            # so the audit row goes to the blocked namespace — and only once,
            # however many capped passes it takes to reach them.
            deferred = _deferred(candidate)
            if not _key_exists(db, deferred.dedupe_key):
                _record(
                    db,
                    incident,
                    deferred,
                    NoticeStatus.skipped_cap,
                    actor=actor,
                    now=evaluated_at,
                )
            continue
        if candidate.email is None:
            # Unreachable by construction — planning routes an addressless
            # customer to the blocked namespace — but a send must never fan
            # out to a resolved contact list, so fail closed rather than
            # letting the intent pick an address for us.
            _record(
                db,
                incident,
                candidate,
                NoticeStatus.skipped_no_recipient,
                actor=actor,
                now=evaluated_at,
            )
            suppressed += 1
            continue

        result = communication_intents.submit(
            db,
            communication_intents.CommunicationIntent(
                subscriber_id=candidate.subscriber_id,
                event_type=EVENT_TYPE,
                category=CATEGORY,
                subject=candidate.subject,
                body=candidate.body,
                communication_class=communication_intents.CommunicationClass.transactional,
                # Pin the channel and the exact address: contact fan-out must
                # not multiply an outage blast.
                channels=(NotificationChannel.email,),
                recipients={NotificationChannel.email: candidate.email},
                include_reseller=False,
                dedupe_key=candidate.dedupe_key,
                metadata={
                    "incident_id": str(incident.id),
                    "incident_status": incident.status,
                    "stage": candidate.stage.value,
                    "sequence": candidate.sequence,
                    "impact_state": candidate.impact_state,
                    "scope_revision_sequence": candidate.scope_revision_sequence,
                    "subscription_ids": list(candidate.subscription_ids),
                    "actor": actor,
                },
            ),
        )
        intent_ids.append(result.intent_id)
        sent += 1
        if result.queued:
            queued += 1
            _record(
                db,
                incident,
                candidate,
                NoticeStatus.queued,
                actor=actor,
                now=evaluated_at,
                intent_id=result.intent_id,
            )
        else:
            suppressed += 1
            _record(
                db,
                incident,
                candidate,
                NoticeStatus.suppressed,
                actor=actor,
                now=evaluated_at,
                intent_id=result.intent_id,
                reason_code=(result.suppressed[0] if result.suppressed else None),
            )

    db.flush()
    _emit(db, incident, plan, queued=queued, planned=planned, actor=actor)
    logger.info(
        "outage_customer_notices incident=%s queued=%s suppressed=%s "
        "deduplicated=%s planned=%s dry_run=%s",
        incident.id,
        queued,
        suppressed,
        deduplicated,
        planned,
        plan.dry_run,
        extra={
            "event": "outage_customer_notices",
            "incident_id": str(incident.id),
            "opened": plan.opened,
            "updates": plan.updates,
            "restored": plan.restored,
            "queued": queued,
            "dry_run": plan.dry_run,
        },
    )
    return IncidentNoticeResult(
        incident_id=str(incident.id),
        dispatched=not plan.dry_run and queued > 0,
        reason="dry_run" if plan.dry_run else "ok",
        queued=queued,
        suppressed=suppressed,
        deduplicated=deduplicated,
        planned=planned,
        intent_ids=tuple(intent_ids),
    )


def _record(
    db: Session,
    incident: OutageIncident,
    candidate: NoticeCandidate,
    status: NoticeStatus,
    *,
    actor: str | None,
    now: datetime,
    intent_id: UUID | None = None,
    reason_code: str | None = None,
) -> None:
    # Stamped with the evaluation clock, not wall time: the update interval
    # measures from the last message, so a row that dated itself differently
    # from the decision that produced it would drift the whole cadence.
    db.add(
        OutageCustomerNotice(
            created_at=now,
            incident_id=incident.id,
            subscriber_id=candidate.subscriber_id,
            stage=candidate.stage.value,
            sequence=candidate.sequence,
            subscription_ids=list(candidate.subscription_ids),
            impact_state=candidate.impact_state,
            scope_revision_sequence=candidate.scope_revision_sequence,
            status=status.value,
            reason_code=reason_code or candidate.reason_code,
            communication_intent_id=intent_id,
            recipient=candidate.email,
            subject=candidate.subject,
            dedupe_key=candidate.dedupe_key,
            actor=actor,
        )
    )


def _emit(
    db: Session,
    incident: OutageIncident,
    plan: IncidentNoticePlan,
    *,
    queued: int,
    planned: int,
    actor: str | None,
) -> None:
    """Operational breadcrumb only — customer messages are the intents."""

    from app.services.events.dispatcher import emit_event
    from app.services.events.types import EventType

    emit_event(
        db,
        EventType.outage_customer_notice_dispatched,
        {
            "incident_id": str(incident.id),
            "incident_status": incident.status,
            "opened": plan.opened,
            "updates": plan.updates,
            "restored": plan.restored,
            "queued": queued,
            "planned": planned,
            "dry_run": plan.dry_run,
            "actor": actor,
        },
        actor=actor,
    )


# --- automatic delivery ----------------------------------------------------


def _consume_definition(name: str):
    from app.services.owner_commands import OwnerCommandDefinition

    return OwnerCommandDefinition(
        owner="network.outage_communications",
        concern="committed outage output communication consumption",
        name=name,
    )


def consume_notice_event(
    session: Session,
    *,
    incident_id,
    event_id,
    event_type: str,
    context,
) -> str | None:
    """Receipt one committed lifecycle output into customer communications.

    A redelivery is an exact no-op through the ``(consumer, event_id)``
    receipt; a genuinely repeated transition is a no-op through the notice
    dedupe key. Disarmed is a clean skip, so the handler can be wired long
    before the decision to arm it.
    """

    from app.services.events.owner_outputs import consume_owner_output
    from app.services.owner_commands import execute_owner_command

    def _effect() -> str:
        if not is_armed(session):
            return "skipped_disarmed"
        incident = session.get(OutageIncident, incident_id)
        if incident is None:
            logger.warning(
                "outage communications consequence: incident %s missing", incident_id
            )
            return "skipped_missing"
        result = send_incident_notices(
            session,
            incident,
            actor="system:outage_communications",
            dry_run=None,
        )
        if result.queued or result.planned:
            return "applied"
        return "noop"

    return execute_owner_command(
        session,
        definition=_consume_definition("consume_notice_event"),
        context=context,
        operation=lambda: consume_owner_output(
            session,
            consumer="network.outage_communications",
            event_id=event_id,
            event_type=event_type,
            producer_owner="network.outage_lifecycle",
            context=context,
            operation=_effect,
        )[0],
    )


def confirm_incident_notices(
    db: Session,
    incident: OutageIncident,
    *,
    actor: str | None,
    expected_impact_token: str | None,
    now: datetime | None = None,
) -> IncidentNoticeResult:
    """Operator preview→confirm send. Owns its commit.

    Split from :func:`send_incident_notices` on purpose: the receipted
    consumer runs inside an owner session that commits for it, while the
    console adapter must not commit (the notice rows, intents and event land
    atomically here or not at all). The drift token is mandatory on this path
    — an operator confirming a preview they can no longer see is exactly the
    case it exists for.
    """

    if not expected_impact_token:
        raise OutageCommunicationsError("Preview the messages before confirming")
    result = send_incident_notices(
        db,
        incident,
        actor=actor,
        now=now,
        expected_impact_token=expected_impact_token,
    )
    db.commit()
    return result


def notices_for_incident(db: Session, incident_id) -> list[OutageCustomerNotice]:
    """The full communication history for one incident (console read)."""

    return (
        db.query(OutageCustomerNotice)
        .filter(OutageCustomerNotice.incident_id == incident_id)
        .order_by(OutageCustomerNotice.created_at.desc())
        .all()
    )
