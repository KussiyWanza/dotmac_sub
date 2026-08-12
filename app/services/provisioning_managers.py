"""CRUD manager classes and service instances for provisioning."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TypedDict

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.provisioning import (
    AppointmentStatus,
    InstallAppointment,
    ProvisioningRun,
    ProvisioningRunStatus,
    ProvisioningStep,
    ProvisioningStepType,
    ProvisioningTask,
    ProvisioningVendor,
    ProvisioningWorkflow,
    ServiceOrder,
    ServiceOrderStatus,
    ServiceState,
    ServiceStateTransition,
    TaskStatus,
    assert_legal_service_state_transition,
)
from app.schemas.provisioning import (
    InstallAppointmentCreate,
    InstallAppointmentUpdate,
    ProvisioningRunCreate,
    ProvisioningRunStart,
    ProvisioningRunUpdate,
    ProvisioningStepCreate,
    ProvisioningStepUpdate,
    ProvisioningTaskCreate,
    ProvisioningTaskUpdate,
    ProvisioningWorkflowCreate,
    ProvisioningWorkflowUpdate,
    ServiceOrderCreate,
    ServiceOrderUpdate,
    ServiceStateTransitionCreate,
    ServiceStateTransitionUpdate,
)
from app.services import settings_spec
from app.services.common import (
    apply_ordering,
    apply_pagination,
    validate_enum,
)
from app.services.crud import CRUDManager
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.provisioning_adapters import get_provisioner
from app.services.provisioning_helpers import (
    _ensure_ip_assignments,
    _extend_provisioning_context,
    _resolve_connector_context,
)
from app.services.query_builders import apply_active_state, apply_optional_equals
from app.validators import provisioning as provisioning_validators

logger = logging.getLogger(__name__)


class ServiceOrders(CRUDManager[ServiceOrder]):
    model = ServiceOrder
    not_found_detail = "Service order not found"

    @staticmethod
    def create(
        db: Session,
        payload: ServiceOrderCreate,
        *,
        actor_id: str = "operations.provisioning_workflow",
        commit: bool = True,
    ):
        requested_by_contact_id = payload.requested_by_contact_id
        provisioning_validators.validate_service_order_links(
            db,
            str(payload.subscriber_id),
            str(payload.subscription_id) if payload.subscription_id else None,
            str(requested_by_contact_id) if requested_by_contact_id else None,
            str(payload.project_id) if payload.project_id else None,
            str(payload.activation_project_task_id)
            if payload.activation_project_task_id
            else None,
        )
        # The ServiceOrder model does not include requested_by_contact_id or project_type.
        data = payload.model_dump(exclude={"requested_by_contact_id", "project_type"})
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.provisioning, "default_service_order_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, ServiceOrderStatus, "status"
                )
        idempotency_key = str(data.get("idempotency_key") or "").strip() or None
        if idempotency_key:
            existing = (
                db.query(ServiceOrder)
                .filter(ServiceOrder.idempotency_key == idempotency_key)
                .one_or_none()
            )
            if existing is not None:
                return existing
            data["idempotency_key"] = idempotency_key
        order = ServiceOrder(**data)
        db.add(order)
        db.flush()
        emit_event(
            db,
            EventType.service_order_created,
            {
                "service_order_id": str(order.id),
                "status": order.status.value if order.status else None,
                "subscription_id": str(order.subscription_id)
                if order.subscription_id
                else None,
            },
            service_order_id=order.id,
            subscriber_id=order.subscriber_id,
            subscription_id=order.subscription_id,
            actor=actor_id,
        )
        if commit:
            db.commit()
            db.refresh(order)

            try:
                from app.services.uisp_control_plane import stage_from_service_order

                stage_from_service_order(db, order)
            except Exception:
                db.rollback()
                logger.exception(
                    "uisp_service_order_staging_failed service_order_id=%s", order.id
                )

        return order

    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Return aggregated stats for the provisioning dashboard."""
        from sqlalchemy import func as sa_func

        total = db.query(sa_func.count(ServiceOrder.id)).scalar() or 0
        pending = (
            db.query(sa_func.count(ServiceOrder.id))
            .filter(
                ServiceOrder.status.in_(
                    [
                        ServiceOrderStatus.draft,
                        ServiceOrderStatus.submitted,
                    ]
                )
            )
            .scalar()
            or 0
        )
        in_progress = (
            db.query(sa_func.count(ServiceOrder.id))
            .filter(
                ServiceOrder.status.in_(
                    [
                        ServiceOrderStatus.scheduled,
                        ServiceOrderStatus.provisioning,
                    ]
                )
            )
            .scalar()
            or 0
        )
        completed = (
            db.query(sa_func.count(ServiceOrder.id))
            .filter(ServiceOrder.status == ServiceOrderStatus.active)
            .scalar()
            or 0
        )
        failed = (
            db.query(sa_func.count(ServiceOrder.id))
            .filter(ServiceOrder.status == ServiceOrderStatus.failed)
            .scalar()
            or 0
        )
        canceled = (
            db.query(sa_func.count(ServiceOrder.id))
            .filter(ServiceOrder.status == ServiceOrderStatus.canceled)
            .scalar()
            or 0
        )

        # Status funnel chart data
        funnel_labels = [
            "Draft",
            "Submitted",
            "Scheduled",
            "Provisioning",
            "Active",
            "Failed",
        ]
        funnel_keys = [
            ServiceOrderStatus.draft,
            ServiceOrderStatus.submitted,
            ServiceOrderStatus.scheduled,
            ServiceOrderStatus.provisioning,
            ServiceOrderStatus.active,
            ServiceOrderStatus.failed,
        ]
        funnel_colors = [
            "#94a3b8",
            "#3b82f6",
            "#f59e0b",
            "#8b5cf6",
            "#10b981",
            "#ef4444",
        ]
        status_rows = db.execute(
            select(ServiceOrder.status, sa_func.count(ServiceOrder.id)).group_by(
                ServiceOrder.status
            )
        ).all()
        status_counts = {row[0]: row[1] for row in status_rows}
        chart_data = {
            "labels": funnel_labels,
            "values": [status_counts.get(k, 0) for k in funnel_keys],
            "colors": funnel_colors,
        }

        # Recent orders (last 10)
        recent_orders = (
            db.query(ServiceOrder)
            .order_by(ServiceOrder.created_at.desc())
            .limit(10)
            .all()
        )

        return {
            "total": total,
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
            "failed": failed,
            "canceled": canceled,
            "chart_data": chart_data,
            "recent_orders": recent_orders,
        }

    @staticmethod
    def run_for_order(db: Session, order_id: str, workflow_id: str) -> ProvisioningRun:
        """Run a provisioning workflow for a service order."""
        order = db.get(ServiceOrder, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Service order not found")
        start_payload = ProvisioningRunStart(
            service_order_id=order.id,
            subscription_id=order.subscription_id,
        )
        return provisioning_runs.run(db, workflow_id, start_payload)

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None = None,
        subscription_id: str | None = None,
        status: str | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 20,
        offset: int = 0,
        account_id: str | None = None,
    ):
        query = db.query(ServiceOrder)
        if account_id and not subscriber_id:
            subscriber_id = account_id
        query = apply_optional_equals(
            query,
            {
                ServiceOrder.subscriber_id: subscriber_id,
                ServiceOrder.subscription_id: subscription_id,
            },
        )
        if status:
            query = query.filter(
                ServiceOrder.status
                == validate_enum(status, ServiceOrderStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ServiceOrder.created_at, "status": ServiceOrder.status},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, order_id: str, payload: ServiceOrderUpdate):
        order = db.get(ServiceOrder, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Service order not found")
        previous_status = order.status
        data = payload.model_dump(exclude_unset=True)
        target_status = data.pop("status", None)
        subscriber_id = str(data.get("subscriber_id", order.subscriber_id))
        subscription_id = data.get("subscription_id", order.subscription_id)
        project_id = data.get("project_id", order.project_id)
        activation_task_id = data.get(
            "activation_project_task_id", order.activation_project_task_id
        )
        # The ServiceOrder model does not include requested_by_contact_id.
        requested_by_contact_id = data.pop("requested_by_contact_id", None)
        # The ServiceOrder model does not include project_type.
        data.pop("project_type", None)
        provisioning_validators.validate_service_order_links(
            db,
            subscriber_id,
            str(subscription_id) if subscription_id else None,
            str(requested_by_contact_id) if requested_by_contact_id else None,
            str(project_id) if project_id else None,
            str(activation_task_id) if activation_task_id else None,
        )
        if target_status is not None:
            try:
                normalized_target = (
                    target_status
                    if isinstance(target_status, ServiceOrderStatus)
                    else ServiceOrderStatus(target_status)
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail="Invalid service order status"
                ) from exc
            if normalized_target in {
                ServiceOrderStatus.active,
                ServiceOrderStatus.failed,
            }:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Active and failed states require a provisioning-readiness "
                        "decision"
                    ),
                )
            if (
                previous_status
                in {
                    ServiceOrderStatus.canceled,
                    ServiceOrderStatus.active,
                    ServiceOrderStatus.failed,
                }
                and normalized_target != previous_status
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Cannot change a {previous_status.value} service order "
                        f"to {normalized_target.value}"
                    ),
                )
        for key, value in data.items():
            setattr(order, key, value)
        if target_status is not None:
            from app.services import service_order_lifecycle

            order = service_order_lifecycle.transition_service_order(
                db,
                service_order_id=order.id,
                target_status=target_status,
                actor_id="operations.provisioning_workflow",
                commit=False,
            )
        db.commit()
        db.refresh(order)
        return order


class InstallAppointments(CRUDManager[InstallAppointment]):
    model = InstallAppointment
    not_found_detail = "Install appointment not found"

    @staticmethod
    def create(db: Session, payload: InstallAppointmentCreate):
        provisioning_validators.validate_install_appointment_links(
            db, str(payload.service_order_id)
        )
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.provisioning, "default_appointment_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, AppointmentStatus, "status"
                )
        appointment = InstallAppointment(**data)
        db.add(appointment)
        db.commit()
        db.refresh(appointment)
        return appointment

    @staticmethod
    def list(
        db: Session,
        service_order_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(InstallAppointment)
        query = apply_optional_equals(
            query,
            {InstallAppointment.service_order_id: service_order_id},
        )
        if status:
            query = query.filter(
                InstallAppointment.status
                == validate_enum(status, AppointmentStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": InstallAppointment.created_at,
                "scheduled_start": InstallAppointment.scheduled_start,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, appointment_id: str, payload: InstallAppointmentUpdate):
        appointment = db.get(InstallAppointment, appointment_id)
        if not appointment:
            raise HTTPException(status_code=404, detail="Install appointment not found")
        data = payload.model_dump(exclude_unset=True)
        service_order_id = data.get("service_order_id", appointment.service_order_id)
        provisioning_validators.validate_install_appointment_links(
            db, str(service_order_id)
        )
        for key, value in data.items():
            setattr(appointment, key, value)
        db.commit()
        db.refresh(appointment)
        return appointment


class ProvisioningTasks(CRUDManager[ProvisioningTask]):
    model = ProvisioningTask
    not_found_detail = "Provisioning task not found"

    @staticmethod
    def create(db: Session, payload: ProvisioningTaskCreate):
        provisioning_validators.validate_provisioning_task_links(
            db, str(payload.service_order_id)
        )
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.provisioning, "default_task_status"
            )
            if default_status:
                data["status"] = validate_enum(default_status, TaskStatus, "status")
        task = ProvisioningTask(**data)
        db.add(task)
        db.commit()
        db.refresh(task)
        return task

    @staticmethod
    def list(
        db: Session,
        service_order_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ProvisioningTask)
        query = apply_optional_equals(
            query,
            {ProvisioningTask.service_order_id: service_order_id},
        )
        if status:
            query = query.filter(
                ProvisioningTask.status == validate_enum(status, TaskStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": ProvisioningTask.created_at,
                "status": ProvisioningTask.status,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, task_id: str, payload: ProvisioningTaskUpdate):
        task = db.get(ProvisioningTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Provisioning task not found")
        data = payload.model_dump(exclude_unset=True)
        service_order_id = data.get("service_order_id", task.service_order_id)
        provisioning_validators.validate_provisioning_task_links(
            db, str(service_order_id)
        )
        for key, value in data.items():
            setattr(task, key, value)
        db.commit()
        db.refresh(task)
        return task


class ServiceStateTransitions(CRUDManager[ServiceStateTransition]):
    model = ServiceStateTransition
    not_found_detail = "State transition not found"

    @staticmethod
    def _current_state(db: Session, service_order_id) -> ServiceState:
        """The order's current ServiceState = the most recent transition's
        ``to_state``, or ``pending`` if no transition has been recorded yet."""
        last = db.execute(
            select(ServiceStateTransition)
            .where(ServiceStateTransition.service_order_id == service_order_id)
            .order_by(
                ServiceStateTransition.changed_at.desc(),
                ServiceStateTransition.id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        return last.to_state if last else ServiceState.pending

    @staticmethod
    def create(db: Session, payload: ServiceStateTransitionCreate):
        provisioning_validators.validate_state_transition_links(
            db, str(payload.service_order_id)
        )
        # Enforce the state machine: the transition's ``from_state`` must match
        # the order's actual current state, and ``from_state → to_state`` must
        # be a legal edge. Without this the transition log can record histories
        # that never happened (canceled → active, active → pending, …).
        current = ServiceStateTransitions._current_state(db, payload.service_order_id)
        from_state = payload.from_state if payload.from_state is not None else current
        if from_state != current:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Stale service-state transition: order is in "
                    f"{current.value}, not {from_state.value}"
                ),
            )
        try:
            assert_legal_service_state_transition(from_state, payload.to_state)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        data = payload.model_dump()
        data["from_state"] = from_state
        transition = ServiceStateTransition(**data)
        db.add(transition)
        db.commit()
        db.refresh(transition)
        return transition

    @staticmethod
    def list(
        db: Session,
        service_order_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ServiceStateTransition)
        query = apply_optional_equals(
            query,
            {ServiceStateTransition.service_order_id: service_order_id},
        )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "changed_at": ServiceStateTransition.changed_at,
                "to_state": ServiceStateTransition.to_state,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, transition_id: str, payload: ServiceStateTransitionUpdate):
        transition = db.get(ServiceStateTransition, transition_id)
        if not transition:
            raise HTTPException(status_code=404, detail="State transition not found")
        data = payload.model_dump(exclude_unset=True)
        service_order_id = data.get("service_order_id", transition.service_order_id)
        provisioning_validators.validate_state_transition_links(
            db, str(service_order_id)
        )
        for key, value in data.items():
            setattr(transition, key, value)
        db.commit()
        db.refresh(transition)
        return transition


class ProvisioningWorkflows(CRUDManager[ProvisioningWorkflow]):
    model = ProvisioningWorkflow
    not_found_detail = "Provisioning workflow not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def create(db: Session, payload: ProvisioningWorkflowCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "vendor" not in fields_set:
            default_vendor = settings_spec.resolve_value(
                db, SettingDomain.provisioning, "default_vendor"
            )
            if default_vendor:
                data["vendor"] = validate_enum(
                    default_vendor, ProvisioningVendor, "vendor"
                )
        workflow = ProvisioningWorkflow(**data)
        db.add(workflow)
        db.commit()
        db.refresh(workflow)
        return workflow

    @staticmethod
    def list(
        db: Session,
        vendor: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ProvisioningWorkflow)
        if vendor:
            query = query.filter(
                ProvisioningWorkflow.vendor
                == validate_enum(vendor, ProvisioningVendor, "vendor")
            )
        query = apply_active_state(query, ProvisioningWorkflow.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": ProvisioningWorkflow.created_at,
                "name": ProvisioningWorkflow.name,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, workflow_id: str, payload: ProvisioningWorkflowUpdate):
        workflow = db.get(ProvisioningWorkflow, workflow_id)
        if not workflow:
            raise HTTPException(
                status_code=404, detail="Provisioning workflow not found"
            )
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(workflow, key, value)
        db.commit()
        db.refresh(workflow)
        return workflow


class ProvisioningSteps(CRUDManager[ProvisioningStep]):
    model = ProvisioningStep
    not_found_detail = "Provisioning step not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def create(db: Session, payload: ProvisioningStepCreate):
        workflow = db.get(ProvisioningWorkflow, payload.workflow_id)
        if not workflow:
            raise HTTPException(
                status_code=404, detail="Provisioning workflow not found"
            )
        step = ProvisioningStep(**payload.model_dump())
        db.add(step)
        db.commit()
        db.refresh(step)
        return step

    @staticmethod
    def list(
        db: Session,
        workflow_id: str | None,
        step_type: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ProvisioningStep)
        query = apply_optional_equals(
            query,
            {ProvisioningStep.workflow_id: workflow_id},
        )
        if step_type:
            query = query.filter(
                ProvisioningStep.step_type
                == validate_enum(step_type, ProvisioningStepType, "step_type")
            )
        query = apply_active_state(query, ProvisioningStep.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": ProvisioningStep.created_at,
                "order_index": ProvisioningStep.order_index,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, step_id: str, payload: ProvisioningStepUpdate):
        step = db.get(ProvisioningStep, step_id)
        if not step:
            raise HTTPException(status_code=404, detail="Provisioning step not found")
        data = payload.model_dump(exclude_unset=True)
        if "workflow_id" in data:
            workflow = db.get(ProvisioningWorkflow, data["workflow_id"])
            if not workflow:
                raise HTTPException(
                    status_code=404, detail="Provisioning workflow not found"
                )
        for key, value in data.items():
            setattr(step, key, value)
        db.commit()
        db.refresh(step)
        return step


class ProvisioningRuns(CRUDManager[ProvisioningRun]):
    model = ProvisioningRun
    not_found_detail = "Provisioning run not found"

    @staticmethod
    def create(db: Session, payload: ProvisioningRunCreate):
        workflow = db.get(ProvisioningWorkflow, payload.workflow_id)
        if not workflow:
            raise HTTPException(
                status_code=404, detail="Provisioning workflow not found"
            )
        run = ProvisioningRun(**payload.model_dump())
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    @staticmethod
    def list(
        db: Session,
        workflow_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ProvisioningRun)
        query = apply_optional_equals(
            query,
            {ProvisioningRun.workflow_id: workflow_id},
        )
        if status:
            query = query.filter(
                ProvisioningRun.status
                == validate_enum(status, ProvisioningRunStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": ProvisioningRun.created_at,
                "status": ProvisioningRun.status,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, run_id: str, payload: ProvisioningRunUpdate):
        run = db.get(ProvisioningRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Provisioning run not found")
        data = payload.model_dump(exclude_unset=True)
        if "workflow_id" in data:
            workflow = db.get(ProvisioningWorkflow, data["workflow_id"])
            if not workflow:
                raise HTTPException(
                    status_code=404, detail="Provisioning workflow not found"
                )
        for key, value in data.items():
            setattr(run, key, value)
        db.commit()
        db.refresh(run)
        return run

    @staticmethod
    def run(
        db: Session, workflow_id: str, payload: ProvisioningRunStart | None = None
    ) -> ProvisioningRun:
        workflow = db.get(ProvisioningWorkflow, workflow_id)
        if not workflow or not workflow.is_active:
            raise HTTPException(
                status_code=404, detail="Provisioning workflow not found"
            )
        if payload is None:
            payload = ProvisioningRunStart()
        run = ProvisioningRun(
            workflow_id=workflow_id,
            service_order_id=payload.service_order_id,
            subscription_id=payload.subscription_id,
            status=ProvisioningRunStatus.running,
            started_at=datetime.now(UTC),
            input_payload=payload.input_payload,
        )
        db.add(run)
        db.flush()

        context = dict(payload.input_payload or {})
        _extend_provisioning_context(
            db,
            str(payload.subscription_id) if payload.subscription_id else None,
            context,
        )

        emit_event(
            db,
            EventType.provisioning_started,
            {
                "provisioning_run_id": str(run.id),
                "workflow_id": str(run.workflow_id),
                "status": run.status.value,
                "service_order_id": str(run.service_order_id)
                if run.service_order_id
                else None,
                "subscription_id": str(run.subscription_id)
                if run.subscription_id
                else None,
            },
            service_order_id=run.service_order_id,
            subscription_id=run.subscription_id,
        )
        db.commit()
        db.refresh(run)
        try:
            context.update(
                _ensure_ip_assignments(
                    db,
                    str(payload.subscription_id) if payload.subscription_id else None,
                    context,
                )
            )
        except Exception as exc:
            error_message = str(getattr(exc, "detail", exc))
            run.status = ProvisioningRunStatus.failed
            run.output_payload = {"results": []}
            run.error_message = error_message
            run.completed_at = datetime.now(UTC)
            emit_event(
                db,
                EventType.provisioning_failed,
                {
                    "provisioning_run_id": str(run.id),
                    "workflow_id": str(run.workflow_id),
                    "status": run.status.value,
                    "service_order_id": str(run.service_order_id)
                    if run.service_order_id
                    else None,
                    "subscription_id": str(run.subscription_id)
                    if run.subscription_id
                    else None,
                    "error_message": error_message,
                },
                service_order_id=run.service_order_id,
                subscription_id=run.subscription_id,
            )
            db.commit()
            db.refresh(run)
            return run

        steps = (
            db.query(ProvisioningStep)
            .filter(ProvisioningStep.workflow_id == workflow_id)
            .filter(ProvisioningStep.is_active.is_(True))
            .order_by(
                ProvisioningStep.order_index.asc(), ProvisioningStep.created_at.asc()
            )
            .all()
        )
        provisioner = get_provisioner(workflow.vendor)
        results: list[dict] = []
        status = ProvisioningRunStatus.success
        step_error_message: str | None = None
        for step in steps:
            step_context = dict(context)
            connector_context = _resolve_connector_context(db, step.config or {})
            if connector_context:
                step_context["connector"] = connector_context
            try:
                if step.step_type == ProvisioningStepType.assign_ont:
                    result = provisioner.assign_ont(step_context, step.config)
                elif step.step_type == ProvisioningStepType.push_config:
                    result = provisioner.push_config(step_context, step.config)
                elif step.step_type == ProvisioningStepType.confirm_up:
                    result = provisioner.confirm_up(step_context, step.config)
                elif step.step_type == ProvisioningStepType.create_olt_service_port:
                    from app.services.provisioning_step_executors import (
                        execute_create_olt_service_port,
                    )

                    result = execute_create_olt_service_port(
                        db, step_context, step.config
                    )
                elif step.step_type == ProvisioningStepType.restore_olt_from_backup:
                    from app.services.provisioning_step_executors import (
                        execute_restore_olt_from_backup,
                    )

                    result = execute_restore_olt_from_backup(
                        db, step_context, step.config
                    )
                elif step.step_type == ProvisioningStepType.ensure_nas_vlan:
                    from app.services.provisioning_step_executors import (
                        execute_ensure_nas_vlan,
                    )

                    result = execute_ensure_nas_vlan(db, step_context, step.config)
                else:
                    raise HTTPException(status_code=400, detail="Unsupported step type")
                results.append(
                    {
                        "step_id": str(step.id),
                        "step_type": step.step_type.value,
                        "status": result.status,
                        "detail": result.detail,
                        "payload": result.payload,
                    }
                )
                # A step that reports failure by RETURN VALUE (not by raising)
                # must fail the run. Previously only a raised exception did, so
                # a step returning ProvisioningResult(status="failed", ...) left
                # the whole run marked ``success`` — a real failure masquerading
                # as a completed provision.
                if result.status not in ("ok", "success"):
                    status = ProvisioningRunStatus.failed
                    step_error_message = (
                        result.detail
                        or f"step {step.step_type.value} returned status "
                        f"{result.status!r}"
                    )
                    break
                if result.payload:
                    context.update(result.payload)
            except Exception as exc:
                logger.error(
                    "Provisioning step %s (%s) failed: %s",
                    step.id,
                    step.step_type.value,
                    exc,
                    exc_info=True,
                )
                status = ProvisioningRunStatus.failed
                step_error_message = str(exc)
                results.append(
                    {
                        "step_id": str(step.id),
                        "step_type": step.step_type.value,
                        "status": "failed",
                        "detail": step_error_message,
                    }
                )
                break
        run.status = status
        run.output_payload = {"results": results}
        run.error_message = step_error_message
        run.completed_at = datetime.now(UTC)

        # Emit provisioning events based on run result
        event_payload = {
            "provisioning_run_id": str(run.id),
            "workflow_id": str(run.workflow_id),
            "status": status.value,
            "service_order_id": str(run.service_order_id)
            if run.service_order_id
            else None,
            "subscription_id": str(run.subscription_id)
            if run.subscription_id
            else None,
            "error_message": step_error_message,
        }
        if status == ProvisioningRunStatus.success:
            emit_event(
                db,
                EventType.provisioning_completed,
                event_payload,
                service_order_id=run.service_order_id,
                subscription_id=run.subscription_id,
            )
        elif status == ProvisioningRunStatus.failed:
            emit_event(
                db,
                EventType.provisioning_failed,
                event_payload,
                service_order_id=run.service_order_id,
                subscription_id=run.subscription_id,
            )

        db.commit()
        db.refresh(run)

        return run

    @staticmethod
    def reap_stale_runs(db: Session, older_than_minutes: int = 30) -> int:
        """Fail provisioning runs stuck in ``running`` past a timeout.

        ``run`` is synchronous; if the worker dies mid-loop the row stays
        ``running`` forever, indistinguishable from an in-flight run and
        blocking the duplicate-run guard and order advancement. This reaper
        (run on a beat schedule) marks such runs ``failed`` and emits
        ``provisioning_failed`` so the linked order advances to ``failed`` too.
        Returns the number of runs reaped.
        """
        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
        stale = list(
            db.scalars(
                select(ProvisioningRun).where(
                    ProvisioningRun.status == ProvisioningRunStatus.running,
                    ProvisioningRun.started_at.is_not(None),
                    ProvisioningRun.started_at < cutoff,
                )
            ).all()
        )
        for run in stale:
            run.status = ProvisioningRunStatus.failed
            run.completed_at = datetime.now(UTC)
            run.error_message = (
                f"Run exceeded {older_than_minutes}m in 'running' and was "
                "reaped as stale (worker likely died mid-run)."
            )
        if stale:
            for run in stale:
                emit_event(
                    db,
                    EventType.provisioning_failed,
                    {
                        "provisioning_run_id": str(run.id),
                        "workflow_id": str(run.workflow_id),
                        "status": run.status.value,
                        "service_order_id": str(run.service_order_id)
                        if run.service_order_id
                        else None,
                        "subscription_id": str(run.subscription_id)
                        if run.subscription_id
                        else None,
                        "error_message": run.error_message,
                        "reaped": True,
                    },
                    service_order_id=run.service_order_id,
                    subscription_id=run.subscription_id,
                )
            db.commit()
            logger.warning("Reaped %d stale provisioning run(s)", len(stale))
        return len(stale)


service_orders = ServiceOrders()
install_appointments = InstallAppointments()
provisioning_tasks = ProvisioningTasks()
service_state_transitions = ServiceStateTransitions()
provisioning_workflows = ProvisioningWorkflows()
provisioning_steps = ProvisioningSteps()
provisioning_runs = ProvisioningRuns()


def service_order_dashboard_counts(db: Session) -> dict[str, int]:
    """Service-order pipeline counts for the operations overview.

    Canonical owner of the overview's pending/in-progress/completed order
    figures (pending = submitted + scheduled).
    """
    from sqlalchemy import func as _func

    row = db.query(
        _func.count(ServiceOrder.id)
        .filter(
            ServiceOrder.status.in_(
                (ServiceOrderStatus.submitted, ServiceOrderStatus.scheduled)
            )
        )
        .label("pending"),
        _func.count(ServiceOrder.id)
        .filter(ServiceOrder.status == ServiceOrderStatus.provisioning)
        .label("in_progress"),
        _func.count(ServiceOrder.id)
        .filter(ServiceOrder.status == ServiceOrderStatus.active)
        .label("completed"),
    ).one()
    return {
        "pending": int(row.pending or 0),
        "in_progress": int(row.in_progress or 0),
        "completed": int(row.completed or 0),
    }


# ---------------------------------------------------------------------------
# Admin report read owners
#
# The admin /reports technician page renders these figures; the web layer
# (app.services.web_reports) composes them and owns presentation only. The
# aggregations were moved here verbatim from the web layer so the displayed
# numbers do not change.
# ---------------------------------------------------------------------------


class TechnicianReportRow(TypedDict):
    name: str
    total_jobs: int
    completed_jobs: int
    avg_hours: float
    completion_rate: float


class TechnicianReportStats(TypedDict):
    total_technicians: int
    jobs_completed: int
    avg_completion_hours: float
    appointment_completion_rate: float
    technician_stats: list[TechnicianReportRow]
    job_type_breakdown: dict[str, int]


def recent_completed_appointments(
    db: Session,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    limit: int = 10,
) -> list[InstallAppointment]:
    """Return recent completed appointments within the report period."""
    filters = [InstallAppointment.status == AppointmentStatus.completed]
    if start_at is not None:
        filters.append(InstallAppointment.scheduled_start >= start_at)
    if end_at is not None:
        filters.append(InstallAppointment.scheduled_start < end_at)
    return list(
        db.scalars(
            select(InstallAppointment)
            .where(*filters)
            .order_by(InstallAppointment.updated_at.desc())
            .limit(limit)
        ).all()
    )


def _ensure_aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def technician_report_stats(
    db: Session,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> TechnicianReportStats:
    """Aggregated technician/provisioning figures for the admin report.

    Returns ``total_technicians``, completed installation appointments,
    ``avg_completion_hours``, ``appointment_completion_rate``,
    ``technician_stats`` (per-technician appointment counts, unsliced), and
    ``job_type_breakdown`` (service-order counts by order type). The optional
    period is applied consistently to every displayed and exported metric.
    """
    appointment_period = []
    task_period = []
    order_period = []
    if start_at is not None:
        appointment_period.append(InstallAppointment.scheduled_start >= start_at)
        task_period.append(ProvisioningTask.completed_at >= start_at)
        order_period.append(ServiceOrder.created_at >= start_at)
    if end_at is not None:
        appointment_period.append(InstallAppointment.scheduled_start < end_at)
        task_period.append(ProvisioningTask.completed_at < end_at)
        order_period.append(ServiceOrder.created_at < end_at)

    jobs_completed = int(
        db.scalar(
            select(func.count(InstallAppointment.id)).where(
                InstallAppointment.status == AppointmentStatus.completed,
                *appointment_period,
            )
        )
        or 0
    )

    # Real technician count from appointments + provisioning tasks
    tech_names: set[str] = set()
    appt_techs = db.scalars(
        select(InstallAppointment.technician)
        .where(InstallAppointment.technician.isnot(None), *appointment_period)
        .distinct()
    ).all()
    tech_names.update(t for t in appt_techs if t)
    task_assignees = db.scalars(
        select(ProvisioningTask.assigned_to)
        .where(ProvisioningTask.assigned_to.isnot(None), *task_period)
        .distinct()
    ).all()
    tech_names.update(t for t in task_assignees if t)
    total_technicians = len(tech_names)

    # Average completion hours from provisioning tasks with start/end times
    completed_tasks = db.execute(
        select(
            ProvisioningTask.started_at,
            ProvisioningTask.completed_at,
        ).where(
            ProvisioningTask.status == TaskStatus.completed,
            ProvisioningTask.started_at.isnot(None),
            ProvisioningTask.completed_at.isnot(None),
            *task_period,
        )
    ).all()
    if completed_tasks:
        total_hours = 0.0
        counted = 0
        for t in completed_tasks:
            t_start = _ensure_aware_datetime(t.started_at)
            t_end = _ensure_aware_datetime(t.completed_at)
            if t_start and t_end:
                total_hours += max(0.0, (t_end - t_start).total_seconds() / 3600)
                counted += 1
        avg_completion_hours = round(total_hours / max(1, counted), 1)
    else:
        avg_completion_hours = 0.0

    total_appointments = int(
        db.scalar(select(func.count(InstallAppointment.id)).where(*appointment_period))
        or 0
    )
    completed_appointments = jobs_completed
    if total_appointments > 0:
        appointment_completion_rate = round(
            (completed_appointments / total_appointments) * 100, 1
        )
    else:
        appointment_completion_rate = 0.0

    # Per-technician stats
    technician_stats: list[TechnicianReportRow] = []
    for tech_name in sorted(tech_names):
        tech_total = int(
            db.scalar(
                select(func.count(InstallAppointment.id)).where(
                    InstallAppointment.technician == tech_name, *appointment_period
                )
            )
            or 0
        )
        tech_completed = int(
            db.scalar(
                select(func.count(InstallAppointment.id)).where(
                    InstallAppointment.technician == tech_name,
                    InstallAppointment.status == AppointmentStatus.completed,
                    *appointment_period,
                )
            )
            or 0
        )
        technician_stats.append(
            {
                "name": tech_name,
                "total_jobs": tech_total,
                "completed_jobs": tech_completed,
                "avg_hours": avg_completion_hours,
                "completion_rate": round(
                    (tech_completed / tech_total * 100) if tech_total > 0 else 0, 1
                ),
            }
        )

    job_type_rows = db.execute(
        select(ServiceOrder.order_type, func.count(ServiceOrder.id))
        .where(*order_period)
        .group_by(ServiceOrder.order_type)
    ).all()
    job_type_breakdown = {
        (row.order_type.value if row.order_type else "unspecified"): int(row[1] or 0)
        for row in job_type_rows
    }

    return {
        "total_technicians": total_technicians,
        "jobs_completed": jobs_completed,
        "avg_completion_hours": avg_completion_hours,
        "appointment_completion_rate": appointment_completion_rate,
        "technician_stats": technician_stats,
        "job_type_breakdown": job_type_breakdown,
    }
