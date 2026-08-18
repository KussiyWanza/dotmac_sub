"""Tunable SLA bands, scores and ordering for workqueue ranking.

Every number the ranker uses lives here, and every threshold is overridable via
environment variables (``WORKQUEUE_*``) so ops can retune urgency without a code
change. Providers never hardcode a score: they ask the config for a band.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

from app.services.workqueue.types import ItemKind, Urgency

SLA_BREACH = "sla_breach"
SLA_IMMINENT = "sla_imminent"
SLA_SOON = "sla_soon"


@dataclass(frozen=True)
class SlaBands:
    """Score bands keyed off the seconds remaining until an item's SLA is due.

    Boundaries are inclusive on the band's upper edge: an item due in exactly
    ``imminent_seconds`` is *imminent*, one second later it is *soon*. At or past
    the due instant (``<= 0``) it is a *breach*.
    """

    imminent_seconds: int
    soon_seconds: int
    breach_score: int = 100
    imminent_score: int = 90
    soon_score: int = 75

    def band(self, seconds_to_due: float) -> tuple[str, int] | None:
        """Return ``(reason, score)`` for the SLA band, or ``None`` if outside."""
        if seconds_to_due <= 0:
            return (SLA_BREACH, self.breach_score)
        if seconds_to_due <= self.imminent_seconds:
            return (SLA_IMMINENT, self.imminent_score)
        if seconds_to_due <= self.soon_seconds:
            return (SLA_SOON, self.soon_score)
        return None


def _default_ticket_scores() -> dict[str, int]:
    return {
        "priority_urgent": 80,
        "priority_high": 60,
        "awaiting_triage": 55,
        "in_queue": 30,
    }


def _default_conversation_scores() -> dict[str, int]:
    return {
        "awaiting_reply": 65,
        "priority_high": 55,
        "unassigned": 45,
        "in_inbox": 35,
    }


def _default_work_order_scores() -> dict[str, int]:
    return {
        "priority_urgent": 75,
        "in_progress": 50,
        "unassigned": 45,
        "scheduled": 40,
    }


@dataclass(frozen=True)
class WorkqueueScoringConfig:
    # SLA bands per source.
    ticket_sla: SlaBands = field(
        default_factory=lambda: SlaBands(
            imminent_seconds=15 * 60, soon_seconds=2 * 3600
        )
    )
    project_sla: SlaBands = field(
        default_factory=lambda: SlaBands(
            imminent_seconds=60 * 60, soon_seconds=24 * 3600
        )
    )
    conversation_sla: SlaBands = field(
        default_factory=lambda: SlaBands(imminent_seconds=5 * 60, soon_seconds=30 * 60)
    )
    work_order_sla: SlaBands = field(
        default_factory=lambda: SlaBands(
            imminent_seconds=30 * 60, soon_seconds=4 * 3600
        )
    )

    # A conversation's response SLA is "reply within N seconds of the last
    # inbound message" — team-inbox has no first-response SLA policy table.
    conversation_response_target_seconds: int = 15 * 60

    # An inbox conversation's numeric priority (lower = hotter, default 100) at
    # or below which it counts as elevated.
    conversation_high_priority_at: int = 50

    ticket_scores: dict[str, int] = field(default_factory=_default_ticket_scores)
    conversation_scores: dict[str, int] = field(
        default_factory=_default_conversation_scores
    )
    work_order_scores: dict[str, int] = field(
        default_factory=_default_work_order_scores
    )

    # Urgency bands (score floors).
    urgency_critical_at: int = 90
    urgency_high_at: int = 70
    urgency_normal_at: int = 40

    # Per-provider fetch limit and hero ("right now") band size.
    provider_limit: int = 50
    hero_band_size: int = 6

    # Stable tie-break / section ordering.
    kind_order: tuple[ItemKind, ...] = (
        ItemKind.conversation,
        ItemKind.ticket,
        ItemKind.project,
        ItemKind.work_order,
    )

    def urgency_for_score(self, score: int) -> Urgency:
        if score >= self.urgency_critical_at:
            return "critical"
        if score >= self.urgency_high_at:
            return "high"
        if score >= self.urgency_normal_at:
            return "normal"
        return "low"

    def kind_rank(self, kind: ItemKind) -> int:
        try:
            return self.kind_order.index(kind)
        except ValueError:  # pragma: no cover — a kind not in the order tuple
            return len(self.kind_order)


DEFAULT_SCORING_CONFIG = WorkqueueScoringConfig()


def _env_int(name: str, fallback: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def _bands_from_env(prefix: str, base: SlaBands) -> SlaBands:
    return SlaBands(
        imminent_seconds=_env_int(f"{prefix}_IMMINENT_SECONDS", base.imminent_seconds),
        soon_seconds=_env_int(f"{prefix}_SOON_SECONDS", base.soon_seconds),
        breach_score=_env_int(f"{prefix}_BREACH_SCORE", base.breach_score),
        imminent_score=_env_int(f"{prefix}_IMMINENT_SCORE", base.imminent_score),
        soon_score=_env_int(f"{prefix}_SOON_SCORE", base.soon_score),
    )


def load_scoring_config() -> WorkqueueScoringConfig:
    """Build the scoring config, applying ``WORKQUEUE_*`` env overrides.

    Read per call (not cached) so a redeploy — or a test's monkeypatched env —
    picks the new thresholds up without process-global state.
    """
    base = DEFAULT_SCORING_CONFIG
    return replace(
        base,
        ticket_sla=_bands_from_env("WORKQUEUE_TICKET_SLA", base.ticket_sla),
        project_sla=_bands_from_env("WORKQUEUE_PROJECT_SLA", base.project_sla),
        conversation_sla=_bands_from_env(
            "WORKQUEUE_CONVERSATION_SLA", base.conversation_sla
        ),
        work_order_sla=_bands_from_env("WORKQUEUE_WORK_ORDER_SLA", base.work_order_sla),
        conversation_response_target_seconds=_env_int(
            "WORKQUEUE_CONVERSATION_RESPONSE_TARGET_SECONDS",
            base.conversation_response_target_seconds,
        ),
        provider_limit=_env_int("WORKQUEUE_PROVIDER_LIMIT", base.provider_limit),
        hero_band_size=_env_int("WORKQUEUE_HERO_BAND_SIZE", base.hero_band_size),
        urgency_critical_at=_env_int(
            "WORKQUEUE_URGENCY_CRITICAL_AT", base.urgency_critical_at
        ),
        urgency_high_at=_env_int("WORKQUEUE_URGENCY_HIGH_AT", base.urgency_high_at),
        urgency_normal_at=_env_int(
            "WORKQUEUE_URGENCY_NORMAL_AT", base.urgency_normal_at
        ),
    )
