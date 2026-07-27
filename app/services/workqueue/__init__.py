"""Native workqueue: pluggable providers → scoped, SLA-ranked queue.

Layout:
* ``types``          — the read-model dataclasses (no DB).
* ``scoring_config`` — every threshold/score, env-overridable.
* ``permissions``    — principal, audience resolution, ``can_act_on_item``.
* ``scope``          — which teams/people a principal may see.
* ``providers/``     — one module per item source; self-registering.
* ``aggregator``     — merges providers into one ranked view.
* ``commands``       — typed action coordinator and personal snooze owner.
* ``snooze``         — transaction-neutral personal snooze participants.
* ``events``         — realtime change notifications.
"""

from __future__ import annotations

from app.services.workqueue.aggregator import (
    build_workqueue,
    collect_items,
    list_workqueue,
)
from app.services.workqueue.commands import (
    SnoozeMode,
    WorkqueueActionCommand,
    WorkqueueActionError,
    WorkqueueActionOutcome,
    WorkqueueSnoozeSnapshot,
    execute_action,
)
from app.services.workqueue.permissions import (
    WORKQUEUE_ACT_PERMISSION,
    WORKQUEUE_VIEW_PERMISSION,
    WorkqueuePermissionError,
    WorkqueuePrincipal,
    can_act_on_item,
    has_workqueue_view,
    principal_from_auth,
    resolve_audience,
)
from app.services.workqueue.providers import (
    all_providers,
    load_builtin_providers,
    register,
)
from app.services.workqueue.scope import WorkqueueScope, get_workqueue_scope
from app.services.workqueue.scoring_config import (
    DEFAULT_SCORING_CONFIG,
    SlaBands,
    WorkqueueScoringConfig,
    load_scoring_config,
)
from app.services.workqueue.snooze import (
    active_snoozed_ids,
    clear_snooze,
    clear_snooze_committed,
    release_until_next_reply,
    snooze_item,
    snooze_item_committed,
)
from app.services.workqueue.types import (
    ActionKind,
    ItemKind,
    WorkqueueAudience,
    WorkqueueItem,
    WorkqueueSection,
    WorkqueueView,
)

__all__ = [
    "DEFAULT_SCORING_CONFIG",
    "WORKQUEUE_ACT_PERMISSION",
    "WORKQUEUE_VIEW_PERMISSION",
    "ActionKind",
    "ItemKind",
    "SlaBands",
    "SnoozeMode",
    "WorkqueueAudience",
    "WorkqueueActionCommand",
    "WorkqueueActionError",
    "WorkqueueActionOutcome",
    "WorkqueueItem",
    "WorkqueuePermissionError",
    "WorkqueuePrincipal",
    "WorkqueueScope",
    "WorkqueueScoringConfig",
    "WorkqueueSection",
    "WorkqueueSnoozeSnapshot",
    "WorkqueueView",
    "active_snoozed_ids",
    "all_providers",
    "build_workqueue",
    "can_act_on_item",
    "clear_snooze",
    "clear_snooze_committed",
    "collect_items",
    "execute_action",
    "get_workqueue_scope",
    "has_workqueue_view",
    "list_workqueue",
    "load_builtin_providers",
    "load_scoring_config",
    "principal_from_auth",
    "register",
    "release_until_next_reply",
    "resolve_audience",
    "snooze_item",
    "snooze_item_committed",
]
