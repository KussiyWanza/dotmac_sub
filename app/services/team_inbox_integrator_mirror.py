"""Read-only parity between an Integrator envelope and Sub's own receiver.

Owner: ``communications.team_inbox_integrator_mirror``. It owns the comparison
and its cutover-readiness verdict, and nothing else. It writes no row, mutates
no observation, delegates to no consequence owner, and cannot authorize a
cutover by itself. It is the evidence the cutover waits on.

## The question this answers

Sub already records inbound messages through provider webhooks it verifies
itself. The Integrator will record the same upstream events through
``app.api.integrator_observations``. Before any provider callback is repointed,
somebody has to be able to answer, with evidence rather than confidence:

    *for the same upstream provider event, does the Integrator produce the
    observation Sub's own receiver already produces?*

This module answers it field by field and names the disagreements. Two
observations that agree on identity and on every normalized field mean the
producers are interchangeable for that event. Anything else is a named,
countable reason the cutover is not yet safe.

## The finding that actually matters: identity shape

The dangerous disagreement is not a differing ``contact_name``. It is a
differing *identity*. ``uq_inbox_provider_observations_identity`` is
``(provider, provider_account_scope, provider_event_id)``. If the Integrator
computes any of those three differently from the webhook receiver, then during
the overlap window the same WhatsApp message is TWO observations, processed
twice, and the customer sees their message twice in the Team Inbox. Worse, it
would not look like a bug in either producer — each is internally consistent.

So the harness does two lookups, not one:

1. by the exact identity the envelope normalizes to — the happy path;
2. failing that, by ``(provider, external_message_id, observation_kind)`` —
   which finds a row Sub recorded for the *same upstream event* under a
   *different identity*.

The second lookup finding a row is the ``identity_shape_mismatch`` verdict, and
it is the single most valuable thing this module can report.

## Same id, different content is a collision, never a duplicate

If the identity matches but Sub's domain fingerprint does not, the two
producers disagree about what the provider actually said. That is a collision:
deduplicating it would silently discard real content on the assumption that one
producer is right. It is reported as ``collision``, it is blocking, and it
escalates. The live port refuses such a delivery outright — the observation
owner already raises ``provider_event_identity_collision`` for exactly this —
so the harness and the port agree on the rule rather than each inventing one.

## Safe to run against production-derived data

Aggregate and per-field verdicts only; no message body, no contact address, and
no customer name ever enters a report. A disagreement names the FIELD and,
for identity-shaped fields only, the two values — because an operator cannot
act on "provider_event_id differs" without seeing how.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.team_inbox import InboxProviderObservation
from app.schemas.integrator_observation import IntegratorObservationEnvelope
from app.services.team_inbox_integrator_envelope import (
    NormalizedEnvelope,
    normalize,
    observation_context,
)
from app.services.team_inbox_observations import (
    RecordProviderObservationCommand,
    normalized_payload,
    observation_fingerprint,
)

OWNER = "communications.team_inbox_integrator_mirror"

#: Stable verdicts, safe to put in a runbook or a gate. Order is fixed so two
#: runs over one population produce identical output.
VERDICT_AGREES = "agrees"
VERDICT_FIELD_DISAGREEMENT = "field_disagreement"
VERDICT_IDENTITY_SHAPE_MISMATCH = "identity_shape_mismatch"
VERDICT_COLLISION = "collision"
VERDICT_NO_COUNTERPART = "no_counterpart"

BLOCKING_COLLISION = "domain_fingerprint_collision"
BLOCKING_IDENTITY_SHAPE = "identity_shape_mismatch"
BLOCKING_FIELD_DISAGREEMENT = "normalized_field_disagreement"
BLOCKING_NO_COUNTERPART = "no_counterpart_observation"

#: Fields whose values are safe to name in a report because they are provider
#: and Sub identifiers, not customer content. Every other disagreement reports
#: the field name alone.
_VALUE_SAFE_FIELDS = frozenset(
    {
        "provider",
        "provider_account_scope",
        "provider_event_id",
        "observation_kind",
        "channel_type",
        "external_message_id",
        "external_thread_id",
        "observed_at",
        "payload_fingerprint",
    }
)


@dataclass(frozen=True, slots=True)
class FieldDisagreement:
    """One named field on which the two producers do not agree."""

    field: str
    integrator: str | None
    sub: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {"field": self.field, "integrator": self.integrator, "sub": self.sub}


@dataclass(frozen=True, slots=True)
class ObservationMirrorReport:
    """Whether one Integrator envelope matches what Sub's receiver recorded."""

    verdict: str
    identity: tuple[str, str, str]
    counterpart_identity: tuple[str, str, str] | None
    disagreements: tuple[FieldDisagreement, ...]

    @property
    def agrees(self) -> bool:
        return self.verdict == VERDICT_AGREES

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        """Every reason this event is not yet proof the producers agree."""

        if self.verdict == VERDICT_AGREES:
            return ()
        if self.verdict == VERDICT_COLLISION:
            return (BLOCKING_COLLISION,)
        if self.verdict == VERDICT_IDENTITY_SHAPE_MISMATCH:
            # Identity is wrong AND the fields may be too; report both so a
            # reviewer never reads a clean field list as reassurance.
            return (
                (BLOCKING_IDENTITY_SHAPE, BLOCKING_FIELD_DISAGREEMENT)
                if self.disagreements
                else (BLOCKING_IDENTITY_SHAPE,)
            )
        if self.verdict == VERDICT_NO_COUNTERPART:
            return (BLOCKING_NO_COUNTERPART,)
        return (BLOCKING_FIELD_DISAGREEMENT,)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "identity": list(self.identity),
            "counterpart_identity": (
                list(self.counterpart_identity) if self.counterpart_identity else None
            ),
            "blocking_reasons": list(self.blocking_reasons),
            "disagreements": [item.as_dict() for item in self.disagreements],
            "agrees": self.agrees,
        }


@dataclass(frozen=True, slots=True)
class MirrorPopulationReport:
    """Aggregate parity over a batch of envelopes, PII-free by construction."""

    compared: int
    agreeing: int
    verdict_counts: dict[str, int]
    blocking_reason_counts: dict[str, int]
    disagreeing_fields: dict[str, int]

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        return tuple(sorted(self.blocking_reason_counts))

    @property
    def is_cutover_safe(self) -> bool:
        """A population proves nothing if it is empty; absence is not parity."""

        return self.compared > 0 and not self.blocking_reason_counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "compared": self.compared,
            "agreeing": self.agreeing,
            "verdict_counts": dict(sorted(self.verdict_counts.items())),
            "blocking_reason_counts": dict(sorted(self.blocking_reason_counts.items())),
            "disagreeing_fields": dict(sorted(self.disagreeing_fields.items())),
            "blocking_reasons": list(self.blocking_reasons),
            "is_cutover_safe": self.is_cutover_safe,
        }


def _as_utc(value: datetime) -> datetime:
    return (
        value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    ).astimezone(UTC)


def _row_identity(row: InboxProviderObservation) -> tuple[str, str, str]:
    return (row.provider, row.provider_account_scope, row.provider_event_id)


def _find_by_identity(
    db: Session, identity: tuple[str, str, str]
) -> InboxProviderObservation | None:
    provider, scope, event_id = identity
    return db.execute(
        select(InboxProviderObservation).where(
            InboxProviderObservation.provider == provider,
            InboxProviderObservation.provider_account_scope == scope,
            InboxProviderObservation.provider_event_id == event_id,
        )
    ).scalar_one_or_none()


def _find_same_upstream_event(
    db: Session, command: RecordProviderObservationCommand
) -> InboxProviderObservation | None:
    """Find a row Sub recorded for this upstream event under any identity.

    Ordered by ``recorded_at`` so a repeat run over one snapshot returns the
    same row rather than whichever the planner happened to emit first.
    """

    external_message_id = str(command.external_message_id or "").strip()
    if not external_message_id:
        return None
    return db.execute(
        select(InboxProviderObservation)
        .where(
            InboxProviderObservation.provider == command.provider.value,
            InboxProviderObservation.external_message_id == external_message_id,
            InboxProviderObservation.observation_kind == command.kind.value,
        )
        .order_by(
            InboxProviderObservation.recorded_at.asc(),
            InboxProviderObservation.id.asc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def _compare_payloads(
    integrator: dict[str, Any], sub: dict[str, Any]
) -> tuple[FieldDisagreement, ...]:
    """Field-by-field over the union of keys, so an absent key is a finding."""

    disagreements: list[FieldDisagreement] = []
    for key in sorted(set(integrator) | set(sub)):
        left, right = integrator.get(key), sub.get(key)
        if left == right:
            continue
        disagreements.append(
            FieldDisagreement(
                field=f"normalized_payload.{key}",
                integrator=None if left is None else "<differs>",
                sub=None if right is None else "<differs>",
            )
        )
    return tuple(disagreements)


def _compare(
    normalized: NormalizedEnvelope, row: InboxProviderObservation
) -> tuple[FieldDisagreement, ...]:
    command = normalized.command
    left: dict[str, str | None] = {
        "provider": command.provider.value,
        "provider_account_scope": command.provider_account_scope,
        "provider_event_id": command.provider_event_id,
        "observation_kind": command.kind.value,
        "channel_type": command.channel_type.value,
        "external_message_id": command.external_message_id,
        "observed_at": _as_utc(command.observed_at).isoformat(),
        "payload_fingerprint": observation_fingerprint(command),
    }
    right: dict[str, str | None] = {
        "provider": row.provider,
        "provider_account_scope": row.provider_account_scope,
        "provider_event_id": row.provider_event_id,
        "observation_kind": row.observation_kind,
        "channel_type": row.channel_type,
        "external_message_id": row.external_message_id,
        "observed_at": _as_utc(row.observed_at).isoformat(),
        "payload_fingerprint": row.payload_fingerprint,
    }
    disagreements = [
        FieldDisagreement(
            field=field,
            integrator=left[field] if field in _VALUE_SAFE_FIELDS else "<differs>",
            sub=right[field] if field in _VALUE_SAFE_FIELDS else "<differs>",
        )
        for field in sorted(left)
        if left[field] != right[field]
    ]
    disagreements.extend(
        _compare_payloads(
            normalized_payload(command.payload), dict(row.normalized_payload)
        )
    )
    return tuple(disagreements)


def compare_envelope(
    db: Session, *, envelope: IntegratorObservationEnvelope
) -> ObservationMirrorReport:
    """Compare one Integrator envelope against what Sub's receiver recorded.

    Read-only: this normalizes the envelope exactly as the live port would,
    then reads. It records nothing, so it is safe to run in parallel with the
    real receiver and safe to run twice.
    """

    normalized = normalize(envelope, context=observation_context(envelope))
    identity = normalized.identity

    row = _find_by_identity(db, identity)
    if row is not None:
        disagreements = _compare(normalized, row)
        verdict = (
            VERDICT_AGREES
            if not disagreements
            else (
                VERDICT_COLLISION
                if any(item.field == "payload_fingerprint" for item in disagreements)
                else VERDICT_FIELD_DISAGREEMENT
            )
        )
        return ObservationMirrorReport(
            verdict=verdict,
            identity=identity,
            counterpart_identity=_row_identity(row),
            disagreements=disagreements,
        )

    sibling = _find_same_upstream_event(db, normalized.command)
    if sibling is not None:
        return ObservationMirrorReport(
            verdict=VERDICT_IDENTITY_SHAPE_MISMATCH,
            identity=identity,
            counterpart_identity=_row_identity(sibling),
            disagreements=_compare(normalized, sibling),
        )

    return ObservationMirrorReport(
        verdict=VERDICT_NO_COUNTERPART,
        identity=identity,
        counterpart_identity=None,
        disagreements=(),
    )


def compare_population(
    db: Session, *, envelopes: tuple[IntegratorObservationEnvelope, ...]
) -> MirrorPopulationReport:
    """Aggregate parity across a batch, for a reviewable cutover-gate paste."""

    verdict_counts: dict[str, int] = {}
    blocking_counts: dict[str, int] = {}
    field_counts: dict[str, int] = {}
    agreeing = 0
    for envelope in envelopes:
        report = compare_envelope(db, envelope=envelope)
        verdict_counts[report.verdict] = verdict_counts.get(report.verdict, 0) + 1
        if report.agrees:
            agreeing += 1
        for reason in report.blocking_reasons:
            blocking_counts[reason] = blocking_counts.get(reason, 0) + 1
        for item in report.disagreements:
            field_counts[item.field] = field_counts.get(item.field, 0) + 1
    return MirrorPopulationReport(
        compared=len(envelopes),
        agreeing=agreeing,
        verdict_counts=verdict_counts,
        blocking_reason_counts=blocking_counts,
        disagreeing_fields=field_counts,
    )
