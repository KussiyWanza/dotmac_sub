import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging import get_logger
from app.models.integration import (
    IntegrationJob,
    IntegrationJobType,
    IntegrationRun,
    IntegrationRunStatus,
    IntegrationScheduleType,
    IntegrationTarget,
    IntegrationTargetType,
)
from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationInstallation,
    IntegrationInstallationState,
)
from app.schemas.integration import (
    IntegrationJobCreate,
    IntegrationJobUpdate,
    IntegrationTargetCreate,
    IntegrationTargetUpdate,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    validate_enum,
)
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)

logger = get_logger(__name__)


class IntegrationJobCommandError(DomainError, ValueError):
    """Stable rejection from the integration-jobs command owner."""


@dataclass(frozen=True, slots=True)
class ActivateCapabilityJobCommand:
    job_id: UUID
    capability_binding_id: UUID
    capability_id: str
    expected_target_type: IntegrationTargetType
    expected_existing_binding_id: UUID | None
    expected_is_active: bool


@dataclass(frozen=True, slots=True)
class ActivateCapabilityJobResult:
    job_id: UUID
    capability_binding_id: UUID
    capability_id: str
    is_active: bool
    schedule_type: IntegrationScheduleType
    replayed: bool


CAPABILITY_JOB_ACTIVATION_SCOPE = "integration-job:activate-capability"
_ACTIVATE_CAPABILITY_JOB_COMMAND = OwnerCommandDefinition(
    owner="integration.jobs",
    concern="integration jobs",
    name="activate_capability_job",
)


def _job_command_error(
    suffix: str,
    message: str,
    **details: object,
) -> IntegrationJobCommandError:
    return IntegrationJobCommandError(
        code=f"integration.jobs.{suffix}",
        message=message,
        details=details,
    )


def activate_capability_job(
    db: Session,
    command: ActivateCapabilityJobCommand,
    *,
    context: CommandContext,
) -> ActivateCapabilityJobResult:
    """Bind and activate one scheduler-owned capability job atomically."""

    return execute_owner_command(
        db,
        definition=_ACTIVATE_CAPABILITY_JOB_COMMAND,
        context=context,
        operation=lambda: _activate_capability_job(
            db,
            command=command,
            context=context,
        ),
    )


def _activate_capability_job(
    db: Session,
    *,
    command: ActivateCapabilityJobCommand,
    context: CommandContext,
) -> ActivateCapabilityJobResult:
    if context.scope != CAPABILITY_JOB_ACTIVATION_SCOPE:
        raise _job_command_error(
            "job_activation_scope_invalid",
            "Capability job activation requires the dedicated command scope.",
            scope=context.scope,
        )
    capability_id = command.capability_id.strip()
    if not capability_id:
        raise _job_command_error(
            "invalid_capability",
            "Capability job activation requires a capability identifier.",
        )

    job = db.scalar(
        select(IntegrationJob)
        .where(IntegrationJob.id == command.job_id)
        .with_for_update()
    )
    if job is None:
        raise _job_command_error(
            "job_not_found",
            "Integration job was not found.",
            job_id=str(command.job_id),
        )
    target = db.scalar(
        select(IntegrationTarget)
        .where(IntegrationTarget.id == job.target_id)
        .with_for_update()
    )
    if target is None:
        raise _job_command_error(
            "target_not_found",
            "Integration job target was not found.",
            job_id=str(job.id),
        )
    if target.target_type != command.expected_target_type:
        raise _job_command_error(
            "target_type_mismatch",
            "Integration job target type changed after review.",
            job_id=str(job.id),
            actual_target_type=target.target_type.value,
        )
    if not target.is_active:
        raise _job_command_error(
            "target_disabled",
            "Integration job target must be active.",
            job_id=str(job.id),
            target_id=str(target.id),
        )
    if job.job_type != IntegrationJobType.sync:
        raise _job_command_error(
            "job_type_mismatch",
            "Capability activation requires a sync job.",
            job_id=str(job.id),
            actual_job_type=job.job_type.value,
        )

    reviewed_installation_id = db.scalar(
        select(IntegrationCapabilityBinding.installation_id).where(
            IntegrationCapabilityBinding.id == command.capability_binding_id
        )
    )
    if reviewed_installation_id is None:
        raise _job_command_error(
            "binding_not_found",
            "Integration capability binding was not found.",
            capability_binding_id=str(command.capability_binding_id),
        )
    installation = db.scalar(
        select(IntegrationInstallation)
        .where(IntegrationInstallation.id == reviewed_installation_id)
        .with_for_update()
    )
    binding = db.scalar(
        select(IntegrationCapabilityBinding)
        .where(IntegrationCapabilityBinding.id == command.capability_binding_id)
        .with_for_update()
    )
    if installation is None or binding is None:
        raise _job_command_error(
            "binding_not_found",
            "Integration capability binding was removed during activation.",
            capability_binding_id=str(command.capability_binding_id),
        )
    if binding.capability_id != capability_id:
        raise _job_command_error(
            "binding_capability_mismatch",
            "Integration binding does not provide the reviewed capability.",
            capability_binding_id=str(binding.id),
            actual_capability_id=binding.capability_id,
        )
    if (
        binding.state != IntegrationBindingState.enabled.value
        or installation.state != IntegrationInstallationState.enabled.value
    ):
        raise _job_command_error(
            "binding_disabled",
            "Capability job activation requires an enabled installation binding.",
            capability_binding_id=str(binding.id),
        )

    if (
        job.capability_binding_id == binding.id
        and job.is_active
        and job.schedule_type == IntegrationScheduleType.manual
    ):
        return ActivateCapabilityJobResult(
            job_id=job.id,
            capability_binding_id=binding.id,
            capability_id=binding.capability_id,
            is_active=True,
            schedule_type=IntegrationScheduleType.manual,
            replayed=True,
        )
    if (
        job.capability_binding_id != command.expected_existing_binding_id
        or job.is_active is not command.expected_is_active
    ):
        raise _job_command_error(
            "stale_job_state",
            "Integration job changed after capability activation review.",
            job_id=str(job.id),
            actual_capability_binding_id=(
                str(job.capability_binding_id)
                if job.capability_binding_id is not None
                else None
            ),
            actual_is_active=job.is_active,
        )
    if (
        job.capability_binding_id is not None
        and job.capability_binding_id != binding.id
    ):
        raise _job_command_error(
            "binding_conflict",
            "Integration job is already bound to another capability.",
            job_id=str(job.id),
            actual_capability_binding_id=str(job.capability_binding_id),
        )

    job.capability_binding_id = binding.id
    job.is_active = True
    # The dedicated scheduler control owns cadence. Keeping this job manual
    # prevents a second interval path from scheduling the same ticket pull.
    job.schedule_type = IntegrationScheduleType.manual
    job.interval_minutes = None
    job.interval_seconds = None
    db.flush()
    emit_event(
        db,
        EventType.integration_job_capability_activated,
        {
            "schema_version": 1,
            "job_id": str(job.id),
            "target_id": str(job.target_id),
            "capability_binding_id": str(binding.id),
            "capability_id": binding.capability_id,
            "connector_key": installation.connector_key,
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
            "idempotency_key": context.idempotency_key,
            "reason": context.reason,
        },
        actor=context.actor,
    )
    return ActivateCapabilityJobResult(
        job_id=job.id,
        capability_binding_id=binding.id,
        capability_id=binding.capability_id,
        is_active=True,
        schedule_type=IntegrationScheduleType.manual,
        replayed=False,
    )


def _require_job_binding(
    db: Session, binding_id, *, active: bool
) -> IntegrationCapabilityBinding:
    binding = db.get(IntegrationCapabilityBinding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="Capability binding not found")
    if active and (
        binding.state != IntegrationBindingState.enabled.value
        or binding.installation.state != IntegrationInstallationState.enabled.value
    ):
        raise HTTPException(
            status_code=409,
            detail="Active jobs require an enabled installation capability",
        )
    return binding


class IntegrationTargets(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: IntegrationTargetCreate):
        target = IntegrationTarget(**payload.model_dump())
        db.add(target)
        db.commit()
        db.refresh(target)
        return target

    @staticmethod
    def get(db: Session, target_id: str):
        target = db.get(IntegrationTarget, coerce_uuid(target_id))
        if not target:
            raise HTTPException(status_code=404, detail="Integration target not found")
        return target

    @staticmethod
    def list(
        db: Session,
        target_type: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IntegrationTarget)
        if target_type:
            query = query.filter(
                IntegrationTarget.target_type
                == validate_enum(target_type, IntegrationTargetType, "target_type")
            )
        if is_active is None:
            query = query.filter(IntegrationTarget.is_active.is_(True))
        else:
            query = query.filter(IntegrationTarget.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": IntegrationTarget.created_at,
                "name": IntegrationTarget.name,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_all(
        db: Session,
        target_type: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IntegrationTarget)
        if target_type:
            query = query.filter(
                IntegrationTarget.target_type
                == validate_enum(target_type, IntegrationTargetType, "target_type")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": IntegrationTarget.created_at,
                "name": IntegrationTarget.name,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, target_id: str, payload: IntegrationTargetUpdate):
        target = db.get(IntegrationTarget, coerce_uuid(target_id))
        if not target:
            raise HTTPException(status_code=404, detail="Integration target not found")
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(target, key, value)
        db.commit()
        db.refresh(target)
        return target

    @staticmethod
    def delete(db: Session, target_id: str):
        target = db.get(IntegrationTarget, coerce_uuid(target_id))
        if not target:
            raise HTTPException(status_code=404, detail="Integration target not found")
        target.is_active = False
        db.commit()


class IntegrationJobs(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: IntegrationJobCreate):
        target = db.get(IntegrationTarget, payload.target_id)
        if not target:
            raise HTTPException(status_code=404, detail="Integration target not found")
        _require_job_binding(
            db, payload.capability_binding_id, active=payload.is_active
        )
        job = IntegrationJob(**payload.model_dump())
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def get(db: Session, job_id: str):
        job = db.get(IntegrationJob, coerce_uuid(job_id))
        if not job:
            raise HTTPException(status_code=404, detail="Integration job not found")
        return job

    @staticmethod
    def list(
        db: Session,
        target_id: str | None,
        job_type: str | None,
        schedule_type: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IntegrationJob)
        if target_id:
            query = query.filter(IntegrationJob.target_id == target_id)
        if job_type:
            query = query.filter(
                IntegrationJob.job_type
                == validate_enum(job_type, IntegrationJobType, "job_type")
            )
        if schedule_type:
            query = query.filter(
                IntegrationJob.schedule_type
                == validate_enum(
                    schedule_type, IntegrationScheduleType, "schedule_type"
                )
            )
        if is_active is None:
            query = query.filter(IntegrationJob.is_active.is_(True))
        else:
            query = query.filter(IntegrationJob.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IntegrationJob.created_at, "name": IntegrationJob.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_all(
        db: Session,
        target_id: str | None,
        job_type: str | None,
        schedule_type: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IntegrationJob)
        if target_id:
            query = query.filter(IntegrationJob.target_id == target_id)
        if job_type:
            query = query.filter(
                IntegrationJob.job_type
                == validate_enum(job_type, IntegrationJobType, "job_type")
            )
        if schedule_type:
            query = query.filter(
                IntegrationJob.schedule_type
                == validate_enum(
                    schedule_type, IntegrationScheduleType, "schedule_type"
                )
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IntegrationJob.created_at, "name": IntegrationJob.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, job_id: str, payload: IntegrationJobUpdate):
        job = db.get(IntegrationJob, coerce_uuid(job_id))
        if not job:
            raise HTTPException(status_code=404, detail="Integration job not found")
        data = payload.model_dump(exclude_unset=True)
        if "target_id" in data:
            target = db.get(IntegrationTarget, data["target_id"])
            if not target:
                raise HTTPException(
                    status_code=404, detail="Integration target not found"
                )
        binding_id = data.get("capability_binding_id", job.capability_binding_id)
        active = bool(data.get("is_active", job.is_active))
        if binding_id is not None:
            _require_job_binding(db, binding_id, active=active)
        elif active:
            raise HTTPException(
                status_code=409,
                detail="Active jobs require a capability binding",
            )
        for key, value in data.items():
            setattr(job, key, value)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def delete(db: Session, job_id: str):
        job = db.get(IntegrationJob, coerce_uuid(job_id))
        if not job:
            raise HTTPException(status_code=404, detail="Integration job not found")
        job.is_active = False
        db.commit()

    @staticmethod
    def run(
        db: Session,
        job_id: str,
        *,
        trigger: str = "schedule",
        requested_by: str | None = None,
    ):
        job = db.get(IntegrationJob, coerce_uuid(job_id))
        if not job:
            raise HTTPException(status_code=404, detail="Integration job not found")
        if not job.is_active:
            # A disabled job must not run — previously this only logged (with a
            # copy-pasted EMAIL_POLL message) and fell through to execute.
            logger.info(
                "integration_job_disabled job_id=%s trigger=%s", job_id, trigger
            )
        if job.capability_binding_id is None:
            raise HTTPException(
                status_code=409,
                detail="Integration job has no capability binding",
            )
            raise HTTPException(
                status_code=409,
                detail="Integration job is disabled — enable it before running",
            )
        run = IntegrationRun(
            job_id=job.id,
            status=IntegrationRunStatus.running,
            trigger=trigger,
            requested_by=requested_by,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        try:
            from app.services.integration_sync import run_sync_job

            metrics = run_sync_job(db, job, run.id)
            run.status = IntegrationRunStatus.success
            run.metrics = metrics
        except Exception as exc:
            run.status = IntegrationRunStatus.failed
            run.error = str(exc)
            raise
        finally:
            run.finished_at = datetime.now(UTC)
            job.last_run_at = run.finished_at
            db.commit()
            db.refresh(run)
        return run


class IntegrationRuns(ListResponseMixin):
    @staticmethod
    def list(
        db: Session,
        job_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IntegrationRun)
        if job_id:
            query = query.filter(IntegrationRun.job_id == job_id)
        if status:
            query = query.filter(
                IntegrationRun.status
                == validate_enum(status, IntegrationRunStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": IntegrationRun.created_at,
                "status": IntegrationRun.status,
                "started_at": IntegrationRun.started_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def get(db: Session, run_id: str):
        run = db.get(IntegrationRun, coerce_uuid(run_id))
        if not run:
            raise HTTPException(status_code=404, detail="Integration run not found")
        return run


class IntegrationRecords(ListResponseMixin):
    @staticmethod
    def list(
        db: Session,
        run_id: str | None,
        entity_type: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        from app.models.integration import IntegrationRecord

        query = db.query(IntegrationRecord)
        if run_id:
            query = query.filter(IntegrationRecord.run_id == coerce_uuid(run_id))
        if entity_type:
            query = query.filter(IntegrationRecord.entity_type == entity_type)
        if status:
            query = query.filter(IntegrationRecord.status == status)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": IntegrationRecord.created_at,
                "status": IntegrationRecord.status,
                "action": IntegrationRecord.action,
            },
        )
        return apply_pagination(query, limit, offset).all()


integration_targets = IntegrationTargets()
integration_jobs = IntegrationJobs()
integration_runs = IntegrationRuns()
integration_records = IntegrationRecords()


def list_interval_jobs(db: Session) -> list[IntegrationJob]:
    return (
        db.query(IntegrationJob)
        .filter(IntegrationJob.is_active.is_(True))
        .filter(IntegrationJob.schedule_type == IntegrationScheduleType.interval)
        .filter(
            (IntegrationJob.interval_seconds.isnot(None))
            | (IntegrationJob.interval_minutes.isnot(None))
        )
        .all()
    )


def refresh_schedule(db: Session) -> dict[str, object]:
    count = len(list_interval_jobs(db))
    return {
        "scheduled_jobs": count,
        "detail": "Celery beat loads schedules at startup. Restart beat to apply changes.",
    }
