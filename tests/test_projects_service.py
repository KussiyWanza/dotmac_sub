"""Native project-domain service tests.

Covers the ported CRM semantics: fiber-stage seeding + SLA due computation,
template instantiation (dependencies + date calculation), the task state
machine, and project lifecycle transitions. Work-order execution links are
covered at their canonical command owner boundary.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.audit import AuditActorType, AuditEvent
from app.models.notification import Notification, NotificationChannel
from app.models.project import (
    Project,
    ProjectTask,
    ProjectTaskDependency,
    ProjectTemplate,
    ProjectTemplateTask,
    ProjectTemplateTaskDependency,
    ProjectType,
)
from app.models.sequence import DocumentSequence
from app.models.support import Ticket
from app.models.ticket_workflow import SlaClock, SlaClockStatus, WorkflowEntityType
from app.models.work_order import WorkOrder
from app.schemas.project import (
    ProjectCreate,
    ProjectTaskCreate,
    ProjectTaskDependenciesReplace,
    ProjectTaskDependencyInput,
    ProjectTaskStatusTransition,
    ProjectTaskUpdate,
    ProjectUpdate,
)
from app.services.owner_commands import CommandContext
from app.services.projects import (
    FIBER_INSTALLATION_STAGE_ORDER,
    FIBER_INSTALLATION_STAGE_TITLES,
    ProjectServiceError,
    project_tasks,
    projects,
    reconcile_project_projection,
)
from tests.staff_identity_fixtures import add_bound_staff_user


def _create_fiber_project(db_session, subscriber, **overrides):
    payload = ProjectCreate(
        name=overrides.pop("name", "Fiber install"),
        project_type=ProjectType.fiber_optics_installation,
        subscriber_id=subscriber.id,
        **overrides,
    )
    return projects.create(db_session, payload)


def _tasks_for(db_session, project):
    return (
        db_session.query(ProjectTask)
        .filter(ProjectTask.project_id == project.id)
        .order_by(ProjectTask.created_at.asc())
        .all()
    )


def test_project_number_continues_after_imported_series(db_session, subscriber):
    db_session.add(
        Project(
            name="Last repaired native project",
            number="PROJ-1107",
            status="active",
            priority="normal",
        )
    )
    db_session.add(DocumentSequence(key="project_number", next_value=8))
    db_session.commit()

    project = _create_fiber_project(db_session, subscriber)

    assert project.number == "PROJ-1108"
    assert (
        db_session.query(DocumentSequence)
        .filter(DocumentSequence.key == "project_number")
        .one()
        .next_value
        == 1109
    )


def test_project_owner_command_is_atomic_and_stages_audit(db_session, subscriber):
    project = _create_fiber_project(db_session, subscriber)

    assert not db_session.in_transaction()
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "project")
        .filter(AuditEvent.entity_id == str(project.id))
        .filter(AuditEvent.action == "create")
        .one()
    )
    assert audit.request_id


def test_project_creation_queues_customer_email(db_session, subscriber):
    project = _create_fiber_project(db_session, subscriber, name="Office fiber build")

    emails = (
        db_session.query(Notification)
        .filter(
            Notification.channel == NotificationChannel.email,
            Notification.recipient == subscriber.email,
            Notification.event_type == "project_created",
        )
        .all()
    )

    assert len(emails) == 1
    assert emails[0].subject == "Project created: Office fiber build"
    assert str(project.number or project.id) in (emails[0].body or "")


def test_project_status_change_queues_one_customer_email(db_session, subscriber):
    project = _create_fiber_project(db_session, subscriber)

    projects.update(db_session, str(project.id), ProjectUpdate(status="active"))

    emails = (
        db_session.query(Notification)
        .filter(
            Notification.channel == NotificationChannel.email,
            Notification.recipient == subscriber.email,
            Notification.event_type == "project_status_changed",
        )
        .all()
    )
    assert len(emails) == 1
    assert "Open to Active" in (emails[0].body or "")
    assert emails[0].metadata_["previous_status"] == "open"
    assert emails[0].metadata_["new_status"] == "active"


def test_project_task_status_change_queues_one_customer_email(db_session, subscriber):
    project = _create_fiber_project(db_session, subscriber)
    task = _tasks_for(db_session, project)[0]

    project_tasks.transition_status(
        db_session,
        str(task.id),
        ProjectTaskStatusTransition(
            expected_status=task.status,
            status="in_progress",
            reason="start customer task",
        ),
    )

    emails = (
        db_session.query(Notification)
        .filter(
            Notification.channel == NotificationChannel.email,
            Notification.recipient == subscriber.email,
            Notification.event_type == "project_task_status_changed",
        )
        .all()
    )
    assert len(emails) == 1
    assert "Todo to In Progress" in (emails[0].body or "")
    assert emails[0].metadata_["project_task_id"] == str(task.id)


def test_non_status_updates_do_not_queue_status_notifications(db_session, subscriber):
    project = _create_fiber_project(db_session, subscriber)
    task = _tasks_for(db_session, project)[0]

    projects.update(db_session, str(project.id), ProjectUpdate(name="Renamed build"))
    project_tasks.update(
        db_session,
        str(task.id),
        ProjectTaskUpdate(priority="high"),
    )

    assert (
        db_session.query(Notification)
        .filter(
            Notification.event_type.in_(
                ("project_status_changed", "project_task_status_changed")
            )
        )
        .count()
        == 0
    )


def test_completion_uses_specialized_message_without_generic_duplicate(
    db_session, subscriber
):
    project = _create_fiber_project(db_session, subscriber)

    projects.update(db_session, str(project.id), ProjectUpdate(status="completed"))

    assert (
        db_session.query(Notification)
        .filter(
            Notification.event_type == "project_completed",
            Notification.channel == NotificationChannel.email,
        )
        .count()
        == 1
    )
    assert (
        db_session.query(Notification)
        .filter(Notification.event_type == "project_status_changed")
        .count()
        == 0
    )


def test_status_notification_failure_keeps_transition_and_records_audit(
    db_session, subscriber, monkeypatch
):
    from app.services import customer_experience_communications

    project = _create_fiber_project(db_session, subscriber)

    def fail_request(*args, **kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(
        customer_experience_communications, "request_update", fail_request
    )

    updated = projects.update(
        db_session,
        str(project.id),
        ProjectUpdate(status="active"),
    )

    assert updated.status == "active"
    failure = (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.entity_type == "project",
            AuditEvent.entity_id == str(project.id),
            AuditEvent.action == "customer_status_notification_failed",
        )
        .one()
    )
    assert failure.metadata_["changed_fields"] == ["customer_notification", "status"]


def test_project_mutation_audits_preserve_authenticated_actor(db_session, subscriber):
    actor_id = uuid.uuid4()
    project = _create_fiber_project(db_session, subscriber)
    task = _tasks_for(db_session, project)[0]

    projects.update(
        db_session,
        str(project.id),
        ProjectUpdate(name="Authenticated project update"),
        actor_id=actor_id,
    )
    project_tasks.update(
        db_session,
        str(task.id),
        ProjectTaskUpdate(title="Authenticated task update"),
        actor_id=actor_id,
    )
    project_tasks.delete(db_session, str(task.id), actor_id=actor_id)
    projects.delete(db_session, str(project.id), actor_id=actor_id)

    mutation_audits = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.actor_id == str(actor_id))
        .filter(AuditEvent.entity_type.in_(("project", "project_task")))
        .filter(AuditEvent.action.in_(("update", "delete")))
        .all()
    )

    assert {(audit.entity_type, audit.action) for audit in mutation_audits} == {
        ("project", "update"),
        ("project", "delete"),
        ("project_task", "update"),
        ("project_task", "delete"),
    }
    assert all(audit.actor_type == AuditActorType.user for audit in mutation_audits)


def test_reconcile_project_projection_repairs_missing_sla_clock(db_session, subscriber):
    project = _create_fiber_project(db_session, subscriber)
    db_session.query(SlaClock).filter(
        SlaClock.entity_type == WorkflowEntityType.project.value,
        SlaClock.entity_id == project.id,
    ).delete(synchronize_session=False)
    db_session.commit()

    outcome = reconcile_project_projection(
        db_session,
        project_id=project.id,
        context=CommandContext.system(
            actor="system:test",
            scope="operations:projects:repair",
            reason="test projection repair",
            idempotency_key=f"repair:{project.id}",
        ),
    )

    assert outcome.repaired_sla_clocks >= 1
    assert (
        db_session.query(SlaClock)
        .filter(
            SlaClock.entity_type == WorkflowEntityType.project.value,
            SlaClock.entity_id == project.id,
        )
        .count()
        == 1
    )


@pytest.fixture()
def emitted_events(monkeypatch):
    """Record lifecycle events at the service seam (the dispatcher persists
    in a separate session invisible to the test transaction)."""
    events: list[dict] = []

    def _record(db, event_type, payload, **kwargs):
        events.append(payload)

    monkeypatch.setattr("app.services.projects.emit_event", _record)
    return events


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class TestFiberStageEngine:
    def test_create_seeds_six_stage_tasks(self, db_session, subscriber):
        project = _create_fiber_project(db_session, subscriber)

        tasks = _tasks_for(db_session, project)
        assert len(tasks) == len(FIBER_INSTALLATION_STAGE_ORDER)
        stage_keys = [t.metadata_["fiber_stage_key"] for t in tasks]
        assert stage_keys == list(FIBER_INSTALLATION_STAGE_ORDER)
        for task in tasks:
            assert task.metadata_["fiber_sla_managed"] is True
            assert (
                task.metadata_["fiber_stage_title"]
                == FIBER_INSTALLATION_STAGE_TITLES[task.metadata_["fiber_stage_key"]]
            )
            assert task.status == "todo"
            assert task.due_at is not None

        # Duration default for fiber installs: 14 days (CRM parity).
        assert project.start_at is not None
        assert project.due_at is not None
        delta = project.due_at - project.start_at
        assert delta == timedelta(days=14)

    def test_seeded_tasks_open_sla_clocks(self, db_session, subscriber):
        project = _create_fiber_project(db_session, subscriber)
        tasks = _tasks_for(db_session, project)
        task_ids = {t.id for t in tasks}
        clocks = (
            db_session.query(SlaClock)
            .filter(
                SlaClock.entity_type == WorkflowEntityType.project_task.value,
                SlaClock.entity_id.in_(task_ids),
            )
            .all()
        )
        assert len(clocks) == len(tasks)
        assert {c.status for c in clocks} == {SlaClockStatus.running.value}

        project_clocks = (
            db_session.query(SlaClock)
            .filter(
                SlaClock.entity_type == WorkflowEntityType.project.value,
                SlaClock.entity_id == project.id,
            )
            .all()
        )
        assert len(project_clocks) == 1

    def test_no_seed_for_non_fiber_types(self, db_session, subscriber):
        payload = ProjectCreate(
            name="Cable rerun",
            project_type=ProjectType.cable_rerun,
            subscriber_id=subscriber.id,
        )
        project = projects.create(db_session, payload)
        assert _tasks_for(db_session, project) == []

    def test_stage_due_recomputed_from_anchor(self, db_session, subscriber):
        project = _create_fiber_project(db_session, subscriber)
        tasks = {
            t.metadata_["fiber_stage_key"]: t for t in _tasks_for(db_session, project)
        }

        plan = tasks["project_plan"]
        project_tasks.transition_status(
            db_session,
            str(plan.id),
            ProjectTaskStatusTransition(
                expected_status=plan.status,
                status="done",
                reason="complete planning",
            ),
        )
        db_session.refresh(plan)
        assert plan.completed_at is not None

        survey = tasks["project_survey"]
        project_tasks.transition_status(
            db_session,
            str(survey.id),
            ProjectTaskStatusTransition(
                expected_status=survey.status,
                status="in_progress",
                reason="start survey",
            ),
        )
        db_session.refresh(survey)
        # Survey due anchors on the completed plan stage + 24h.
        assert survey.due_at == plan.completed_at + timedelta(hours=24)


class TestTaskStateMachine:
    def test_done_sets_completed_at_and_completes_clock(
        self, db_session, subscriber, emitted_events
    ):
        project = _create_fiber_project(db_session, subscriber)
        task = _tasks_for(db_session, project)[0]

        updated = project_tasks.transition_status(
            db_session,
            str(task.id),
            ProjectTaskStatusTransition(
                expected_status=task.status,
                status="done",
                reason="complete task",
            ),
        )
        assert updated.status == "done"
        assert updated.completed_at is not None

        clock = (
            db_session.query(SlaClock)
            .filter(
                SlaClock.entity_type == WorkflowEntityType.project_task.value,
                SlaClock.entity_id == task.id,
            )
            .order_by(SlaClock.created_at.desc())
            .first()
        )
        assert clock is not None
        assert clock.status == SlaClockStatus.completed.value

        assert "project_task.completed" in [e.get("name") for e in emitted_events]

    def test_done_queues_customer_stage_email(self, db_session, subscriber):
        project = _create_fiber_project(db_session, subscriber)
        task = _tasks_for(db_session, project)[0]
        project_tasks.transition_status(
            db_session,
            str(task.id),
            ProjectTaskStatusTransition(
                expected_status=task.status,
                status="done",
                reason="complete customer stage",
            ),
        )

        emails = (
            db_session.query(Notification)
            .filter(Notification.channel == NotificationChannel.email)
            .filter(Notification.recipient == subscriber.email)
            .all()
        )
        assert any(n.subject == "Project Update - Stage Completed" for n in emails)

    def test_transition_fails_closed_on_stale_expected_status(
        self, db_session, subscriber
    ):
        project = _create_fiber_project(db_session, subscriber)
        task = _tasks_for(db_session, project)[0]

        with pytest.raises(ProjectServiceError) as exc:
            project_tasks.transition_status(
                db_session,
                str(task.id),
                ProjectTaskStatusTransition(
                    expected_status="in_progress",
                    status="done",
                    reason="close completed stage",
                ),
            )

        assert exc.value.code == "operations.project_lifecycle.stale_state"

    def test_dependency_completion_and_cycle_guards(self, db_session, subscriber):
        project = _create_fiber_project(db_session, subscriber)
        predecessor = project_tasks.create(
            db_session,
            ProjectTaskCreate(project_id=project.id, title="Predecessor"),
        )
        dependent = project_tasks.create(
            db_session,
            ProjectTaskCreate(project_id=project.id, title="Dependent"),
        )
        project_tasks.replace_dependencies(
            db_session,
            str(dependent.id),
            ProjectTaskDependenciesReplace(
                expected_task_status="todo",
                dependencies=[
                    ProjectTaskDependencyInput(depends_on_task_id=predecessor.id)
                ],
                reason="sequence field work",
            ),
        )

        with pytest.raises(ProjectServiceError) as exc:
            project_tasks.transition_status(
                db_session,
                str(dependent.id),
                ProjectTaskStatusTransition(
                    expected_status="todo",
                    status="done",
                    reason="finish dependent task",
                ),
            )
        assert exc.value.code == "operations.project_lifecycle.relationship_conflict"

        with pytest.raises(ProjectServiceError) as exc:
            project_tasks.replace_dependencies(
                db_session,
                str(predecessor.id),
                ProjectTaskDependenciesReplace(
                    expected_task_status="todo",
                    dependencies=[
                        ProjectTaskDependencyInput(depends_on_task_id=dependent.id)
                    ],
                    reason="invalid reverse dependency",
                ),
            )
        assert exc.value.code == "operations.project_lifecycle.relationship_conflict"

        project_tasks.transition_status(
            db_session,
            str(predecessor.id),
            ProjectTaskStatusTransition(
                expected_status="todo",
                status="done",
                reason="finish predecessor",
            ),
        )
        completed = project_tasks.transition_status(
            db_session,
            str(dependent.id),
            ProjectTaskStatusTransition(
                expected_status="todo",
                status="done",
                reason="finish dependent task",
            ),
        )
        assert completed.status == "done"

    def test_parent_relationship_must_stay_in_active_project(
        self, db_session, subscriber
    ):
        first_project = _create_fiber_project(db_session, subscriber)
        second_project = _create_fiber_project(db_session, subscriber)
        foreign_parent = _tasks_for(db_session, second_project)[0]

        with pytest.raises(ProjectServiceError) as exc:
            project_tasks.create(
                db_session,
                ProjectTaskCreate(
                    project_id=first_project.id,
                    parent_task_id=foreign_parent.id,
                    title="Cross-project child",
                ),
            )

        assert exc.value.code == "operations.project_lifecycle.relationship_conflict"

    def test_assignee_sync_multi(self, db_session, subscriber):
        project = _create_fiber_project(db_session, subscriber)
        task = _tasks_for(db_session, project)[0]
        first, second = uuid.uuid4(), uuid.uuid4()

        updated = project_tasks.update(
            db_session,
            str(task.id),
            ProjectTaskUpdate(assigned_to_person_ids=[first, second]),
        )
        assert updated.assigned_to_person_id == first
        assert {a.person_id for a in updated.assignees} == {first, second}

        updated = project_tasks.update(
            db_session,
            str(task.id),
            ProjectTaskUpdate(assigned_to_person_ids=[second]),
        )
        assert updated.assigned_to_person_id == second
        assert {a.person_id for a in updated.assignees} == {second}

    def test_reassignment_emails_only_new_active_assignee(self, db_session, subscriber):
        project = _create_fiber_project(db_session, subscriber)
        task = _tasks_for(db_session, project)[0]
        first_user, _first_person = add_bound_staff_user(
            db_session, email="first-assignee@example.test"
        )
        second_user, second_person = add_bound_staff_user(
            db_session, email="second-assignee@example.test"
        )
        inactive_user, inactive_person = add_bound_staff_user(
            db_session,
            email="inactive-assignee@example.test",
            is_active=False,
        )
        db_session.commit()

        project_tasks.update(
            db_session,
            str(task.id),
            ProjectTaskUpdate(assigned_to_person_id=first_user.id),
        )
        first_email_count = (
            db_session.query(Notification)
            .filter(
                Notification.channel == NotificationChannel.email,
                Notification.recipient == first_user.email,
            )
            .count()
        )
        assert first_email_count == 1

        project_tasks.update(
            db_session,
            str(task.id),
            ProjectTaskUpdate(
                assigned_to_person_ids=[
                    first_user.id,
                    second_person.id,
                    inactive_person.id,
                ]
            ),
        )

        assert (
            db_session.query(Notification)
            .filter(
                Notification.channel == NotificationChannel.email,
                Notification.recipient == first_user.email,
            )
            .count()
            == first_email_count
        )
        second_emails = (
            db_session.query(Notification)
            .filter(
                Notification.channel == NotificationChannel.email,
                Notification.recipient == second_user.email,
            )
            .all()
        )
        assert len(second_emails) == 1
        assert second_emails[0].subject.startswith("New project task assigned:")
        assert (
            db_session.query(Notification)
            .filter(Notification.recipient == inactive_user.email)
            .count()
            == 0
        )
        assert (
            db_session.query(Notification)
            .filter(
                Notification.channel == NotificationChannel.push,
                Notification.recipient == second_user.email,
            )
            .count()
            == 0
        )

        project_tasks.update(
            db_session,
            str(task.id),
            ProjectTaskUpdate(assigned_to_person_ids=[second_person.id]),
        )
        project_tasks.update(
            db_session,
            str(task.id),
            ProjectTaskUpdate(priority="high"),
        )
        assert (
            db_session.query(Notification)
            .filter(
                Notification.channel == NotificationChannel.email,
                Notification.recipient == second_user.email,
            )
            .count()
            == 1
        )

    def test_ticket_link_requires_existing_support_ticket(self, db_session, subscriber):
        project = _create_fiber_project(db_session, subscriber)

        with pytest.raises(ProjectServiceError) as exc:
            project_tasks.create(
                db_session,
                ProjectTaskCreate(
                    project_id=project.id,
                    title="Linked task",
                    ticket_id=uuid.uuid4(),
                ),
            )
        assert exc.value.code == "operations.project_lifecycle.not_found"

        ticket = Ticket(title="Install issue", subscriber_id=subscriber.id)
        db_session.add(ticket)
        db_session.commit()
        task = project_tasks.create(
            db_session,
            ProjectTaskCreate(
                project_id=project.id,
                title="Linked task",
                ticket_id=ticket.id,
            ),
        )
        assert task.ticket_id == ticket.id

    def test_project_task_exposes_native_one_to_many_field_visits(
        self, db_session, subscriber
    ):
        project = _create_fiber_project(db_session, subscriber)
        task = project_tasks.create(
            db_session,
            ProjectTaskCreate(project_id=project.id, title="WO task"),
        )
        first = WorkOrder(
            public_id="sub-visit-one",
            subscriber_id=subscriber.id,
            project_id=project.id,
            project_task_id=task.id,
            title="First visit",
            status="scheduled",
        )
        second = WorkOrder(
            public_id="sub-visit-two",
            subscriber_id=subscriber.id,
            project_id=project.id,
            project_task_id=task.id,
            title="Follow-up visit",
            status="scheduled",
        )
        db_session.add_all(
            [
                first,
                second,
            ]
        )
        db_session.commit()
        db_session.refresh(task)

        assert {row.public_id for row in task.work_orders} == {
            "sub-visit-one",
            "sub-visit-two",
        }
        assert first.project_task_id == task.id
        assert second.project_task_id == task.id


class TestTemplateInstantiation:
    def _template_with_tasks(self, db_session):
        template = ProjectTemplate(name="Relocation flow")
        db_session.add(template)
        db_session.flush()
        first = ProjectTemplateTask(
            template_id=template.id,
            title="Survey site",
            sort_order=1,
            effort_hours=4,
        )
        second = ProjectTemplateTask(
            template_id=template.id,
            title="Move equipment",
            sort_order=2,
            effort_hours=8,
        )
        db_session.add_all([first, second])
        db_session.flush()
        db_session.add(
            ProjectTemplateTaskDependency(
                template_task_id=second.id,
                depends_on_template_task_id=first.id,
            )
        )
        db_session.commit()
        return template, first, second

    def test_create_with_template_instantiates_tasks_and_dependencies(
        self, db_session, subscriber
    ):
        template, first, second = self._template_with_tasks(db_session)
        start_at = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
        payload = ProjectCreate(
            name="Relocation",
            project_type=ProjectType.fiber_optics_relocation,
            subscriber_id=subscriber.id,
            project_template_id=template.id,
            start_at=start_at,
        )
        project = projects.create(db_session, payload)

        tasks = _tasks_for(db_session, project)
        assert [t.title for t in tasks] == ["Survey site", "Move equipment"]
        by_template = {t.template_task_id: t for t in tasks}
        assert set(by_template) == {first.id, second.id}

        deps = (
            db_session.query(ProjectTaskDependency)
            .filter(
                ProjectTaskDependency.task_id.in_([t.id for t in tasks]),
            )
            .all()
        )
        assert len(deps) == 1
        assert deps[0].task_id == by_template[second.id].id
        assert deps[0].depends_on_task_id == by_template[first.id].id
        assert deps[0].dependency_type == "finish_to_start"

        # Date calculation: no-predecessor task starts at project start;
        # dependent task starts at its predecessor's due date.
        survey = by_template[first.id]
        move = by_template[second.id]
        assert _utc(survey.start_at) == start_at
        assert _utc(survey.due_at) == start_at + timedelta(hours=4)
        assert _utc(move.start_at) == _utc(survey.due_at)
        assert _utc(move.due_at) == _utc(survey.due_at) + timedelta(hours=8)

    def test_template_swap_replaces_template_tasks(self, db_session, subscriber):
        template, _first, _second = self._template_with_tasks(db_session)
        payload = ProjectCreate(
            name="Relocation",
            project_type=ProjectType.fiber_optics_relocation,
            subscriber_id=subscriber.id,
            project_template_id=template.id,
        )
        project = projects.create(db_session, payload)
        assert len(_tasks_for(db_session, project)) == 2

        projects.update(
            db_session, str(project.id), ProjectUpdate(project_template_id=None)
        )
        assert _tasks_for(db_session, project) == []


class TestProjectLifecycle:
    def test_update_to_completed(self, db_session, subscriber, emitted_events):
        project = _create_fiber_project(db_session, subscriber)

        updated = projects.update(
            db_session, str(project.id), ProjectUpdate(status="completed")
        )
        assert updated.status == "completed"
        assert updated.completed_at is not None

        clock = (
            db_session.query(SlaClock)
            .filter(
                SlaClock.entity_type == WorkflowEntityType.project.value,
                SlaClock.entity_id == project.id,
            )
            .order_by(SlaClock.created_at.desc())
            .first()
        )
        assert clock is not None
        assert clock.status == SlaClockStatus.completed.value

        assert "project.completed" in [e.get("name") for e in emitted_events]

        emails = (
            db_session.query(Notification)
            .filter(Notification.channel == NotificationChannel.email)
            .filter(Notification.recipient == subscriber.email)
            .all()
        )
        assert any(n.subject.startswith("Project completed:") for n in emails)

    def test_update_status_kanban_move_uses_canonical_lifecycle(
        self, db_session, subscriber, emitted_events
    ):
        project = _create_fiber_project(db_session, subscriber)
        emitted_events.clear()

        result = projects.update_status(db_session, str(project.id), "completed")

        assert result == {"status": "ok"}
        db_session.refresh(project)
        assert project.status == "completed"
        assert project.completed_at is not None

        clock = (
            db_session.query(SlaClock)
            .filter(
                SlaClock.entity_type == WorkflowEntityType.project.value,
                SlaClock.entity_id == project.id,
            )
            .order_by(SlaClock.created_at.desc())
            .first()
        )
        assert clock is not None
        assert clock.status == SlaClockStatus.completed.value
        assert [event["name"] for event in emitted_events] == ["project.completed"]

        emails = (
            db_session.query(Notification)
            .filter(Notification.channel == NotificationChannel.email)
            .filter(Notification.recipient == subscriber.email)
            .all()
        )
        assert any(n.subject.startswith("Project completed:") for n in emails)

        with pytest.raises(ProjectServiceError) as exc:
            projects.update_status(db_session, str(project.id), "not-a-status")
        assert exc.value.code == "operations.project_lifecycle.invalid_transition"

    def test_update_gantt_date_uses_canonical_update(
        self, db_session, subscriber, emitted_events
    ):
        project = _create_fiber_project(db_session, subscriber)
        emitted_events.clear()

        result = projects.update_gantt_date(
            db_session,
            str(project.id),
            "due_date",
            "2026-08-31",
        )

        assert result == {
            "status": "ok",
            "field": "due_date",
            "value": "2026-08-31",
        }
        db_session.refresh(project)
        assert _utc(project.due_at) == datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC)

        clock = (
            db_session.query(SlaClock)
            .filter(
                SlaClock.entity_type == WorkflowEntityType.project.value,
                SlaClock.entity_id == project.id,
            )
            .order_by(SlaClock.created_at.desc())
            .first()
        )
        assert clock is not None
        assert _utc(clock.due_at) == _utc(project.due_at)
        assert emitted_events == [
            {
                "name": "project.updated",
                "project_id": str(project.id),
                "project_name": project.name,
                "status": project.status,
                "changed_fields": ["due_at"],
            }
        ]

    def test_update_status_noop_does_not_emit_duplicate_event(
        self, db_session, subscriber, emitted_events
    ):
        project = _create_fiber_project(db_session, subscriber)
        projects.update_status(db_session, str(project.id), "active")
        emitted_events.clear()

        result = projects.update_status(db_session, str(project.id), "active")

        assert result == {"status": "ok"}
        assert emitted_events == []

    def test_soft_delete(self, db_session, subscriber):
        project = _create_fiber_project(db_session, subscriber)
        projects.delete(db_session, str(project.id))
        db_session.refresh(project)
        assert project.is_active is False

    def test_chart_summary_shape(self, db_session, subscriber):
        _create_fiber_project(db_session, subscriber)
        summary = projects.chart_summary(db_session)
        assert set(summary) == {"series"}
        data = summary["series"][0]["data"]
        assert {row["status"] for row in data} == {
            "open",
            "planned",
            "active",
            "on_hold",
            "completed",
            "canceled",
        }

    def test_kanban_and_gantt_views(self, db_session, subscriber):
        project = _create_fiber_project(db_session, subscriber)
        kanban = projects.kanban_view(db_session)
        assert str(project.id) in {r["id"] for r in kanban["records"]}
        assert {c["id"] for c in kanban["columns"]} >= {"open", "completed"}

        gantt = projects.gantt_view(db_session)
        item = next(i for i in gantt["items"] if i["id"] == str(project.id))
        assert item["start_date"] is not None
        assert item["due_date"] is not None

    def test_get_missing_project_404(self, db_session):
        with pytest.raises(ProjectServiceError) as exc:
            projects.get(db_session, str(uuid.uuid4()))
        assert exc.value.code == "operations.project_lifecycle.not_found"

    def test_list_filters_by_status_and_subscriber(self, db_session, subscriber):
        project = _create_fiber_project(db_session, subscriber)
        rows = projects.list(
            db_session,
            str(subscriber.id),
            "open",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "created_at",
            "desc",
            50,
            0,
        )
        assert [r.id for r in rows] == [project.id]
        assert (
            projects.list(
                db_session,
                str(subscriber.id),
                "completed",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                "created_at",
                "desc",
                50,
                0,
            )
            == []
        )

    def test_list_response_shell(self, db_session, subscriber):
        _create_fiber_project(db_session, subscriber)
        shell = projects.list_response(
            db_session,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "created_at",
            "desc",
            50,
            0,
        )
        assert set(shell) == {"items", "count", "limit", "offset"}
        assert shell["count"] >= 1
