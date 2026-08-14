"""Own DotMac ERP operational-feed administration and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationInstallation,
)
from app.services.integrations import installations
from app.services.integrations.backoffice_contracts import (
    ERP_OPERATIONAL_SYNC_CAPABILITY,
)
from app.services.owner_commands import CommandContext


class ErpOperationalDomain(StrEnum):
    projects = "projects"
    project_tasks = "project_tasks"
    tickets = "tickets"
    work_orders = "work_orders"


ERP_OPERATIONAL_DOMAINS = tuple(domain.value for domain in ErpOperationalDomain)


@dataclass(frozen=True, slots=True)
class ErpOperationalDomainSelection:
    domains: tuple[ErpOperationalDomain, ...]


@dataclass(frozen=True, slots=True)
class ErpOperationalDomainConfigurationPlan:
    installation_id: UUID
    connector_version: str
    manifest_digest: str
    expected_binding_id: UUID | None
    expected_binding_state: IntegrationBindingState | None
    domains: tuple[ErpOperationalDomain, ...]


def parse_domain_selection(domains: list[str]) -> ErpOperationalDomainSelection:
    parsed: list[ErpOperationalDomain] = []
    for raw_domain in domains:
        value = raw_domain.strip()
        if not value:
            continue
        try:
            domain = ErpOperationalDomain(value)
        except ValueError as exc:
            raise installations.InstallationError(
                f"Unsupported ERP sync domain: {value}"
            ) from exc
        if domain not in parsed:
            parsed.append(domain)
    return ErpOperationalDomainSelection(domains=tuple(parsed))


def _installation(db: Session) -> IntegrationInstallation:
    row = (
        db.query(IntegrationInstallation)
        .filter(IntegrationInstallation.connector_key == "dotmac.erp")
        .filter(IntegrationInstallation.state != "retired")
        .order_by(IntegrationInstallation.created_at.desc())
        .first()
    )
    if row is None:
        raise installations.InstallationError("DotMac ERP is not installed")
    return row


def _binding(db: Session, installation_id: UUID) -> IntegrationCapabilityBinding | None:
    return (
        db.query(IntegrationCapabilityBinding)
        .filter(IntegrationCapabilityBinding.installation_id == installation_id)
        .filter(
            IntegrationCapabilityBinding.capability_id
            == ERP_OPERATIONAL_SYNC_CAPABILITY
        )
        .one_or_none()
    )


def build_config_state(db: Session) -> dict[str, object]:
    installation = _installation(db)
    binding = _binding(db, installation.id)
    configured = (
        tuple((binding.scope_json or {}).get("domains") or ()) if binding else ()
    )
    return {
        "installation": installation,
        "binding": binding,
        "available_domains": ERP_OPERATIONAL_DOMAINS,
        "configured_domains": configured,
        "enabled": bool(
            binding and binding.state == IntegrationBindingState.enabled.value
        ),
    }


def review_domain_configuration(
    db: Session,
    *,
    selection: ErpOperationalDomainSelection,
) -> ErpOperationalDomainConfigurationPlan:
    selected = selection.domains
    dependency_domains: tuple[ErpOperationalDomain, ...] = ()
    if (
        ErpOperationalDomain.project_tasks in selected
        or ErpOperationalDomain.work_orders in selected
    ):
        dependency_domains = (
            ErpOperationalDomain.projects,
            ErpOperationalDomain.tickets,
        )
    selected = tuple(dict.fromkeys((*dependency_domains, *selected)))
    installation = _installation(db)
    binding = _binding(db, installation.id)
    return ErpOperationalDomainConfigurationPlan(
        installation_id=installation.id,
        connector_version=installation.connector_version,
        manifest_digest=installation.manifest_digest,
        expected_binding_id=binding.id if binding else None,
        expected_binding_state=(
            IntegrationBindingState(binding.state) if binding else None
        ),
        domains=selected,
    )


def configure_domains(
    db: Session,
    *,
    plan: ErpOperationalDomainConfigurationPlan,
    actor: str,
    context: CommandContext,
) -> None:
    selected = plan.domains
    if not selected:
        binding_id = plan.expected_binding_id
        if (
            binding_id is not None
            and plan.expected_binding_state is IntegrationBindingState.enabled
        ):
            installations.execute_command(
                db,
                lambda: installations.disable_capability_binding(
                    db,
                    capability_binding_id=binding_id,
                    actor=actor,
                ),
            )
        from app.services.scheduler_config import sync_erp_operational_schedule

        sync_erp_operational_schedule(db, enabled=False)
        return
    installations.provision_installation_capability(
        db,
        installations.ProvisionCapabilityCommand(
            installation_id=plan.installation_id,
            capability_id=ERP_OPERATIONAL_SYNC_CAPABILITY,
            expected_installed_pin=installations.ManifestPin(
                connector_version=plan.connector_version,
                manifest_digest=plan.manifest_digest,
            ),
            expected_binding_id=plan.expected_binding_id,
            expected_binding_state=plan.expected_binding_state,
            capability_scope={"domains": [domain.value for domain in selected]},
            policy={"route": "/api/v1/sync/sub/bulk", "batch_size": 100},
        ),
        context=context,
    )
    from app.services.scheduler_config import sync_erp_operational_schedule

    sync_erp_operational_schedule(db, enabled=True)
