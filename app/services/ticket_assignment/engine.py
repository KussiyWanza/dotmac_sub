"""CRM-style rule-based assignment engine for native tickets and projects."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.project import Project
from app.models.support import Ticket
from app.models.ticket_workflow import (
    TicketAssignmentCounter,
    TicketAssignmentRule,
    TicketAssignmentStrategy,
)
from app.services.audit_adapter import stage_audit_event
from app.services.common import coerce_uuid
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.ticket_assignment.rules import (
    build_context,
    build_project_context,
    list_active_rules,
    matches_rule,
)

_PROJECT_ASSIGNMENT = OwnerCommandDefinition(
    owner="operations.project_lifecycle",
    concern="project and task assignment and scheduling",
    name="apply_project_assignment_rules",
)
from app.services.ticket_assignment.selectors import (
    list_team_candidate_person_ids,
    pick_least_loaded,
)


@dataclass(frozen=True)
class AssignmentResult:
    assigned: bool
    ticket_id: str | None = None
    project_id: str | None = None
    rule_id: str | None = None
    rule_name: str | None = None
    strategy: str | None = None
    assignment_target: str | None = None
    candidate_count: int = 0
    assignee_person_id: str | None = None
    fallback_service_team_id: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, str | bool | int | None]:
        return {
            "assigned": self.assigned,
            "ticket_id": self.ticket_id,
            "project_id": self.project_id,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "strategy": self.strategy,
            "assignment_target": self.assignment_target,
            "candidate_count": self.candidate_count,
            "assignee_person_id": self.assignee_person_id,
            "fallback_service_team_id": self.fallback_service_team_id,
            "reason": self.reason,
        }


def find_authoritative_project_creation_rule(
    db: Session, project: Project
) -> TicketAssignmentRule | None:
    """Return the first creation rule that owns initial assignment for a
    native project. Accepts a transient Project
    probe (Projects.create builds one before INSERT)."""
    ctx = build_project_context(project)
    for rule in list_active_rules(db):
        if (
            matches_rule(rule, ctx)
            and _assignee_person_id(rule) is None
            and _has_authoritative_creation_scope(rule)
        ):
            return rule
    return None


def auto_assign_project(
    db: Session,
    project_id: str,
    *,
    trigger: str = "create",
    actor_person_id: str | None = None,
    context: CommandContext | None = None,
) -> list[AssignmentResult]:
    """Apply active workflow assignment rules to a project (CRM parity)."""
    if context is None:
        context = CommandContext.system(
            actor=str(actor_person_id or "system:project-assignment"),
            scope="operations:projects:assign",
            reason=f"project_assignment:{trigger}",
            idempotency_key=f"project_assignment:{trigger}:{project_id}",
        )
        db_session_adapter.release_read_transaction(db)
        return execute_owner_command(
            db,
            definition=_PROJECT_ASSIGNMENT,
            context=context,
            operation=lambda: auto_assign_project(
                db,
                project_id,
                trigger=trigger,
                actor_person_id=actor_person_id,
                context=context,
            ),
        )

    project = (
        db.query(Project)
        .filter(Project.id == coerce_uuid(project_id))
        .with_for_update()
        .one_or_none()
    )
    if not project or not project.is_active:
        return [
            AssignmentResult(
                assigned=False,
                project_id=project_id,
                reason="project_not_found_or_inactive",
            )
        ]

    ctx = build_project_context(project)
    results: list[AssignmentResult] = []
    for rule in list_active_rules(db):
        if not matches_rule(rule, ctx):
            continue
        authoritative_result = _apply_authoritative_project_rule(
            db, project=project, rule=rule
        )
        if authoritative_result:
            results.append(authoritative_result)
            continue
        result = _apply_direct_project_assignment(db, project=project, rule=rule)
        if result:
            results.append(result)
    if not results:
        return [
            AssignmentResult(
                assigned=False,
                project_id=str(project.id),
                reason="no_matching_rule",
            )
        ]
    return results


def _apply_authoritative_project_rule(
    db: Session,
    *,
    project: Project,
    rule: TicketAssignmentRule,
) -> AssignmentResult | None:
    from app.services.projects import apply_project_assignment_rule

    result = apply_project_assignment_rule(
        db, project=project, rule=rule, authoritative_creation=True
    )
    return _project_assignment_result(result)


def _apply_direct_project_assignment(
    db: Session,
    *,
    project: Project,
    rule: TicketAssignmentRule,
) -> AssignmentResult | None:
    from app.services.projects import apply_project_assignment_rule

    result = apply_project_assignment_rule(
        db, project=project, rule=rule, authoritative_creation=False
    )
    return _project_assignment_result(result)


def _project_assignment_result(
    result: dict[str, object] | None,
) -> AssignmentResult | None:
    if result is None:
        return None

    def text_value(key: str) -> str | None:
        value = result.get(key)
        return str(value) if value is not None else None

    candidate_count_value = result.get("candidate_count")
    candidate_count = (
        candidate_count_value
        if isinstance(candidate_count_value, int)
        else int(candidate_count_value)
        if isinstance(candidate_count_value, str)
        else 0
    )

    return AssignmentResult(
        assigned=bool(result["assigned"]),
        project_id=text_value("project_id"),
        rule_id=text_value("rule_id"),
        rule_name=text_value("rule_name"),
        strategy=text_value("strategy"),
        assignment_target=text_value("assignment_target"),
        candidate_count=candidate_count,
        assignee_person_id=text_value("assignee_person_id"),
        fallback_service_team_id=text_value("fallback_service_team_id"),
        reason=text_value("reason"),
    )


def auto_assign_ticket(
    db: Session,
    ticket_id: str,
    *,
    max_open_tickets: int | None = None,
) -> AssignmentResult:
    """Propose a ticket assignment without mutating Ticket lifecycle state."""
    results = auto_assign_ticket_all(db, ticket_id, max_open_tickets=max_open_tickets)
    assigned_result = next((result for result in results if result.assigned), None)
    return assigned_result or results[0]


def stage_ticket_evaluation_evidence(
    db: Session,
    *,
    ticket_id: UUID,
    result: AssignmentResult,
) -> None:
    """Stage bounded policy evidence in the Ticket owner's transaction."""

    stage_audit_event(
        db,
        action="ticket.assignment_evaluated",
        entity_type="support_ticket",
        entity_id=str(ticket_id),
        actor_type=AuditActorType.system,
        metadata={
            "owner": "support.ticket_assignment_evaluation",
            "assigned": result.assigned,
            "rule_id": result.rule_id,
            "assignment_target": result.assignment_target,
            "assignee_person_id": result.assignee_person_id,
            "fallback_service_team_id": result.fallback_service_team_id,
            "reason": result.reason,
        },
    )


def _pick_round_robin(
    db: Session, *, rule_id: str, person_ids: list[str]
) -> str | None:
    """Advance the evaluation owner's locked rule-scoped cursor."""

    if not person_ids:
        return None
    ordered = sorted(person_ids)
    counter = (
        db.query(TicketAssignmentCounter)
        .filter(TicketAssignmentCounter.rule_id == coerce_uuid(rule_id))
        .with_for_update()
        .one_or_none()
    )
    last = (
        str(counter.last_assigned_person_id)
        if counter and counter.last_assigned_person_id
        else None
    )
    next_person = ordered[0]
    if last and last in ordered:
        next_person = ordered[(ordered.index(last) + 1) % len(ordered)]
    if counter is None:
        counter = TicketAssignmentCounter(rule_id=coerce_uuid(rule_id))
        db.add(counter)
    counter.last_assigned_person_id = coerce_uuid(next_person)
    db.flush()
    return next_person


def auto_assign_ticket_all(
    db: Session,
    ticket_id: str,
    *,
    max_open_tickets: int | None = None,
) -> list[AssignmentResult]:
    """Return ordered assignment proposals without mutating the Ticket."""
    ticket = db.get(Ticket, coerce_uuid(ticket_id))
    if not ticket or not ticket.is_active:
        return [
            AssignmentResult(
                assigned=False,
                ticket_id=ticket_id,
                reason="ticket_not_found_or_inactive",
            )
        ]

    ctx = build_context(ticket)
    rules = list_active_rules(db)
    last_matched_rule: TicketAssignmentRule | None = None
    last_candidate_count = 0
    results: list[AssignmentResult] = []
    for rule in rules:
        if not matches_rule(rule, ctx):
            continue
        last_matched_rule = rule
        authoritative_result = _propose_authoritative_ticket_rule(
            ticket=ticket, rule=rule
        )
        if authoritative_result:
            results.append(authoritative_result)
            continue
        direct_result = _propose_direct_ticket_assignment(ticket=ticket, rule=rule)
        if direct_result:
            results.append(direct_result)
            continue
        if ticket.assigned_to_person_id:
            continue
        assignee, candidate_count = _select_assignee(
            db,
            ticket=ticket,
            rule=rule,
            max_open_tickets=max_open_tickets,
        )
        last_candidate_count = candidate_count
        if not assignee:
            if rule.team_id and not ticket.service_team_id:
                results.append(
                    AssignmentResult(
                        assigned=False,
                        ticket_id=str(ticket.id),
                        rule_id=str(rule.id),
                        rule_name=rule.name,
                        strategy=str(rule.strategy),
                        assignment_target="technician",
                        candidate_count=candidate_count,
                        fallback_service_team_id=str(rule.team_id),
                        reason="queue_fallback_team_assigned",
                    )
                )
                continue
            continue
        results.append(
            AssignmentResult(
                assigned=True,
                ticket_id=str(ticket.id),
                rule_id=str(rule.id),
                rule_name=rule.name,
                strategy=str(rule.strategy),
                assignment_target="technician",
                candidate_count=candidate_count,
                assignee_person_id=assignee,
                reason="assigned",
            )
        )
    if results:
        return results

    if last_matched_rule is not None:
        return [
            AssignmentResult(
                assigned=False,
                ticket_id=str(ticket.id),
                rule_id=str(last_matched_rule.id),
                rule_name=last_matched_rule.name,
                strategy=str(last_matched_rule.strategy),
                assignment_target=_assignment_target(last_matched_rule),
                candidate_count=last_candidate_count,
                assignee_person_id=str(ticket.assigned_to_person_id)
                if ticket.assigned_to_person_id
                else None,
                reason="already_assigned"
                if ticket.assigned_to_person_id
                else "no_eligible_candidates",
            )
        ]
    return [
        AssignmentResult(
            assigned=False, ticket_id=str(ticket.id), reason="no_matching_rule"
        )
    ]


def _select_assignee(
    db: Session,
    *,
    ticket: Ticket,
    rule: TicketAssignmentRule,
    max_open_tickets: int | None,
) -> tuple[str | None, int]:
    team_id = (
        str(rule.team_id)
        if rule.team_id
        else (str(ticket.service_team_id) if ticket.service_team_id else None)
    )
    candidates = list_team_candidate_person_ids(
        db, team_id, max_open_tickets=max_open_tickets
    )
    if not candidates:
        return None, 0
    if rule.strategy == TicketAssignmentStrategy.least_loaded.value:
        return pick_least_loaded(db, candidates), len(candidates)
    return _pick_round_robin(db, rule_id=str(rule.id), person_ids=candidates), len(
        candidates
    )


def _assignment_config(rule: TicketAssignmentRule) -> dict:
    return rule.match_config if isinstance(rule.match_config, dict) else {}


def _assignment_target(rule: TicketAssignmentRule) -> str:
    return (
        str(_assignment_config(rule).get("assignment_target") or "technician")
        .strip()
        .lower()
    )


def _assignee_person_id(rule: TicketAssignmentRule) -> str | None:
    value = str(_assignment_config(rule).get("assignee_person_id") or "").strip()
    return value or None


def _has_authoritative_creation_scope(rule: TicketAssignmentRule) -> bool:
    config = _assignment_config(rule)
    return bool(
        config.get("entity_types")
        or config.get("ticket_types")
        or config.get("project_types")
        or config.get("regions")
        or config.get("service_team_ids")
        or config.get("tags_any")
    )


def _propose_authoritative_ticket_rule(
    *,
    ticket: Ticket,
    rule: TicketAssignmentRule,
) -> AssignmentResult | None:
    if _assignee_person_id(rule) or not _has_authoritative_creation_scope(rule):
        return None

    return AssignmentResult(
        assigned=bool(rule.team_id),
        ticket_id=str(ticket.id),
        rule_id=str(rule.id),
        rule_name=rule.name,
        strategy="group" if rule.team_id else None,
        assignment_target="team" if rule.team_id else "rule_scope",
        fallback_service_team_id=str(rule.team_id) if rule.team_id else None,
        reason="group_assigned"
        if rule.team_id
        else "individual_assignment_suppressed_by_rule",
    )


def _propose_direct_ticket_assignment(
    *,
    ticket: Ticket,
    rule: TicketAssignmentRule,
) -> AssignmentResult | None:
    assignee = _assignee_person_id(rule)
    if not assignee:
        return None
    target = _assignment_target(rule)
    if target == "technical_supervisor":
        changed = not bool(ticket.ticket_manager_person_id)
    elif target == "technician":
        changed = not bool(ticket.assigned_to_person_id) or not any(
            str(existing.person_id) == assignee for existing in ticket.assignees
        )
    else:
        return AssignmentResult(
            assigned=False,
            ticket_id=str(ticket.id),
            rule_id=str(rule.id),
            rule_name=rule.name,
            assignment_target=target,
            assignee_person_id=assignee,
            reason="unsupported_assignment_target",
        )

    return AssignmentResult(
        assigned=changed,
        ticket_id=str(ticket.id),
        rule_id=str(rule.id),
        rule_name=rule.name,
        strategy="direct",
        assignment_target=target,
        candidate_count=1,
        assignee_person_id=assignee,
        reason="assigned" if changed else "already_assigned",
    )
