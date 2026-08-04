"""Per-subscription SLA policy resolution and period scoring (shadow phase).

Owner: ``customer.service_level`` (OUTAGE_SLA_SPINE §4). Read-time only in
this slice: it resolves the effective policy (offer-version precedence today;
subscription/account contract tables arrive with cutover), merges the accrual
ledger's qualifying intervals, and scores one Africa/Lagos calendar-month
period. It never invents a contractual SLA — no effective policy renders
measured availability with the ``no_contractual_sla`` verdict, not a 99.5%
default. Overlapping intervals are unioned, never summed; excluded intervals
are reported in their own bucket, never silently dropped; unknown-coverage
tracking and persisted effective-dated policy versions are later slices, so
every score carries ``policy`` + ``evidence_digest`` for lineage and the
existing read-time ``topology.customer_availability`` stays authoritative for
display until the shadow-comparison gate cuts over.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.catalog import (
    CatalogOffer,
    SlaProfile,
    Subscription,
)
from app.models.catalog import (
    SlaPolicyVersion as SlaPolicyVersionRecord,
)
from app.services.domain_errors import DomainError
from app.services.network.customer_outage_accrual import intervals_for_subscription
from app.services.service_impact_contracts import (
    SLA_CALENDAR_TIMEZONE,
    ImpactState,
    SlaPolicySource,
    SlaPolicyVersion,
    SlaScore,
    SlaVerdict,
)

if TYPE_CHECKING:  # avoids a runtime import cycle via owner_commands
    from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)

# Downtime consuming more than this share of the allowed budget flags the
# period at-risk before it breaches.
_AT_RISK_BUDGET_SHARE = 0.8


@dataclass(frozen=True, slots=True)
class SlaShadowComparison:
    """Ledger-based score next to the legacy read-time availability.

    Approximate by design (calendar month-to-date vs trailing window); the
    comparison exists to find discrepancies before cutover, not to display
    two numbers to customers.
    """

    score: SlaScore
    legacy_availability_percent: float | None
    delta_percent: float | None


#: Approved precedence, highest first (design §4). A subscription contract
#: beats an account contract, which beats the subscribed offer version, which
#: beats the internal measurement policy. Encoded once, here, so the resolver
#: and any future reporting surface cannot disagree about which term won.
_PRECEDENCE: tuple[SlaPolicySource, ...] = (
    SlaPolicySource.subscription_contract,
    SlaPolicySource.account_contract,
    SlaPolicySource.offer_version,
    SlaPolicySource.internal_measurement,
)


def _row_to_policy(row: SlaPolicyVersionRecord) -> SlaPolicyVersion:
    """Persisted row → the immutable typed contract the scorer consumes."""

    return SlaPolicyVersion(
        policy_id=row.id,
        version=row.version,
        source=SlaPolicySource(row.source),
        effective_from=_utc(row.effective_from) or datetime.now(UTC),
        effective_to=_utc(row.effective_to),
        availability_target_percent=(
            float(row.availability_target_percent)
            if row.availability_target_percent is not None
            else None
        ),
        calendar_timezone=row.calendar_timezone or SLA_CALENDAR_TIMEZONE,
        maintenance_excludable=bool(row.maintenance_excludable),
        credit_percent_per_breach=(
            float(row.credit_percent_per_breach)
            if row.credit_percent_per_breach is not None
            else None
        ),
        credit_cap_percent=(
            float(row.credit_cap_percent)
            if row.credit_cap_percent is not None
            else None
        ),
    )


def _persisted_versions_covering(
    db: Session,
    subscription: Subscription,
    *,
    start: datetime,
    end: datetime,
) -> list[SlaPolicyVersionRecord]:
    """Every persisted version whose effective range overlaps [start, end).

    Ordered by precedence then by ``effective_from`` so the caller can take
    the winning source without re-deciding the ranking.
    """

    scope_filters = [
        SlaPolicyVersionRecord.source == SlaPolicySource.internal_measurement.value,
        SlaPolicyVersionRecord.subscription_id == subscription.id,
    ]
    if subscription.subscriber_id is not None:
        scope_filters.append(
            SlaPolicyVersionRecord.subscriber_id == subscription.subscriber_id
        )
    if subscription.offer_id is not None:
        scope_filters.append(SlaPolicyVersionRecord.offer_id == subscription.offer_id)

    rows = (
        db.query(SlaPolicyVersionRecord)
        .filter(
            or_(*scope_filters),
            SlaPolicyVersionRecord.effective_from < end,
            or_(
                SlaPolicyVersionRecord.effective_to.is_(None),
                SlaPolicyVersionRecord.effective_to > start,
            ),
        )
        .all()
    )
    rank = {source.value: index for index, source in enumerate(_PRECEDENCE)}
    return sorted(
        rows,
        key=lambda row: (
            rank.get(row.source, len(rank)),
            _utc(row.effective_from) or datetime.min.replace(tzinfo=UTC),
        ),
    )


def resolve_effective_policy(
    db: Session,
    subscription: Subscription,
    *,
    at: datetime | None = None,
) -> SlaPolicyVersion | None:
    """The effective contractual policy at ``at``, or None — never invented.

    During the shadow migration BOTH sources are live, so both are ranked by
    the one precedence order. Preferring any persisted row over the legacy
    derivation would let a global ``internal_measurement`` policy — the
    LOWEST precedence there is — mask a customer's actual offer SLA. The
    legacy profile therefore competes at ``offer_version`` precedence, which
    is what it represents.

    Ties go to the persisted row: it is the authority the legacy derivation
    is being retired in favour of.
    """

    instant = at or datetime.now(UTC)
    covering = _persisted_versions_covering(
        db, subscription, start=instant, end=instant + timedelta(microseconds=1)
    )
    candidates = [_row_to_policy(row) for row in covering]
    legacy = _legacy_offer_policy(db, subscription)
    if legacy is not None:
        candidates.append(legacy)
    if not candidates:
        return None
    rank = {source: index for index, source in enumerate(_PRECEDENCE)}
    # min() is stable, so persisted rows (added first) win an equal-precedence
    # tie against the legacy derivation.
    return min(candidates, key=lambda policy: rank[policy.source])


def _legacy_offer_policy(
    db: Session, subscription: Subscription
) -> SlaPolicyVersion | None:
    """The mutable-``SlaProfile`` derivation this slice supersedes.

    Retained as the fallback for subscriptions with no persisted policy, and
    as the scorer's only input until segmented scoring lands. Retired by the
    cutover PR.
    """

    offer = (
        db.get(CatalogOffer, subscription.offer_id) if subscription.offer_id else None
    )
    profile = (
        db.get(SlaProfile, offer.sla_profile_id)
        if offer is not None and offer.sla_profile_id
        else None
    )
    if profile is None or profile.uptime_percent is None:
        return None
    effective_from = profile.created_at or datetime.now(UTC)
    if effective_from.tzinfo is None:
        effective_from = effective_from.replace(tzinfo=UTC)
    return SlaPolicyVersion(
        policy_id=profile.id,
        version=1,
        source=SlaPolicySource.offer_version,
        effective_from=effective_from,
        effective_to=None,
        availability_target_percent=float(profile.uptime_percent),
        credit_percent_per_breach=(
            float(profile.credit_percent)
            if profile.credit_percent is not None
            else None
        ),
    )


@dataclass(frozen=True, slots=True)
class PolicySegment:
    """One contiguous stretch of a reporting period under a single policy.

    A mid-period policy change must split the calculation rather than apply
    the latest terms retroactively (design §4), so scoring consumes segments,
    not one policy. ``policy`` is None where no contractual policy was in
    force for that stretch — measured availability still applies there, under
    the ``no_contractual_sla`` verdict.
    """

    start: datetime
    end: datetime
    policy: SlaPolicyVersion | None

    @property
    def seconds(self) -> int:
        return max(int((self.end - self.start).total_seconds()), 0)


def policy_segments_for_period(
    db: Session,
    subscription: Subscription,
    *,
    period_start: datetime,
    period_end: datetime,
) -> tuple[PolicySegment, ...]:
    """Split ``[period_start, period_end)`` at every effective policy change.

    Boundaries come from the winning policy at each instant, so a change of
    *precedence* splits the period exactly like a change of terms: if a
    subscription contract starts mid-month, the offer policy governs the
    stretch before it and the subscription contract the stretch after.
    """

    covering = _persisted_versions_covering(
        db, subscription, start=period_start, end=period_end
    )
    boundaries = {period_start, period_end}
    for row in covering:
        for edge in (_utc(row.effective_from), _utc(row.effective_to)):
            if edge is not None and period_start < edge < period_end:
                boundaries.add(edge)

    ordered = sorted(boundaries)
    segments: list[PolicySegment] = []
    for start, end in zip(ordered, ordered[1:], strict=False):
        if end <= start:
            continue
        segments.append(
            PolicySegment(
                start=start,
                end=end,
                # Resolve at the segment's own start: the winning policy is a
                # property of the instant, not of the period.
                policy=resolve_effective_policy(db, subscription, at=start),
            )
        )
    return tuple(segments)


def period_bounds(now: datetime) -> tuple[datetime, datetime]:
    """The Africa/Lagos calendar month containing ``now``, as UTC instants."""

    zone = ZoneInfo(SLA_CALENDAR_TIMEZONE)
    local = now.astimezone(zone)
    start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_local.month == 12:
        end_local = start_local.replace(year=start_local.year + 1, month=1)
    else:
        end_local = start_local.replace(month=start_local.month + 1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _merge_windows(
    windows: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Union overlapping windows so concurrent incidents never double-count."""

    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def score_subscription_period(
    db: Session,
    subscription: Subscription,
    *,
    now: datetime | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> SlaScore:
    """Score one subscription for one reporting period from the ledger."""

    evaluated_at = now or datetime.now(UTC)
    if period_start is None or period_end is None:
        period_start, period_end = period_bounds(evaluated_at)
    # GATED: this scorer still applies ONE policy across the whole requested
    # period, so consuming persisted effective-dated versions here would let a
    # policy recorded today govern a historical or mid-period score — exactly
    # the retroactive defect this programme exists to remove. Segmented
    # scoring lands with PR 2 (`policy_segments_for_period` is its input);
    # until then the scorer deliberately reads only the legacy offer profile,
    # so recording a policy version changes no existing score.
    policy = _legacy_offer_policy(db, subscription)

    # Eligibility approximation for the shadow phase: an active subscription
    # is entitled from its creation; non-active subscriptions score
    # unavailable rather than pretending entitlement intervals exist.
    status_value = getattr(subscription.status, "value", subscription.status)
    created_at = _utc(subscription.created_at) or period_start
    eligible_start = max(period_start, created_at)
    eligible_end = min(period_end, evaluated_at)
    active = str(status_value) == "active"
    eligible_seconds = (
        max(int((eligible_end - eligible_start).total_seconds()), 0) if active else 0
    )

    qualifying: list[tuple[datetime, datetime]] = []
    excluded: list[tuple[datetime, datetime]] = []
    interval_ids: list[str] = []
    if eligible_seconds:
        for interval in intervals_for_subscription(
            db, subscription.id, since=eligible_start
        ):
            if interval.state != ImpactState.confirmed_unavailable.value:
                continue
            started = _utc(interval.started_at) or eligible_start
            ended = _utc(interval.ended_at) or eligible_end
            start = max(started, eligible_start)
            end = min(ended, eligible_end)
            if end <= start:
                continue
            interval_ids.append(str(interval.id))
            if interval.exclusion_candidate is not None:
                excluded.append((start, end))
            elif interval.quality == "exact":
                qualifying.append((start, end))
            else:
                # Estimated/unavailable evidence never silently becomes
                # contractual downtime; report it with the exclusions until
                # a reviewed adjustment upgrades it.
                excluded.append((start, end))

    unavailable_seconds = sum(
        int((end - start).total_seconds()) for start, end in _merge_windows(qualifying)
    )
    excluded_seconds = sum(
        int((end - start).total_seconds()) for start, end in _merge_windows(excluded)
    )
    unavailable_seconds = min(unavailable_seconds, eligible_seconds)

    digest_material = "\0".join(
        [
            str(subscription.id),
            period_start.isoformat(),
            period_end.isoformat(),
            str(policy.policy_id) if policy else "no-policy",
            *sorted(interval_ids),
        ]
    ).encode()
    evidence_digest = f"sha256:{hashlib.sha256(digest_material).hexdigest()}"

    verdict = _verdict(
        policy=policy,
        eligible_seconds=eligible_seconds,
        unavailable_seconds=unavailable_seconds,
    )
    return SlaScore(
        subscription_id=subscription.id,
        period_start=period_start,
        period_end=period_end,
        eligible_seconds=eligible_seconds,
        unavailable_seconds=unavailable_seconds,
        excluded_seconds=min(excluded_seconds, eligible_seconds),
        unknown_seconds=0,
        verdict=verdict,
        policy=policy,
        evidence_digest=evidence_digest,
        interval_ids=tuple(sorted(interval_ids)),
    )


def _verdict(
    *,
    policy: SlaPolicyVersion | None,
    eligible_seconds: int,
    unavailable_seconds: int,
) -> SlaVerdict:
    if eligible_seconds <= 0:
        return SlaVerdict.unavailable
    if policy is None or policy.availability_target_percent is None:
        return SlaVerdict.no_contractual_sla
    availability = 100.0 * (eligible_seconds - unavailable_seconds) / eligible_seconds
    target = policy.availability_target_percent
    if availability < target:
        return SlaVerdict.breach
    allowed_downtime = eligible_seconds * (100.0 - target) / 100.0
    if allowed_downtime > 0 and unavailable_seconds >= (
        _AT_RISK_BUDGET_SHARE * allowed_downtime
    ):
        return SlaVerdict.at_risk
    return SlaVerdict.passing


def shadow_compare(
    db: Session,
    subscription: Subscription,
    *,
    now: datetime | None = None,
) -> SlaShadowComparison:
    """Ledger score vs the legacy read-time availability, for the cutover gate."""

    evaluated_at = now or datetime.now(UTC)
    score = score_subscription_period(db, subscription, now=evaluated_at)
    legacy_percent: float | None = None
    try:
        from app.services.topology.customer_availability import (
            customer_availability,
        )

        elapsed_days = max(
            int(
                (evaluated_at - score.period_start).total_seconds()
                // int(timedelta(days=1).total_seconds())
            ),
            1,
        )
        legacy = customer_availability(
            db, subscription, days=elapsed_days, now=evaluated_at
        )
        legacy_percent = getattr(legacy, "availability_percent", None)
        if legacy_percent is not None:
            legacy_percent = float(legacy_percent)
    except Exception:
        logger.warning(
            "Legacy availability comparison failed for subscription %s",
            subscription.id,
            exc_info=True,
        )
    measured = score.measured_availability_percent
    delta = (
        round(measured - legacy_percent, 3)
        if measured is not None and legacy_percent is not None
        else None
    )
    return SlaShadowComparison(
        score=score,
        legacy_availability_percent=legacy_percent,
        delta_percent=delta,
    )


# --- recording a new effective-dated version --------------------------------


class SlaPolicyError(DomainError):
    """Invalid policy-version input (adapter: HTTP 400)."""


@dataclass(frozen=True, slots=True)
class RecordPolicyVersionCommand:
    """Typed input for establishing new contractual terms.

    ``effective_from`` is the instant the terms begin, not the instant the
    record was typed — a contract signed today may start next month, and the
    resolver answers by instant, so the two must not be conflated.

    There is deliberately no ``policy_key``: the series identity is derived
    from ``(source, scope)`` inside the owner. A caller-supplied key would let
    two different keys target the same subscription for the same period,
    producing two equal-precedence policies and an undefined resolver winner —
    the exclusion constraint only forbids overlap *within* a key.
    """

    source: SlaPolicySource
    effective_from: datetime
    availability_target_percent: float | None
    context: CommandContext
    subscription_id: UUID | None = None
    subscriber_id: UUID | None = None
    offer_id: UUID | None = None
    calendar_timezone: str = SLA_CALENDAR_TIMEZONE
    maintenance_excludable: bool = True
    credit_percent_per_breach: float | None = None
    credit_cap_percent: float | None = None
    contract_reference: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyVersionOutcome:
    """Immutable result of recording terms — never the ORM row itself.

    Returning the entity would hand callers a mutable handle to a record whose
    entire purpose is being append-only, and would let a caller edit terms
    after the command validated them.
    """

    policy_version_id: UUID
    policy_key: str
    version: int
    source: SlaPolicySource
    effective_from: datetime
    superseded_version_id: UUID | None
    superseded_at: datetime | None
    replayed: bool
    fingerprint: str


def derive_policy_key(
    source: SlaPolicySource,
    *,
    subscription_id: UUID | None = None,
    subscriber_id: UUID | None = None,
    offer_id: UUID | None = None,
) -> str:
    """The series identity for one real scope. Owner-derived, never supplied.

    One scope has exactly one policy series, so "the terms in force for this
    subscription at T" cannot have two equal-precedence answers. The database
    enforces the same shape: a unique index on (source, scope) inside the
    exclusion constraint.
    """

    if source is SlaPolicySource.subscription_contract:
        if subscription_id is None:
            raise SlaPolicyError(
                code="customer.service_level.scope_required",
                message="A subscription contract needs its subscription.",
            )
        return f"subscription_contract:{subscription_id}"
    if source is SlaPolicySource.account_contract:
        if subscriber_id is None:
            raise SlaPolicyError(
                code="customer.service_level.scope_required",
                message="An account contract needs its account.",
            )
        return f"account_contract:{subscriber_id}"
    if source is SlaPolicySource.offer_version:
        if offer_id is None:
            raise SlaPolicyError(
                code="customer.service_level.scope_required",
                message="An offer policy needs its offer.",
            )
        return f"offer_version:{offer_id}"
    return "internal_measurement:global"


#: Constraints that genuinely represent a lost race. A raw collision does not
#: reveal WHAT the winner wrote, so all of these are reported as retryable:
#: the retry re-reads the winner and decides replay-or-conflict from evidence
#: rather than guessing here.
_RACE_CONSTRAINTS = frozenset(
    {
        "ex_sla_policy_versions_no_overlap",
        "uq_sla_policy_versions_key_version",
        "uq_sla_policy_versions_fingerprint",
        "uq_sla_policy_versions_idempotency_key",
    }
)

#: Constraints that can only mean bad input. Everything outside these two sets
#: is an unexpected defect and MUST stay unexpected — swallowing an unknown
#: constraint or driver failure into a tidy domain error hides the bug.
_INPUT_CONSTRAINTS = frozenset(
    {
        "ck_sla_policy_versions_version",
        "ck_sla_policy_versions_range",
        "ck_sla_policy_versions_target_bounds",
        "ck_sla_policy_versions_contractual_target",
        "ck_sla_policy_versions_scope_matches_source",
        "ck_sla_policy_versions_key_is_derived",
    }
)


def _validate_scope(db: Session, command: RecordPolicyVersionCommand) -> None:
    """Exactly one source-matching scope, and its parent must exist.

    Without this, an extra scope id or a nonexistent parent reaches PostgreSQL
    and surfaces as an IntegrityError, which the writer would then mislabel as
    a concurrency conflict — telling the caller to retry something that can
    never succeed.
    """

    from app.models.subscriber import Subscriber

    supplied = {
        "subscription_id": command.subscription_id,
        "subscriber_id": command.subscriber_id,
        "offer_id": command.offer_id,
    }
    expected = {
        SlaPolicySource.subscription_contract: "subscription_id",
        SlaPolicySource.account_contract: "subscriber_id",
        SlaPolicySource.offer_version: "offer_id",
        SlaPolicySource.internal_measurement: None,
    }[command.source]

    extra = sorted(k for k, v in supplied.items() if v is not None and k != expected)
    if extra:
        raise SlaPolicyError(
            code="customer.service_level.invalid_scope",
            message=(
                f"A {command.source.value} policy must carry only its own "
                f"scope; got {', '.join(extra)}."
            ),
            details={"unexpected_scope": extra},
        )
    if expected is None:
        return
    scope_id = supplied[expected]
    if scope_id is None:
        raise SlaPolicyError(
            code="customer.service_level.scope_required",
            message=f"A {command.source.value} policy needs its {expected}.",
        )
    parent = {
        "subscription_id": Subscription,
        "subscriber_id": Subscriber,
        "offer_id": CatalogOffer,
    }[expected]
    if db.get(parent, scope_id) is None:
        raise SlaPolicyError(
            code="customer.service_level.unknown_scope",
            message=f"No {parent.__name__} exists for the supplied {expected}.",
            details={expected: str(scope_id)},
        )


def _policy_fingerprint(command: RecordPolicyVersionCommand, key: str) -> str:
    """Stable identity of the *intent*, for replay arbitration.

    A retry of the same command must return the original outcome rather than
    raising ``not_after_current`` against the row it already created.
    """

    material = "\0".join(
        str(part)
        for part in (
            key,
            command.source.value,
            _utc(command.effective_from),
            command.availability_target_percent,
            command.calendar_timezone,
            command.maintenance_excludable,
            command.credit_percent_per_breach,
            command.credit_cap_percent,
            command.contract_reference,
        )
    )
    return f"sha256:{hashlib.sha256(material.encode()).hexdigest()}"


def _policy_command(name: str):
    from app.services.owner_commands import OwnerCommandDefinition

    return OwnerCommandDefinition(
        owner="customer.service_level",
        concern="immutable effective-dated SLA policy versions",
        name=name,
    )


def _outcome(record: SlaPolicyVersionRecord, *, replayed: bool) -> PolicyVersionOutcome:
    return PolicyVersionOutcome(
        policy_version_id=record.id,
        policy_key=record.policy_key,
        version=record.version,
        source=SlaPolicySource(record.source),
        effective_from=_utc(record.effective_from) or datetime.now(UTC),
        superseded_version_id=record.supersedes_id,
        # Nothing was superseded on version 1, so there is no instant at which
        # it happened. Reporting effective_from here would invent one.
        superseded_at=(
            _utc(record.effective_from) if record.supersedes_id is not None else None
        ),
        replayed=replayed,
        fingerprint=record.command_fingerprint or "",
    )


def record_policy_version(
    db: Session, command: RecordPolicyVersionCommand
) -> PolicyVersionOutcome:
    """Append a new version, closing the one it supersedes. Never edits terms.

    A period already scored keeps the terms that were in force when it was
    measured — that is the whole reason this table is append-only. Changing
    terms means closing the open version at ``effective_from`` and inserting
    the next, so the two abut exactly and the exclusion constraint stays
    satisfiable.

    Concurrency and replay:

    - the series row set is locked ``FOR UPDATE`` before any decision, so two
      concurrent writers on one scope serialise rather than both reading a
      stale "current version";
    - a replay of the same command fingerprint returns the original outcome
      instead of raising against the row it already created;
    - a different command that loses the race surfaces
      ``concurrent_version_conflict`` rather than a raw database error.

    Backdating behind an already-closed version is refused: it would silently
    rewrite a scored period, which is exactly what superseding ``SlaProfile``
    was meant to stop.
    """

    from sqlalchemy.exc import IntegrityError

    from app.services.events.dispatcher import emit_event
    from app.services.events.types import EventType
    from app.services.owner_commands import execute_owner_command

    def operation() -> PolicyVersionOutcome:
        effective_from = _utc(command.effective_from)
        if effective_from is None:
            raise SlaPolicyError(
                code="customer.service_level.missing_effective_from",
                message="A policy version needs the instant its terms begin.",
            )
        if (
            command.source is not SlaPolicySource.internal_measurement
            and command.availability_target_percent is None
        ):
            raise SlaPolicyError(
                code="customer.service_level.contractual_target_required",
                message=(
                    "A contractual policy must state its availability target; "
                    "only the internal measurement policy may omit one."
                ),
            )

        _validate_scope(db, command)
        policy_key = derive_policy_key(
            command.source,
            subscription_id=command.subscription_id,
            subscriber_id=command.subscriber_id,
            offer_id=command.offer_id,
        )
        fingerprint = _policy_fingerprint(command, policy_key)
        idempotency_key = getattr(command.context, "idempotency_key", None)

        # Idempotency semantics. When a key is supplied it — not the
        # fingerprint — is the identity:
        #   same key + same fingerprint  -> replay the original outcome
        #   same key + different inputs  -> conflict, never a second version
        # Replaying on a fingerprint match alone would report success under a
        # key that was never persisted, leaving that key free to append later
        # with different terms instead of conflicting.
        duplicate_terms = (
            db.query(SlaPolicyVersionRecord)
            .filter(SlaPolicyVersionRecord.command_fingerprint == fingerprint)
            .one_or_none()
        )
        if idempotency_key:
            prior = (
                db.query(SlaPolicyVersionRecord)
                .filter(
                    SlaPolicyVersionRecord.command_idempotency_key == idempotency_key
                )
                .with_for_update()
                .one_or_none()
            )
            if prior is not None:
                if prior.command_fingerprint == fingerprint:
                    return _outcome(prior, replayed=True)
                raise SlaPolicyError(
                    code="customer.service_level.idempotency_conflict",
                    message=(
                        "This idempotency key was already used for different "
                        "policy terms; issue a new key or resend the original "
                        "command unchanged."
                    ),
                    details={"policy_key": prior.policy_key},
                )
            if duplicate_terms is not None:
                # These exact terms exist under a DIFFERENT key. Reporting
                # success would reserve nothing for this key. Retrying cannot
                # help, so this is a conflict, not concurrency.
                raise SlaPolicyError(
                    code="customer.service_level.duplicate_policy_terms",
                    message=(
                        "These exact policy terms were already recorded under "
                        "a different command; resend that command's key to "
                        "replay it, or change the terms."
                    ),
                    details={"policy_key": duplicate_terms.policy_key},
                )
        elif duplicate_terms is not None:
            # No key supplied, so the fingerprint is the only identity there
            # is and replaying on it is the best available contract.
            return _outcome(duplicate_terms, replayed=True)

        # Serialise writers on this series before reading "current version".
        existing = (
            db.query(SlaPolicyVersionRecord)
            .filter(SlaPolicyVersionRecord.policy_key == policy_key)
            .order_by(SlaPolicyVersionRecord.version.desc())
            .with_for_update()
            .all()
        )

        for row in existing:
            row_end = _utc(row.effective_to)
            if row_end is not None and row_end > effective_from:
                raise SlaPolicyError(
                    code="customer.service_level.would_rewrite_closed_period",
                    message=(
                        "Backdating behind a closed version would rewrite a "
                        "period that may already have been scored."
                    ),
                    details={"policy_key": policy_key},
                )

        open_row = next((r for r in existing if r.effective_to is None), None)
        if open_row is not None:
            if (_utc(open_row.effective_from) or effective_from) >= effective_from:
                raise SlaPolicyError(
                    code="customer.service_level.not_after_current",
                    message=(
                        "New terms must begin after the version currently in force."
                    ),
                    details={"policy_key": policy_key},
                )
            open_row.effective_to = effective_from

        record = SlaPolicyVersionRecord(
            policy_key=policy_key,
            version=(existing[0].version + 1) if existing else 1,
            source=command.source.value,
            subscription_id=command.subscription_id,
            subscriber_id=command.subscriber_id,
            offer_id=command.offer_id,
            effective_from=effective_from,
            effective_to=None,
            availability_target_percent=(
                Decimal(str(command.availability_target_percent))
                if command.availability_target_percent is not None
                else None
            ),
            calendar_timezone=command.calendar_timezone,
            maintenance_excludable=command.maintenance_excludable,
            credit_percent_per_breach=(
                Decimal(str(command.credit_percent_per_breach))
                if command.credit_percent_per_breach is not None
                else None
            ),
            credit_cap_percent=(
                Decimal(str(command.credit_cap_percent))
                if command.credit_cap_percent is not None
                else None
            ),
            contract_reference=command.contract_reference,
            established_by=command.context.actor,
            supersedes_id=open_row.id if open_row is not None else None,
            command_fingerprint=fingerprint,
            command_idempotency_key=idempotency_key,
        )
        db.add(record)
        try:
            db.flush()
        except IntegrityError as exc:
            constraint = getattr(
                getattr(getattr(exc, "orig", None), "diag", None),
                "constraint_name",
                None,
            )
            if constraint in _RACE_CONSTRAINTS:
                # A raw collision does not reveal whether the winner wrote the
                # SAME terms or different ones — a concurrent identical retry
                # hits the key constraint exactly as a conflicting one does.
                # So this is retryable; the retry re-reads the winner above and
                # decides replay-or-conflict from evidence.
                raise SlaPolicyError(
                    code="customer.service_level.concurrent_version_conflict",
                    message=(
                        "Another writer recorded a version of this policy "
                        "concurrently; re-read the series and retry."
                    ),
                    details={"policy_key": policy_key, "constraint": constraint},
                ) from exc
            if constraint in _INPUT_CONSTRAINTS:
                raise SlaPolicyError(
                    code="customer.service_level.invalid_policy_version",
                    message="The policy version was rejected by the database.",
                    details={"policy_key": policy_key, "constraint": constraint},
                ) from exc
            # An unrecognised constraint or a driver failure is an unexpected
            # defect. Dressing it as a domain error would hide the bug behind a
            # tidy contract the caller can neither act on nor report.
            raise

        emit_event(
            db,
            EventType.sla_policy_version_recorded,
            {
                "policy_key": record.policy_key,
                "version": record.version,
                "source": record.source,
                "effective_from": effective_from.isoformat(),
                "supersedes_id": str(open_row.id) if open_row is not None else None,
                "fingerprint": fingerprint,
            },
            actor=command.context.actor,
        )
        return _outcome(record, replayed=False)

    return execute_owner_command(
        db,
        definition=_policy_command("record_policy_version"),
        context=command.context,
        operation=operation,
    )
