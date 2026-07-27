"""Owner-produced web projection for the native agent workqueue."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.services import display_format, service_team_lifecycle
from app.services.action_forms import (
    ActionConfirmation,
    ActionForm,
    ActionHiddenValue,
    ActionTone,
)
from app.services.workqueue.aggregator import build_workqueue
from app.services.workqueue.commands import action_state_fingerprint
from app.services.workqueue.permissions import WorkqueuePrincipal
from app.services.workqueue.scope import get_workqueue_scope
from app.services.workqueue.snooze import active_snoozed_ids
from app.services.workqueue.types import (
    ActionKind,
    ItemKind,
    WorkqueueAudience,
    WorkqueueItem,
)

_KIND_LABELS = {
    ItemKind.conversation: "Inbox",
    ItemKind.ticket: "Tickets",
    ItemKind.work_order: "Work orders",
}
_URGENCY_LABELS = {
    "critical": "Critical",
    "high": "High",
    "normal": "Normal",
    "low": "Low",
}
_URGENCY_TONES = {
    "critical": "negative",
    "high": "warning",
    "normal": "info",
    "low": "neutral",
}
_REASON_LABELS = {
    "sla_breach": "SLA breached",
    "sla_critical": "SLA deadline imminent",
    "sla_high": "SLA deadline approaching",
    "sla_warning": "SLA needs attention",
    "awaiting_reply": "Customer awaiting reply",
    "awaiting_triage": "Awaiting triage",
    "priority_urgent": "Urgent priority",
    "priority_high": "High priority",
    "unassigned": "Unassigned",
    "in_inbox": "Active Inbox thread",
    "in_queue": "Active support ticket",
    "scheduled_soon": "Scheduled soon",
    "overdue": "Overdue",
}


@dataclass(frozen=True)
class WorkqueueTeamOption:
    team_id: UUID
    label: str


@dataclass(frozen=True)
class WorkqueueSnoozeOption:
    value: str
    label: str


@dataclass(frozen=True)
class WorkqueueRow:
    item_kind: ItemKind
    item_id: UUID
    kind_label: str
    title: str
    subtitle: str | None
    status_label: str
    urgency_label: str
    urgency_tone: str
    reason_label: str
    happened_at_label: str
    due_at_label: str
    score: int
    url: str | None
    is_snoozed: bool
    can_claim: bool
    can_complete: bool
    can_snooze: bool
    claim_action: ActionForm | None
    complete_action: ActionForm | None
    snooze_options: tuple[WorkqueueSnoozeOption, ...]
    snooze_request_id: UUID
    clear_snooze_request_id: UUID


@dataclass(frozen=True)
class WorkqueueSectionProjection:
    item_kind: ItemKind
    label: str
    rows: tuple[WorkqueueRow, ...]
    total: int


@dataclass(frozen=True)
class WorkqueuePageProjection:
    audience: WorkqueueAudience
    total: int
    generated_at_label: str
    right_now: tuple[WorkqueueRow, ...]
    sections: tuple[WorkqueueSectionProjection, ...]
    team_options: tuple[WorkqueueTeamOption, ...]
    selected_team_id: UUID | None
    include_snoozed: bool


def _status_label(value: str) -> str:
    return str(value or "unknown").replace("_", " ").strip().title()


def _reason_label(value: str) -> str:
    return _REASON_LABELS.get(
        value,
        str(value or "Ranked operational work").replace("_", " ").strip().title(),
    )


def _snooze_options(item_kind: ItemKind) -> tuple[WorkqueueSnoozeOption, ...]:
    options = [
        WorkqueueSnoozeOption("30_minutes", "30 minutes"),
        WorkqueueSnoozeOption("2_hours", "2 hours"),
        WorkqueueSnoozeOption("1_day", "24 hours"),
    ]
    if item_kind is ItemKind.conversation:
        options.append(WorkqueueSnoozeOption("next_reply", "Until next reply"))
    options.append(WorkqueueSnoozeOption("indefinite", "Until I restore it"))
    return tuple(options)


def _action_hidden_values(
    item: WorkqueueItem,
    action: ActionKind,
    *,
    audience: WorkqueueAudience,
    service_team_id: UUID | None,
    include_snoozed: bool,
) -> tuple[ActionHiddenValue, ...]:
    values = [
        ActionHiddenValue("item_kind", item.item_kind.value),
        ActionHiddenValue("item_id", str(item.item_id)),
        ActionHiddenValue("request_id", str(uuid4())),
        ActionHiddenValue("audience", audience.value),
        ActionHiddenValue(
            "include_snoozed",
            "true" if include_snoozed else "false",
        ),
        ActionHiddenValue(
            "state_fingerprint",
            action_state_fingerprint(item, action),
        ),
    ]
    if service_team_id is not None:
        values.append(ActionHiddenValue("service_team_id", str(service_team_id)))
    return tuple(values)


def _claim_action(
    item: WorkqueueItem,
    *,
    audience: WorkqueueAudience,
    service_team_id: UUID | None,
    include_snoozed: bool,
) -> ActionForm:
    return ActionForm(
        key=f"workqueue.claim.{item.item_kind.value}.{item.item_id}",
        title="Claim this item",
        description="Assign this source item to your authenticated staff identity.",
        action_url="/admin/workqueue/claim",
        submit_label="Claim for me",
        fields=(),
        hidden_values=_action_hidden_values(
            item,
            ActionKind.claim,
            audience=audience,
            service_team_id=service_team_id,
            include_snoozed=include_snoozed,
        ),
        tone=ActionTone.neutral,
        impact=(
            "Assignment changes through the source owner. Customer and source "
            "lifecycle state do not change."
        ),
    )


def _complete_action(
    item: WorkqueueItem,
    *,
    audience: WorkqueueAudience,
    service_team_id: UUID | None,
    include_snoozed: bool,
) -> ActionForm:
    source_label = _KIND_LABELS[item.item_kind].rstrip("s").lower()
    return ActionForm(
        key=f"workqueue.complete.{item.item_kind.value}.{item.item_id}",
        title=f"Resolve this {source_label}",
        description=("Complete the item through its canonical source lifecycle owner."),
        action_url="/admin/workqueue/complete",
        submit_label="Complete through owner",
        fields=(),
        hidden_values=_action_hidden_values(
            item,
            ActionKind.complete,
            audience=audience,
            service_team_id=service_team_id,
            include_snoozed=include_snoozed,
        ),
        tone=ActionTone.positive,
        impact=(
            f"The {_KIND_LABELS[item.item_kind].lower()} state will become "
            "resolved and the item will leave the active queue. Owner-defined "
            "lifecycle consequences still apply."
        ),
        confirmation=ActionConfirmation(
            title="Confirm resolution",
            message=(
                "I reviewed the current source state and understand this is a "
                "customer-visible lifecycle change."
            ),
        ),
    )


def _row(
    db: Session,
    item: WorkqueueItem,
    *,
    snoozed_ids: dict[ItemKind, set[UUID]],
    audience: WorkqueueAudience,
    service_team_id: UUID | None,
    include_snoozed: bool,
) -> WorkqueueRow:
    is_snoozed = item.item_id in snoozed_ids[item.item_kind]
    actions = set(item.actions)
    can_claim = ActionKind.claim in actions and item.can_act
    can_complete = ActionKind.complete in actions and item.can_act
    return WorkqueueRow(
        item_kind=item.item_kind,
        item_id=item.item_id,
        kind_label=_KIND_LABELS[item.item_kind],
        title=item.title,
        subtitle=item.subtitle,
        status_label=_status_label(item.status),
        urgency_label=_URGENCY_LABELS[item.urgency],
        urgency_tone=_URGENCY_TONES[item.urgency],
        reason_label=_reason_label(item.reason),
        happened_at_label=display_format.format_timestamp(item.happened_at, db),
        due_at_label=display_format.format_timestamp(item.due_at, db),
        score=item.score,
        url=item.url,
        is_snoozed=is_snoozed,
        can_claim=can_claim,
        can_complete=can_complete,
        can_snooze=ActionKind.snooze in actions,
        claim_action=(
            _claim_action(
                item,
                audience=audience,
                service_team_id=service_team_id,
                include_snoozed=include_snoozed,
            )
            if can_claim
            else None
        ),
        complete_action=(
            _complete_action(
                item,
                audience=audience,
                service_team_id=service_team_id,
                include_snoozed=include_snoozed,
            )
            if can_complete
            else None
        ),
        snooze_options=_snooze_options(item.item_kind),
        snooze_request_id=uuid4(),
        clear_snooze_request_id=uuid4(),
    )


def build_page(
    db: Session,
    principal: WorkqueuePrincipal,
    *,
    requested_audience: str | None = None,
    service_team_id: UUID | None = None,
    include_snoozed: bool = False,
) -> WorkqueuePageProjection:
    """Build the full queue and its owner-controlled display semantics."""

    scope = get_workqueue_scope(
        db,
        principal,
        requested_audience=requested_audience,
        service_team_id=service_team_id,
    )
    view = build_workqueue(
        db,
        principal,
        requested_audience=scope.audience.value,
        service_team_id=service_team_id,
        include_snoozed=include_snoozed,
    )
    snoozed_ids = active_snoozed_ids(db, user_id=principal.person_id)
    rows_by_id = {
        (item.item_kind, item.item_id): _row(
            db,
            item,
            snoozed_ids=snoozed_ids,
            audience=view.audience,
            service_team_id=service_team_id,
            include_snoozed=include_snoozed,
        )
        for section in view.sections
        for item in section.items
    }
    active_teams = service_team_lifecycle.list_active_team_options(db)
    if not scope.is_org_wide:
        allowed_team_ids = scope.accessible_service_team_ids
        active_teams = tuple(
            option for option in active_teams if option[0] in allowed_team_ids
        )
    return WorkqueuePageProjection(
        audience=view.audience,
        total=view.total,
        generated_at_label=display_format.format_timestamp(view.generated_at, db),
        right_now=tuple(
            rows_by_id[(item.item_kind, item.item_id)] for item in view.right_now
        ),
        sections=tuple(
            WorkqueueSectionProjection(
                item_kind=section.item_kind,
                label=_KIND_LABELS[section.item_kind],
                rows=tuple(
                    rows_by_id[(item.item_kind, item.item_id)] for item in section.items
                ),
                total=section.total,
            )
            for section in view.sections
        ),
        team_options=tuple(
            WorkqueueTeamOption(team_id=team_id, label=label)
            for team_id, label in active_teams
        ),
        selected_team_id=service_team_id,
        include_snoozed=include_snoozed,
    )
