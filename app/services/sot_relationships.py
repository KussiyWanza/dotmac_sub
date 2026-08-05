"""Compatibility facade for the canonical modular SOT registry.

New registry code belongs under :mod:`app.services.sot_registry`; existing
callers may continue importing this module during the compatibility window.
"""

from __future__ import annotations

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    EventContract,
    MigrationContract,
    OwnerRole,
    ProjectionContract,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    contract_validation_errors,
    owner_command_boundary_error_codes,
)
from app.services.sot_registry.model import DomainSOT
from app.services.sot_registry.registry import (
    DOMAIN_SOT_RELATIONSHIPS,
    all_services,
    dependencies_for,
    domain_order,
    domain_relationship,
    owning_service_for,
    registry_validation_errors,
    service_names_for_domain,
    service_relationship,
    services_for_domain,
)

__all__ = (
    "AuthorityInput",
    "AuthorityKind",
    "AuthorityMigrationState",
    "ConcernContract",
    "ErrorContract",
    "EventContract",
    "MigrationContract",
    "OwnerRole",
    "ProjectionContract",
    "ServiceContract",
    "SOTService",
    "TransactionContract",
    "TransactionMode",
    "contract_validation_errors",
    "owner_command_boundary_error_codes",
    "DomainSOT",
    "DOMAIN_SOT_RELATIONSHIPS",
    "all_services",
    "dependencies_for",
    "domain_order",
    "domain_relationship",
    "owning_service_for",
    "registry_validation_errors",
    "service_names_for_domain",
    "service_relationship",
    "services_for_domain",
)
