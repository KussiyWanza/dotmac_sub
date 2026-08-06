"""Team-Inbox-scoped automation policy and participant execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxAutomationActionType,
    InboxAutomationRule,
    InboxAutomationTrigger,
    InboxConversation,
)
from app.services import team_inbox_assignment, team_inbox_operations

OWNER = "communications.team_inbox_automation"


@dataclass(frozen=True)
class InboxAutomationProposal:
    rule_id: UUID
    rule_name: str
    action_type: InboxAutomationActionType
    action_value: Mapping[str, object]


@dataclass(frozen=True)
class InboxAutomationExecutionResult:
    matched_rule_ids: tuple[UUID, ...]
    executed_rule_ids: tuple[UUID, ...]


def conditions_match(
    conditions: Mapping[str, object], conversation: InboxConversation
) -> bool:
    metadata = dict(conversation.metadata_ or {})
    supported = {
        "channel_type": conversation.channel_type,
        "status": conversation.status,
        "priority": conversation.priority,
        "primary_service_team_id": (
            str(conversation.primary_service_team_id)
            if conversation.primary_service_team_id
            else None
        ),
        "contact_resolution_status": (
            metadata.get("contact_resolution", {}).get("status")
            if isinstance(metadata.get("contact_resolution"), dict)
            else None
        ),
    }
    return all(
        key in supported and supported[key] == expected
        for key, expected in conditions.items()
    )


def evaluate_rules(
    db: Session,
    *,
    conversation: InboxConversation,
    trigger: InboxAutomationTrigger,
) -> tuple[InboxAutomationProposal, ...]:
    rows = (
        db.query(InboxAutomationRule)
        .filter(InboxAutomationRule.is_active.is_(True))
        .filter(InboxAutomationRule.trigger == trigger)
        .order_by(InboxAutomationRule.sort_order, InboxAutomationRule.created_at)
        .all()
    )
    return tuple(
        InboxAutomationProposal(
            rule_id=row.id,
            rule_name=row.name,
            action_type=row.action_type,
            action_value=dict(row.action_value or {}),
        )
        for row in rows
        if conditions_match(row.conditions or {}, conversation)
    )


def execute_matching_rules(
    db: Session,
    *,
    conversation: InboxConversation,
    trigger: InboxAutomationTrigger,
    actor_person_id: UUID | None = None,
) -> InboxAutomationExecutionResult:
    proposals = evaluate_rules(db, conversation=conversation, trigger=trigger)
    executed: list[UUID] = []
    for proposal in proposals:
        value = proposal.action_value
        if proposal.action_type is InboxAutomationActionType.assign_agent:
            team_id = (
                value.get("service_team_id") or conversation.primary_service_team_id
            )
            person_id = value.get("person_id")
            if team_id and person_id:
                result = team_inbox_assignment.assign_conversation_to_agent(
                    db,
                    conversation=conversation,
                    service_team_id=str(team_id),
                    person_id=str(person_id),
                    assigned_by_person_id=actor_person_id,
                    reason=f"Inbox automation: {proposal.rule_name}",
                )
                if result.kind == "assigned":
                    executed.append(proposal.rule_id)
        elif proposal.action_type is InboxAutomationActionType.auto_assign:
            team_id = (
                value.get("service_team_id") or conversation.primary_service_team_id
            )
            if team_id:
                result = team_inbox_assignment.assign_conversation_to_available_agent(
                    db,
                    conversation=conversation,
                    service_team_id=str(team_id),
                    assigned_by_person_id=actor_person_id,
                    reason=f"Inbox automation: {proposal.rule_name}",
                )
                if result.kind in {"assigned", "queued"}:
                    executed.append(proposal.rule_id)
        elif proposal.action_type is InboxAutomationActionType.add_tag:
            tag = str(value.get("tag") or "").strip()
            if tag:
                label = team_inbox_operations.create_or_reactivate_label(db, name=tag)
                team_inbox_operations.apply_label(
                    db,
                    conversation=conversation,
                    label_id=label.id,
                    applied_by_person_id=actor_person_id,
                )
                executed.append(proposal.rule_id)
        if proposal.rule_id in executed:
            rule = db.get(InboxAutomationRule, proposal.rule_id)
            if rule is not None:
                rule.last_executed_at = datetime.now(UTC)
    db.flush()
    return InboxAutomationExecutionResult(
        matched_rule_ids=tuple(item.rule_id for item in proposals),
        executed_rule_ids=tuple(executed),
    )
