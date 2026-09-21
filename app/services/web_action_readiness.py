"""Render-context builder for ``ActionReadiness`` (web/mobile presentation).

Same import-cleanliness constraint as ``app.services.action_readiness``: no
``sqlalchemy``, ``app.models``, or ``app.db``, and no ``Session`` reference —
this module only shapes an already-decided verdict for rendering, it never
reads live state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.schemas.status_presentation import StatusPresentation
from app.services.action_readiness import (
    ActionableBlocker,
    ActionCorrelation,
    ActionReadiness,
    BlockerEvidence,
    NextAction,
    ReadinessImpact,
)
from app.services.status_presentation import action_readiness_presentation

Audience = Literal["staff", "customer"]


@dataclass(frozen=True, slots=True)
class ReadinessBlockerRow:
    """One blocker, already audience-selected for rendering."""

    code: str
    owner: str
    message: str
    detail: str | None
    evidence: BlockerEvidence
    repair_label: str | None
    repair_url: str | None
    advisory: bool


@dataclass(frozen=True, slots=True)
class ReadinessPanel:
    """The fully resolved, template-ready projection of one readiness verdict."""

    presentation: StatusPresentation
    headline: str
    blockers: tuple[ReadinessBlockerRow, ...]
    next_actions: tuple[NextAction, ...]
    correlation: ActionCorrelation | None
    show_correlation: bool


def _row(blocker: ActionableBlocker, *, audience: Audience) -> ReadinessBlockerRow:
    message = (
        blocker.customer_message if audience == "customer" else blocker.staff_detail
    )
    detail = None if audience == "customer" else blocker.staff_detail
    repair = blocker.repair
    return ReadinessBlockerRow(
        code=blocker.code,
        owner=blocker.owner,
        message=message,
        detail=detail,
        evidence=blocker.evidence,
        repair_label=repair.label if repair is not None else None,
        repair_url=repair.action_url if repair is not None else None,
        advisory=blocker.impact == ReadinessImpact.advisory,
    )


def readiness_panel(
    readiness: ActionReadiness,
    *,
    audience: Audience = "staff",
) -> ReadinessPanel:
    """Project one ``ActionReadiness`` into a template-ready ``ReadinessPanel``.

    Pure projection: selects ``customer_message`` vs ``staff_detail`` per
    audience and derives ``presentation`` from the readiness state. It decides
    nothing about the underlying action — the owner already decided that.
    """

    presentation = action_readiness_presentation(readiness.state)
    primary = readiness.primary_blocker
    if primary is not None:
        headline = (
            primary.customer_message if audience == "customer" else primary.staff_detail
        )
    else:
        headline = presentation.label

    return ReadinessPanel(
        presentation=presentation,
        headline=headline,
        blockers=tuple(
            _row(blocker, audience=audience) for blocker in readiness.blockers
        ),
        next_actions=readiness.next_actions,
        correlation=readiness.correlation,
        show_correlation=audience == "staff" and readiness.correlation is not None,
    )
