from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.catalog import SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.project import Project, ProjectTask, ProjectTaskStatus
from app.models.provisioning import (
    ProvisioningReadinessCheckKind,
    ProvisioningReadinessCheckResult,
    ProvisioningReadinessDecision,
    ProvisioningReadinessDecisionStatus,
    ProvisioningReadinessEvidenceImmutableError,
    ProvisioningRun,
    ProvisioningRunStatus,
    ProvisioningWorkflow,
    ServiceOrder,
    ServiceOrderStatus,
    ServiceOrderType,
)
from app.models.vendor_routes import InstallationProject, InstallationProjectStatus
from app.models.work_order import WorkOrder
from app.services.events.types import EventType
from app.services.owner_commands import CommandContext
from app.services.provisioning_lifecycle import (
    ConfirmActivationCommand,
    EvaluateReadinessCommand,
    ProvisioningLifecycleError,
    confirm_activation,
    evaluate_readiness,
)


def _context(reason: str) -> CommandContext:
    return CommandContext.system(
        actor="pytest:provisioning_lifecycle",
        scope="pytest:service_order",
        reason=reason,
    )


def _graph(db, subscriber, subscription, *, task_status: str):
    subscription.status = SubscriptionStatus.pending
    project = Project(
        name="Customer installation",
        project_type="fiber_optics_installation",
        subscriber_id=subscriber.id,
        status="completed",
    )
    db.add(project)
    db.flush()
    task = ProjectTask(
        project_id=project.id,
        title="Power Direction, Splicing & Customer Activation",
        status=task_status,
        metadata_={"fiber_stage_key": "power_splicing_activation"},
    )
    db.add(task)
    installation = InstallationProject(
        project_id=project.id,
        subscriber_id=subscriber.id,
        status=InstallationProjectStatus.verified.value,
    )
    db.add(installation)
    workflow = ProvisioningWorkflow(name=f"workflow-{uuid4()}")
    db.add(workflow)
    db.flush()
    order = ServiceOrder(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        project_id=project.id,
        installation_project_id=installation.id,
        activation_project_task_id=task.id,
        implementation_verified_at=datetime.now(UTC),
        implementation_verification_event_id=uuid4(),
        order_type=ServiceOrderType.new_install,
        status=ServiceOrderStatus.provisioning,
    )
    db.add(order)
    db.flush()
    run = ProvisioningRun(
        workflow_id=workflow.id,
        service_order_id=order.id,
        subscription_id=subscription.id,
        status=ProvisioningRunStatus.success,
    )
    address = IPv4Address(address=f"10.253.{uuid4().int % 200}.10")
    db.add(address)
    db.flush()
    db.add(
        IPAssignment(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
            is_active=True,
        )
    )
    db.add(run)
    db.commit()
    return order, run


def _invoke(db, fn, command):
    """Owner commands require a transaction-free session at entry. Committing
    the fixture session ends its transaction without discarding setup (the
    harness cannot survive a rollback across multiple commit cycles)."""
    db.commit()
    return fn(db, command)


def _field_visit(db, order, *, status: str) -> WorkOrder:
    """One field work order bound to the order's activation gate task."""
    work_order = WorkOrder(
        subscriber_id=order.subscriber_id,
        project_id=order.project_id,
        project_task_id=order.activation_project_task_id,
        title="Fiber activation visit",
        status=status,
    )
    db.add(work_order)
    db.commit()
    return work_order


def _field_work_check(outcome):
    return next(
        check
        for check in outcome.checks
        if check.kind == ProvisioningReadinessCheckKind.field_work
    )


def test_open_field_visit_blocks_activation(
    db_session, subscriber, subscription, monkeypatch
):
    order, run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.done.value,
    )
    _field_visit(db_session, order, status="dispatched")
    monkeypatch.setattr(
        "app.services.provisioning_lifecycle.emit_event", lambda *a, **k: None
    )

    outcome = _invoke(
        db_session,
        evaluate_readiness,
        EvaluateReadinessCommand(
            context=_context("activation task done but visit still in the field"),
            service_order_id=order.id,
            provisioning_run_id=run.id,
        ),
    )

    assert outcome.status == ProvisioningReadinessDecisionStatus.blocked
    assert outcome.reason_code == "field_work_incomplete"
    check = _field_work_check(outcome)
    assert check.result == ProvisioningReadinessCheckResult.failed
    assert check.reason_code == "field_work_incomplete"
    assert (
        db_session.get(ServiceOrder, order.id).status == ServiceOrderStatus.provisioning
    )
    assert subscription.status == SubscriptionStatus.pending


def test_all_visits_canceled_blocks_activation(
    db_session, subscriber, subscription, monkeypatch
):
    order, run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.done.value,
    )
    _field_visit(db_session, order, status="canceled")
    _field_visit(db_session, order, status="canceled")
    monkeypatch.setattr(
        "app.services.provisioning_lifecycle.emit_event", lambda *a, **k: None
    )

    outcome = _invoke(
        db_session,
        evaluate_readiness,
        EvaluateReadinessCommand(
            context=_context("all field visits ended without a completion"),
            service_order_id=order.id,
            provisioning_run_id=run.id,
        ),
    )

    assert outcome.status == ProvisioningReadinessDecisionStatus.blocked
    assert outcome.reason_code == "field_work_incomplete"
    check = _field_work_check(outcome)
    assert check.result == ProvisioningReadinessCheckResult.failed
    assert check.reason_code == "field_work_incomplete"
    assert (
        db_session.get(ServiceOrder, order.id).status == ServiceOrderStatus.provisioning
    )
    assert subscription.status == SubscriptionStatus.pending


def test_completed_visit_among_terminal_visits_passes_field_gate(
    db_session, subscriber, subscription, monkeypatch
):
    order, run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.done.value,
    )
    _field_visit(db_session, order, status="completed")
    _field_visit(db_session, order, status="canceled")
    monkeypatch.setattr(
        "app.services.provisioning_lifecycle.emit_event", lambda *a, **k: None
    )

    outcome = _invoke(
        db_session,
        evaluate_readiness,
        EvaluateReadinessCommand(
            context=_context("terminal field visits with one completion"),
            service_order_id=order.id,
            provisioning_run_id=run.id,
        ),
    )

    assert outcome.status == ProvisioningReadinessDecisionStatus.activation_requested
    check = _field_work_check(outcome)
    assert check.result == ProvisioningReadinessCheckResult.passed
    assert check.reason_code == "field_work_completed"


def test_no_field_visits_passes_gate_as_not_required(
    db_session, subscriber, subscription, monkeypatch
):
    order, run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.done.value,
    )
    monkeypatch.setattr(
        "app.services.provisioning_lifecycle.emit_event", lambda *a, **k: None
    )

    outcome = _invoke(
        db_session,
        evaluate_readiness,
        EvaluateReadinessCommand(
            context=_context("activation task done with no field visits raised"),
            service_order_id=order.id,
            provisioning_run_id=run.id,
        ),
    )

    # Today the gate treats zero task-linked visits as nothing to wait for:
    # the check passes with reason field_work_not_required and activation
    # proceeds on the remaining facts.
    assert outcome.status == ProvisioningReadinessDecisionStatus.activation_requested
    check = _field_work_check(outcome)
    assert check.result == ProvisioningReadinessCheckResult.passed
    assert check.reason_code == "field_work_not_required"


def test_incomplete_activation_task_blocks_without_activating(
    db_session, subscriber, subscription, monkeypatch
):
    order, run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.in_progress.value,
    )
    monkeypatch.setattr(
        "app.services.provisioning_lifecycle.emit_event", lambda *a, **k: None
    )

    outcome = _invoke(
        db_session,
        evaluate_readiness,
        EvaluateReadinessCommand(
            context=_context("successful run with incomplete field scope"),
            service_order_id=order.id,
            provisioning_run_id=run.id,
        ),
    )

    assert outcome.status == ProvisioningReadinessDecisionStatus.blocked
    assert outcome.reason_code == "activation_task_incomplete"
    assert (
        db_session.get(ServiceOrder, order.id).status == ServiceOrderStatus.provisioning
    )
    assert subscription.status == SubscriptionStatus.pending


def test_ready_order_requests_activation_then_requires_projection_confirmation(
    db_session, subscriber, subscription, monkeypatch
):
    order, run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.done.value,
    )
    emitted = []
    monkeypatch.setattr(
        "app.services.provisioning_lifecycle.emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "app.services.service_order_lifecycle.emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )

    requested = _invoke(
        db_session,
        evaluate_readiness,
        EvaluateReadinessCommand(
            context=_context("all readiness facts established"),
            service_order_id=order.id,
            provisioning_run_id=run.id,
        ),
    )

    assert requested.status == ProvisioningReadinessDecisionStatus.activation_requested
    assert subscription.status == SubscriptionStatus.pending
    assert (
        db_session.get(ServiceOrder, order.id).status == ServiceOrderStatus.provisioning
    )
    assert emitted[0][1]["service_order_id"] == order.id

    # Release the read transaction before entering the next public owner command.
    db_session.commit()
    confirmed = _invoke(
        db_session,
        confirm_activation,
        ConfirmActivationCommand(
            context=_context("connectivity projections succeeded"),
            service_order_id=order.id,
            subscription_id=subscription.id,
        ),
    )

    assert confirmed.status == ProvisioningReadinessDecisionStatus.activated
    assert db_session.get(ServiceOrder, order.id).status == ServiceOrderStatus.active
    assert subscription.status == SubscriptionStatus.active
    assert [call[0][1] for call in emitted] == [
        EventType.service_order_activation_requested,
        EventType.service_order_completed,
        EventType.subscription_activated,
    ]


def test_readiness_command_id_is_an_idempotent_replay(
    db_session, subscriber, subscription, monkeypatch
):
    order, run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.in_progress.value,
    )
    monkeypatch.setattr(
        "app.services.provisioning_lifecycle.emit_event", lambda *a, **k: None
    )
    context = _context("retryable terminal run event")
    command = EvaluateReadinessCommand(
        context=context,
        service_order_id=order.id,
        provisioning_run_id=run.id,
    )

    first = _invoke(db_session, evaluate_readiness, command)
    second = _invoke(db_session, evaluate_readiness, command)

    assert second.decision_id == first.decision_id
    assert second.command_id == context.command_id


def test_readiness_command_id_reuse_for_another_run_fails_closed(
    db_session, subscriber, subscription, monkeypatch
):
    order, run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.in_progress.value,
    )
    second_run = ProvisioningRun(
        workflow_id=run.workflow_id,
        service_order_id=order.id,
        subscription_id=subscription.id,
        status=ProvisioningRunStatus.success,
    )
    db_session.add(second_run)
    db_session.commit()
    monkeypatch.setattr(
        "app.services.provisioning_lifecycle.emit_event", lambda *a, **k: None
    )
    context = _context("collision-safe terminal run event")

    _invoke(
        db_session,
        evaluate_readiness,
        EvaluateReadinessCommand(
            context=context,
            service_order_id=order.id,
            provisioning_run_id=run.id,
        ),
    )
    with pytest.raises(ProvisioningLifecycleError) as exc:
        _invoke(
            db_session,
            evaluate_readiness,
            EvaluateReadinessCommand(
                context=context,
                service_order_id=order.id,
                provisioning_run_id=second_run.id,
            ),
        )

    assert exc.value.code == "operations.provisioning_lifecycle.command_replay_conflict"


def test_confirmation_rejects_activation_without_readiness_request(
    db_session, subscriber, subscription, monkeypatch
):
    order, _run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.done.value,
    )
    subscription.status = SubscriptionStatus.active
    db_session.commit()
    monkeypatch.setattr(
        "app.services.provisioning_lifecycle.emit_event", lambda *a, **k: None
    )

    with pytest.raises(ProvisioningLifecycleError) as exc:
        _invoke(
            db_session,
            confirm_activation,
            ConfirmActivationCommand(
                context=_context("unsolicited confirmation"),
                service_order_id=order.id,
                subscription_id=subscription.id,
            ),
        )

    assert (
        exc.value.code == "operations.provisioning_lifecycle.activation_not_requested"
    )


def test_failed_run_records_decision_through_service_order_owner(
    db_session, subscriber, subscription, monkeypatch
):
    order, run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.done.value,
    )
    run.status = ProvisioningRunStatus.failed
    db_session.commit()
    monkeypatch.setattr(
        "app.services.provisioning_lifecycle.emit_event", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "app.services.service_order_lifecycle.emit_event", lambda *a, **k: None
    )

    outcome = _invoke(
        db_session,
        evaluate_readiness,
        EvaluateReadinessCommand(
            context=_context("terminal provisioning failure"),
            service_order_id=order.id,
            provisioning_run_id=run.id,
        ),
    )

    assert outcome.status == ProvisioningReadinessDecisionStatus.failed
    assert db_session.get(ServiceOrder, order.id).status == ServiceOrderStatus.failed


def test_readiness_evidence_is_append_only(
    db_session, subscriber, subscription, monkeypatch
):
    order, run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.in_progress.value,
    )
    monkeypatch.setattr(
        "app.services.provisioning_lifecycle.emit_event", lambda *a, **k: None
    )
    outcome = _invoke(
        db_session,
        evaluate_readiness,
        EvaluateReadinessCommand(
            context=_context("persist immutable blocked evidence"),
            service_order_id=order.id,
            provisioning_run_id=run.id,
        ),
    )
    decision = db_session.get(ProvisioningReadinessDecision, outcome.decision_id)
    assert decision is not None
    decision.reason_code = "rewritten"

    with pytest.raises(ProvisioningReadinessEvidenceImmutableError):
        db_session.flush()
    db_session.rollback()
