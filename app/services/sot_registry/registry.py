"""Canonical aggregate and query API for the modular SOT manifest."""

from __future__ import annotations

from collections import Counter
from graphlib import CycleError, TopologicalSorter

from app.services.sot_manifest import SOTService, contract_validation_errors
from app.services.sot_registry.domains import DOMAIN_DECLARATIONS
from app.services.sot_registry.model import DomainSOT

DOMAIN_SOT_RELATIONSHIPS: tuple[DomainSOT, ...] = DOMAIN_DECLARATIONS


def all_services() -> tuple[SOTService, ...]:
    """Return registered services in domain and dependency declaration order."""

    return tuple(
        service for domain in DOMAIN_SOT_RELATIONSHIPS for service in domain.services
    )


def setting_domain_declaration_errors() -> tuple[str, ...]:
    """Return structural errors in ``setting_domains`` ownership.

    Lives here rather than in ``app.services.setting_domain_registry`` so the
    registry module can depend on this one without a cycle, and so one call
    still answers "is the SOT registry sound".
    """

    errors: list[str] = []
    seen: dict[str, str] = {}
    for domain_sot in DOMAIN_SOT_RELATIONSHIPS:
        for setting_domain in domain_sot.setting_domains:
            if not setting_domain or setting_domain != setting_domain.strip():
                errors.append(
                    f"{domain_sot.domain} declares a blank or padded setting "
                    f"domain {setting_domain!r}"
                )
                continue
            previous = seen.get(setting_domain)
            if previous == domain_sot.domain:
                errors.append(
                    f"{domain_sot.domain} declares setting domain "
                    f"{setting_domain!r} more than once"
                )
                continue
            if previous is not None:
                errors.append(
                    f"setting domain {setting_domain!r} is declared by both "
                    f"{previous!r} and {domain_sot.domain!r}; exactly one SOT "
                    "domain may own it"
                )
                continue
            seen[setting_domain] = domain_sot.domain
    return tuple(sorted(errors))


def registry_validation_errors() -> tuple[str, ...]:
    """Return structural errors that make ownership resolution ambiguous."""

    errors: list[str] = list(setting_domain_declaration_errors())
    services = all_services()

    duplicate_domains = sorted(
        name
        for name, count in Counter(
            domain.domain.strip().casefold() for domain in DOMAIN_SOT_RELATIONSHIPS
        ).items()
        if count > 1
    )
    errors.extend(f"duplicate domain name: {name}" for name in duplicate_domains)

    duplicate_services = sorted(
        name
        for name, count in Counter(
            service.name.strip().casefold() for service in services
        ).items()
        if count > 1
    )
    errors.extend(f"duplicate service name: {name}" for name in duplicate_services)

    concern_owners: dict[str, list[str]] = {}
    for service in services:
        if not service.name.strip():
            errors.append("service has an empty name")
        if not service.module.strip():
            errors.append(f"service {service.name!r} has an empty module")
        if not service.owns:
            errors.append(f"service {service.name!r} has no owned concerns")
        for concern in service.owns:
            normalized = concern.strip().casefold()
            if not normalized:
                errors.append(f"service {service.name!r} has an empty concern")
                continue
            concern_owners.setdefault(normalized, []).append(service.name)

    errors.extend(
        f"duplicate exact concern {concern!r}: {', '.join(sorted(owners))}"
        for concern, owners in sorted(concern_owners.items())
        if len(owners) > 1
    )

    service_names = {service.name for service in services}
    for service in services:
        duplicate_dependencies = sorted(
            name for name, count in Counter(service.depends_on).items() if count > 1
        )
        errors.extend(
            f"service {service.name!r} repeats dependency {dependency!r}"
            for dependency in duplicate_dependencies
        )
        errors.extend(
            f"service {service.name!r} has unknown dependency {dependency!r}"
            for dependency in service.depends_on
            if dependency not in service_names
        )
        errors.extend(contract_validation_errors(service, service_names=service_names))

    dependency_graph = {service.name: set(service.depends_on) for service in services}
    try:
        tuple(TopologicalSorter(dependency_graph).static_order())
    except CycleError as exc:
        cycle = " -> ".join(str(item) for item in exc.args[1])
        errors.append(f"service dependency cycle: {cycle}")

    return tuple(sorted(errors))


def domain_order() -> list[str]:
    return [domain.domain for domain in DOMAIN_SOT_RELATIONSHIPS]


def domain_relationship(domain_name: str) -> DomainSOT:
    for domain in DOMAIN_SOT_RELATIONSHIPS:
        if domain.domain == domain_name:
            return domain
    raise KeyError(domain_name)


def services_for_domain(domain_name: str) -> tuple[SOTService, ...]:
    return domain_relationship(domain_name).services


def service_names_for_domain(domain_name: str) -> tuple[str, ...]:
    return tuple(service.name for service in services_for_domain(domain_name))


def dependencies_for(service_name: str) -> tuple[str, ...]:
    for domain in DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            if service.name == service_name:
                return service.depends_on
    raise KeyError(service_name)


def service_relationship(service_name: str) -> SOTService:
    """Return one exactly named service from the canonical registry."""

    for service in all_services():
        if service.name == service_name:
            return service
    raise KeyError(service_name)


def owning_service_for(concern: str) -> SOTService | None:
    """Return the owner of one exact, normalized concern string."""

    needle = concern.strip().lower()
    if not needle:
        return None
    for domain in DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            if any(needle == owned.strip().lower() for owned in service.owns):
                return service
    return None
