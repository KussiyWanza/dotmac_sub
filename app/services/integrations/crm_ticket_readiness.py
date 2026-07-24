"""CRM ticket capability cutover preview and executable-readiness resolver."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.integration import (
    IntegrationJob,
    IntegrationJobType,
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
from app.services import control_registry
from app.services.integrations import installations
from app.services.integrations.connectors.dotmac_crm import (
    CRM_TICKET_OBSERVATION_CAPABILITY,
)
from app.services.integrations.registry import pinned_connector_definition

CRM_CONNECTOR_KEY = "dotmac.crm"
CRM_TICKET_PULL_CONTROL = "crm.ticket_pull"
CRM_TICKET_PULL_JOB_NAME = "Pull CRM Tickets"


@dataclass(frozen=True, slots=True)
class CrmTicketPullReadiness:
    control_enabled: bool
    enabled_binding_ids: tuple[UUID, ...]
    active_job_ids: tuple[UUID, ...]
    issue_codes: tuple[str, ...]

    @property
    def ready(self) -> bool:
        """Whether the effective control state is safe and executable."""

        return not self.issue_codes

    @property
    def schedule_enabled(self) -> bool:
        return self.control_enabled and self.ready

    def to_dict(self) -> dict[str, object]:
        return {
            "active_job_ids": [str(value) for value in self.active_job_ids],
            "control_enabled": self.control_enabled,
            "enabled_binding_ids": [str(value) for value in self.enabled_binding_ids],
            "issue_codes": list(self.issue_codes),
            "ok": self.ready,
            "schedule_enabled": self.schedule_enabled,
        }


@dataclass(frozen=True, slots=True)
class CrmTicketCutoverPreview:
    installation_id: UUID | None
    connector_version: str | None
    manifest_digest: str | None
    installation_state: str | None
    installation_validated: bool
    binding_id: UUID | None
    binding_state: str | None
    job_id: UUID | None
    job_binding_id: UUID | None
    job_is_active: bool | None
    job_schedule_type: str | None
    control_enabled: bool
    blocking_errors: tuple[str, ...]
    readiness: CrmTicketPullReadiness
    fingerprint: str

    @property
    def eligible(self) -> bool:
        return not self.blocking_errors

    @property
    def already_ready(self) -> bool:
        return (
            self.eligible
            and self.readiness.ready
            and self.binding_id is not None
            and self.job_binding_id == self.binding_id
            and self.job_is_active is True
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "already_ready": self.already_ready,
            "binding_id": str(self.binding_id) if self.binding_id else None,
            "binding_state": self.binding_state,
            "blocking_errors": list(self.blocking_errors),
            "connector_version": self.connector_version,
            "control_enabled": self.control_enabled,
            "eligible": self.eligible,
            "fingerprint": self.fingerprint,
            "installation_id": (
                str(self.installation_id) if self.installation_id else None
            ),
            "installation_state": self.installation_state,
            "installation_validated": self.installation_validated,
            "job_binding_id": (
                str(self.job_binding_id) if self.job_binding_id else None
            ),
            "job_id": str(self.job_id) if self.job_id else None,
            "job_is_active": self.job_is_active,
            "job_schedule_type": self.job_schedule_type,
            "manifest_digest": self.manifest_digest,
            "readiness": self.readiness.to_dict(),
        }


def resolve_crm_ticket_pull_readiness(
    db: Session,
    *,
    control_enabled: bool | None = None,
) -> CrmTicketPullReadiness:
    """Require exactly one executable binding and one active bound job."""

    enabled = (
        control_registry.is_enabled(db, CRM_TICKET_PULL_CONTROL)
        if control_enabled is None
        else control_enabled
    )
    bindings = tuple(
        db.scalars(
            select(IntegrationCapabilityBinding)
            .join(IntegrationInstallation)
            .where(
                IntegrationCapabilityBinding.capability_id
                == CRM_TICKET_OBSERVATION_CAPABILITY,
                IntegrationCapabilityBinding.state
                == IntegrationBindingState.enabled.value,
                IntegrationInstallation.connector_key == CRM_CONNECTOR_KEY,
                IntegrationInstallation.state
                == IntegrationInstallationState.enabled.value,
            )
            .order_by(IntegrationCapabilityBinding.id)
        ).all()
    )
    jobs = tuple(
        db.scalars(
            select(IntegrationJob)
            .join(
                IntegrationCapabilityBinding,
                IntegrationJob.capability_binding_id == IntegrationCapabilityBinding.id,
            )
            .join(
                IntegrationInstallation,
                IntegrationCapabilityBinding.installation_id
                == IntegrationInstallation.id,
            )
            .join(IntegrationTarget, IntegrationJob.target_id == IntegrationTarget.id)
            .where(
                IntegrationJob.is_active.is_(True),
                IntegrationJob.job_type == IntegrationJobType.sync,
                IntegrationJob.schedule_type == IntegrationScheduleType.manual,
                IntegrationTarget.target_type == IntegrationTargetType.crm,
                IntegrationTarget.is_active.is_(True),
                IntegrationCapabilityBinding.capability_id
                == CRM_TICKET_OBSERVATION_CAPABILITY,
                IntegrationCapabilityBinding.state
                == IntegrationBindingState.enabled.value,
                IntegrationInstallation.connector_key == CRM_CONNECTOR_KEY,
                IntegrationInstallation.state
                == IntegrationInstallationState.enabled.value,
            )
            .order_by(IntegrationJob.id)
        ).all()
    )
    issues: list[str] = []
    if enabled:
        if len(bindings) != 1:
            issues.append(f"enabled_ticket_observation_binding_count:{len(bindings)}")
        if len(jobs) != 1:
            issues.append(f"active_ticket_observation_job_count:{len(jobs)}")
        if (
            len(bindings) == 1
            and len(jobs) == 1
            and jobs[0].capability_binding_id != bindings[0].id
        ):
            issues.append("active_ticket_job_binding_mismatch")
    return CrmTicketPullReadiness(
        control_enabled=enabled,
        enabled_binding_ids=tuple(binding.id for binding in bindings),
        active_job_ids=tuple(job.id for job in jobs),
        issue_codes=tuple(issues),
    )


def _installation_candidates(
    db: Session,
    installation_id: UUID | None,
) -> tuple[IntegrationInstallation, ...]:
    query = select(IntegrationInstallation).where(
        IntegrationInstallation.connector_key == CRM_CONNECTOR_KEY
    )
    if installation_id is not None:
        query = query.where(IntegrationInstallation.id == installation_id)
    else:
        query = query.where(
            IntegrationInstallation.environment == "production",
            IntegrationInstallation.state == IntegrationInstallationState.enabled.value,
        )
    return tuple(
        db.scalars(
            query.order_by(
                IntegrationInstallation.name,
                IntegrationInstallation.id,
            )
        ).all()
    )


def _job_candidates(
    db: Session,
    job_id: UUID | None,
) -> tuple[IntegrationJob, ...]:
    query = select(IntegrationJob).join(IntegrationTarget)
    if job_id is not None:
        query = query.where(IntegrationJob.id == job_id)
    else:
        query = query.where(
            IntegrationTarget.target_type == IntegrationTargetType.crm,
            IntegrationJob.name == CRM_TICKET_PULL_JOB_NAME,
        )
    return tuple(db.scalars(query.order_by(IntegrationJob.id)).all())


def preview_crm_ticket_cutover(
    db: Session,
    *,
    installation_id: UUID | None = None,
    job_id: UUID | None = None,
) -> CrmTicketCutoverPreview:
    """Build a non-secret exact-state review for the CRM ticket cutover."""

    installations_found = _installation_candidates(db, installation_id)
    jobs_found = _job_candidates(db, job_id)
    blockers: list[str] = []
    if len(installations_found) != 1:
        blockers.append(f"crm_installation_count:{len(installations_found)}")
    if len(jobs_found) != 1:
        blockers.append(f"crm_ticket_job_count:{len(jobs_found)}")

    installation = installations_found[0] if len(installations_found) == 1 else None
    job = jobs_found[0] if len(jobs_found) == 1 else None
    binding: IntegrationCapabilityBinding | None = None
    if installation is not None:
        if installation.state != IntegrationInstallationState.enabled.value:
            blockers.append(f"installation_state:{installation.state}")
        if installation.environment != "production":
            blockers.append(f"installation_environment:{installation.environment}")
        if installation.current_config_revision_id is None:
            blockers.append("installation_config_missing")
        if installation.validated_at is None:
            blockers.append("installation_not_validated")
        pin = installations.manifest_pin_check(installation)
        if pin.state is installations.ManifestPinState.unavailable:
            blockers.append("installation_manifest_unavailable")
        definition = pinned_connector_definition(
            installation.connector_key,
            version=installation.connector_version,
            manifest_digest=installation.manifest_digest,
        )
        if (
            definition is None
            or definition.capability(CRM_TICKET_OBSERVATION_CAPABILITY) is None
        ):
            blockers.append("ticket_observation_not_declared")
        binding = db.scalar(
            select(IntegrationCapabilityBinding).where(
                IntegrationCapabilityBinding.installation_id == installation.id,
                IntegrationCapabilityBinding.capability_id
                == CRM_TICKET_OBSERVATION_CAPABILITY,
            )
        )

    if job is not None:
        if job.target.target_type != IntegrationTargetType.crm:
            blockers.append(f"job_target_type:{job.target.target_type.value}")
        if not job.target.is_active:
            blockers.append("job_target_disabled")
        if job.job_type != IntegrationJobType.sync:
            blockers.append(f"job_type:{job.job_type.value}")
        if job.schedule_type != IntegrationScheduleType.manual:
            blockers.append(f"job_schedule_type:{job.schedule_type.value}")
        if job.capability_binding_id is not None and (
            binding is None or job.capability_binding_id != binding.id
        ):
            blockers.append("job_bound_to_other_capability")

    readiness = resolve_crm_ticket_pull_readiness(db)
    payload = {
        "binding_id": str(binding.id) if binding is not None else None,
        "binding_state": binding.state if binding is not None else None,
        "blocking_errors": sorted(blockers),
        "connector_version": (
            installation.connector_version if installation is not None else None
        ),
        "control_enabled": readiness.control_enabled,
        "installation_id": (str(installation.id) if installation is not None else None),
        "installation_state": (
            installation.state if installation is not None else None
        ),
        "installation_validated": bool(
            installation is not None and installation.validated_at is not None
        ),
        "job_binding_id": (
            str(job.capability_binding_id)
            if job is not None and job.capability_binding_id is not None
            else None
        ),
        "job_id": str(job.id) if job is not None else None,
        "job_is_active": job.is_active if job is not None else None,
        "job_schedule_type": (job.schedule_type.value if job is not None else None),
        "manifest_digest": (
            installation.manifest_digest if installation is not None else None
        ),
        "readiness": readiness.to_dict(),
        "schema_version": 1,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return CrmTicketCutoverPreview(
        installation_id=installation.id if installation is not None else None,
        connector_version=(
            installation.connector_version if installation is not None else None
        ),
        manifest_digest=(
            installation.manifest_digest if installation is not None else None
        ),
        installation_state=(installation.state if installation is not None else None),
        installation_validated=bool(
            installation is not None and installation.validated_at is not None
        ),
        binding_id=binding.id if binding is not None else None,
        binding_state=binding.state if binding is not None else None,
        job_id=job.id if job is not None else None,
        job_binding_id=job.capability_binding_id if job is not None else None,
        job_is_active=job.is_active if job is not None else None,
        job_schedule_type=(job.schedule_type.value if job is not None else None),
        control_enabled=readiness.control_enabled,
        blocking_errors=tuple(sorted(blockers)),
        readiness=readiness,
        fingerprint=fingerprint,
    )


__all__ = [
    "CRM_CONNECTOR_KEY",
    "CRM_TICKET_PULL_CONTROL",
    "CRM_TICKET_PULL_JOB_NAME",
    "CrmTicketCutoverPreview",
    "CrmTicketPullReadiness",
    "preview_crm_ticket_cutover",
    "resolve_crm_ticket_pull_readiness",
]
