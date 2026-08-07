"""Native project-domain service adapted from CRM's project contract.

Compatibility and ownership decisions:

* Operates on Sub's native ``app/models/project.py`` family. Statuses,
  priorities and project types are string columns plus application enums;
  comparisons and writes use ``Enum.value`` strings.
* Customer party is ``subscribers.id``; staff person
  ids are plain UUIDs carried verbatim (CRM legacy ids stay valid; new
  assignments use sub principals = ``SystemUser`` ids, which is also how
  display/emails resolve — a legacy id that doesn't resolve simply skips the
  notification).
* Task-to-ticket linkage is the real ``support_tickets`` foreign key. Field
  execution links from ``work_order.project_task_id`` so one task can own many
  visits; ``operations.work_order_commands`` is the sole writer of that link.
* The fiber-stage engine (``FIBER_INSTALLATION_STAGE_ORDER``,
  ``_compute_fiber_stage_due_at``, ``_seed_fiber_installation_tasks``) and
  template instantiation (``replace_project_tasks`` + ``_calculate_task_dates``)
  remain project decisions. Customer composition lives in
  ``customer_experience_lifecycle``.
* Retired CRM-to-Sub mirror emitters are absent. The "Installation complete"
  consequence is owned by ``Projects.update``.
* Events: sub has no ``project.*`` ``EventType`` members — lifecycle events are
  emitted as ``EventType.custom`` with ``payload["name"]`` set to the CRM event
  name (``project.created|updated|completed|canceled``,
  ``project_task.completed|updated``), the same pattern the support service
  uses. These stable names are the automation contract.
* Settings: projects-domain keys keep their CRM names
  (``default_project_status/priority``, ``default_task_status/priority``,
  ``region_pm_assignments``); numbering keys (``project_number_*``,
  ``project_task_number_*``) live under ``SettingDomain.projects`` (sub has no
  ``numbering`` domain). The auto-assignment gate keeps its shared name
  ``ticket_auto_assignment_enabled`` in ``SettingDomain.workflow``.
"""

from __future__ import annotations

import enum as enum_module
import html
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, ClassVar, TypeVar
from uuid import UUID

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.models.audit import AuditActorType, AuditEvent
from app.models.domain_settings import SettingDomain
from app.models.project import (
    Project,
    ProjectComment,
    ProjectPriority,
    ProjectStatus,
    ProjectTask,
    ProjectTaskAssignee,
    ProjectTaskComment,
    ProjectTaskDependency,
    ProjectTaskPriority,
    ProjectTaskStatus,
    ProjectTemplate,
    ProjectTemplateTask,
    ProjectTemplateTaskDependency,
    ProjectType,
)
from app.models.sales import Lead
from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.models.system_user import SystemUser
from app.models.ticket_workflow import (
    SlaClock,
    SlaClockStatus,
    SlaPolicy,
    TicketAssignmentRule,
    WorkflowEntityType,
)
from app.schemas.project import (
    ProjectCommentCreate,
    ProjectCommentUpdate,
    ProjectCreate,
    ProjectTaskCommentCreate,
    ProjectTaskCreate,
    ProjectTaskDependenciesReplace,
    ProjectTaskStatusTransition,
    ProjectTaskUpdate,
    ProjectTemplateCreate,
    ProjectTemplateTaskCreate,
    ProjectTemplateTaskUpdate,
    ProjectTemplateUpdate,
    ProjectUpdate,
)
from app.services import domain_settings as domain_settings_service
from app.services import numbering as numbering_service
from app.services import settings_spec
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    ensure_exists,
    validate_enum,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.numbering import generate_number
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    execute_owner_savepoint,
)
from app.services.response import ListResponseMixin
from app.services.staff_notifications import queue_staff_email, queue_staff_push

logger = logging.getLogger(__name__)

_EnumT = TypeVar("_EnumT", bound=enum_module.Enum)

_TEMPLATE_AUTO_CREATE_WORK_ORDER = "template_auto_create_work_order"
_TEMPLATE_WORK_ORDER_REQUIRES_AS_BUILT = (
    "template_work_order_requires_as_built_evidence"
)


def active_project_status_values() -> tuple[str, ...]:
    """Return project-owner statuses eligible for active customer context."""

    return tuple(
        status.value
        for status in ProjectStatus
        if status not in {ProjectStatus.completed, ProjectStatus.canceled}
    )


def active_project_task_status_values() -> tuple[str, ...]:
    """Return task-owner statuses eligible for active customer context."""

    return tuple(
        status.value
        for status in ProjectTaskStatus
        if status not in {ProjectTaskStatus.done, ProjectTaskStatus.canceled}
    )


def _resolve_project_number(db: Session) -> str | None:
    """Reserve a free project number after reconciling preserved imports."""

    enabled = settings_spec.resolve_value(
        db, SettingDomain.projects, "project_number_enabled"
    )
    if enabled is False:
        return None
    prefix = settings_spec.resolve_value(
        db, SettingDomain.projects, "project_number_prefix"
    )
    start = settings_spec.resolve_value(
        db, SettingDomain.projects, "project_number_start"
    )
    try:
        start_value = max(int(start) if start is not None else 1, 1)
    except (TypeError, ValueError):
        start_value = 1
    prefix_text = prefix if isinstance(prefix, str) else ""

    sequence = numbering_service.lock_sequence(db, "project_number", start_value)
    max_value: int | None = None
    for (number,) in db.query(Project.number).filter(Project.number.isnot(None)):
        number_text = str(number)
        if prefix_text:
            if not number_text.startswith(prefix_text):
                continue
            number_text = number_text[len(prefix_text) :]
        if not number_text.isdecimal():
            continue
        value = int(number_text)
        if max_value is None or value > max_value:
            max_value = value

    minimum_next = max(start_value, (max_value or 0) + 1)
    if sequence.next_value < minimum_next:
        sequence.next_value = minimum_next
        db.flush()

    for _attempt in range(10_000):
        candidate = numbering_service.generate_number(
            db=db,
            domain=SettingDomain.projects,
            sequence_key="project_number",
            enabled_key="project_number_enabled",
            prefix_key="project_number_prefix",
            padding_key="project_number_padding",
            start_key="project_number_start",
        )
        if candidate is None:
            return None
        occupied = db.scalar(
            select(Project.id).where(Project.number == candidate).limit(1)
        )
        if occupied is None:
            return candidate
    raise RuntimeError("project number sequence could not find a free value")


@dataclass(frozen=True)
class ProjectTaskWorkOrderAutomation:
    """Captured field-work policy for one instantiated template task."""

    auto_create: bool
    requires_as_built_evidence: bool


def template_task_work_order_automation_metadata(
    template_task: ProjectTemplateTask,
) -> dict[str, bool]:
    """Serialize the template decision when its ProjectTask is created."""

    return {
        _TEMPLATE_AUTO_CREATE_WORK_ORDER: template_task.auto_create_work_order,
        _TEMPLATE_WORK_ORDER_REQUIRES_AS_BUILT: (
            template_task.work_order_requires_as_built_evidence
        ),
    }


def resolve_project_task_work_order_automation(
    task: ProjectTask,
    template_task: ProjectTemplateTask,
) -> ProjectTaskWorkOrderAutomation:
    """Return captured policy, with current template state as legacy fallback."""

    metadata = dict(task.metadata_ or {})
    if _TEMPLATE_AUTO_CREATE_WORK_ORDER in metadata:
        return ProjectTaskWorkOrderAutomation(
            auto_create=metadata[_TEMPLATE_AUTO_CREATE_WORK_ORDER] is True,
            requires_as_built_evidence=(
                metadata.get(_TEMPLATE_WORK_ORDER_REQUIRES_AS_BUILT, True) is True
            ),
        )
    return ProjectTaskWorkOrderAutomation(
        auto_create=(template_task.is_active and template_task.auto_create_work_order),
        requires_as_built_evidence=(
            template_task.work_order_requires_as_built_evidence
        ),
    )


def _preserve_template_task_work_order_automation(
    task: ProjectTask,
    data: dict[str, Any],
) -> None:
    """Keep owner-captured automation keys out of generic metadata updates."""

    if "metadata_" not in data:
        return
    existing = dict(task.metadata_ or {})
    updated = dict(data.get("metadata_") or {})
    for key in (
        _TEMPLATE_AUTO_CREATE_WORK_ORDER,
        _TEMPLATE_WORK_ORDER_REQUIRES_AS_BUILT,
    ):
        if key in existing:
            updated[key] = existing[key]
    data["metadata_"] = updated or None


_PROJECT_RECONCILE = OwnerCommandDefinition(
    owner="operations.project_lifecycle",
    concern="project derived-state reconciliation",
    name="reconcile_project_projection",
)
_PROJECT_MUTATION = OwnerCommandDefinition(
    owner="operations.project_lifecycle",
    concern="Project and ProjectTask identity and lifecycle",
    name="mutate_project_aggregate",
)


def _project_command_context(
    *,
    action: str,
    actor: UUID | str | None,
    aggregate_id: UUID | str | None = None,
    reason: str | None = None,
) -> CommandContext:
    return CommandContext.system(
        actor=str(actor or "system:projects-adapter"),
        scope="operations:projects",
        reason=reason or action,
        idempotency_key=(
            f"{action}:{aggregate_id}" if aggregate_id is not None else None
        ),
    )


def _stage_project_audit(
    db: Session,
    *,
    context: CommandContext,
    action: str,
    entity_type: str,
    entity_id: UUID,
    changed_fields: list[str] | None = None,
) -> None:
    actor = context.actor.strip()
    db.add(
        AuditEvent(
            actor_type=(
                AuditActorType.system
                if actor.startswith("system:")
                else AuditActorType.user
            ),
            actor_id=actor,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            request_id=str(context.correlation_id),
            metadata_={
                "command_id": str(context.command_id),
                "reason": context.reason,
                "changed_fields": sorted(changed_fields or []),
            },
        )
    )
    db.flush()


@dataclass(frozen=True)
class ProjectProjectionRepairOutcome:
    project_id: UUID
    task_count: int
    repaired_sla_clocks: int
    repaired_primary_assignees: int


@dataclass(frozen=True, slots=True)
class ProjectTaskReassignmentNotificationOutcome:
    """Typed evidence of email consequences for newly added task assignees."""

    added_assignee_ids: tuple[UUID, ...]
    queued_email_user_ids: tuple[UUID, ...]
    failed_user_ids: tuple[UUID, ...]
    unresolved_assignee_ids: tuple[UUID, ...]


class ProjectCustomerStatusAggregate(enum_module.Enum):
    project = "project"
    project_task = "project_task"


@dataclass(frozen=True, slots=True)
class ProjectCustomerStatusNotificationCommand:
    """Typed customer consequence input for one committed status transition."""

    aggregate: ProjectCustomerStatusAggregate
    project_id: UUID
    project_task_id: UUID | None
    subscriber_id: UUID | None
    previous_status: ProjectStatus | ProjectTaskStatus
    new_status: ProjectStatus | ProjectTaskStatus
    transition_id: UUID


@dataclass(frozen=True, slots=True)
class ProjectCustomerStatusNotificationOutcome:
    command: ProjectCustomerStatusNotificationCommand
    queued: bool
    failure_recorded: bool


def reconcile_project_projection(
    db: Session,
    *,
    project_id: UUID,
    context: CommandContext,
) -> ProjectProjectionRepairOutcome:
    """Idempotently repair synchronous Project-derived state."""

    db_session_adapter.release_read_transaction(db)

    def operation() -> ProjectProjectionRepairOutcome:
        project = db.scalar(
            select(Project).where(Project.id == project_id).with_for_update()
        )
        if project is None:
            raise SalesProjectLifecycleError(
                "operations.project_lifecycle.not_found", "Project not found"
            )
        before_project_clock = _latest_project_sla_clock(db, project.id)
        _sync_project_sla_clock(db, project)
        db.flush()
        repaired_clocks = int(
            before_project_clock is None
            and _latest_project_sla_clock(db, project.id) is not None
        )
        tasks = db.scalars(
            select(ProjectTask)
            .where(ProjectTask.project_id == project.id)
            .order_by(ProjectTask.id)
            .with_for_update()
        ).all()
        repaired_assignees = 0
        for task in tasks:
            before_task_clock = _latest_task_sla_clock(db, task.id)
            _sync_task_sla_clock(db, task)
            db.flush()
            repaired_clocks += int(
                before_task_clock is None
                and _latest_task_sla_clock(db, task.id) is not None
            )
            normalized = _normalize_assignee_ids(
                [str(row.person_id) for row in task.assignees]
            )
            if (
                task.assigned_to_person_id
                and str(task.assigned_to_person_id) not in normalized
            ):
                normalized.insert(0, str(task.assigned_to_person_id))
                _sync_project_task_assignees(db, task, normalized)
                repaired_assignees += 1
        db.flush()
        return ProjectProjectionRepairOutcome(
            project_id=project.id,
            task_count=len(tasks),
            repaired_sla_clocks=repaired_clocks,
            repaired_primary_assignees=repaired_assignees,
        )

    return execute_owner_command(
        db,
        definition=_PROJECT_RECONCILE,
        context=context,
        operation=operation,
    )


def apply_project_assignment_rule(
    db: Session,
    *,
    project: Project,
    rule: TicketAssignmentRule,
    authoritative_creation: bool,
) -> dict[str, object] | None:
    """Apply a policy decision inside the Project owner's transaction.

    The shared ticket-assignment engine evaluates and orders rules but delegates
    every authoritative Project and ProjectTask write to this flush-only helper.
    """
    config = rule.match_config if isinstance(rule.match_config, dict) else {}
    assignee = str(config.get("assignee_person_id") or "").strip() or None
    target = str(config.get("assignment_target") or "technician").strip().lower()
    has_scope = any(
        config.get(key)
        for key in (
            "entity_types",
            "ticket_types",
            "project_types",
            "regions",
            "service_team_ids",
            "tags_any",
        )
    )
    if authoritative_creation:
        if assignee or not has_scope:
            return None
        changed = False
        if rule.team_id and project.service_team_id != rule.team_id:
            project.service_team_id = rule.team_id
            changed = True
        for field in (
            "manager_person_id",
            "project_manager_person_id",
            "assistant_manager_person_id",
        ):
            if getattr(project, field):
                setattr(project, field, None)
                changed = True
        if changed:
            db.flush()
        return {
            "assigned": bool(rule.team_id),
            "project_id": str(project.id),
            "rule_id": str(rule.id),
            "rule_name": rule.name,
            "strategy": "group" if rule.team_id else None,
            "assignment_target": "team" if rule.team_id else "rule_scope",
            "fallback_service_team_id": str(rule.team_id) if rule.team_id else None,
            "reason": "group_assigned"
            if rule.team_id
            else "individual_assignment_suppressed_by_rule",
        }
    if not assignee:
        return None
    assignee_uuid = coerce_uuid(assignee)
    changed = False
    if target == "technical_supervisor":
        for field in ("manager_person_id", "project_manager_person_id"):
            if not getattr(project, field):
                setattr(project, field, assignee_uuid)
                changed = True
    elif target == "site_coordinator":
        if not project.assistant_manager_person_id:
            project.assistant_manager_person_id = assignee_uuid
            changed = True
    elif target == "technician":
        tasks = db.scalars(
            select(ProjectTask)
            .where(ProjectTask.project_id == project.id)
            .where(ProjectTask.is_active.is_(True))
            .order_by(ProjectTask.id)
        ).all()
        for task in tasks:
            if not task.assigned_to_person_id:
                task.assigned_to_person_id = assignee_uuid
                changed = True
            if not any(str(row.person_id) == assignee for row in task.assignees):
                task.assignees.append(
                    ProjectTaskAssignee(task_id=task.id, person_id=assignee_uuid)
                )
                changed = True
    else:
        return {
            "assigned": False,
            "project_id": str(project.id),
            "rule_id": str(rule.id),
            "rule_name": rule.name,
            "assignment_target": target,
            "assignee_person_id": assignee,
            "reason": "unsupported_assignment_target",
        }
    if changed:
        db.flush()
    return {
        "assigned": changed,
        "project_id": str(project.id),
        "rule_id": str(rule.id),
        "rule_name": rule.name,
        "strategy": "direct",
        "assignment_target": target,
        "candidate_count": 1,
        "assignee_person_id": assignee,
        "reason": "assigned" if changed else "already_assigned",
    }


FIBER_INSTALLATION_STAGE_ORDER: tuple[str, ...] = (
    "project_plan",
    "project_survey",
    "drop_cable_installation",
    "survey_approval_po_issuance",
    "last_mile_installation",
    "power_splicing_activation",
)

FIBER_INSTALLATION_STAGE_TITLES: dict[str, str] = {
    "project_plan": "Project Plan",
    "project_survey": "Project Survey",
    "drop_cable_installation": "Drop Cable Installation",
    "survey_approval_po_issuance": "Survey Approval & PO Issuance",
    "last_mile_installation": "Last Mile Installation",
    "power_splicing_activation": "Power Direction, Splicing & Customer Activation",
}

FIBER_ACTIVATION_STAGE_KEY = FIBER_INSTALLATION_STAGE_ORDER[-1]

FIBER_PROJECT_TASK_SLA_POLICY_NAME = "Fiber Project Task SLA"
PROJECT_COMPLETION_SLA_POLICY_NAME = "Project Completion SLA"

_DEFAULT_LOGO_URL = "https://erp.dotmac.ng/files/dotmac%20no%20bg.png"

_TASK_TERMINAL_STATUSES = {
    ProjectTaskStatus.done.value,
    ProjectTaskStatus.canceled.value,
}
_PROJECT_TERMINAL_STATUSES = {
    ProjectStatus.completed.value,
    ProjectStatus.canceled.value,
}
# Provenance marker for projects rooted in a network buildout rather than a
# sale. Paired with ``Project.external_reference`` (the BuildoutProject id) it
# reuses ``uq_projects_external_system_reference`` as the idempotency key.
BUILDOUT_PROJECT_SYSTEM = "buildout"
_PROJECT_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    status.value: frozenset(
        candidate.value
        for candidate in ProjectStatus
        if candidate.value not in {status.value}
    )
    for status in ProjectStatus
    if status.value not in _PROJECT_TERMINAL_STATUSES
}
_PROJECT_TASK_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    status.value: frozenset(
        candidate.value
        for candidate in ProjectTaskStatus
        if candidate.value not in {status.value}
    )
    for status in ProjectTaskStatus
    if status.value not in _TASK_TERMINAL_STATUSES
}


class SalesProjectLifecycleError(ValueError):
    """Transport-neutral error raised by the sales implementation seam."""

    def __init__(self, code: str, message: str, *, kind: str = "conflict") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


class ProjectServiceError(DomainError):
    """Transport-neutral Projects boundary error."""


def _project_error(code: str, message: str, **details: object) -> ProjectServiceError:
    return ProjectServiceError(
        code=f"operations.project_lifecycle.{code}",
        message=message,
        details=details,
    )


def _require_status_transition(
    *,
    current: str,
    requested: str,
    transitions: dict[str, frozenset[str]],
    aggregate: str,
) -> None:
    if current == requested:
        return
    if requested not in transitions.get(current, frozenset()):
        raise _project_error(
            "invalid_transition",
            f"Invalid {aggregate} status transition",
            aggregate=aggregate,
            current_status=current,
            requested_status=requested,
        )


def _sales_project_enum(value: str, enum_cls: type[_EnumT], label: str) -> _EnumT:
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise SalesProjectLifecycleError(
            f"invalid_{label}", f"Invalid {label}", kind="invalid"
        ) from exc


def _model_data(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce enum members from pydantic dumps to their string values.

    Sub stores CRM's PostgreSQL enums as string columns while the
    API schemas keep the enum types — this is the seam between the two.
    """
    return {
        key: (value.value if isinstance(value, enum_module.Enum) else value)
        for key, value in data.items()
    }


# ── settings (domain_settings-backed, support-service pattern) ───────────────


def _read_setting_raw(db: Session, domain: SettingDomain, key: str) -> object | None:
    try:
        domain_client = getattr(domain_settings_service, f"{domain.value}_settings")
        setting = domain_client.get_by_key(db, key)
    except Exception:  # noqa: BLE001 - missing settings resolve to defaults
        return None
    if setting.value_json is not None:
        return setting.value_json
    return setting.value_text


def _read_text_setting(db: Session, domain: SettingDomain, key: str) -> str | None:
    raw = _read_setting_raw(db, domain, key)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _read_json_setting(db: Session, domain: SettingDomain, key: str) -> dict[str, Any]:
    raw = _read_setting_raw(db, domain, key)
    return dict(raw) if isinstance(raw, dict) else {}


def _read_bool_setting(
    db: Session, domain: SettingDomain, key: str, default: bool
) -> bool:
    raw = _read_setting_raw(db, domain, key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


# ── shared helpers ────────────────────────────────────────────────────────────


def _normalize_title(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.strip().lower().replace("-", " ").replace("_", " ").split())


def _subscriber_email(subscriber: Subscriber | None) -> str | None:
    if subscriber is None:
        return None
    if isinstance(subscriber.email, str) and subscriber.email.strip():
        return subscriber.email.strip()
    return None


def _subscriber_name(subscriber: Subscriber | None) -> str | None:
    if subscriber is None:
        return None
    if isinstance(subscriber.display_name, str) and subscriber.display_name.strip():
        return subscriber.display_name.strip()
    first = subscriber.first_name if isinstance(subscriber.first_name, str) else ""
    last = subscriber.last_name if isinstance(subscriber.last_name, str) else ""
    full_name = f"{first} {last}".strip()
    if full_name:
        return full_name
    return _subscriber_email(subscriber)


def _lead_subscriber(db: Session, project: Project) -> Subscriber | None:
    lead = project.lead
    if lead is None and project.lead_id:
        lead = db.get(Lead, project.lead_id)
    if lead is None:
        return None
    if lead.subscriber is not None:
        return lead.subscriber
    if lead.subscriber_id:
        return db.get(Subscriber, lead.subscriber_id)
    return None


def _resolve_customer_name(db: Session, project: Project) -> str:
    name = _subscriber_name(project.subscriber)
    if name:
        return name
    name = _subscriber_name(_lead_subscriber(db, project))
    if name:
        return name
    return "Customer"


def _resolve_fiber_stage_key(task: ProjectTask) -> str | None:
    metadata = task.metadata_ if isinstance(task.metadata_, dict) else {}
    raw_stage = metadata.get("fiber_stage_key")
    if isinstance(raw_stage, str) and raw_stage in FIBER_INSTALLATION_STAGE_ORDER:
        return raw_stage

    normalized = _normalize_title(task.title)
    if "project plan" in normalized:
        return "project_plan"
    if "project survey" in normalized:
        return "project_survey"
    if "drop cable" in normalized:
        return "drop_cable_installation"
    if ("po" in normalized and "issuance" in normalized) or (
        "survey approval" in normalized
    ):
        return "survey_approval_po_issuance"
    if "last mile" in normalized:
        return "last_mile_installation"
    if (
        "splicing" in normalized
        or "activation" in normalized
        or "power direction" in normalized
    ):
        return "power_splicing_activation"
    return None


def _fiber_stage_task(
    db: Session, project_id: UUID, stage_key: str
) -> ProjectTask | None:
    candidates = (
        db.query(ProjectTask)
        .filter(ProjectTask.project_id == project_id, ProjectTask.is_active.is_(True))
        .order_by(ProjectTask.created_at.asc())
        .all()
    )
    for candidate in candidates:
        if _resolve_fiber_stage_key(candidate) == stage_key:
            return candidate
    return None


def _fiber_stage_anchor(task: ProjectTask | None, fallback: datetime) -> datetime:
    if not task:
        return fallback
    return task.completed_at or task.created_at or fallback


def _compute_fiber_stage_due_at(
    db: Session, project: Project, task: ProjectTask, stage_key: str
) -> datetime:
    baseline = project.created_at or datetime.now(UTC)
    if stage_key == "project_plan":
        return baseline + timedelta(hours=24)
    if stage_key == "project_survey":
        plan = _fiber_stage_task(db, project.id, "project_plan")
        return _fiber_stage_anchor(plan, baseline) + timedelta(hours=24)
    if stage_key == "drop_cable_installation":
        survey = _fiber_stage_task(db, project.id, "project_survey")
        return _fiber_stage_anchor(survey, baseline) + timedelta(hours=48)
    if stage_key == "survey_approval_po_issuance":
        survey = _fiber_stage_task(db, project.id, "project_survey")
        return _fiber_stage_anchor(survey, baseline) + timedelta(hours=24)
    if stage_key == "last_mile_installation":
        survey = _fiber_stage_task(db, project.id, "project_survey")
        return _fiber_stage_anchor(survey, baseline) + timedelta(days=5)
    if stage_key == "power_splicing_activation":
        drop_task = _fiber_stage_task(db, project.id, "drop_cable_installation")
        last_mile_task = _fiber_stage_task(db, project.id, "last_mile_installation")
        drop_anchor = _fiber_stage_anchor(drop_task, baseline)
        last_mile_anchor = _fiber_stage_anchor(last_mile_task, baseline)
        return max(drop_anchor, last_mile_anchor) + timedelta(hours=24)
    return (task.created_at or baseline) + timedelta(hours=24)


# ── SLA clocks (entity types project / project_task) ─────────────────────────


def _ensure_project_task_sla_policy(db: Session) -> SlaPolicy:
    policy = (
        db.query(SlaPolicy)
        .filter(SlaPolicy.entity_type == WorkflowEntityType.project_task.value)
        .filter(SlaPolicy.name == FIBER_PROJECT_TASK_SLA_POLICY_NAME)
        .filter(SlaPolicy.is_active.is_(True))
        .first()
    )
    if policy:
        return policy
    policy = SlaPolicy(
        name=FIBER_PROJECT_TASK_SLA_POLICY_NAME,
        entity_type=WorkflowEntityType.project_task.value,
        description="SLA policy for fiber installation project stages",
        is_active=True,
    )
    db.add(policy)
    db.flush()
    return policy


def _ensure_project_sla_policy(db: Session) -> SlaPolicy:
    policy = (
        db.query(SlaPolicy)
        .filter(SlaPolicy.entity_type == WorkflowEntityType.project.value)
        .filter(SlaPolicy.name == PROJECT_COMPLETION_SLA_POLICY_NAME)
        .filter(SlaPolicy.is_active.is_(True))
        .first()
    )
    if policy:
        return policy
    policy = SlaPolicy(
        name=PROJECT_COMPLETION_SLA_POLICY_NAME,
        entity_type=WorkflowEntityType.project.value,
        description="SLA policy for overall project completion timelines",
        is_active=True,
    )
    db.add(policy)
    db.flush()
    return policy


def _latest_project_sla_clock(db: Session, project_id: UUID) -> SlaClock | None:
    return (
        db.query(SlaClock)
        .filter(
            SlaClock.entity_type == WorkflowEntityType.project.value,
            SlaClock.entity_id == project_id,
        )
        .order_by(SlaClock.created_at.desc())
        .first()
    )


def _sync_project_sla_clock(db: Session, project: Project) -> None:
    if not project.due_at:
        return
    policy = _ensure_project_sla_policy(db)
    clock = _latest_project_sla_clock(db, project.id)
    now = datetime.now(UTC)

    if project.status in _PROJECT_TERMINAL_STATUSES:
        if clock and clock.status != SlaClockStatus.completed.value:
            clock.status = SlaClockStatus.completed.value
            clock.completed_at = project.completed_at or now
        return

    if not clock or clock.status == SlaClockStatus.completed.value:
        db.add(
            SlaClock(
                policy_id=policy.id,
                entity_type=WorkflowEntityType.project.value,
                entity_id=project.id,
                priority=project.project_type,
                status=SlaClockStatus.running.value,
                started_at=project.start_at or project.created_at or now,
                due_at=project.due_at,
            )
        )
        return

    if clock.status in {SlaClockStatus.paused.value, SlaClockStatus.breached.value}:
        clock.status = SlaClockStatus.running.value
        clock.paused_at = None
    clock.priority = project.project_type
    clock.completed_at = None
    clock.due_at = project.due_at


def _latest_task_sla_clock(db: Session, task_id: UUID) -> SlaClock | None:
    return (
        db.query(SlaClock)
        .filter(
            SlaClock.entity_type == WorkflowEntityType.project_task.value,
            SlaClock.entity_id == task_id,
        )
        .order_by(SlaClock.created_at.desc())
        .first()
    )


def _sync_task_sla_clock(db: Session, task: ProjectTask) -> None:
    if not task.due_at:
        return
    policy = _ensure_project_task_sla_policy(db)
    clock = _latest_task_sla_clock(db, task.id)
    now = datetime.now(UTC)

    if task.status in _TASK_TERMINAL_STATUSES:
        if clock and clock.status != SlaClockStatus.completed.value:
            clock.status = SlaClockStatus.completed.value
            clock.completed_at = task.completed_at or now
        return

    if not clock or clock.status == SlaClockStatus.completed.value:
        db.add(
            SlaClock(
                policy_id=policy.id,
                entity_type=WorkflowEntityType.project_task.value,
                entity_id=task.id,
                priority=task.priority,
                status=SlaClockStatus.running.value,
                started_at=task.created_at or now,
                due_at=task.due_at,
            )
        )
        return

    if clock.status in {SlaClockStatus.paused.value, SlaClockStatus.breached.value}:
        clock.status = SlaClockStatus.running.value
    clock.priority = task.priority
    clock.due_at = task.due_at


def _apply_fiber_stage_defaults(db: Session, task: ProjectTask) -> None:
    project = db.get(Project, task.project_id)
    if (
        not project
        or project.project_type != ProjectType.fiber_optics_installation.value
    ):
        return

    stage_key = _resolve_fiber_stage_key(task)
    if not stage_key:
        return

    metadata = dict(task.metadata_) if isinstance(task.metadata_, dict) else {}
    metadata["fiber_stage_key"] = stage_key
    metadata.setdefault(
        "fiber_stage_title", FIBER_INSTALLATION_STAGE_TITLES.get(stage_key, task.title)
    )
    metadata["fiber_sla_managed"] = True
    task.metadata_ = metadata
    task.due_at = _compute_fiber_stage_due_at(db, project, task, stage_key)


# ── notifications ─────────────────────────────────────────────────────────────


def _queue_in_app_notification(
    db: Session, recipient: str, subject: str, body: str
) -> None:
    queue_staff_push(
        db,
        recipient=recipient,
        subject=subject,
        body=body,
    )


def _company_name(db: Session) -> str:
    """Company display name for customer-facing emails (best-effort)."""
    try:
        from app.services import web_system_company_info as company_info_service

        name = (company_info_service.get_company_info(db) or {}).get("company_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except Exception:  # noqa: BLE001 - branding is advisory
        pass
    return "Dotmac Technologies"


def _app_base_url(db: Session) -> str:
    try:
        from app.services.email import _configured_app_url

        return (_configured_app_url(db) or "").rstrip("/")
    except Exception:  # noqa: BLE001 - links degrade to relative paths
        return ""


def _next_fiber_stage_label(task: ProjectTask) -> str | None:
    stage_key = _resolve_fiber_stage_key(task)
    if not stage_key or stage_key not in FIBER_INSTALLATION_STAGE_ORDER:
        return None
    index = FIBER_INSTALLATION_STAGE_ORDER.index(stage_key)
    if index >= len(FIBER_INSTALLATION_STAGE_ORDER) - 1:
        return None
    next_key = FIBER_INSTALLATION_STAGE_ORDER[index + 1]
    return FIBER_INSTALLATION_STAGE_TITLES.get(
        next_key, next_key.replace("_", " ").title()
    )


def _next_template_task_label(
    db: Session, project: Project, task: ProjectTask
) -> str | None:
    if not project.project_template_id or not task.template_task_id:
        return None

    template_tasks = (
        db.query(ProjectTemplateTask)
        .filter(ProjectTemplateTask.template_id == project.project_template_id)
        .filter(ProjectTemplateTask.is_active.is_(True))
        .order_by(
            ProjectTemplateTask.sort_order.asc(), ProjectTemplateTask.created_at.asc()
        )
        .all()
    )
    if not template_tasks:
        return None

    current_index = None
    for index, template_task in enumerate(template_tasks):
        if template_task.id == task.template_task_id:
            current_index = index
            break
    if current_index is None:
        return None

    project_tasks = (
        db.query(ProjectTask)
        .filter(ProjectTask.project_id == project.id)
        .filter(ProjectTask.is_active.is_(True))
        .all()
    )
    project_tasks_by_template_id = {
        project_task.template_task_id: project_task
        for project_task in project_tasks
        if project_task.template_task_id
    }

    for template_task in template_tasks[current_index + 1 :]:
        mapped_task = project_tasks_by_template_id.get(template_task.id)
        if mapped_task and mapped_task.status in _TASK_TERMINAL_STATUSES:
            continue
        return mapped_task.title if mapped_task else template_task.title
    return None


def _notify_customer_task_completed(
    db: Session, project: Project, task: ProjectTask
) -> None:
    if project.subscriber_id is None:
        return
    customer_name = _resolve_customer_name(db, project)
    next_stage = _next_template_task_label(
        db, project, task
    ) or _next_fiber_stage_label(task)
    subject = "Project Update - Stage Completed"
    project_ref = project.number or str(project.id)
    company = html.escape(_company_name(db))
    customer_label = html.escape(customer_name)
    project_name = html.escape(project.name or "Project")
    project_code = html.escape(project_ref)
    completed_stage = html.escape(task.title or "Project Task")
    next_stage_html = html.escape(next_stage) if next_stage else ""
    logo_url_html = html.escape(_DEFAULT_LOGO_URL)

    next_stage_block = ""
    if next_stage_html:
        next_stage_block = (
            '<div style="background-color: #ffffff; border: 1px solid #dbeafe; '
            'border-radius: 8px; padding: 14px 16px; margin: 12px 0 18px;">'
            '<p style="margin: 0; font-size: 15px; color: #0f172a;">'
            "<strong>&#128279; Next Stage</strong></p>"
            f'<p style="margin: 6px 0 0; font-size: 16px; color: #111827;">'
            f"{next_stage_html}</p>"
            "</div>"
        )

    body = (
        "<div style=\"font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; "
        "line-height: 1.8; color: #333; background-color: #f4f4f9; padding: 25px; "
        "border: 1px solid #ccc; border-radius: 10px; "
        "box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1); "
        'position: relative;">'
        '<div style="position: absolute; top: 14px; right: 14px;">'
        f'<img src="{logo_url_html}" alt="Dotmac Logo" '
        'style="max-width: 150px; height: auto;">'
        "</div>"
        '<div style="text-align: center; margin-bottom: 20px;">'
        '<h1 style="color: green; font-size: 24px; margin: 0;">'
        "Project Stage Completed</h1>"
        "</div>"
        f'<p style="font-size: 16px; color: #0f172a; margin-top: 20px;">'
        f"Dear {customer_label},</p>"
        '<p style="font-size: 15px; color: #555; margin: 15px 0;">'
        "We are pleased to inform you that your project "
        f"<strong>{project_name}</strong> ({project_code}) has successfully completed "
        f"the <strong>{completed_stage}</strong> stage."
        "</p>"
        '<div style="background-color: #ffffff; border: 1px solid #dcfce7; '
        'border-radius: 8px; padding: 14px 16px; margin: 12px 0 18px;">'
        '<p style="margin: 0; font-size: 15px; color: #14532d;">'
        "<strong>&#9989; Completed Stage</strong></p>"
        f'<p style="margin: 6px 0 0; font-size: 16px; color: #111827;">'
        f"{completed_stage}</p>"
        "</div>"
        f"{next_stage_block}"
        '<p style="font-size: 15px; color: #555; margin: 10px 0;">'
        "Our technical team is progressing steadily to ensure a smooth and timely "
        "completion of your installation."
        "</p>"
        '<p style="font-size: 15px; color: #555; margin: 10px 0 18px;">'
        "We will continue to keep you informed at every key milestone."
        "</p>"
        '<p style="font-size: 15px; color: #555; margin: 10px 0;">'
        f"Thank you for choosing {company}."
        "</p>"
        '<p style="font-size: 15px; color: #0f172a; margin: 10px 0 0;">'
        "Warm regards,<br>The Dotmac Team."
        "</p>"
        "</div>"
    )
    from app.models.notification import NotificationChannel
    from app.services import customer_experience_communications

    if project.subscriber_id is not None:
        customer_experience_communications.request_update(
            db,
            subscriber_id=project.subscriber_id,
            event_type="project_task_completed",
            subject=subject,
            body=body,
            metadata={
                "type": "project",
                "project_id": str(project.id),
                "project_task_id": str(task.id),
            },
            dedupe_key=f"project-task-completed:{task.id}:{task.completed_at}",
            default_channels=(NotificationChannel.email,),
        )


def _notify_customer_project_completed(db: Session, project: Project) -> None:
    if project.subscriber_id is None:
        return
    project_ref = project.number or str(project.id)
    subject = f"Project completed: {project.name}"
    body = (
        f"Your installation project '{project.name}' ({project_ref}) is now "
        "completed.\n"
        "You can review the completed installation and contact support from self-care."
    )
    from app.models.notification import NotificationChannel
    from app.services import customer_experience_communications

    customer_experience_communications.request_update(
        db,
        subscriber_id=project.subscriber_id,
        event_type="project_completed",
        subject=subject,
        body=body,
        metadata={"type": "project", "project_id": str(project.id)},
        dedupe_key=f"project-completed:{project.id}:{project.completed_at}",
        default_channels=(
            NotificationChannel.email,
            NotificationChannel.whatsapp,
            NotificationChannel.push,
        ),
    )


def _queue_customer_status_transition(
    db: Session,
    *,
    project: Project,
    task: ProjectTask | None,
    command: ProjectCustomerStatusNotificationCommand,
) -> bool:
    """Queue the ordinary customer message for one genuine status transition."""

    if command.subscriber_id is None:
        return False
    from app.models.notification import NotificationChannel
    from app.services import customer_experience_communications

    project_ref = project.number or str(project.id)
    previous_label = command.previous_status.value.replace("_", " ").title()
    new_label = command.new_status.value.replace("_", " ").title()
    metadata: dict[str, object] = {
        "type": command.aggregate.value,
        "project_id": str(command.project_id),
        "previous_status": command.previous_status.value,
        "new_status": command.new_status.value,
        "transition_id": str(command.transition_id),
    }
    if task is None:
        event_type = "project_status_changed"
        subject = f"Project status updated: {project.name}"
        body = (
            f"Your project '{project.name}' ({project_ref}) has moved from "
            f"{previous_label} to {new_label}."
        )
        aggregate_id = project.id
    else:
        task_ref = task.number or str(task.id)
        event_type = "project_task_status_changed"
        subject = f"Project task status updated: {task.title}"
        body = (
            f"The task '{task.title}' ({task_ref}) in your project "
            f"'{project.name}' ({project_ref}) has moved from {previous_label} "
            f"to {new_label}."
        )
        aggregate_id = task.id
        metadata["project_task_id"] = str(task.id)

    customer_experience_communications.request_update(
        db,
        subscriber_id=command.subscriber_id,
        event_type=event_type,
        subject=subject,
        body=body,
        metadata=metadata,
        dedupe_key=(
            f"{command.aggregate.value}-status-transition:"
            f"{aggregate_id}:{command.transition_id}"
        ),
        default_channels=(NotificationChannel.email,),
    )
    return True


def _stage_customer_status_transition(
    db: Session,
    *,
    project: Project,
    task: ProjectTask | None,
    previous_status: str,
    new_status: str,
    context: CommandContext,
) -> ProjectCustomerStatusNotificationOutcome:
    """Isolate optional delivery while retaining durable failure evidence."""

    aggregate = (
        ProjectCustomerStatusAggregate.project_task
        if task is not None
        else ProjectCustomerStatusAggregate.project
    )
    status_type = ProjectTaskStatus if task is not None else ProjectStatus
    command = ProjectCustomerStatusNotificationCommand(
        aggregate=aggregate,
        project_id=project.id,
        project_task_id=task.id if task is not None else None,
        subscriber_id=project.subscriber_id,
        previous_status=status_type(previous_status),
        new_status=status_type(new_status),
        transition_id=context.command_id,
    )

    def queue() -> bool:
        if task is not None and new_status == ProjectTaskStatus.done.value:
            _notify_customer_task_completed(db, project, task)
            return project.subscriber_id is not None
        if task is None and new_status == ProjectStatus.completed.value:
            _notify_customer_project_completed(db, project)
            return project.subscriber_id is not None
        return _queue_customer_status_transition(
            db,
            project=project,
            task=task,
            command=command,
        )

    try:
        queued = execute_owner_savepoint(db, queue)
        return ProjectCustomerStatusNotificationOutcome(
            command=command,
            queued=queued,
            failure_recorded=False,
        )
    except Exception as exc:  # noqa: BLE001 - transition must remain valid
        entity_type = aggregate.value
        entity_id = task.id if task is not None else project.id
        logger.error(
            "project_customer_status_notification_failed entity_type=%s "
            "entity_id=%s transition_id=%s error=%s",
            entity_type,
            entity_id,
            context.command_id,
            exc,
        )
        _stage_project_audit(
            db,
            context=context,
            action="customer_status_notification_failed",
            entity_type=entity_type,
            entity_id=entity_id,
            changed_fields=["customer_notification", "status"],
        )
        return ProjectCustomerStatusNotificationOutcome(
            command=command,
            queued=False,
            failure_recorded=True,
        )


def _queue_customer_project_created_email(db: Session, project: Project) -> bool:
    """Request one customer email when a subscriber project is created."""

    if project.subscriber_id is None:
        return False
    project_ref = project.number or str(project.id)
    from app.models.notification import NotificationChannel
    from app.services import customer_experience_communications

    customer_experience_communications.request_update(
        db,
        subscriber_id=project.subscriber_id,
        event_type="project_created",
        subject=f"Project created: {project.name}",
        body=(
            f"Your project '{project.name}' ({project_ref}) has been created.\n\n"
            f"Current status: {project.status}.\n\n"
            "We will keep you informed as work progresses."
        ),
        metadata={"type": "project", "project_id": str(project.id)},
        dedupe_key=f"project-created:{project.id}",
        default_channels=(NotificationChannel.email,),
    )
    return True


def _stage_customer_project_created_email(
    db: Session,
    *,
    project: Project,
    context: CommandContext,
) -> bool:
    """Isolate optional email staging from the Project create command."""

    try:
        return execute_owner_savepoint(
            db,
            lambda: _queue_customer_project_created_email(db, project),
        )
    except Exception as exc:  # noqa: BLE001 - Project creation must remain valid
        logger.error(
            "project_created_customer_email_failed project_id=%s error=%s",
            project.id,
            exc,
        )
        _stage_project_audit(
            db,
            context=context,
            action="creation_customer_email_failed",
            entity_type="project",
            entity_id=project.id,
            changed_fields=["customer_email"],
        )
        return False


def notify_project_task_sla_breach(db: Session, clock: SlaClock) -> None:
    if clock.entity_type != WorkflowEntityType.project_task.value:
        return
    task = db.get(ProjectTask, clock.entity_id)
    if not task:
        return
    project = db.get(Project, task.project_id)
    if not project:
        return

    metadata = dict(task.metadata_) if isinstance(task.metadata_, dict) else {}
    metadata["sla_breached"] = True
    metadata["sla_breached_at"] = (clock.breached_at or datetime.now(UTC)).isoformat()
    task.metadata_ = metadata

    role_person_ids = [
        project.project_manager_person_id,
        project.assistant_manager_person_id,
        project.manager_person_id,
    ]
    person_ids = [person_id for person_id in role_person_ids if person_id]
    if not person_ids:
        return

    from app.models.operational_escalation import OperationalEntityType
    from app.services import operational_escalation

    policies = operational_escalation.matching_policies(
        db,
        entity_type=OperationalEntityType.project_task,
        trigger="project_task.sla_breached",
        severity=str(task.priority or "") or None,
    )
    if not policies:
        return
    for person_id in person_ids:
        operational_escalation.add_watcher(
            db,
            entity_type=OperationalEntityType.project_task,
            entity_id=task.id,
            person_id=person_id,
            source="project_task_sla",
            reason="Project delivery lead",
        )

    task_ref = task.number or str(task.id)
    project_ref = project.number or str(project.id)
    operational_escalation.emit_sla_event(
        db,
        entity_type=OperationalEntityType.project_task,
        entity_id=task.id,
        trigger="project_task.sla_breached",
        severity=str(task.priority or "") or None,
        metadata={
            "title": f"Project task SLA breached: {task.title}",
            "body": (
                f"Task {task_ref} in project {project_ref} passed its configured "
                "SLA due time."
            ),
            "target_url": f"/admin/projects/{project.id}",
            "category": "operations",
            "project_id": str(project.id),
        },
        triggered_at=clock.breached_at,
        policies=policies,
    )


def _seed_fiber_installation_tasks(db: Session, project: Project) -> None:
    if project.project_type != ProjectType.fiber_optics_installation.value:
        return
    existing = (
        db.query(ProjectTask)
        .filter(ProjectTask.project_id == project.id, ProjectTask.is_active.is_(True))
        .first()
    )
    if existing:
        return

    stage_offsets = {
        "project_plan": timedelta(hours=24),
        "project_survey": timedelta(hours=48),
        "drop_cable_installation": timedelta(hours=96),
        "survey_approval_po_issuance": timedelta(hours=72),
        "last_mile_installation": timedelta(days=7),
        "power_splicing_activation": timedelta(days=8),
    }
    baseline = project.created_at or datetime.now(UTC)

    for stage_key in FIBER_INSTALLATION_STAGE_ORDER:
        number = generate_number(
            db=db,
            domain=SettingDomain.projects,
            sequence_key="project_task_number",
            enabled_key="project_task_number_enabled",
            prefix_key="project_task_number_prefix",
            padding_key="project_task_number_padding",
            start_key="project_task_number_start",
        )
        task = ProjectTask(
            project_id=project.id,
            title=FIBER_INSTALLATION_STAGE_TITLES[stage_key],
            status=ProjectTaskStatus.todo.value,
            priority=ProjectTaskPriority.normal.value,
            created_by_person_id=project.created_by_person_id,
            due_at=baseline + stage_offsets[stage_key],
            metadata_={
                "fiber_stage_key": stage_key,
                "fiber_stage_title": FIBER_INSTALLATION_STAGE_TITLES[stage_key],
                "fiber_sla_managed": True,
            },
        )
        if number:
            task.number = number
        db.add(task)
        db.flush()
        _sync_task_sla_clock(db, task)


def resolve_activation_gate_task(db: Session, project_id: UUID) -> ProjectTask | None:
    """Return the uniquely configured activation-stage task for a project.

    The project domain owns the stage vocabulary. Callers persist the returned
    task identity and never infer readiness from titles or duplicate stage keys.
    """

    tasks = (
        db.query(ProjectTask)
        .filter(
            ProjectTask.project_id == project_id,
            ProjectTask.is_active.is_(True),
        )
        .order_by(ProjectTask.created_at.asc(), ProjectTask.id.asc())
        .all()
    )
    matches = [
        task
        for task in tasks
        if isinstance(task.metadata_, dict)
        and task.metadata_.get("fiber_stage_key") == FIBER_ACTIVATION_STAGE_KEY
    ]
    return matches[0] if len(matches) == 1 else None


def prepare_sales_project(
    db: Session,
    *,
    sales_order_id: UUID,
    quote_id: UUID | None,
    subscriber_id: UUID,
    lead_id: UUID | None,
    name: str,
    project_type: str,
    customer_address: str | None,
    region: str | None,
    actor_id: str,
    require_project_template: bool = False,
) -> Project:
    """Create the native project root for one exact SalesOrder, without commit."""

    normalized_type = _sales_project_enum(
        project_type, ProjectType, "project_type"
    ).value
    existing = (
        db.query(Project).filter(Project.sales_order_id == sales_order_id).one_or_none()
    )
    if existing is not None:
        if (
            existing.subscriber_id != subscriber_id
            or existing.quote_id != quote_id
            or existing.lead_id != lead_id
            or existing.project_type != normalized_type
            or (require_project_template and existing.project_template_id is None)
        ):
            raise SalesProjectLifecycleError(
                "project_binding_conflict",
                "SalesOrder project binding conflicts with canonical state",
            )
        return existing
    project_template = (
        db.query(ProjectTemplate)
        .filter(
            ProjectTemplate.project_type == normalized_type,
            ProjectTemplate.is_active.is_(True),
        )
        .one_or_none()
    )
    if project_template is None and require_project_template:
        raise SalesProjectLifecycleError(
            "project_template_unconfigured",
            (
                "No active Project Template is configured for the selected "
                f"Project Type '{normalized_type}'"
            ),
        )
    configured_status = _read_text_setting(
        db, SettingDomain.projects, "default_project_status"
    )
    configured_priority = _read_text_setting(
        db, SettingDomain.projects, "default_project_priority"
    )
    project_status = _sales_project_enum(
        configured_status or ProjectStatus.open.value,
        ProjectStatus,
        "default_project_status",
    ).value
    project_priority = _sales_project_enum(
        configured_priority or ProjectPriority.normal.value,
        ProjectPriority,
        "default_project_priority",
    ).value
    number = _resolve_project_number(db)
    now = datetime.now(UTC)
    duration_days = Projects._duration_days_for_type(normalized_type)
    project = Project(
        name=str(name).strip()[:160],
        number=number,
        description="Implementation scope created from an accepted sales order",
        customer_address=(customer_address or "").strip() or None,
        project_type=normalized_type,
        project_template_id=(project_template.id if project_template else None),
        status=project_status,
        priority=project_priority,
        subscriber_id=subscriber_id,
        lead_id=lead_id,
        quote_id=quote_id,
        sales_order_id=sales_order_id,
        start_at=now,
        due_at=now + timedelta(days=duration_days) if duration_days else None,
        region=(region or "").strip() or None,
        metadata_={"fulfillment_contract_version": 1},
    )
    db.add(project)
    db.flush()
    _sync_project_sla_clock(db, project)
    if project_template is not None:
        ProjectTemplateTasks.replace_project_tasks(
            db,
            project_id=str(project.id),
            template_id=str(project_template.id),
        )
    else:
        _seed_fiber_installation_tasks(db, project)
    emit_event(
        db,
        EventType.project_created,
        {
            "project_id": str(project.id),
            "sales_order_id": str(sales_order_id),
            "quote_id": str(quote_id) if quote_id else None,
            "subscriber_id": str(subscriber_id),
            "project_type": normalized_type,
            "project_template_id": (
                str(project_template.id) if project_template else None
            ),
        },
        actor=actor_id,
        subscriber_id=subscriber_id,
    )
    return project


def prepare_buildout_project(
    db: Session,
    *,
    buildout_project_id: UUID,
    name: str,
    project_type: str,
    customer_address: str | None = None,
    region: str | None = None,
    actor_id: str,
) -> Project:
    """Create the native project root for one exact BuildoutProject, no commit.

    Network buildout is the other way work reaches a vendor. It has no
    subscriber, quote, or sales order — the project exists because we decided
    to build plant, not because someone bought a service. ``Project`` already
    allows that (``subscriber_id`` is nullable); only the sales entry point
    required one, which is why buildout could not previously be scoped.

    Idempotency rides the existing ``uq_projects_external_system_reference``
    constraint rather than a new column: one native project per buildout root.
    """

    existing = (
        db.query(Project)
        .filter(Project.external_system == BUILDOUT_PROJECT_SYSTEM)
        .filter(Project.external_reference == str(buildout_project_id))
        .one_or_none()
    )
    if existing is not None:
        return existing
    normalized_type = _sales_project_enum(
        project_type, ProjectType, "project_type"
    ).value
    configured_priority = _read_text_setting(
        db, SettingDomain.projects, "default_project_priority"
    )
    project_priority = _sales_project_enum(
        configured_priority or ProjectPriority.normal.value,
        ProjectPriority,
        "default_project_priority",
    ).value
    number = _resolve_project_number(db)
    now = datetime.now(UTC)
    duration_days = Projects._duration_days_for_type(normalized_type)
    project = Project(
        name=str(name).strip()[:160],
        number=number,
        description="Implementation scope created from a network buildout project",
        customer_address=(customer_address or "").strip() or None,
        project_type=normalized_type,
        status=ProjectStatus.open.value,
        priority=project_priority,
        external_system=BUILDOUT_PROJECT_SYSTEM,
        external_reference=str(buildout_project_id),
        start_at=now,
        due_at=now + timedelta(days=duration_days) if duration_days else None,
        region=(region or "").strip() or None,
        metadata_={"buildout_project_id": str(buildout_project_id)},
    )
    db.add(project)
    db.flush()
    _sync_project_sla_clock(db, project)
    emit_event(
        db,
        EventType.project_created,
        {
            "project_id": str(project.id),
            "buildout_project_id": str(buildout_project_id),
            "project_type": normalized_type,
        },
        actor=actor_id,
    )
    return project


def complete_from_verified_installation(
    db: Session,
    *,
    project_id: UUID,
    actor_id: str,
    verification_event_id: UUID,
) -> Project:
    """Apply verified implementation evidence to the native project root."""

    project = db.scalars(
        select(Project).where(Project.id == project_id).with_for_update()
    ).one_or_none()
    if project is None:
        raise SalesProjectLifecycleError(
            "project_not_found", "Project not found", kind="not_found"
        )
    if project.status == ProjectStatus.completed.value:
        return project
    if project.status == ProjectStatus.canceled.value:
        raise SalesProjectLifecycleError(
            "project_canceled",
            "Canceled project cannot receive implementation verification",
        )
    project.status = ProjectStatus.completed.value
    project.completed_at = datetime.now(UTC)
    metadata = dict(project.metadata_ or {})
    metadata["implementation_verification_event_id"] = str(verification_event_id)
    project.metadata_ = metadata
    _sync_project_sla_clock(db, project)
    _emit_project_event(
        db,
        "project.completed",
        project,
        {
            "project_id": str(project.id),
            "sales_order_id": str(project.sales_order_id)
            if project.sales_order_id
            else None,
            "verification_event_id": str(verification_event_id),
            "actor_id": actor_id,
        },
    )
    db.flush()
    return project


def _notify_project_roles_created_in_app(db: Session, project: Project) -> None:
    """In-app notifications for internal roles on project creation.

    Stored as Notification rows with a non-email channel so the email queue
    does not attempt delivery. Role UUIDs resolve via sub principals
    (SystemUser); legacy CRM staff ids that don't resolve are skipped.
    """
    role_specs: list[tuple[str, str]] = [
        ("project_manager_person_id", "Project Manager"),
        ("assistant_manager_person_id", "Site Project Coordinator"),
    ]

    roles_by_person_id: dict[UUID, list[str]] = {}
    person_ids: list[UUID] = []
    for attr, label in role_specs:
        person_id = getattr(project, attr, None)
        if not person_id:
            continue
        if person_id not in roles_by_person_id:
            roles_by_person_id[person_id] = []
            person_ids.append(person_id)
        if label not in roles_by_person_id[person_id]:
            roles_by_person_id[person_id].append(label)

    if not person_ids:
        return

    users = db.query(SystemUser).filter(SystemUser.id.in_(person_ids)).all()
    users_by_id = {user.id: user for user in users}

    base_url = _app_base_url(db)
    project_ref = project.number or str(project.id)
    project_url = (
        f"{base_url}/admin/projects/{project_ref}"
        if base_url
        else f"/admin/projects/{project_ref}"
    )

    site = (project.customer_address or project.region or "").strip()

    subject = f"New Project Assignment: {project.name}"
    # De-dupe by staff identity so one person with multiple roles gets one
    # notification.
    created_for: set[str] = set()
    for person_id, roles in roles_by_person_id.items():
        user = users_by_id.get(person_id)
        if not user:
            continue
        recipient = str(user.id)
        if recipient in created_for:
            continue
        created_for.add(recipient)

        roles_label = ", ".join(roles)
        body_lines = [f"You have been assigned as {roles_label} for this project."]
        if site:
            body_lines.append(f"Site: {site}.")
        body_lines.append(f"Open: {project_url}")

        queue_staff_push(
            db,
            recipient=recipient,
            subject=subject,
            body="\n".join(body_lines),
            target_url=f"/admin/projects/{project_ref}",
        )

    db.flush()


# ── reference guards ──────────────────────────────────────────────────────────


def _ensure_staff_uuid(person_id: str) -> None:
    """Validate an unfederated staff UUID carried from CRM or Sub identity."""
    try:
        coerce_uuid(str(person_id))
    except (ValueError, TypeError) as exc:
        raise _project_error("invalid_input", "Invalid person id") from exc


def _ensure_project_template(db: Session, template_id: str) -> ProjectTemplate:
    template = db.get(ProjectTemplate, coerce_uuid(template_id))
    if not template:
        raise _project_error("not_found", "Project template not found")
    return template


def _ensure_subscriber(db: Session, subscriber_id: str) -> None:
    ensure_exists(db, Subscriber, subscriber_id, "Subscriber not found")


def _ensure_lead(db: Session, lead_id: str) -> None:
    lead = db.get(Lead, coerce_uuid(lead_id))
    if not lead:
        raise _project_error("not_found", "Lead not found")


def _ensure_ticket(db: Session, ticket_id) -> None:
    ticket = db.get(Ticket, coerce_uuid(str(ticket_id)))
    if not ticket:
        raise _project_error("not_found", "Ticket not found")


# ── assignee handling ─────────────────────────────────────────────────────────


def _normalize_assignee_ids(assignee_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in assignee_ids:
        if not raw:
            continue
        try:
            coerced = str(coerce_uuid(raw))
        except Exception:  # noqa: BLE001 - invalid ids are skipped, CRM parity
            coerced = None
        if not coerced:
            continue
        if coerced not in seen:
            seen.add(coerced)
            normalized.append(coerced)
    return normalized


def _sync_project_task_assignees(
    db: Session, task: ProjectTask, assignee_ids: list[str] | None
) -> None:
    if assignee_ids is None:
        return
    normalized = _normalize_assignee_ids(assignee_ids)
    for person_id in normalized:
        _ensure_staff_uuid(person_id)

    task.assigned_to_person_id = coerce_uuid(normalized[0]) if normalized else None

    current_ids = {str(assignee.person_id) for assignee in task.assignees}
    target_ids = set(normalized)

    for person_id in target_ids - current_ids:
        task.assignees.append(
            ProjectTaskAssignee(task_id=task.id, person_id=coerce_uuid(person_id))
        )
    if target_ids != current_ids:
        for assignee in list(task.assignees):
            if str(assignee.person_id) not in target_ids:
                task.assignees.remove(assignee)


def _person_label(user: SystemUser | None) -> str:
    if not user:
        return "Someone"
    if user.display_name:
        return user.display_name
    name = f"{user.first_name} {user.last_name}".strip()
    if name:
        return name
    return user.email


def _format_dt(value: datetime | None) -> str | None:
    if not value:
        return None
    if value.tzinfo:
        return value.strftime("%b %d, %Y %H:%M %Z")
    return value.strftime("%b %d, %Y %H:%M")


def _notify_project_task_assigned(
    db: Session,
    task: ProjectTask,
    project: Project,
    assigned_to: SystemUser,
    *,
    include_push: bool = True,
) -> bool:
    if not assigned_to.email:
        logger.warning("project_task_assigned_missing_email task_id=%s", task.id)
        return False

    assignee_name = html.escape(_person_label(assigned_to))
    due_label = _format_dt(task.due_at)
    start_label = _format_dt(task.start_at)

    app_url = _app_base_url(db)
    task_url = f"{app_url}/admin/projects/tasks/{task.id}" if app_url else None
    project_ref = project.number or str(project.id)
    project_url = f"{app_url}/admin/projects/{project_ref}" if app_url else None

    company = html.escape(_company_name(db))
    logo_url = _DEFAULT_LOGO_URL

    subject = f"New project task assigned: {task.title or 'Task'}"
    safe_title = html.escape(task.title or "Task")
    safe_project = html.escape(project.name or "Project")
    status_label = html.escape(task.status) if task.status else "todo"
    priority_label = html.escape(task.priority) if task.priority else "normal"
    description_block = ""
    if task.description:
        description_block = (
            f"<p><strong>Description:</strong><br>{html.escape(task.description)}</p>"
        )

    task_link_url = task_url or f"{app_url}/admin/projects/tasks"
    task_link_block = (
        '<div style="text-align: center; margin: 20px 0;">'
        f'<a href="{task_link_url}" '
        'style="background-color: #16a34a; color: #fff; text-decoration: none; '
        "padding: 12px 20px; border-radius: 6px; display: inline-block; "
        'font-weight: 600;">'
        "View Project Task"
        "</a>"
        "</div>"
    )

    project_link_block = ""
    if project_url:
        project_link_block = (
            '<div style="text-align: center; margin: 12px 0 20px;">'
            f'<a href="{project_url}" '
            'style="background-color: #0f766e; color: #fff; '
            "text-decoration: none; padding: 12px 20px; border-radius: 6px; "
            'display: inline-block; font-weight: 600;">'
            "View Project"
            "</a>"
            "</div>"
        )

    logo_block = (
        '<div style="position: absolute; top: 15px; right: 15px;">'
        f'<img src="{html.escape(logo_url)}" '
        f'alt="{company}" style="max-width: 150px; height: auto;">'
        "</div>"
    )

    body = (
        "<div style=\"font-family: 'Segoe UI', Tahoma, Geneva, Verdana, "
        "sans-serif; line-height: 1.8; "
        "color: #333; background-color: #f4f4f9; padding: 25px; "
        "border: 1px solid #ccc; "
        "border-radius: 10px; box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1); "
        'position: relative;">'
        f"{logo_block}"
        '<div style="text-align: center; margin-bottom: 20px;">'
        '<h1 style="color: green; font-size: 24px; margin: 0;">Task Assigned</h1>'
        "</div>"
        f'<p style="font-size: 16px; color: green; margin-top: 20px;">'
        f"Dear {assignee_name},</p>"
        '<p style="font-size: 15px; color: #555; margin: 15px 0;">'
        "You have been assigned a new project task. Please find the details below:"
        "</p>"
        '<div style="background-color: #fff; border: 2px solid #e2e2e2; '
        'border-radius: 8px; padding: 20px; margin-bottom: 20px;">'
        f'<p style="font-size: 15px; margin: 0; line-height: 1.5;">'
        f'<strong style="color: red;">Task:</strong> '
        f'<span style="color: #555;">{safe_title}</span><br>'
        f'<strong style="color: red;">Project:</strong> '
        f'<span style="color: #555;">{safe_project}</span><br>'
        f'<strong style="color: red;">Status:</strong> '
        f'<span style="color: #555;">{status_label}</span><br>'
        f'<strong style="color: red;">Task ID:</strong> '
        f'<span style="color: #555;">{task.id}</span><br>'
        f'<strong style="color: red;">Start:</strong> '
        f'<span style="color: #555;">{start_label or "N/A"}</span><br>'
        f'<strong style="color: red;">Due:</strong> '
        f'<span style="color: #555;">{due_label or "N/A"}</span><br>'
        f'<strong style="color: red;">Priority:</strong> '
        f'<span style="color: #555;">{priority_label}</span>'
        f"</p>"
        "</div>"
        f"{description_block}"
        '<p style="font-size: 15px; color: #555; margin: 15px 0;">'
        "We will keep you updated with further progress."
        "</p>"
        f"{task_link_block}"
        f"{project_link_block}"
        '<p style="font-size: 15px; color: green; text-align: left; '
        'font-style: italic;">'
        f'Thank you for choosing <strong style="color: red;">{company}</strong>.'
        "</p>"
        '<p style="font-size: 15px; color: green; text-align: right; '
        'font-style: italic;">'
        "Best regards,<br>"
        f'<span style="color: red; font-weight: bold;">{company} Support Team'
        "</span>"
        "</p>"
        "</div>"
    )

    queue_staff_email(
        db,
        recipient=assigned_to.email,
        subject=subject,
        body=body,
    )
    if include_push:
        queue_staff_push(
            db,
            recipient=str(assigned_to.id),
            subject=subject,
            body=f"You have been assigned a project task: {task.title or 'Task'}",
            delivered=False,
            target_url=f"/admin/projects/tasks/{task.id}",
        )
    db.flush()
    return True


def _stage_project_task_assignment_notification(
    db: Session,
    *,
    task: ProjectTask,
    project: Project,
    assigned_to: SystemUser,
    context: CommandContext,
    include_push: bool,
) -> bool:
    """Isolate an optional delivery and record durable failure evidence."""

    try:
        return execute_owner_savepoint(
            db,
            lambda: _notify_project_task_assigned(
                db,
                task,
                project,
                assigned_to,
                include_push=include_push,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - assignment must remain successful
        logger.error(
            "project_task_assigned_notify_failed task_id=%s user_id=%s error=%s",
            task.id,
            assigned_to.id,
            exc,
        )
        _stage_project_audit(
            db,
            context=context,
            action="assignment_notification_failed",
            entity_type="project_task",
            entity_id=task.id,
            changed_fields=["assignee_email"],
        )
        return False


def _notify_new_project_task_assignees(
    db: Session,
    *,
    task: ProjectTask,
    project: Project,
    previous_assignee_ids: frozenset[UUID],
    context: CommandContext,
) -> ProjectTaskReassignmentNotificationOutcome:
    """Email active staff newly added to an already-existing project task."""

    added_assignee_ids = tuple(
        sorted(
            set(task.assigned_to_person_ids) - previous_assignee_ids,
            key=str,
        )
    )
    if not added_assignee_ids:
        return ProjectTaskReassignmentNotificationOutcome((), (), (), ())

    users = db.scalars(
        select(SystemUser)
        .where(
            SystemUser.is_active.is_(True),
            or_(
                SystemUser.id.in_(added_assignee_ids),
                SystemUser.person_party_id.in_(added_assignee_ids),
            ),
        )
        .order_by(SystemUser.id)
    ).all()

    queued_email_user_ids: list[UUID] = []
    failed_user_ids: list[UUID] = []
    resolved_assignee_ids: set[UUID] = set()
    for user in users:
        if user.id in added_assignee_ids:
            resolved_assignee_ids.add(user.id)
        if user.person_party_id in added_assignee_ids:
            resolved_assignee_ids.add(user.person_party_id)
        if _stage_project_task_assignment_notification(
            db,
            task=task,
            project=project,
            assigned_to=user,
            context=context,
            include_push=False,
        ):
            queued_email_user_ids.append(user.id)
        else:
            failed_user_ids.append(user.id)

    return ProjectTaskReassignmentNotificationOutcome(
        added_assignee_ids=added_assignee_ids,
        queued_email_user_ids=tuple(queued_email_user_ids),
        failed_user_ids=tuple(failed_user_ids),
        unresolved_assignee_ids=tuple(
            assignee_id
            for assignee_id in added_assignee_ids
            if assignee_id not in resolved_assignee_ids
        ),
    )


def _maybe_auto_assign_project(
    db: Session, project: Project, *, context: CommandContext
):
    """Apply workflow rule-based project assignments when enabled.

    The shared workflow control keeps its established external key
    ``ticket_auto_assignment_enabled``."""
    enabled = _read_bool_setting(
        db, SettingDomain.workflow, "ticket_auto_assignment_enabled", False
    )
    if not enabled:
        return None

    from app.services.ticket_assignment import auto_assign_project

    actor_id = (
        str(project.created_by_person_id) if project.created_by_person_id else None
    )
    results = auto_assign_project(
        db,
        str(project.id),
        trigger="create",
        actor_person_id=actor_id,
        context=context,
    )
    for result in results:
        action = (
            "project_auto_assigned" if result.assigned else "project_auto_assign_noop"
        )
        _stage_project_audit(
            db,
            context=context,
            action=action,
            entity_type="project",
            entity_id=project.id,
            changed_fields=["assignment"] if result.assigned else [],
        )
    # The engine flushes; persist assignments + audit rows here (the CRM
    # engine committed per rule).
    db.flush()
    return results


# ── lifecycle events ─────────────────────────────────────────────────────────


def _emit_project_event(
    db: Session,
    event_name: str,
    project: Project,
    payload: dict[str, Any],
) -> None:
    """Emit a sub-native event row for a project lifecycle change.

    Sub's EventType has no project members — like the support service, the
    CRM event name (``project.created`` …) travels in ``payload["name"]``
    (the project's display name is ``payload["project_name"]``). These stable
    event names form the automation contract."""
    emit_event(
        db,
        EventType.custom,
        {"name": event_name, **payload},
        subscriber_id=project.subscriber_id,
    )


class Projects(ListResponseMixin):
    PROJECT_TYPE_DURATIONS: ClassVar[dict[str, int]] = {
        ProjectType.air_fiber_installation.value: 3,
        ProjectType.air_fiber_relocation.value: 3,
        ProjectType.fiber_optics_installation.value: 14,
        ProjectType.fiber_optics_relocation.value: 14,
        ProjectType.cable_rerun.value: 5,
    }

    @staticmethod
    def _duration_days_for_type(project_type: str | None) -> int | None:
        if not project_type:
            return None
        return Projects.PROJECT_TYPE_DURATIONS.get(project_type)

    @staticmethod
    def _get_region_pm_assignments(
        db: Session, region: str | None
    ) -> tuple[str | None, str | None]:
        """Look up the PM person_id for the given region from the
        projects-domain ``region_pm_assignments`` setting.

        Project SPC assignment is intentionally disabled in the project flow.
        """
        if not region:
            return None, None
        region_pm_map = _read_json_setting(
            db, SettingDomain.projects, "region_pm_assignments"
        )
        if not region_pm_map:
            return None, None
        entry = region_pm_map.get(region)
        pm_id: str | None = None
        if isinstance(entry, dict):
            pm_id = entry.get("manager_person_id") or entry.get(
                "project_manager_person_id"
            )
        elif isinstance(entry, str):
            pm_id = entry
        if pm_id:
            user = db.get(SystemUser, coerce_uuid(pm_id))
            if not user:
                pm_id = None
            else:
                pm_id = str(user.id)
        return pm_id, None

    @staticmethod
    def _get_pm_for_region(db: Session, region: str | None) -> str | None:
        pm_id, _assistant_id = Projects._get_region_pm_assignments(db, region)
        return pm_id

    @staticmethod
    def list_for_site_surveys(db: Session):
        return (
            db.query(Project)
            .filter(Project.status.notin_(sorted(_PROJECT_TERMINAL_STATUSES)))
            .order_by(Project.name)
            .all()
        )

    @staticmethod
    def create(
        db: Session,
        payload: ProjectCreate,
        *,
        context: CommandContext | None = None,
    ) -> Project:
        if context is None:
            context = _project_command_context(
                action="create_project", actor=payload.created_by_person_id
            )
            db_session_adapter.release_read_transaction(db)
            return execute_owner_command(
                db,
                definition=_PROJECT_MUTATION,
                context=context,
                operation=lambda: Projects.create(db, payload, context=context),
            )
        if payload.created_by_person_id:
            _ensure_staff_uuid(str(payload.created_by_person_id))
        if payload.owner_person_id:
            _ensure_staff_uuid(str(payload.owner_person_id))
        if payload.manager_person_id:
            _ensure_staff_uuid(str(payload.manager_person_id))
        if payload.subscriber_id:
            _ensure_subscriber(db, str(payload.subscriber_id))
        if payload.lead_id:
            _ensure_lead(db, str(payload.lead_id))
        if payload.project_template_id:
            _ensure_project_template(db, str(payload.project_template_id))
        data = _model_data(payload.model_dump())
        number = _resolve_project_number(db)
        if number:
            data["number"] = number
        from app.services.ticket_assignment import (
            find_authoritative_project_creation_rule,
        )

        rule_probe = Project(**data)
        creation_rule = find_authoritative_project_creation_rule(db, rule_probe)
        if creation_rule:
            data["manager_person_id"] = None
            data["project_manager_person_id"] = None
            data["assistant_manager_person_id"] = None
            if creation_rule.team_id:
                data["service_team_id"] = creation_rule.team_id
        # Auto-assign PM based on region if not already specified
        if data.get("region") and not creation_rule:
            auto_pm, auto_spc = Projects._get_region_pm_assignments(db, data["region"])
            if auto_pm:
                if not data.get("project_manager_person_id"):
                    data["project_manager_person_id"] = coerce_uuid(auto_pm)
                if not data.get("manager_person_id"):
                    data["manager_person_id"] = coerce_uuid(auto_pm)
            if auto_spc and not data.get("assistant_manager_person_id"):
                data["assistant_manager_person_id"] = coerce_uuid(auto_spc)
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = _read_text_setting(
                db, SettingDomain.projects, "default_project_status"
            )
            if default_status:
                status_enum = validate_enum(default_status, ProjectStatus, "status")
                if status_enum:
                    data["status"] = status_enum.value
        if "priority" not in fields_set:
            default_priority = _read_text_setting(
                db, SettingDomain.projects, "default_project_priority"
            )
            if default_priority:
                priority_enum = validate_enum(
                    default_priority, ProjectPriority, "priority"
                )
                if priority_enum:
                    data["priority"] = priority_enum.value
        if not data.get("start_at") or not data.get("due_at"):
            duration_days = Projects._duration_days_for_type(data.get("project_type"))
            if duration_days:
                start_at = data.get("start_at") or datetime.now(UTC)
                data["start_at"] = start_at
                if not data.get("due_at"):
                    data["due_at"] = start_at + timedelta(days=duration_days)
        project = Project(**data)
        db.add(project)
        db.flush()
        _sync_project_sla_clock(db, project)
        db.flush()
        db.refresh(project)

        if not payload.project_template_id:
            _seed_fiber_installation_tasks(db, project)
            db.flush()
            db.refresh(project)

        customer_name = _subscriber_name(project.subscriber)
        if not customer_name and project.lead_id:
            customer_name = _subscriber_name(_lead_subscriber(db, project))

        if payload.project_template_id:
            ProjectTemplateTasks.replace_project_tasks(
                db=db,
                project_id=str(project.id),
                template_id=str(payload.project_template_id),
            )
            _maybe_auto_assign_project(db, project, context=context)
        else:
            _maybe_auto_assign_project(db, project, context=context)

        # Emit project created event after core project setup so failed
        # handlers cannot prevent template task creation or other intrinsic
        # project data.
        _emit_project_event(
            db,
            "project.created",
            project,
            {
                "project_id": str(project.id),
                "project_name": project.name,
                "status": project.status,
                "project_type": project.project_type,
                "region": project.region,
                "customer_name": customer_name,
            },
        )
        _stage_project_audit(
            db,
            context=context,
            action="create",
            entity_type="project",
            entity_id=project.id,
        )

        _stage_customer_project_created_email(
            db,
            project=project,
            context=context,
        )

        _notify_project_roles_created_in_app(db, project)

        return project

    @staticmethod
    def get(db: Session, project_id: str):
        project = db.get(Project, coerce_uuid(project_id))
        if not project:
            raise _project_error("not_found", "Project not found")
        return project

    @staticmethod
    def get_by_number(db: Session, number: str):
        if not number:
            raise _project_error("not_found", "Project not found")
        project = db.query(Project).filter(Project.number == number).first()
        if not project:
            raise _project_error("not_found", "Project not found")
        return project

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None,
        status: str | None,
        project_type: str | None,
        priority: str | None,
        owner_person_id: str | None,
        manager_person_id: str | None,
        project_manager_person_id: str | None,
        assistant_manager_person_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
        search: str | None = None,
        filter_clause: ColumnElement[bool] | None = None,
        region: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ):
        query = db.query(Project).options(selectinload(Project.subscriber))
        if subscriber_id:
            query = query.filter(Project.subscriber_id == coerce_uuid(subscriber_id))
        if status:
            status_enum = validate_enum(status, ProjectStatus, "status")
            query = query.filter(Project.status == status_enum.value)
        if project_type:
            type_enum = validate_enum(project_type, ProjectType, "project_type")
            query = query.filter(Project.project_type == type_enum.value)
        if priority:
            priority_enum = validate_enum(priority, ProjectPriority, "priority")
            query = query.filter(Project.priority == priority_enum.value)
        if owner_person_id:
            query = query.filter(
                Project.owner_person_id == coerce_uuid(owner_person_id)
            )
        if manager_person_id:
            query = query.filter(
                Project.manager_person_id == coerce_uuid(manager_person_id)
            )
        if project_manager_person_id:
            query = query.filter(
                Project.project_manager_person_id
                == coerce_uuid(project_manager_person_id)
            )
        if assistant_manager_person_id:
            query = query.filter(
                Project.assistant_manager_person_id
                == coerce_uuid(assistant_manager_person_id)
            )
        if region and region.strip():
            query = query.filter(Project.region == region.strip())
        if date_from:
            query = query.filter(
                Project.created_at >= datetime.combine(date_from, time.min, tzinfo=UTC)
            )
        if date_to:
            query = query.filter(
                Project.created_at <= datetime.combine(date_to, time.max, tzinfo=UTC)
            )
        if search and search.strip():
            like_term = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    Project.name.ilike(like_term),
                    Project.code.ilike(like_term),
                    Project.number.ilike(like_term),
                    Project.customer_address.ilike(like_term),
                    Project.region.ilike(like_term),
                )
            )
        if is_active is None:
            query = query.filter(Project.is_active.is_(True))
        else:
            query = query.filter(Project.is_active == is_active)
        if filter_clause is not None:
            query = query.filter(filter_clause)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": Project.created_at,
                "name": Project.name,
                "priority": Project.priority,
            },
        )
        query = query.order_by(Project.id.asc())
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def chart_summary(db: Session) -> dict:
        """Get status count aggregation for chart display."""
        rows = (
            db.query(Project.status, func.count(Project.id))
            .filter(Project.is_active.is_(True))
            .group_by(Project.status)
            .all()
        )
        counts = {status: count for status, count in rows if status}
        data = [
            {"status": status.value, "count": counts.get(status.value, 0)}
            for status in ProjectStatus
        ]
        return {"series": [{"label": "Projects", "data": data}]}

    @staticmethod
    def kanban_view(db: Session) -> dict:
        """Get kanban board columns and project records."""
        columns = [
            {"id": status.value, "title": status.value.replace("_", " ").title()}
            for status in ProjectStatus
        ]
        projects_list = (
            db.query(Project)
            .filter(Project.is_active.is_(True))
            .order_by(Project.updated_at.desc())
            .all()
        )
        records = []
        for project in projects_list:
            records.append(
                {
                    "id": str(project.id),
                    "name": project.name,
                    "project_type": project.project_type,
                    "status": project.status,
                    "due_date": project.due_at.date().isoformat()
                    if project.due_at
                    else None,
                }
            )
        return {"columns": columns, "records": records}

    @staticmethod
    def gantt_view(db: Session) -> dict:
        """Get gantt chart items with dates."""
        projects_list = (
            db.query(Project)
            .filter(Project.is_active.is_(True))
            .order_by(Project.updated_at.desc())
            .all()
        )
        items = []
        for project in projects_list:
            start_dt = project.start_at or project.created_at
            due_dt = project.due_at or start_dt
            items.append(
                {
                    "id": str(project.id),
                    "name": project.name,
                    "start_date": start_dt.date().isoformat() if start_dt else None,
                    "due_date": due_dt.date().isoformat() if due_dt else None,
                }
            )
        return {"items": items}

    @staticmethod
    def update_gantt_date(db: Session, project_id: str, field: str, value: str) -> dict:
        """Update a project date through the canonical project writer."""
        field_map = {
            "due_date": "due_at",
            "start_date": "start_at",
            "completed_date": "completed_at",
            "due_at": "due_at",
            "start_at": "start_at",
            "completed_at": "completed_at",
        }
        if field not in field_map:
            raise _project_error("invalid_input", "Invalid field", field=field)
        try:
            target_day = date.fromisoformat(value)
        except ValueError as exc:
            raise _project_error("invalid_input", "Invalid date", field=field) from exc
        Projects.update(
            db,
            project_id,
            ProjectUpdate.model_validate(
                {
                    field_map[field]: datetime.combine(
                        target_day,
                        time(23, 59, 59),
                        tzinfo=UTC,
                    )
                }
            ),
        )
        return {"status": "ok", "field": field, "value": target_day.isoformat()}

    @staticmethod
    def update_status(db: Session, project_id: str, new_status: str) -> dict:
        """Move a Kanban card through the canonical project lifecycle writer."""
        try:
            payload = ProjectUpdate(status=ProjectStatus(new_status))
        except ValueError as exc:
            raise _project_error("invalid_transition", "Invalid status") from exc
        Projects.update(db, project_id, payload)
        return {"status": "ok"}

    @staticmethod
    def delete(
        db: Session,
        project_id: str,
        *,
        actor_id: UUID | None = None,
        context: CommandContext | None = None,
    ):
        """Soft delete a project."""
        if context is None:
            context = _project_command_context(
                action="delete_project", actor=actor_id, aggregate_id=project_id
            )
            db_session_adapter.release_read_transaction(db)
            return execute_owner_command(
                db,
                definition=_PROJECT_MUTATION,
                context=context,
                operation=lambda: Projects.delete(
                    db,
                    project_id,
                    actor_id=actor_id,
                    context=context,
                ),
            )
        project = db.scalar(
            select(Project)
            .where(Project.id == coerce_uuid(project_id))
            .with_for_update()
        )
        if not project:
            raise _project_error("not_found", "Project not found")
        project.is_active = False
        _stage_project_audit(
            db,
            context=context,
            action="delete",
            entity_type="project",
            entity_id=project.id,
            changed_fields=["is_active"],
        )
        db.flush()

    @staticmethod
    def update(
        db: Session,
        project_id: str,
        payload: ProjectUpdate,
        *,
        actor_id: UUID | None = None,
        context: CommandContext | None = None,
    ):
        if context is None:
            context = _project_command_context(
                action="update_project",
                actor=actor_id or payload.created_by_person_id,
                aggregate_id=project_id,
            )
            db_session_adapter.release_read_transaction(db)
            return execute_owner_command(
                db,
                definition=_PROJECT_MUTATION,
                context=context,
                operation=lambda: Projects.update(
                    db,
                    project_id,
                    payload,
                    actor_id=actor_id,
                    context=context,
                ),
            )
        project = db.scalar(
            select(Project)
            .where(Project.id == coerce_uuid(project_id))
            .with_for_update()
        )
        if not project:
            raise _project_error("not_found", "Project not found")
        previous_status = project.status
        previous_template_id = (
            str(project.project_template_id) if project.project_template_id else None
        )
        data = _model_data(payload.model_dump(exclude_unset=True))
        if data.get("status"):
            _require_status_transition(
                current=project.status,
                requested=str(data["status"]),
                transitions=_PROJECT_STATUS_TRANSITIONS,
                aggregate="project",
            )
        if data.get("created_by_person_id"):
            _ensure_staff_uuid(str(data["created_by_person_id"]))
        if data.get("owner_person_id"):
            _ensure_staff_uuid(str(data["owner_person_id"]))
        if data.get("manager_person_id"):
            _ensure_staff_uuid(str(data["manager_person_id"]))
        if data.get("project_template_id"):
            _ensure_project_template(db, str(data["project_template_id"]))
        if data.get("lead_id"):
            _ensure_lead(db, str(data["lead_id"]))
        if data.get("subscriber_id"):
            _ensure_subscriber(db, str(data["subscriber_id"]))
        # Auto-assign PM based on region if region changes and no PM is set
        new_region = data.get("region")
        current_pm = (
            data.get("manager_person_id")
            if "manager_person_id" in data
            else project.manager_person_id
        )
        if new_region:
            auto_pm, auto_spc = Projects._get_region_pm_assignments(db, new_region)
            if auto_pm and not current_pm:
                data["manager_person_id"] = coerce_uuid(auto_pm)
            if (
                auto_pm
                and not project.project_manager_person_id
                and "project_manager_person_id" not in data
            ):
                data["project_manager_person_id"] = coerce_uuid(auto_pm)
            if (
                auto_spc
                and not project.assistant_manager_person_id
                and "assistant_manager_person_id" not in data
            ):
                data["assistant_manager_person_id"] = coerce_uuid(auto_spc)
        changed_fields = [
            key for key, value in data.items() if getattr(project, key) != value
        ]
        for key, value in data.items():
            setattr(project, key, value)
        if (
            data.get("status") == ProjectStatus.completed.value
            and project.completed_at is None
        ):
            project.completed_at = datetime.now(UTC)
        _sync_project_sla_clock(db, project)
        db.flush()
        db.refresh(project)

        # Emit events based on status changes
        new_status = project.status
        if (
            new_status == ProjectStatus.completed.value
            and previous_status != ProjectStatus.completed.value
        ):
            customer_name = _subscriber_name(project.subscriber)
            if not customer_name and project.lead_id:
                customer_name = _subscriber_name(_lead_subscriber(db, project))
            _emit_project_event(
                db,
                "project.completed",
                project,
                {
                    "project_id": str(project.id),
                    "project_name": project.name,
                    "from_status": previous_status,
                    "to_status": new_status,
                    "customer_name": customer_name,
                },
            )
            _stage_customer_status_transition(
                db,
                project=project,
                task=None,
                previous_status=previous_status,
                new_status=new_status,
                context=context,
            )
        elif (
            new_status == ProjectStatus.canceled.value
            and previous_status != ProjectStatus.canceled.value
        ):
            _emit_project_event(
                db,
                "project.canceled",
                project,
                {
                    "project_id": str(project.id),
                    "project_name": project.name,
                    "from_status": previous_status,
                    "to_status": new_status,
                },
            )
            _stage_customer_status_transition(
                db,
                project=project,
                task=None,
                previous_status=previous_status,
                new_status=new_status,
                context=context,
            )
        elif changed_fields:
            # Emit generic update if status changed or other fields updated
            _emit_project_event(
                db,
                "project.updated",
                project,
                {
                    "project_id": str(project.id),
                    "project_name": project.name,
                    "status": new_status,
                    "changed_fields": changed_fields,
                },
            )
            if previous_status != new_status:
                _stage_customer_status_transition(
                    db,
                    project=project,
                    task=None,
                    previous_status=previous_status,
                    new_status=new_status,
                    context=context,
                )

        if "project_template_id" in data:
            new_template_id = (
                str(project.project_template_id)
                if project.project_template_id
                else None
            )
            if previous_template_id != new_template_id:
                ProjectTemplateTasks.replace_project_tasks(
                    db=db, project_id=str(project.id), template_id=new_template_id
                )
        _stage_project_audit(
            db,
            context=context,
            action="update",
            entity_type="project",
            entity_id=project.id,
            changed_fields=changed_fields,
        )
        db.flush()
        return project


class ProjectTemplates(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ProjectTemplateCreate):
        data = _model_data(payload.model_dump())
        template = ProjectTemplate(**data)
        db.add(template)
        db.commit()
        db.refresh(template)
        return template

    @staticmethod
    def get(db: Session, template_id: str):
        template = db.get(ProjectTemplate, coerce_uuid(template_id))
        if not template:
            raise _project_error("not_found", "Project template not found")
        return template

    @staticmethod
    def list(
        db: Session,
        project_type: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ProjectTemplate)
        if project_type:
            type_enum = validate_enum(project_type, ProjectType, "project_type")
            query = query.filter(ProjectTemplate.project_type == type_enum.value)
        if is_active is None:
            query = query.filter(ProjectTemplate.is_active.is_(True))
        else:
            query = query.filter(ProjectTemplate.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": ProjectTemplate.created_at,
                "name": ProjectTemplate.name,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, template_id: str, payload: ProjectTemplateUpdate):
        template = db.get(ProjectTemplate, coerce_uuid(template_id))
        if not template:
            raise _project_error("not_found", "Project template not found")
        data = _model_data(payload.model_dump(exclude_unset=True))
        for key, value in data.items():
            setattr(template, key, value)
        db.commit()
        db.refresh(template)
        return template

    @staticmethod
    def delete(db: Session, template_id: str):
        template = db.get(ProjectTemplate, coerce_uuid(template_id))
        if not template:
            raise _project_error("not_found", "Project template not found")
        template.is_active = False
        db.commit()

    @staticmethod
    def list_tasks(db: Session, template_id: str):
        return (
            db.query(ProjectTemplateTask)
            .filter(ProjectTemplateTask.template_id == coerce_uuid(template_id))
            .filter(ProjectTemplateTask.is_active.is_(True))
            .order_by(
                ProjectTemplateTask.sort_order.asc(),
                ProjectTemplateTask.created_at.asc(),
            )
            .all()
        )


class ProjectTemplateTasks(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ProjectTemplateTaskCreate):
        _ensure_project_template(db, str(payload.template_id))
        data = _model_data(payload.model_dump())
        task = ProjectTemplateTask(**data)
        db.add(task)
        db.commit()
        db.refresh(task)
        return task

    @staticmethod
    def get(db: Session, task_id: str):
        task = db.get(ProjectTemplateTask, coerce_uuid(task_id))
        if not task:
            raise _project_error("not_found", "Project template task not found")
        return task

    @staticmethod
    def list(
        db: Session,
        template_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ProjectTemplateTask)
        if template_id:
            query = query.filter(
                ProjectTemplateTask.template_id == coerce_uuid(template_id)
            )
        if is_active is None:
            query = query.filter(ProjectTemplateTask.is_active.is_(True))
        else:
            query = query.filter(ProjectTemplateTask.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": ProjectTemplateTask.created_at,
                "sort_order": ProjectTemplateTask.sort_order,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, task_id: str, payload: ProjectTemplateTaskUpdate):
        task = db.get(ProjectTemplateTask, coerce_uuid(task_id))
        if not task:
            raise _project_error("not_found", "Project template task not found")
        data = _model_data(payload.model_dump(exclude_unset=True))
        for key, value in data.items():
            setattr(task, key, value)
        db.commit()
        db.refresh(task)
        return task

    @staticmethod
    def delete(db: Session, task_id: str):
        task = db.get(ProjectTemplateTask, coerce_uuid(task_id))
        if not task:
            raise _project_error("not_found", "Project template task not found")
        task.is_active = False
        db.query(ProjectTemplateTaskDependency).filter(
            ProjectTemplateTaskDependency.template_task_id == task.id
        ).delete(synchronize_session=False)
        db.query(ProjectTemplateTaskDependency).filter(
            ProjectTemplateTaskDependency.depends_on_template_task_id == task.id
        ).delete(synchronize_session=False)
        db.commit()

    @staticmethod
    def replace_project_tasks(db: Session, project_id: str, template_id: str | None):
        project_uuid = coerce_uuid(project_id)
        template_task_ids_subquery = select(ProjectTask.id).where(
            ProjectTask.project_id == project_uuid,
            ProjectTask.template_task_id.isnot(None),
        )
        db.query(ProjectTaskDependency).filter(
            ProjectTaskDependency.task_id.in_(template_task_ids_subquery)
        ).delete(synchronize_session=False)
        db.query(ProjectTaskDependency).filter(
            ProjectTaskDependency.depends_on_task_id.in_(template_task_ids_subquery)
        ).delete(synchronize_session=False)
        db.query(ProjectTask).filter(
            ProjectTask.project_id == project_uuid,
            ProjectTask.template_task_id.isnot(None),
        ).delete(synchronize_session=False)
        if not template_id:
            db.flush()
            return
        template_tasks = (
            db.query(ProjectTemplateTask)
            .filter(ProjectTemplateTask.template_id == coerce_uuid(template_id))
            .filter(ProjectTemplateTask.is_active.is_(True))
            .order_by(
                ProjectTemplateTask.sort_order.asc(),
                ProjectTemplateTask.created_at.asc(),
            )
            .all()
        )
        task_id_map: dict[str, str] = {}
        task_obj_map: dict[str, ProjectTask] = {}
        for template_task in template_tasks:
            data: dict = {
                "project_id": project_uuid,
                "title": template_task.title,
                "template_task_id": template_task.id,
                # Capture the automation decision on the ProjectTask. A later
                # template edit must not reinterpret an already-created
                # implementation scope during Quote-acceptance replay.
                "metadata_": template_task_work_order_automation_metadata(
                    template_task
                ),
            }
            number = generate_number(
                db=db,
                domain=SettingDomain.projects,
                sequence_key="project_task_number",
                enabled_key="project_task_number_enabled",
                prefix_key="project_task_number_prefix",
                padding_key="project_task_number_padding",
                start_key="project_task_number_start",
            )
            if number:
                data["number"] = number
            if template_task.description:
                data["description"] = template_task.description
            if template_task.status:
                data["status"] = template_task.status
            if template_task.priority:
                data["priority"] = template_task.priority
            if template_task.effort_hours is not None:
                data["effort_hours"] = template_task.effort_hours
            task = ProjectTask(**data)
            db.add(task)
            db.flush()
            task_id_map[str(template_task.id)] = str(task.id)
            task_obj_map[str(task.id)] = task

        template_task_ids = [template_task.id for template_task in template_tasks]
        dep_graph: dict[str, list[str]] = {}
        if template_task_ids:
            dependencies = (
                db.query(ProjectTemplateTaskDependency)
                .filter(
                    ProjectTemplateTaskDependency.template_task_id.in_(
                        template_task_ids
                    )
                )
                .all()
            )
            for dependency in dependencies:
                task_id = task_id_map.get(str(dependency.template_task_id))
                depends_on_id = task_id_map.get(
                    str(dependency.depends_on_template_task_id)
                )
                if not task_id or not depends_on_id or task_id == depends_on_id:
                    continue
                dep_graph.setdefault(task_id, []).append(depends_on_id)
                db.add(
                    ProjectTaskDependency(
                        task_id=coerce_uuid(task_id),
                        depends_on_task_id=coerce_uuid(depends_on_id),
                        dependency_type=dependency.dependency_type,
                        lag_days=dependency.lag_days,
                    )
                )

        # Auto-calculate start_at/due_at from effort_hours and dependencies
        project = db.get(Project, project_uuid)
        project_start = (
            project.start_at if project and project.start_at else datetime.now(UTC)
        )
        _calculate_task_dates(task_obj_map, dep_graph, project_start)

        db.flush()


def _calculate_task_dates(
    task_obj_map: dict[str, ProjectTask],
    dep_graph: dict[str, list[str]],
    project_start: datetime,
) -> None:
    """Calculate start_at/due_at for tasks based on effort_hours and dependencies.

    Tasks with no predecessors start at project_start.
    Tasks with predecessors start at the latest predecessor due_at.
    due_at = start_at + effort_hours (if effort_hours is set).
    """
    resolved: dict[str, datetime] = {}

    def _resolve_due(task_id: str) -> datetime | None:
        if task_id in resolved:
            return resolved[task_id]
        task = task_obj_map.get(task_id)
        if not task:
            return None

        predecessors = dep_graph.get(task_id, [])
        if predecessors:
            pred_dues = [_resolve_due(pid) for pid in predecessors]
            valid_dues = [d for d in pred_dues if d is not None]
            start = max(valid_dues) if valid_dues else project_start
        else:
            start = project_start

        task.start_at = start
        if task.effort_hours:
            task.due_at = start + timedelta(hours=task.effort_hours)
            resolved[task_id] = task.due_at
        else:
            resolved[task_id] = start
        return resolved[task_id]

    for task_id in task_obj_map:
        _resolve_due(task_id)


def _lock_project_task_scope(
    db: Session,
    task_id: UUID,
    *,
    require_active_task: bool = True,
) -> tuple[Project, ProjectTask]:
    """Lock the authoritative Project before its task and revalidate the scope."""

    observed_project_id = db.scalar(
        select(ProjectTask.project_id).where(ProjectTask.id == task_id)
    )
    if observed_project_id is None:
        raise _project_error("not_found", "Project task not found")
    project = db.scalar(
        select(Project).where(Project.id == observed_project_id).with_for_update()
    )
    if project is None:
        raise _project_error("relationship_conflict", "Task project not found")
    task = db.scalar(
        select(ProjectTask).where(ProjectTask.id == task_id).with_for_update()
    )
    if task is None:
        raise _project_error("not_found", "Project task not found")
    if task.project_id != project.id:
        raise _project_error(
            "stale_state", "Project task scope changed while acquiring locks"
        )
    if not project.is_active:
        raise _project_error("invalid_transition", "Project is archived")
    if require_active_task and not task.is_active:
        raise _project_error("invalid_transition", "Project task is archived")
    return project, task


def _validate_parent_task(
    db: Session,
    *,
    project_id: UUID,
    task_id: UUID | None,
    parent_task_id: UUID,
) -> ProjectTask:
    if task_id is not None and parent_task_id == task_id:
        raise _project_error("relationship_conflict", "Task cannot parent itself")
    parent = db.scalar(
        select(ProjectTask).where(ProjectTask.id == parent_task_id).with_for_update()
    )
    if parent is None or not parent.is_active:
        raise _project_error("not_found", "Active parent task not found")
    if parent.project_id != project_id:
        raise _project_error(
            "relationship_conflict", "Parent task must belong to the same project"
        )
    ancestor_id = parent.parent_task_id
    visited = {parent.id}
    while ancestor_id is not None:
        if ancestor_id == task_id:
            raise _project_error(
                "relationship_conflict", "Parent relationship would create a cycle"
            )
        if ancestor_id in visited:
            raise _project_error(
                "relationship_conflict", "Existing parent relationship contains a cycle"
            )
        visited.add(ancestor_id)
        ancestor = db.get(ProjectTask, ancestor_id)
        if ancestor is None or ancestor.project_id != project_id:
            raise _project_error(
                "relationship_conflict", "Parent chain leaves the authoritative project"
            )
        ancestor_id = ancestor.parent_task_id
    return parent


def _require_task_completion_ready(db: Session, task: ProjectTask) -> None:
    blockers = (
        db.query(ProjectTask)
        .join(
            ProjectTaskDependency,
            ProjectTaskDependency.depends_on_task_id == ProjectTask.id,
        )
        .filter(ProjectTaskDependency.task_id == task.id)
        .filter(
            or_(
                ProjectTask.is_active.is_(False),
                ProjectTask.status != ProjectTaskStatus.done.value,
            )
        )
        .order_by(ProjectTask.id)
        .all()
    )
    if blockers:
        raise _project_error(
            "relationship_conflict",
            "Complete all dependency tasks before completing this task",
            blocking_task_ids=[str(blocker.id) for blocker in blockers],
        )


def _dependency_graph_has_cycle(
    edges: dict[UUID, set[UUID]],
) -> bool:
    visiting: set[UUID] = set()
    visited: set[UUID] = set()

    def visit(node: UUID) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(dependency) for dependency in edges.get(node, set())):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in edges)


class ProjectTasks(ListResponseMixin):
    @staticmethod
    def create(
        db: Session,
        payload: ProjectTaskCreate,
        *,
        context: CommandContext | None = None,
    ):
        if context is None:
            context = _project_command_context(
                action="create_project_task", actor=payload.created_by_person_id
            )
            db_session_adapter.release_read_transaction(db)
            return execute_owner_command(
                db,
                definition=_PROJECT_MUTATION,
                context=context,
                operation=lambda: ProjectTasks.create(db, payload, context=context),
            )
        project = db.scalar(
            select(Project)
            .where(Project.id == coerce_uuid(str(payload.project_id)))
            .with_for_update()
        )
        if not project or not project.is_active:
            raise _project_error("not_found", "Project not found")
        if payload.parent_task_id:
            _validate_parent_task(
                db,
                project_id=project.id,
                task_id=None,
                parent_task_id=coerce_uuid(str(payload.parent_task_id)),
            )
        if payload.assigned_to_person_id:
            _ensure_staff_uuid(str(payload.assigned_to_person_id))
        if payload.created_by_person_id:
            _ensure_staff_uuid(str(payload.created_by_person_id))
        if payload.ticket_id:
            _ensure_ticket(db, payload.ticket_id)
        data = _model_data(payload.model_dump(exclude={"assigned_to_person_ids"}))
        fields_set = payload.model_fields_set
        assignee_ids: list[str] | None = None
        if "assigned_to_person_ids" in fields_set:
            assignee_ids = [
                str(value) for value in (payload.assigned_to_person_ids or [])
            ]
        elif payload.assigned_to_person_id:
            assignee_ids = [str(payload.assigned_to_person_id)]
        number = generate_number(
            db=db,
            domain=SettingDomain.projects,
            sequence_key="project_task_number",
            enabled_key="project_task_number_enabled",
            prefix_key="project_task_number_prefix",
            padding_key="project_task_number_padding",
            start_key="project_task_number_start",
        )
        if number:
            data["number"] = number
        if "status" not in fields_set:
            default_status = _read_text_setting(
                db, SettingDomain.projects, "default_task_status"
            )
            if default_status:
                status_enum = validate_enum(default_status, ProjectTaskStatus, "status")
                if status_enum:
                    data["status"] = status_enum.value
        if "priority" not in fields_set:
            default_priority = _read_text_setting(
                db, SettingDomain.projects, "default_task_priority"
            )
            if default_priority:
                priority_enum = validate_enum(
                    default_priority, ProjectTaskPriority, "priority"
                )
                if priority_enum:
                    data["priority"] = priority_enum.value
        task = ProjectTask(**data)
        db.add(task)
        db.flush()
        _apply_fiber_stage_defaults(db, task)
        if task.status == ProjectTaskStatus.done.value and not task.completed_at:
            task.completed_at = datetime.now(UTC)
        _sync_task_sla_clock(db, task)
        _sync_project_task_assignees(db, task, assignee_ids)
        db.flush()
        db.refresh(task)
        if task.assigned_to_person_id:
            assigned_to = db.get(SystemUser, task.assigned_to_person_id)
            if assigned_to:
                _stage_project_task_assignment_notification(
                    db,
                    task=task,
                    project=project,
                    assigned_to=assigned_to,
                    context=context,
                    include_push=True,
                )
        _stage_project_audit(
            db,
            context=context,
            action="create",
            entity_type="project_task",
            entity_id=task.id,
        )
        emit_event(
            db,
            EventType.custom,
            {
                "name": "project_task.created",
                "task_id": str(task.id),
                "project_id": str(project.id),
                "status": task.status,
                "priority": task.priority,
            },
            subscriber_id=project.subscriber_id,
        )
        return task

    @staticmethod
    def get(db: Session, task_id: str):
        task = db.get(ProjectTask, coerce_uuid(task_id))
        if not task:
            raise _project_error("not_found", "Project task not found")
        return task

    @staticmethod
    def get_by_number(db: Session, number: str):
        if not number:
            raise _project_error("not_found", "Project task not found")
        task = db.query(ProjectTask).filter(ProjectTask.number == number).first()
        if not task:
            raise _project_error("not_found", "Project task not found")
        return task

    @staticmethod
    def list(
        db: Session,
        project_id: str | None,
        status: str | None,
        priority: str | None,
        assigned_to_person_id: str | None,
        parent_task_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
        include_assigned: bool = False,
        filter_clause: ColumnElement[bool] | None = None,
    ):
        query = db.query(ProjectTask)
        if include_assigned:
            query = query.options(selectinload(ProjectTask.assignees))
        if project_id:
            query = query.filter(ProjectTask.project_id == coerce_uuid(project_id))
        if status:
            status_enum = validate_enum(status, ProjectTaskStatus, "status")
            query = query.filter(ProjectTask.status == status_enum.value)
        if priority:
            priority_enum = validate_enum(priority, ProjectTaskPriority, "priority")
            query = query.filter(ProjectTask.priority == priority_enum.value)
        if assigned_to_person_id:
            assigned_uuid = coerce_uuid(assigned_to_person_id)
            query = query.filter(
                or_(
                    ProjectTask.assigned_to_person_id == assigned_uuid,
                    exists().where(
                        ProjectTaskAssignee.task_id == ProjectTask.id,
                        ProjectTaskAssignee.person_id == assigned_uuid,
                    ),
                )
            )
        if parent_task_id:
            query = query.filter(
                ProjectTask.parent_task_id == coerce_uuid(parent_task_id)
            )
        if is_active is None:
            query = query.filter(ProjectTask.is_active.is_(True))
        else:
            query = query.filter(ProjectTask.is_active == is_active)
        if filter_clause is not None:
            query = query.filter(filter_clause)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": ProjectTask.created_at,
                "status": ProjectTask.status,
                "priority": ProjectTask.priority,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(
        db: Session,
        task_id: str,
        payload: ProjectTaskUpdate,
        *,
        actor_id: UUID | None = None,
        context: CommandContext | None = None,
        _status_transition: bool = False,
    ):
        if "status" in payload.model_fields_set and not _status_transition:
            raise _project_error(
                "invalid_transition",
                "Use the typed project-task status transition command",
            )
        if context is None:
            context = _project_command_context(
                action="update_project_task",
                actor=actor_id or payload.created_by_person_id,
                aggregate_id=task_id,
            )
            db_session_adapter.release_read_transaction(db)
            return execute_owner_command(
                db,
                definition=_PROJECT_MUTATION,
                context=context,
                operation=lambda: ProjectTasks.update(
                    db,
                    task_id,
                    payload,
                    actor_id=actor_id,
                    context=context,
                    _status_transition=_status_transition,
                ),
            )
        project, task = _lock_project_task_scope(db, coerce_uuid(task_id))
        previous_status = task.status
        previous_assignee_ids = frozenset(task.assigned_to_person_ids)
        changed_fields: list[str] = []
        data = _model_data(payload.model_dump(exclude_unset=True))
        if data.get("status"):
            _require_status_transition(
                current=task.status,
                requested=str(data["status"]),
                transitions=_PROJECT_TASK_STATUS_TRANSITIONS,
                aggregate="project_task",
            )
        assignee_ids: list[str] | None = None
        if "assigned_to_person_ids" in payload.model_fields_set:
            assignee_ids = [
                str(value) for value in (payload.assigned_to_person_ids or [])
            ]
        elif "assigned_to_person_id" in data:
            if data.get("assigned_to_person_id"):
                assignee_ids = [str(data["assigned_to_person_id"])]
            else:
                assignee_ids = []
        data.pop("assigned_to_person_ids", None)
        if "project_id" in data and coerce_uuid(str(data["project_id"])) != project.id:
            raise _project_error(
                "relationship_conflict",
                "Move a task through a dedicated project-transfer command",
            )
        if data.get("parent_task_id"):
            _validate_parent_task(
                db,
                project_id=project.id,
                task_id=task.id,
                parent_task_id=coerce_uuid(str(data["parent_task_id"])),
            )
        if data.get("assigned_to_person_id"):
            _ensure_staff_uuid(str(data["assigned_to_person_id"]))
        if data.get("created_by_person_id"):
            _ensure_staff_uuid(str(data["created_by_person_id"]))
        if data.get("ticket_id"):
            _ensure_ticket(db, data["ticket_id"])
        _preserve_template_task_work_order_automation(task, data)
        changed_fields.extend(list(data.keys()))
        for key, value in data.items():
            setattr(task, key, value)
        _apply_fiber_stage_defaults(db, task)
        if (
            previous_status != ProjectTaskStatus.done.value
            and task.status == ProjectTaskStatus.done.value
        ):
            _require_task_completion_ready(db, task)
        if task.status == ProjectTaskStatus.done.value and not task.completed_at:
            task.completed_at = datetime.now(UTC)
        _sync_task_sla_clock(db, task)
        _sync_project_task_assignees(db, task, assignee_ids)
        db.flush()
        db.refresh(task)
        if assignee_ids is not None:
            _notify_new_project_task_assignees(
                db,
                task=task,
                project=project,
                previous_assignee_ids=previous_assignee_ids,
                context=context,
            )
        if (
            "assigned_to_person_ids" in payload.model_fields_set
            or "assigned_to_person_id" in payload.model_fields_set
        ) and ("assigned_to_person_ids" not in changed_fields):
            changed_fields.append("assigned_to_person_ids")

        event_payload: dict[str, object | None] = {
            "task_id": str(task.id),
            "project_id": str(task.project_id) if task.project_id else None,
            "title": task.title,
            "from_status": previous_status,
            "to_status": task.status,
            "status": task.status,
            "priority": task.priority,
            "changed_fields": changed_fields,
        }

        if (
            previous_status != ProjectTaskStatus.done.value
            and task.status == ProjectTaskStatus.done.value
        ):
            _stage_customer_status_transition(
                db,
                project=project,
                task=task,
                previous_status=previous_status,
                new_status=task.status,
                context=context,
            )
            emit_event(
                db,
                EventType.custom,
                {"name": "project_task.completed", **event_payload},
                subscriber_id=project.subscriber_id,
            )
        elif previous_status != task.status or bool(changed_fields):
            emit_event(
                db,
                EventType.custom,
                {"name": "project_task.updated", **event_payload},
                subscriber_id=project.subscriber_id,
            )
            if project and previous_status != task.status:
                _stage_customer_status_transition(
                    db,
                    project=project,
                    task=task,
                    previous_status=previous_status,
                    new_status=task.status,
                    context=context,
                )
        _stage_project_audit(
            db,
            context=context,
            action="update",
            entity_type="project_task",
            entity_id=task.id,
            changed_fields=changed_fields,
        )
        return task

    @staticmethod
    def transition_status(
        db: Session,
        task_id: str,
        command: ProjectTaskStatusTransition,
        *,
        actor_id: UUID | None = None,
        context: CommandContext | None = None,
    ) -> ProjectTask:
        if context is None:
            context = _project_command_context(
                action="transition_project_task_status",
                actor=actor_id,
                aggregate_id=task_id,
                reason=command.reason,
            )
            db_session_adapter.release_read_transaction(db)
            return execute_owner_command(
                db,
                definition=_PROJECT_MUTATION,
                context=context,
                operation=lambda: ProjectTasks.transition_status(
                    db,
                    task_id,
                    command,
                    actor_id=actor_id,
                    context=context,
                ),
            )
        _project, task = _lock_project_task_scope(db, coerce_uuid(task_id))
        if task.status != command.expected_status.value:
            raise _project_error(
                "stale_state",
                "Project task status changed before this transition",
                expected_status=command.expected_status.value,
                current_status=task.status,
            )
        return ProjectTasks.update(
            db,
            task_id,
            ProjectTaskUpdate(status=command.status),
            actor_id=actor_id,
            context=context,
            _status_transition=True,
        )

    @staticmethod
    def replace_dependencies(
        db: Session,
        task_id: str,
        command: ProjectTaskDependenciesReplace,
        *,
        actor_id: UUID | None = None,
        context: CommandContext | None = None,
    ) -> ProjectTask:
        if context is None:
            context = _project_command_context(
                action="replace_project_task_dependencies",
                actor=actor_id,
                aggregate_id=task_id,
                reason=command.reason,
            )
            db_session_adapter.release_read_transaction(db)
            return execute_owner_command(
                db,
                definition=_PROJECT_MUTATION,
                context=context,
                operation=lambda: ProjectTasks.replace_dependencies(
                    db,
                    task_id,
                    command,
                    actor_id=actor_id,
                    context=context,
                ),
            )
        project, task = _lock_project_task_scope(db, coerce_uuid(task_id))
        if task.status != command.expected_task_status.value:
            raise _project_error(
                "stale_state",
                "Project task status changed before dependency replacement",
            )
        dependency_ids = [item.depends_on_task_id for item in command.dependencies]
        if len(dependency_ids) != len(set(dependency_ids)):
            raise _project_error("relationship_conflict", "Duplicate dependency task")
        dependencies: dict[UUID, ProjectTask] = {}
        if dependency_ids:
            rows = db.scalars(
                select(ProjectTask)
                .where(ProjectTask.id.in_(sorted(dependency_ids, key=str)))
                .order_by(ProjectTask.id)
                .with_for_update()
            ).all()
            dependencies = {row.id: row for row in rows}
        for dependency_id in dependency_ids:
            dependency = dependencies.get(dependency_id)
            if dependency is None or not dependency.is_active:
                raise _project_error("not_found", "Active dependency task not found")
            if dependency.project_id != project.id or dependency.id == task.id:
                raise _project_error(
                    "relationship_conflict",
                    "Dependencies must be distinct tasks in the same project",
                )

        existing_rows = (
            db.query(ProjectTaskDependency)
            .filter(
                ProjectTaskDependency.task_id.in_(
                    select(ProjectTask.id).where(ProjectTask.project_id == project.id)
                )
            )
            .all()
        )
        edges: dict[UUID, set[UUID]] = {}
        for row in existing_rows:
            if row.task_id != task.id:
                edges.setdefault(row.task_id, set()).add(row.depends_on_task_id)
        edges[task.id] = set(dependency_ids)
        if _dependency_graph_has_cycle(edges):
            raise _project_error(
                "relationship_conflict", "Dependency replacement would create a cycle"
            )

        db.query(ProjectTaskDependency).filter(
            ProjectTaskDependency.task_id == task.id
        ).delete(synchronize_session=False)
        for item in command.dependencies:
            db.add(
                ProjectTaskDependency(
                    task_id=task.id,
                    depends_on_task_id=item.depends_on_task_id,
                    dependency_type=item.dependency_type.value,
                    lag_days=item.lag_days,
                )
            )
        _stage_project_audit(
            db,
            context=context,
            action="replace_dependencies",
            entity_type="project_task",
            entity_id=task.id,
            changed_fields=["dependencies"],
        )
        emit_event(
            db,
            EventType.custom,
            {
                "name": "project_task.dependencies_replaced",
                "task_id": str(task.id),
                "project_id": str(project.id),
                "dependency_ids": [str(value) for value in dependency_ids],
                "reason": command.reason,
            },
            subscriber_id=project.subscriber_id,
        )
        db.flush()
        db.refresh(task)
        return task

    @staticmethod
    def delete(
        db: Session,
        task_id: str,
        *,
        actor_id: UUID | None = None,
        context: CommandContext | None = None,
    ):
        if context is None:
            context = _project_command_context(
                action="delete_project_task",
                actor=actor_id,
                aggregate_id=task_id,
            )
            db_session_adapter.release_read_transaction(db)
            return execute_owner_command(
                db,
                definition=_PROJECT_MUTATION,
                context=context,
                operation=lambda: ProjectTasks.delete(
                    db,
                    task_id,
                    actor_id=actor_id,
                    context=context,
                ),
            )
        _project, task = _lock_project_task_scope(db, coerce_uuid(task_id))
        task.is_active = False
        _stage_project_audit(
            db,
            context=context,
            action="delete",
            entity_type="project_task",
            entity_id=task.id,
            changed_fields=["is_active"],
        )
        db.flush()


class ProjectTaskComments(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ProjectTaskCommentCreate):
        task = db.get(ProjectTask, coerce_uuid(str(payload.task_id)))
        if not task:
            raise _project_error("not_found", "Project task not found")
        if payload.author_person_id:
            _ensure_staff_uuid(str(payload.author_person_id))
        comment = ProjectTaskComment(**payload.model_dump())
        db.add(comment)
        db.commit()
        db.refresh(comment)
        return comment

    @staticmethod
    def list(
        db: Session,
        task_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ProjectTaskComment)
        if task_id:
            query = query.filter(ProjectTaskComment.task_id == coerce_uuid(task_id))
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ProjectTaskComment.created_at},
        )
        return apply_pagination(query, limit, offset).all()


class ProjectComments(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ProjectCommentCreate):
        project = db.get(Project, coerce_uuid(str(payload.project_id)))
        if not project:
            raise _project_error("not_found", "Project not found")
        if payload.author_person_id:
            _ensure_staff_uuid(str(payload.author_person_id))
        comment = ProjectComment(**payload.model_dump())
        db.add(comment)
        db.commit()
        db.refresh(comment)
        return comment

    @staticmethod
    def update(db: Session, comment_id: str, payload: ProjectCommentUpdate):
        comment = db.get(ProjectComment, coerce_uuid(comment_id))
        if not comment:
            raise _project_error("not_found", "Comment not found")
        data = payload.model_dump(exclude_unset=True)
        if "body" in data and data["body"] is None:
            data.pop("body")
        if not data:
            return comment
        for key, value in data.items():
            setattr(comment, key, value)
        db.commit()
        db.refresh(comment)
        return comment

    @staticmethod
    def list(
        db: Session,
        project_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ProjectComment)
        if project_id:
            query = query.filter(ProjectComment.project_id == coerce_uuid(project_id))
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ProjectComment.created_at},
        )
        return apply_pagination(query, limit, offset).all()


projects = Projects()
project_tasks = ProjectTasks()
project_templates = ProjectTemplates()
project_template_tasks = ProjectTemplateTasks()
project_task_comments = ProjectTaskComments()
project_comments = ProjectComments()
