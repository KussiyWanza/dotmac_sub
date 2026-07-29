"""Customer connection status — outage classifier P4 surface (design §P4/§5).

Project the internal last-mile diagnosis (P2, ``diagnose_last_mile``) and the
area-outage localization (P1, ``localize_outage``) into a **customer-safe**
answer to "what's wrong with my connection?".

The core rule is the **area-vs-last-mile split** (design §5 + §7.3): if this
customer sits under an *active area outage* (their access node is dark for
everyone — P1 ``node_outage``), we tell them "known outage, we're on it" and
**suppress the last-mile blame** ("reboot your router") — it is not their
router's fault, and telling 200 people on a cut splitter to reboot is noise.
Only when there is no area outage do we surface the per-customer verdict.

Customer-safe means: no internal node names/ids, no raw signal values, no
verdict internals, nothing about other customers. ``assess()`` returns the full
internal view (operator-side, used by the notifier for dedup); ``connection_status()``
returns only the projected safe payload.

Relationship to ``selfcare.customer_connection_status``: that older surface (used
by the mobile ``/api/me`` banner) returns a coarse healthy/degraded/outage state
from live-status + operator incidents. This module is the richer P2/P1-aware
successor — it adds the per-customer last-mile verdict and the area-vs-last-mile
blame split — and it reuses the SAME authoritative ``open_incident_for_path``
signal, so the two never disagree about whether an area outage is in progress.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.services.device_operational_status import warmer_is_stale
from app.services.topology import last_mile
from app.services.topology.affected import affected_customers
from app.services.topology.customer_path import resolve_customer_path
from app.services.topology.health_classifier import NODE_OUTAGE, localize_outage
from app.services.topology.outage import open_incident_for_path

if TYPE_CHECKING:
    from app.schemas.status_presentation import StatusPresentation


class ConnectionHealthState(StrEnum):
    """Complete customer-safe connection-health vocabulary."""

    connected = "connected"
    trouble = "trouble"  # a per-customer last-mile problem
    outage = "outage"  # a known area outage above this customer


CONNECTION_HEALTH_STATE_VALUES = tuple(state.value for state in ConnectionHealthState)

# Compatibility aliases for existing callers.
STATE_CONNECTED = ConnectionHealthState.connected.value
STATE_TROUBLE = ConnectionHealthState.trouble.value
STATE_OUTAGE = ConnectionHealthState.outage.value

# An area outage is only declared to the customer when at least this many
# customers behind the dark node were online before (design §7.1 small-N: below
# this we don't claim "area", we fall back to the per-customer verdict). Keeps
# us from telling a lone customer "your area is down" when it's just them.
AREA_MIN_AFFECTED = 3

# Per-verdict customer projection: (state, headline, message, advice).
# ``message`` mirrors P2's customer_message; ``advice`` is the one action to
# take (None when there is nothing for the customer to do — we're fixing it).
_VERDICT_VIEW: dict[str, tuple[str, str, str, str | None]] = {
    last_mile.HEALTHY: (
        STATE_CONNECTED,
        "You're connected",
        "Your connection looks healthy.",
        None,
    ),
    last_mile.POWER: (
        STATE_TROUBLE,
        "Check your equipment",
        "Your equipment appears to have no power.",
        "Check the ONT/router is plugged in and its lights are on.",
    ),
    last_mile.SIGNAL_DEGRADED: (
        STATE_TROUBLE,
        "Weak line signal",
        "We're seeing a weak signal on your line.",
        "We'll arrange a technician to check the line — no action needed from you.",
    ),
    last_mile.ROUTER_OFFLINE: (
        STATE_TROUBLE,
        "Router not responding",
        "Your router isn't responding.",
        "Power it off, wait 30 seconds, then turn it back on.",
    ),
    last_mile.AUTH: (
        STATE_TROUBLE,
        "We're on it",
        "There's an account issue on our side and we're resolving it.",
        None,
    ),
    last_mile.CONFIG: (
        STATE_TROUBLE,
        "Finishing setup",
        "Your equipment is online but isn't connecting yet.",
        None,
    ),
    last_mile.UNKNOWN: (
        STATE_TROUBLE,
        "Checking your connection",
        "We're still diagnosing your connection.",
        None,
    ),
}

_AREA_VIEW = (
    STATE_OUTAGE,
    "Service interruption in your area",
    "There's a known service interruption affecting your area. Our team is "
    "already working on it — you don't need to do anything.",
    None,
)


@dataclass
class Assessment:
    """Internal (operator-side) connection assessment for one subscription.

    ``area_boundary_id`` is the id of the dark access node when this customer is
    under an area outage — used by the notifier to dedup/debounce per boundary.
    It is NEVER projected into the customer payload.
    """

    state: str
    is_area_outage: bool
    area_boundary_id: uuid.UUID | None
    verdict: str
    medium: str | None
    headline: str
    message: str
    advice: str | None
    checked_at: datetime

    @property
    def status_presentation(self) -> StatusPresentation:
        """Cross-client semantics for the already-derived safe state."""
        from app.services.status_presentation import (
            connection_health_status_presentation,
        )

        return connection_health_status_presentation(self.state)


def _area_outage_boundary(
    session: Session, path, now: datetime | None, warm_stale: bool | None = None
) -> uuid.UUID | None:
    """An opaque area-outage boundary id for this customer, or None.

    Two signals, in priority order:
      1. An **operator-declared** open ``OutageIncident`` covering the path — the
         authoritative area outage the admin console + mobile surface already
         use (``open_incident_for_path``). Its id is the boundary key.
      2. Failing that, P1 **inference**: the customer's access node (and its
         downstream) localizes to a ``node_outage`` boundary (all planes dark,
         nobody online) affecting >= ``AREA_MIN_AFFECTED`` customers. Scoped to
         the customer's own node so an unrelated fault elsewhere is never
         mislabelled "your area" (design §7.1 small-N guard).

    Either way, "down but under an area outage" suppresses the last-mile blame.

    ``warm_stale`` carries the warmer's dead-man signal into the P1 inference.
    Without it a node frozen at ``live_status='up'`` (deactivated, checks
    disabled, or warmer dead) classifies ``service_fault`` instead of
    ``node_outage``, so this returns ``None`` and the customer is told to reboot
    their router while the cabinet above them is dark.
    """
    incident = open_incident_for_path(session, path)
    if incident is not None:
        return incident.id

    node = path.node
    if node is None:
        return None
    impact = affected_customers(session, node=node)
    loc = localize_outage(session, impact["node_ids"], now=now, warm_stale=warm_stale)
    if loc is None or loc["class"] != NODE_OUTAGE:
        return None
    if loc["affected_online_before"] < AREA_MIN_AFFECTED:
        return None
    return loc["failure_node"]


def assess(
    session: Session, subscription: Subscription, *, now: datetime | None = None
) -> Assessment:
    """Full internal assessment (last-mile verdict + area-outage overlay)."""
    checked_at = now or datetime.now(UTC)
    # One dead-man read shared by the last-mile diagnosis and the area
    # inference, so the two cannot disagree about warmer freshness.
    warm_stale = warmer_is_stale(checked_at)
    diag = last_mile.diagnose_last_mile(
        session, subscription, now=now, warm_stale=warm_stale
    )
    verdict = diag["verdict"]
    medium = diag["medium"]

    # Healthy short-circuits: proof-of-life beats everything, no area lookup.
    if verdict == last_mile.HEALTHY:
        state, headline, message, advice = _VERDICT_VIEW[last_mile.HEALTHY]
        return Assessment(
            state=state,
            is_area_outage=False,
            area_boundary_id=None,
            verdict=verdict,
            medium=medium,
            headline=headline,
            message=message,
            advice=advice,
            checked_at=checked_at,
        )

    # Down: is there an area outage above this customer? If so, suppress the
    # last-mile blame and present the area message (design §5/§7.3).
    path = resolve_customer_path(session, subscription)
    boundary = _area_outage_boundary(session, path, now, warm_stale)
    if boundary is not None:
        state, headline, message, advice = _AREA_VIEW
        return Assessment(
            state=state,
            is_area_outage=True,
            area_boundary_id=boundary,
            verdict=verdict,
            medium=medium,
            headline=headline,
            message=message,
            advice=advice,
            checked_at=checked_at,
        )

    state, headline, message, advice = _VERDICT_VIEW.get(
        verdict, _VERDICT_VIEW[last_mile.UNKNOWN]
    )
    return Assessment(
        state=state,
        is_area_outage=False,
        area_boundary_id=None,
        verdict=verdict,
        medium=medium,
        headline=headline,
        message=message,
        advice=advice,
        checked_at=checked_at,
    )


def connection_status(
    session: Session, subscription: Subscription, *, now: datetime | None = None
) -> dict:
    """Customer-safe connection status payload (mobile + selfcare web).

    Contains ONLY customer-facing fields — no node ids/names, no raw signal
    values, no verdict internals, nothing about other customers::

        {state, status_presentation, headline, message, advice, medium,
         area_outage, checked_at}
    """
    a = assess(session, subscription, now=now)
    return {
        "state": a.state,
        "status_presentation": a.status_presentation.model_dump(mode="json"),
        "headline": a.headline,
        "message": a.message,
        "advice": a.advice,
        "medium": a.medium,
        "area_outage": a.is_area_outage,
        "checked_at": a.checked_at.isoformat(),
    }
