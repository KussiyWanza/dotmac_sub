"""Own DotMac ERP operational-feed administration and persistence."""

from __future__ import annotations

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

ERP_OPERATIONAL_DOMAINS = (
    "projects",
    "project_tasks",
    "tickets",
    "work_orders",
)


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


def configure_domains(
    db: Session,
    *,
    domains: list[str],
    actor: str,
    context: CommandContext,
) -> None:
    selected = tuple(dict.fromkeys(item.strip() for item in domains if item.strip()))
    unknown = set(selected) - set(ERP_OPERATIONAL_DOMAINS)
    if unknown:
        raise installations.InstallationError(
            f"Unsupported ERP sync domains: {', '.join(sorted(unknown))}"
        )
    dependency_domains: tuple[str, ...] = ()
    if "project_tasks" in selected or "work_orders" in selected:
        dependency_domains = ("projects", "tickets")
    selected = tuple(dict.fromkeys((*dependency_domains, *selected)))
    installation = _installation(db)
    binding = _binding(db, installation.id)
    if not selected:
        if (
            binding is not None
            and binding.state == IntegrationBindingState.enabled.value
        ):
            installations.execute_command(
                db,
                lambda: installations.disable_capability_binding(
                    db, capability_binding_id=binding.id, actor=actor
                ),
            )
        from app.services.scheduler_config import sync_erp_operational_schedule

        sync_erp_operational_schedule(db, enabled=False)
        return
    installations.provision_installation_capability(
        db,
        installations.ProvisionCapabilityCommand(
            installation_id=installation.id,
            capability_id=ERP_OPERATIONAL_SYNC_CAPABILITY,
            expected_installed_pin=installations.ManifestPin(
                connector_version=installation.connector_version,
                manifest_digest=installation.manifest_digest,
            ),
            expected_binding_id=binding.id if binding else None,
            expected_binding_state=(
                IntegrationBindingState(binding.state) if binding else None
            ),
            capability_scope={"domains": list(selected)},
            policy={"route": "/api/v1/sync/sub/bulk", "batch_size": 100},
        ),
        context=context,
    )
    from app.services.scheduler_config import sync_erp_operational_schedule

    sync_erp_operational_schedule(db, enabled=True)
