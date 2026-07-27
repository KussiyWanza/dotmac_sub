"""In-memory types for the native workqueue.

The workqueue is a read model: providers project domain rows (support tickets,
team-inbox conversations, work-order mirrors) into :class:`WorkqueueItem`s that
the aggregator ranks into a single queue. Nothing here touches the DB.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID


class ItemKind(enum.StrEnum):
    """Kinds of work sub natively owns."""

    conversation = "conversation"
    ticket = "ticket"
    work_order = "work_order"


class ActionKind(enum.StrEnum):
    open = "open"
    snooze = "snooze"
    clear_snooze = "clear_snooze"
    claim = "claim"
    complete = "complete"


class WorkqueueAudience(enum.StrEnum):
    """Whose work the queue is showing."""

    self_ = "self"
    team = "team"
    org = "org"


AUDIENCE_RANK: dict[WorkqueueAudience, int] = {
    WorkqueueAudience.self_: 0,
    WorkqueueAudience.team: 1,
    WorkqueueAudience.org: 2,
}

Urgency = Literal["critical", "high", "normal", "low"]

#: Actions that mutate the underlying record (as opposed to the personal view).
MUTATING_ACTIONS: frozenset[ActionKind] = frozenset(
    {ActionKind.claim, ActionKind.complete}
)


@dataclass(frozen=True)
class WorkqueueItem:
    """One ranked row in the queue.

    ``score``/``reason``/``urgency`` come from SLA banding (see
    ``scoring_config``). ``priority`` is the legacy numeric priority kept for
    API back-compat (lower = more important) and is only used as a display hint.
    """

    item_kind: ItemKind
    item_id: UUID
    title: str
    subtitle: str | None
    status: str
    priority: int
    score: int
    reason: str
    urgency: Urgency
    happened_at: datetime
    due_at: datetime | None = None
    last_activity_at: datetime | None = None
    subscriber_id: UUID | None = None
    service_team_id: UUID | None = None
    assigned_person_id: UUID | None = None
    url: str | None = None
    actions: tuple[ActionKind, ...] = ()
    can_act: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkqueueSection:
    item_kind: ItemKind
    items: tuple[WorkqueueItem, ...]
    total: int


@dataclass(frozen=True)
class WorkqueueView:
    audience: WorkqueueAudience
    generated_at: datetime
    right_now: tuple[WorkqueueItem, ...]
    sections: tuple[WorkqueueSection, ...]

    @property
    def total(self) -> int:
        return sum(section.total for section in self.sections)
