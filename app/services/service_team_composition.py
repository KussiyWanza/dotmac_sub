"""Composable service-team authority and set-valued routing queries.

``ServiceTeam`` is only the stable operational group identity. Capability,
responsibility, geographic scope, topology, external observations, and routing
are independent facts with typed contracts. RBAC is intentionally absent from
this module: callers must authorize first and then apply the returned team
scope as a narrowing condition.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models.audit import AuditActorType
from app.models.gis import GeoArea
from app.models.party import Party, PartyIdentityStatus, PartyType
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamCapability,
    ServiceTeamCapabilityDefinition,
    ServiceTeamCapabilityKey,
    ServiceTeamExternalReference,
    ServiceTeamMember,
    ServiceTeamMemberResponsibility,
    ServiceTeamRelationship,
    ServiceTeamRelationshipKind,
    ServiceTeamResponsibilityKey,
    ServiceTeamRoutingPolicy,
    ServiceTeamScopeBinding,
    ServiceTeamScopeType,
)
from app.models.system_user import SystemUser
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "operations.service_team_composition"
COMPOSITION_CONCERN = "service-team composition lifecycle"
ROUTING_CONCERN = "explicit service-team routing policy"

_SET_CAPABILITY = OwnerCommandDefinition(
    owner=OWNER, concern=COMPOSITION_CONCERN, name="set_service_team_capability"
)
_SET_RESPONSIBILITY = OwnerCommandDefinition(
    owner=OWNER,
    concern=COMPOSITION_CONCERN,
    name="set_service_team_member_responsibility",
)
_SET_SCOPE = OwnerCommandDefinition(
    owner=OWNER, concern=COMPOSITION_CONCERN, name="set_service_team_scope"
)
_SET_RELATIONSHIP = OwnerCommandDefinition(
    owner=OWNER, concern=COMPOSITION_CONCERN, name="set_service_team_relationship"
)
_RECORD_EXTERNAL_REFERENCE = OwnerCommandDefinition(
    owner=OWNER,
    concern=COMPOSITION_CONCERN,
    name="record_service_team_external_reference",
)
_SET_ROUTING_POLICY = OwnerCommandDefinition(
    owner=OWNER, concern=ROUTING_CONCERN, name="set_service_team_routing_policy"
)


@dataclass(frozen=True)
class CapabilityContract:
    key: ServiceTeamCapabilityKey
    display_name: str
    contract_owner: str
    contract_version: int


@dataclass(frozen=True)
class RoutingPolicyContract:
    domain: str
    route_key: str
    contract_owner: str
    contract_version: int
    required_capability: ServiceTeamCapabilityKey


CAPABILITY_CONTRACTS: Mapping[ServiceTeamCapabilityKey, CapabilityContract] = (
    MappingProxyType(
        {
            ServiceTeamCapabilityKey.customer_support: CapabilityContract(
                key=ServiceTeamCapabilityKey.customer_support,
                display_name="Customer support",
                contract_owner="support.ticket_lifecycle",
                contract_version=1,
            ),
            ServiceTeamCapabilityKey.billing_operations: CapabilityContract(
                key=ServiceTeamCapabilityKey.billing_operations,
                display_name="Billing operations",
                contract_owner="billing.account_lifecycle",
                contract_version=1,
            ),
            ServiceTeamCapabilityKey.field_service: CapabilityContract(
                key=ServiceTeamCapabilityKey.field_service,
                display_name="Field service",
                contract_owner="operations.work_orders",
                contract_version=1,
            ),
            ServiceTeamCapabilityKey.project_delivery: CapabilityContract(
                key=ServiceTeamCapabilityKey.project_delivery,
                display_name="Project delivery",
                contract_owner="operations.project_lifecycle",
                contract_version=1,
            ),
            ServiceTeamCapabilityKey.network_operations: CapabilityContract(
                key=ServiceTeamCapabilityKey.network_operations,
                display_name="Network operations",
                contract_owner="network.topology",
                contract_version=1,
            ),
            ServiceTeamCapabilityKey.outage_response: CapabilityContract(
                key=ServiceTeamCapabilityKey.outage_response,
                display_name="Outage response",
                contract_owner="network.outage_lifecycle",
                contract_version=1,
            ),
        }
    )
)

ROUTING_POLICY_CONTRACTS: Mapping[
    tuple[str, str],
    RoutingPolicyContract,
] = MappingProxyType(
    {
        ("network.outage", "incident.primary"): RoutingPolicyContract(
            domain="network.outage",
            route_key="incident.primary",
            contract_owner="network.outage_lifecycle",
            contract_version=1,
            required_capability=ServiceTeamCapabilityKey.outage_response,
        ),
        ("network.outage", "incident.support_watcher"): RoutingPolicyContract(
            domain="network.outage",
            route_key="incident.support_watcher",
            contract_owner="network.outage_lifecycle",
            contract_version=1,
            required_capability=ServiceTeamCapabilityKey.customer_support,
        ),
        ("network.outage", "incident.field_watcher"): RoutingPolicyContract(
            domain="network.outage",
            route_key="incident.field_watcher",
            contract_owner="network.outage_lifecycle",
            contract_version=1,
            required_capability=ServiceTeamCapabilityKey.field_service,
        ),
    }
)

LEGACY_TYPE_CAPABILITIES: Mapping[str, tuple[ServiceTeamCapabilityKey, ...]] = (
    MappingProxyType(
        {
            "support": (ServiceTeamCapabilityKey.customer_support,),
            "billing": (ServiceTeamCapabilityKey.billing_operations,),
            "field_service": (ServiceTeamCapabilityKey.field_service,),
            "project_management": (ServiceTeamCapabilityKey.project_delivery,),
            # Legacy operations teams owned outages, so they carry both.
            "operations": (
                ServiceTeamCapabilityKey.network_operations,
                ServiceTeamCapabilityKey.outage_response,
            ),
        }
    )
)

LEGACY_ROLE_RESPONSIBILITIES: Mapping[str, tuple[ServiceTeamResponsibilityKey, ...]] = (
    MappingProxyType(
        {
            "member": (ServiceTeamResponsibilityKey.agent,),
            "lead": (
                ServiceTeamResponsibilityKey.agent,
                ServiceTeamResponsibilityKey.queue_lead,
            ),
            "manager": (
                ServiceTeamResponsibilityKey.agent,
                ServiceTeamResponsibilityKey.accountable_manager,
            ),
        }
    )
)


class ServiceTeamCompositionError(DomainError):
    """Stable, transport-neutral composition or routing refusal."""


@dataclass(frozen=True)
class SetTeamCapability:
    context: CommandContext
    team_id: UUID
    capability: ServiceTeamCapabilityKey
    is_active: bool


@dataclass(frozen=True)
class SetMemberResponsibility:
    context: CommandContext
    team_id: UUID
    membership_id: UUID
    responsibility: ServiceTeamResponsibilityKey
    is_active: bool


@dataclass(frozen=True)
class SetTeamScope:
    context: CommandContext
    team_id: UUID
    scope_type: ServiceTeamScopeType
    geo_area_id: UUID | None
    is_active: bool


@dataclass(frozen=True)
class SetTeamRelationship:
    context: CommandContext
    parent_team_id: UUID
    child_team_id: UUID
    kind: ServiceTeamRelationshipKind
    is_active: bool


@dataclass(frozen=True)
class RecordTeamExternalReference:
    context: CommandContext
    team_id: UUID
    provider: str
    account_scope: str
    external_id: str
    provenance: str
    observed_at: datetime
    is_active: bool = True


@dataclass(frozen=True)
class SetTeamRoutingPolicy:
    context: CommandContext
    policy_id: UUID
    domain: str
    route_key: str
    team_id: UUID
    priority: int
    scope_binding_id: UUID | None
    is_active: bool


@dataclass(frozen=True)
class CompositionMutation:
    team_id: UUID
    record_id: UUID
    operation: str
    replayed: bool


@dataclass(frozen=True)
class RoutedServiceTeam:
    policy_id: UUID
    team_id: UUID
    domain: str
    route_key: str
    priority: int
    scope_binding_id: UUID | None


@dataclass(frozen=True)
class TeamScopeBinding:
    binding_id: UUID
    scope_type: ServiceTeamScopeType
    geo_area_id: UUID | None
    is_active: bool


@dataclass(frozen=True)
class TeamRelationshipEdge:
    relationship_id: UUID
    parent_team_id: UUID
    child_team_id: UUID
    kind: ServiceTeamRelationshipKind
    is_active: bool


@dataclass(frozen=True)
class TeamExternalReferenceObservation:
    reference_id: UUID
    provider: str
    account_scope: str
    external_id: str
    provenance: str
    observed_at: datetime
    is_active: bool


@dataclass(frozen=True)
class TeamRoutingPolicyBinding:
    policy_id: UUID
    domain: str
    route_key: str
    priority: int
    scope_binding_id: UUID | None
    is_active: bool


@dataclass(frozen=True)
class StaffCapabilityScope:
    system_user_id: UUID
    person_party_id: UUID | None
    identity_available: bool
    team_ids: tuple[UUID, ...]
    capability_team_ids: Mapping[ServiceTeamCapabilityKey, tuple[UUID, ...]]
    responsibility_team_ids: Mapping[ServiceTeamResponsibilityKey, tuple[UUID, ...]]


@dataclass(frozen=True)
class CompositionShadowDrift:
    legacy_type_without_capability: int
    legacy_role_without_responsibility: int
    legacy_manager_without_responsibility: int
    legacy_region_without_geo_scope: int
    legacy_workforce_without_external_reference: int

    @property
    def blocker_count(self) -> int:
        return sum(
            (
                self.legacy_type_without_capability,
                self.legacy_role_without_responsibility,
                self.legacy_manager_without_responsibility,
                self.legacy_region_without_geo_scope,
                self.legacy_workforce_without_external_reference,
            )
        )


def _error(code: str, message: str, **details: object) -> ServiceTeamCompositionError:
    return ServiceTeamCompositionError(code=code, message=message, details=details)


def _actor(context: CommandContext) -> tuple[AuditActorType, str | None]:
    candidate = str(context.actor or "").rsplit(":", maxsplit=1)[-1]
    try:
        UUID(candidate)
    except (TypeError, ValueError):
        return AuditActorType.system, None
    return AuditActorType.user, candidate


def _clean(value: str, *, field: str, maximum: int) -> str:
    cleaned = " ".join(str(value or "").split())
    if not cleaned or len(cleaned) > maximum:
        raise _error(
            "service_team_composition_invalid",
            f"{field.replace('_', ' ').title()} is required and must be "
            f"{maximum} characters or fewer.",
            field=field,
        )
    return cleaned


def _locked_team(db: Session, team_id: UUID) -> ServiceTeam:
    team = db.scalar(
        select(ServiceTeam).where(ServiceTeam.id == team_id).with_for_update()
    )
    if team is None:
        raise _error(
            "service_team_not_found",
            "Service team was not found.",
            team_id=str(team_id),
        )
    return team


def _governed_capability_definition(
    db: Session,
    capability: ServiceTeamCapabilityKey,
    *,
    lock: bool = False,
) -> ServiceTeamCapabilityDefinition:
    statement = select(ServiceTeamCapabilityDefinition).where(
        ServiceTeamCapabilityDefinition.key == capability.value,
        ServiceTeamCapabilityDefinition.is_active.is_(True),
    )
    definition = db.scalar(statement.with_for_update() if lock else statement)
    if definition is None:
        raise _error(
            "service_team_capability_unregistered",
            "The capability is not registered for assignment or use.",
            capability=capability.value,
        )
    contract = CAPABILITY_CONTRACTS[capability]
    if (
        definition.display_name != contract.display_name
        or definition.contract_owner != contract.contract_owner
        or definition.contract_version != contract.contract_version
    ):
        raise _error(
            "service_team_capability_contract_drift",
            "The registered capability definition differs from its code contract.",
            capability=capability.value,
        )
    return definition


def _routing_policy_contract(
    domain: str,
    route_key: str,
) -> RoutingPolicyContract:
    contract = ROUTING_POLICY_CONTRACTS.get((domain, route_key))
    if contract is None:
        raise _error(
            "service_team_routing_unregistered",
            "The domain route key has no registered routing contract.",
            domain=domain,
            route_key=route_key,
        )
    return contract


def _audit_event(
    db: Session,
    *,
    context: CommandContext,
    team_id: UUID,
    record_id: UUID,
    operation: str,
    details: Mapping[str, object],
) -> None:
    actor_type, actor_id = _actor(context)
    evidence = {
        "schema_version": 1,
        "owner": OWNER,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
        "team_id": str(team_id),
        "record_id": str(record_id),
        "operation": operation,
        **details,
    }
    stage_audit_event(
        db,
        action=f"service_team.composition.{operation}",
        entity_type="service_team",
        entity_id=str(team_id),
        actor_type=actor_type,
        actor_id=actor_id,
        metadata=evidence,
    )
    emit_event(
        db,
        EventType.service_team_changed,
        {
            **evidence,
            "aggregate_type": "service_team",
            "aggregate_id": str(team_id),
            "aggregate_version": str(context.command_id),
        },
        actor=context.actor,
    )


def set_capability(db: Session, command: SetTeamCapability) -> CompositionMutation:
    def apply() -> CompositionMutation:
        team = _locked_team(db, command.team_id)
        _governed_capability_definition(db, command.capability, lock=True)
        row = db.scalar(
            select(ServiceTeamCapability)
            .where(
                ServiceTeamCapability.team_id == team.id,
                ServiceTeamCapability.capability_key == command.capability.value,
            )
            .with_for_update()
        )
        if row is not None and row.is_active is command.is_active:
            return CompositionMutation(team.id, row.id, "capability_set", True)
        if row is None:
            row = ServiceTeamCapability(
                team_id=team.id,
                capability_key=command.capability.value,
                is_active=command.is_active,
            )
            db.add(row)
        else:
            row.is_active = command.is_active
        db.flush()
        _audit_event(
            db,
            context=command.context,
            team_id=team.id,
            record_id=row.id,
            operation="capability_set",
            details={
                "capability": command.capability.value,
                "is_active": command.is_active,
            },
        )
        return CompositionMutation(team.id, row.id, "capability_set", False)

    return execute_owner_command(
        db,
        definition=_SET_CAPABILITY,
        context=command.context,
        operation=apply,
    )


def set_responsibility(
    db: Session, command: SetMemberResponsibility
) -> CompositionMutation:
    def apply() -> CompositionMutation:
        team = _locked_team(db, command.team_id)
        member = db.scalar(
            select(ServiceTeamMember)
            .where(
                ServiceTeamMember.id == command.membership_id,
                ServiceTeamMember.team_id == team.id,
            )
            .with_for_update()
        )
        if member is None:
            raise _error(
                "service_team_member_not_found",
                "Service-team membership was not found.",
                membership_id=str(command.membership_id),
            )
        if command.is_active and not member.is_active:
            raise _error(
                "service_team_member_inactive",
                "An inactive membership cannot carry an active responsibility.",
                membership_id=str(member.id),
            )
        row = db.scalar(
            select(ServiceTeamMemberResponsibility)
            .where(
                ServiceTeamMemberResponsibility.membership_id == member.id,
                ServiceTeamMemberResponsibility.responsibility_key
                == command.responsibility.value,
            )
            .with_for_update()
        )
        if row is not None and row.is_active is command.is_active:
            return CompositionMutation(team.id, row.id, "responsibility_set", True)
        if row is None:
            row = ServiceTeamMemberResponsibility(
                membership_id=member.id,
                responsibility_key=command.responsibility.value,
                is_active=command.is_active,
            )
            db.add(row)
        else:
            row.is_active = command.is_active
        db.flush()
        _audit_event(
            db,
            context=command.context,
            team_id=team.id,
            record_id=row.id,
            operation="responsibility_set",
            details={
                "membership_id": str(member.id),
                "responsibility": command.responsibility.value,
                "is_active": command.is_active,
            },
        )
        return CompositionMutation(team.id, row.id, "responsibility_set", False)

    return execute_owner_command(
        db,
        definition=_SET_RESPONSIBILITY,
        context=command.context,
        operation=apply,
    )


def set_scope(db: Session, command: SetTeamScope) -> CompositionMutation:
    if (
        command.scope_type is ServiceTeamScopeType.global_
        and command.geo_area_id is not None
    ) or (
        command.scope_type is ServiceTeamScopeType.geo_area
        and command.geo_area_id is None
    ):
        raise _error(
            "service_team_scope_invalid",
            "Choose either global scope or one GeoArea.",
        )

    def apply() -> CompositionMutation:
        team = _locked_team(db, command.team_id)
        if command.geo_area_id is not None:
            area_statement = select(GeoArea).where(GeoArea.id == command.geo_area_id)
            if command.is_active:
                area_statement = area_statement.where(GeoArea.is_active.is_(True))
            area = db.scalar(area_statement.with_for_update())
            if area is None:
                raise _error(
                    "service_team_geo_area_not_found",
                    "The geographic area was not found or is inactive.",
                    geo_area_id=str(command.geo_area_id),
                )
        statement = select(ServiceTeamScopeBinding).where(
            ServiceTeamScopeBinding.team_id == team.id,
            ServiceTeamScopeBinding.scope_type == command.scope_type.value,
        )
        if command.geo_area_id is None:
            statement = statement.where(ServiceTeamScopeBinding.geo_area_id.is_(None))
        else:
            statement = statement.where(
                ServiceTeamScopeBinding.geo_area_id == command.geo_area_id
            )
        row = db.scalar(statement.with_for_update())
        if row is not None and row.is_active is command.is_active:
            return CompositionMutation(team.id, row.id, "scope_set", True)
        if row is None:
            row = ServiceTeamScopeBinding(
                team_id=team.id,
                scope_type=command.scope_type.value,
                geo_area_id=command.geo_area_id,
                is_active=command.is_active,
            )
            db.add(row)
        else:
            row.is_active = command.is_active
        db.flush()
        _audit_event(
            db,
            context=command.context,
            team_id=team.id,
            record_id=row.id,
            operation="scope_set",
            details={
                "scope_type": command.scope_type.value,
                "geo_area_id": (
                    str(command.geo_area_id) if command.geo_area_id else None
                ),
                "is_active": command.is_active,
            },
        )
        return CompositionMutation(team.id, row.id, "scope_set", False)

    return execute_owner_command(
        db, definition=_SET_SCOPE, context=command.context, operation=apply
    )


def set_relationship(db: Session, command: SetTeamRelationship) -> CompositionMutation:
    if command.parent_team_id == command.child_team_id:
        raise _error(
            "service_team_relationship_invalid",
            "A service team cannot be related to itself.",
        )

    def apply() -> CompositionMutation:
        teams = {
            team_id: _locked_team(db, team_id)
            for team_id in sorted(
                (command.parent_team_id, command.child_team_id),
                key=str,
            )
        }
        parent = teams[command.parent_team_id]
        if (
            command.is_active
            and command.kind is ServiceTeamRelationshipKind.parent_child
        ):
            active_edges = db.execute(
                select(
                    ServiceTeamRelationship.parent_team_id,
                    ServiceTeamRelationship.child_team_id,
                )
                .where(
                    ServiceTeamRelationship.relationship_kind
                    == ServiceTeamRelationshipKind.parent_child.value,
                    ServiceTeamRelationship.is_active.is_(True),
                )
                .order_by(ServiceTeamRelationship.id.asc())
                .with_for_update()
            ).all()
            children_by_parent: dict[UUID, set[UUID]] = {}
            for parent_id, child_id in active_edges:
                children_by_parent.setdefault(parent_id, set()).add(child_id)
            pending = [command.child_team_id]
            visited: set[UUID] = set()
            while pending:
                candidate = pending.pop()
                if candidate == command.parent_team_id:
                    raise _error(
                        "service_team_relationship_cycle",
                        "The parent-child relationship would create a cycle.",
                        parent_team_id=str(command.parent_team_id),
                        child_team_id=str(command.child_team_id),
                    )
                if candidate in visited:
                    continue
                visited.add(candidate)
                pending.extend(children_by_parent.get(candidate, ()))
        row = db.scalar(
            select(ServiceTeamRelationship)
            .where(
                ServiceTeamRelationship.parent_team_id == command.parent_team_id,
                ServiceTeamRelationship.child_team_id == command.child_team_id,
                ServiceTeamRelationship.relationship_kind == command.kind.value,
            )
            .with_for_update()
        )
        if row is not None and row.is_active is command.is_active:
            return CompositionMutation(parent.id, row.id, "relationship_set", True)
        if row is None:
            row = ServiceTeamRelationship(
                parent_team_id=command.parent_team_id,
                child_team_id=command.child_team_id,
                relationship_kind=command.kind.value,
                is_active=command.is_active,
            )
            db.add(row)
        else:
            row.is_active = command.is_active
        db.flush()
        _audit_event(
            db,
            context=command.context,
            team_id=parent.id,
            record_id=row.id,
            operation="relationship_set",
            details={
                "child_team_id": str(command.child_team_id),
                "relationship_kind": command.kind.value,
                "is_active": command.is_active,
            },
        )
        return CompositionMutation(parent.id, row.id, "relationship_set", False)

    return execute_owner_command(
        db,
        definition=_SET_RELATIONSHIP,
        context=command.context,
        operation=apply,
    )


def record_external_reference(
    db: Session, command: RecordTeamExternalReference
) -> CompositionMutation:
    provider = _clean(command.provider, field="provider", maximum=80).casefold()
    account_scope = _clean(command.account_scope, field="account_scope", maximum=120)
    external_id = _clean(command.external_id, field="external_id", maximum=200)
    provenance = _clean(command.provenance, field="provenance", maximum=160)
    observed_at = (
        command.observed_at.replace(tzinfo=UTC)
        if command.observed_at.tzinfo is None
        else command.observed_at.astimezone(UTC)
    )

    def apply() -> CompositionMutation:
        team = _locked_team(db, command.team_id)
        row = db.scalar(
            select(ServiceTeamExternalReference)
            .where(
                ServiceTeamExternalReference.provider == provider,
                ServiceTeamExternalReference.account_scope == account_scope,
                ServiceTeamExternalReference.external_id == external_id,
            )
            .with_for_update()
        )
        if row is not None and row.team_id != team.id:
            raise _error(
                "service_team_external_reference_conflict",
                "The provider reference is already observed for another team.",
                provider=provider,
                account_scope=account_scope,
            )
        desired = (provenance, observed_at, command.is_active)
        if (
            row is not None
            and (row.provenance, row.observed_at, row.is_active) == desired
        ):
            return CompositionMutation(
                team.id, row.id, "external_reference_recorded", True
            )
        if row is None:
            row = ServiceTeamExternalReference(
                team_id=team.id,
                provider=provider,
                account_scope=account_scope,
                external_id=external_id,
                provenance=provenance,
                observed_at=observed_at,
                is_active=command.is_active,
            )
            db.add(row)
        else:
            row.provenance = provenance
            row.observed_at = observed_at
            row.is_active = command.is_active
        db.flush()
        _audit_event(
            db,
            context=command.context,
            team_id=team.id,
            record_id=row.id,
            operation="external_reference_recorded",
            details={
                "provider": provider,
                "account_scope": account_scope,
                "observed_at": observed_at.isoformat(),
                "is_active": command.is_active,
            },
        )
        return CompositionMutation(
            team.id, row.id, "external_reference_recorded", False
        )

    return execute_owner_command(
        db,
        definition=_RECORD_EXTERNAL_REFERENCE,
        context=command.context,
        operation=apply,
    )


def set_routing_policy(
    db: Session, command: SetTeamRoutingPolicy
) -> CompositionMutation:
    domain = _clean(command.domain, field="domain", maximum=80).casefold()
    route_key = _clean(command.route_key, field="route_key", maximum=120).casefold()
    contract = _routing_policy_contract(domain, route_key)
    if command.priority < 0:
        raise _error(
            "service_team_routing_invalid",
            "Routing priority cannot be negative.",
            priority=command.priority,
        )

    def apply() -> CompositionMutation:
        team = _locked_team(db, command.team_id)
        if command.is_active and not team.is_active:
            raise _error(
                "service_team_inactive",
                "An inactive service team cannot receive an active route.",
                team_id=str(team.id),
            )
        if command.is_active:
            _governed_capability_definition(
                db,
                contract.required_capability,
                lock=True,
            )
            capability = db.scalar(
                select(ServiceTeamCapability)
                .where(
                    ServiceTeamCapability.team_id == team.id,
                    ServiceTeamCapability.capability_key
                    == contract.required_capability.value,
                    ServiceTeamCapability.is_active.is_(True),
                )
                .with_for_update()
            )
            if capability is None:
                raise _error(
                    "service_team_routing_capability_missing",
                    "The selected team lacks the route contract capability.",
                    domain=domain,
                    route_key=route_key,
                    required_capability=contract.required_capability.value,
                )
        scope = None
        if command.scope_binding_id is not None:
            scope = db.scalar(
                select(ServiceTeamScopeBinding)
                .where(
                    ServiceTeamScopeBinding.id == command.scope_binding_id,
                    ServiceTeamScopeBinding.team_id == team.id,
                    ServiceTeamScopeBinding.is_active.is_(True),
                )
                .with_for_update()
            )
            if scope is None:
                raise _error(
                    "service_team_scope_not_found",
                    "The active team scope binding was not found.",
                    scope_binding_id=str(command.scope_binding_id),
                )
        row = db.scalar(
            select(ServiceTeamRoutingPolicy)
            .where(ServiceTeamRoutingPolicy.id == command.policy_id)
            .with_for_update()
        )
        desired_identity = (
            domain,
            route_key,
            team.id,
            command.scope_binding_id,
            command.priority,
        )
        if row is not None:
            current_identity = (
                row.domain,
                row.route_key,
                row.team_id,
                row.scope_binding_id,
                row.priority,
            )
            if current_identity != desired_identity:
                raise _error(
                    "service_team_routing_identity_collision",
                    "The routing-policy identifier is bound to different evidence.",
                    policy_id=str(command.policy_id),
                )
            if row.is_active is command.is_active:
                return CompositionMutation(team.id, row.id, "routing_policy_set", True)
            row.is_active = command.is_active
        else:
            row = ServiceTeamRoutingPolicy(
                id=command.policy_id,
                domain=domain,
                route_key=route_key,
                team_id=team.id,
                scope_binding_id=scope.id if scope is not None else None,
                priority=command.priority,
                is_active=command.is_active,
            )
            db.add(row)
        db.flush()
        _audit_event(
            db,
            context=command.context,
            team_id=team.id,
            record_id=row.id,
            operation="routing_policy_set",
            details={
                "domain": domain,
                "route_key": route_key,
                "route_contract_owner": contract.contract_owner,
                "route_contract_version": contract.contract_version,
                "required_capability": contract.required_capability.value,
                "scope_binding_id": (
                    str(command.scope_binding_id)
                    if command.scope_binding_id is not None
                    else None
                ),
                "priority": command.priority,
                "is_active": command.is_active,
            },
        )
        return CompositionMutation(team.id, row.id, "routing_policy_set", False)

    return execute_owner_command(
        db,
        definition=_SET_ROUTING_POLICY,
        context=command.context,
        operation=apply,
    )


def capabilities_for_team(
    db: Session, team_id: UUID
) -> tuple[ServiceTeamCapabilityKey, ...]:
    return capabilities_by_team(db, (team_id,))[team_id]


def capabilities_by_team(
    db: Session,
    team_ids: tuple[UUID, ...],
) -> Mapping[UUID, tuple[ServiceTeamCapabilityKey, ...]]:
    normalized_team_ids = tuple(sorted(set(team_ids), key=str))
    grouped: dict[UUID, list[ServiceTeamCapabilityKey]] = {
        team_id: [] for team_id in normalized_team_ids
    }
    if not normalized_team_ids:
        return MappingProxyType({})
    rows = db.execute(
        select(
            ServiceTeamCapability.team_id,
            ServiceTeamCapability.capability_key,
        )
        .where(
            ServiceTeamCapability.team_id.in_(normalized_team_ids),
            ServiceTeamCapability.is_active.is_(True),
        )
        .order_by(
            ServiceTeamCapability.team_id.asc(),
            ServiceTeamCapability.capability_key.asc(),
        )
    ).all()
    validated: dict[str, ServiceTeamCapabilityKey] = {}
    for team_id, key in rows:
        capability = validated.get(key)
        if capability is None:
            try:
                capability = ServiceTeamCapabilityKey(key)
            except ValueError as exc:
                raise _error(
                    "service_team_capability_contract_drift",
                    "An active capability binding is absent from the code registry.",
                    capability=key,
                ) from exc
            validated[key] = capability
        if len(grouped[team_id]) == 0 or capability not in grouped[team_id]:
            grouped[team_id].append(capability)
    for capability in validated.values():
        _governed_capability_definition(db, capability)
    return MappingProxyType(
        {team_id: tuple(capabilities) for team_id, capabilities in grouped.items()}
    )


def scope_bindings_for_team(
    db: Session,
    team_id: UUID,
    *,
    active_only: bool = True,
) -> tuple[TeamScopeBinding, ...]:
    return scope_bindings_by_team(
        db,
        (team_id,),
        active_only=active_only,
    )[team_id]


def scope_bindings_by_team(
    db: Session,
    team_ids: tuple[UUID, ...],
    *,
    active_only: bool = True,
) -> Mapping[UUID, tuple[TeamScopeBinding, ...]]:
    normalized_team_ids = tuple(sorted(set(team_ids), key=str))
    if not normalized_team_ids:
        return MappingProxyType({})
    statement = (
        select(ServiceTeamScopeBinding, GeoArea.is_active)
        .outerjoin(GeoArea, GeoArea.id == ServiceTeamScopeBinding.geo_area_id)
        .where(ServiceTeamScopeBinding.team_id.in_(normalized_team_ids))
    )
    if active_only:
        statement = statement.where(ServiceTeamScopeBinding.is_active.is_(True))
    rows = db.execute(
        statement.order_by(
            ServiceTeamScopeBinding.team_id.asc(),
            ServiceTeamScopeBinding.scope_type.asc(),
            ServiceTeamScopeBinding.geo_area_id.asc(),
            ServiceTeamScopeBinding.id.asc(),
        )
    ).all()
    grouped: dict[UUID, list[TeamScopeBinding]] = {
        team_id: [] for team_id in normalized_team_ids
    }
    for row, geo_area_is_active in rows:
        try:
            scope_type = ServiceTeamScopeType(row.scope_type)
        except ValueError as exc:
            raise _error(
                "service_team_scope_contract_drift",
                "An active scope binding has an unknown scope type.",
                scope_type=row.scope_type,
            ) from exc
        if (
            active_only
            and scope_type is ServiceTeamScopeType.geo_area
            and geo_area_is_active is not True
        ):
            raise _error(
                "service_team_scope_geo_area_inactive",
                "An active team scope references an inactive geographic area.",
                scope_binding_id=str(row.id),
                geo_area_id=str(row.geo_area_id),
            )
        grouped[row.team_id].append(
            TeamScopeBinding(
                binding_id=row.id,
                scope_type=scope_type,
                geo_area_id=row.geo_area_id,
                is_active=row.is_active,
            )
        )
    return MappingProxyType(
        {team_id: tuple(bindings) for team_id, bindings in grouped.items()}
    )


def responsibilities_by_membership(
    db: Session,
    membership_ids: tuple[UUID, ...],
    *,
    active_only: bool = True,
) -> Mapping[UUID, tuple[ServiceTeamResponsibilityKey, ...]]:
    normalized_ids = tuple(sorted(set(membership_ids), key=str))
    if not normalized_ids:
        return MappingProxyType({})
    statement = select(ServiceTeamMemberResponsibility).where(
        ServiceTeamMemberResponsibility.membership_id.in_(normalized_ids)
    )
    if active_only:
        statement = statement.where(ServiceTeamMemberResponsibility.is_active.is_(True))
    rows = db.scalars(
        statement.order_by(
            ServiceTeamMemberResponsibility.membership_id.asc(),
            ServiceTeamMemberResponsibility.responsibility_key.asc(),
        )
    ).all()
    grouped: dict[UUID, list[ServiceTeamResponsibilityKey]] = {
        membership_id: [] for membership_id in normalized_ids
    }
    for row in rows:
        try:
            responsibility = ServiceTeamResponsibilityKey(row.responsibility_key)
        except ValueError as exc:
            raise _error(
                "service_team_responsibility_contract_drift",
                "An active membership responsibility is absent from the registry.",
                responsibility=row.responsibility_key,
            ) from exc
        if responsibility not in grouped[row.membership_id]:
            grouped[row.membership_id].append(responsibility)
    return MappingProxyType(
        {
            membership_id: tuple(responsibilities)
            for membership_id, responsibilities in grouped.items()
        }
    )


def relationships_for_team(
    db: Session,
    team_id: UUID,
    *,
    active_only: bool = True,
) -> tuple[TeamRelationshipEdge, ...]:
    statement = select(ServiceTeamRelationship).where(
        or_(
            ServiceTeamRelationship.parent_team_id == team_id,
            ServiceTeamRelationship.child_team_id == team_id,
        )
    )
    if active_only:
        statement = statement.where(ServiceTeamRelationship.is_active.is_(True))
    rows = db.scalars(
        statement.order_by(
            ServiceTeamRelationship.relationship_kind.asc(),
            ServiceTeamRelationship.parent_team_id.asc(),
            ServiceTeamRelationship.child_team_id.asc(),
            ServiceTeamRelationship.id.asc(),
        )
    ).all()
    return tuple(
        TeamRelationshipEdge(
            relationship_id=row.id,
            parent_team_id=row.parent_team_id,
            child_team_id=row.child_team_id,
            kind=ServiceTeamRelationshipKind(row.relationship_kind),
            is_active=row.is_active,
        )
        for row in rows
    )


def external_references_for_team(
    db: Session,
    team_id: UUID,
    *,
    active_only: bool = True,
) -> tuple[TeamExternalReferenceObservation, ...]:
    statement = select(ServiceTeamExternalReference).where(
        ServiceTeamExternalReference.team_id == team_id
    )
    if active_only:
        statement = statement.where(ServiceTeamExternalReference.is_active.is_(True))
    rows = db.scalars(
        statement.order_by(
            ServiceTeamExternalReference.provider.asc(),
            ServiceTeamExternalReference.account_scope.asc(),
            ServiceTeamExternalReference.external_id.asc(),
        )
    ).all()
    return tuple(
        TeamExternalReferenceObservation(
            reference_id=row.id,
            provider=row.provider,
            account_scope=row.account_scope,
            external_id=row.external_id,
            provenance=row.provenance,
            observed_at=row.observed_at,
            is_active=row.is_active,
        )
        for row in rows
    )


def routing_policies_for_team(
    db: Session,
    team_id: UUID,
    *,
    active_only: bool = True,
) -> tuple[TeamRoutingPolicyBinding, ...]:
    statement = select(ServiceTeamRoutingPolicy).where(
        ServiceTeamRoutingPolicy.team_id == team_id
    )
    if active_only:
        statement = statement.where(ServiceTeamRoutingPolicy.is_active.is_(True))
    rows = db.scalars(
        statement.order_by(
            ServiceTeamRoutingPolicy.domain.asc(),
            ServiceTeamRoutingPolicy.route_key.asc(),
            ServiceTeamRoutingPolicy.priority.asc(),
            ServiceTeamRoutingPolicy.id.asc(),
        )
    ).all()
    return tuple(
        TeamRoutingPolicyBinding(
            policy_id=row.id,
            domain=row.domain,
            route_key=row.route_key,
            priority=row.priority,
            scope_binding_id=row.scope_binding_id,
            is_active=row.is_active,
        )
        for row in rows
    )


def team_ids_for_capability(
    db: Session,
    capability: ServiceTeamCapabilityKey,
    *,
    geo_area_id: UUID | None = None,
) -> tuple[UUID, ...]:
    _governed_capability_definition(db, capability)
    statement = (
        select(ServiceTeam.id)
        .join(
            ServiceTeamCapability,
            ServiceTeamCapability.team_id == ServiceTeam.id,
        )
        .join(
            ServiceTeamCapabilityDefinition,
            ServiceTeamCapabilityDefinition.key == ServiceTeamCapability.capability_key,
        )
        .where(
            ServiceTeam.is_active.is_(True),
            ServiceTeamCapability.is_active.is_(True),
            ServiceTeamCapability.capability_key == capability.value,
            ServiceTeamCapabilityDefinition.is_active.is_(True),
        )
    )
    if geo_area_id is not None:
        scopes = aliased(ServiceTeamScopeBinding)
        statement = statement.join(scopes, scopes.team_id == ServiceTeam.id).where(
            scopes.is_active.is_(True),
            or_(
                scopes.scope_type == ServiceTeamScopeType.global_.value,
                (
                    (scopes.scope_type == ServiceTeamScopeType.geo_area.value)
                    & (scopes.geo_area_id == geo_area_id)
                ),
            ),
        )
    return tuple(db.scalars(statement.distinct().order_by(ServiceTeam.id.asc())).all())


def resolve_staff_capability_scope(
    db: Session,
    system_user_id: UUID,
    *,
    capabilities: tuple[ServiceTeamCapabilityKey, ...] = (),
    responsibilities: tuple[ServiceTeamResponsibilityKey, ...] = (),
) -> StaffCapabilityScope:
    """Resolve set-valued membership, capability, and responsibility scope."""

    for capability in capabilities:
        _governed_capability_definition(db, capability)
    user = db.get(SystemUser, system_user_id)
    if user is None or not user.is_active or user.person_party_id is None:
        return StaffCapabilityScope(
            system_user_id=system_user_id,
            person_party_id=(user.person_party_id if user is not None else None),
            identity_available=False,
            team_ids=(),
            capability_team_ids=MappingProxyType({}),
            responsibility_team_ids=MappingProxyType({}),
        )
    person_party_id = user.person_party_id
    active_party = db.scalar(
        select(Party.id).where(
            Party.id == person_party_id,
            Party.party_type == PartyType.person.value,
            Party.status == PartyIdentityStatus.active.value,
        )
    )
    if active_party is None:
        return StaffCapabilityScope(
            system_user_id=system_user_id,
            person_party_id=person_party_id,
            identity_available=False,
            team_ids=(),
            capability_team_ids=MappingProxyType({}),
            responsibility_team_ids=MappingProxyType({}),
        )
    team_ids = tuple(
        db.scalars(
            select(ServiceTeam.id)
            .join(ServiceTeamMember, ServiceTeamMember.team_id == ServiceTeam.id)
            .where(
                ServiceTeam.is_active.is_(True),
                ServiceTeamMember.is_active.is_(True),
                ServiceTeamMember.person_id == person_party_id,
            )
            .distinct()
            .order_by(ServiceTeam.id.asc())
        ).all()
    )
    capability_scope = {
        capability: tuple(
            db.scalars(
                select(ServiceTeam.id)
                .join(
                    ServiceTeamMember,
                    ServiceTeamMember.team_id == ServiceTeam.id,
                )
                .join(
                    ServiceTeamCapability,
                    ServiceTeamCapability.team_id == ServiceTeam.id,
                )
                .join(
                    ServiceTeamCapabilityDefinition,
                    ServiceTeamCapabilityDefinition.key
                    == ServiceTeamCapability.capability_key,
                )
                .where(
                    ServiceTeam.is_active.is_(True),
                    ServiceTeamMember.is_active.is_(True),
                    ServiceTeamMember.person_id == person_party_id,
                    ServiceTeamCapability.is_active.is_(True),
                    ServiceTeamCapability.capability_key == capability.value,
                    ServiceTeamCapabilityDefinition.is_active.is_(True),
                )
                .distinct()
                .order_by(ServiceTeam.id.asc())
            ).all()
        )
        for capability in capabilities
    }
    responsibility_scope = {
        responsibility: tuple(
            db.scalars(
                select(ServiceTeam.id)
                .join(
                    ServiceTeamMember,
                    ServiceTeamMember.team_id == ServiceTeam.id,
                )
                .join(
                    ServiceTeamMemberResponsibility,
                    ServiceTeamMemberResponsibility.membership_id
                    == ServiceTeamMember.id,
                )
                .where(
                    ServiceTeam.is_active.is_(True),
                    ServiceTeamMember.is_active.is_(True),
                    ServiceTeamMember.person_id == person_party_id,
                    ServiceTeamMemberResponsibility.is_active.is_(True),
                    ServiceTeamMemberResponsibility.responsibility_key
                    == responsibility.value,
                )
                .distinct()
                .order_by(ServiceTeam.id.asc())
            ).all()
        )
        for responsibility in responsibilities
    }
    return StaffCapabilityScope(
        system_user_id=system_user_id,
        person_party_id=person_party_id,
        identity_available=True,
        team_ids=team_ids,
        capability_team_ids=MappingProxyType(capability_scope),
        responsibility_team_ids=MappingProxyType(responsibility_scope),
    )


def resolve_routing_team(
    db: Session,
    *,
    domain: str,
    route_key: str,
    geo_area_id: UUID | None = None,
) -> RoutedServiceTeam | None:
    """Resolve one exact policy-selected team, never row-age or name precedence."""

    normalized_domain = domain.strip().casefold()
    normalized_key = route_key.strip().casefold()
    contract = _routing_policy_contract(normalized_domain, normalized_key)
    required_capability = contract.required_capability
    _governed_capability_definition(db, required_capability)
    scope = aliased(ServiceTeamScopeBinding)
    geo_area = aliased(GeoArea)
    statement = (
        select(ServiceTeamRoutingPolicy)
        .join(ServiceTeam, ServiceTeam.id == ServiceTeamRoutingPolicy.team_id)
        .join(
            ServiceTeamCapability,
            ServiceTeamCapability.team_id == ServiceTeam.id,
        )
        .join(
            ServiceTeamCapabilityDefinition,
            ServiceTeamCapabilityDefinition.key == ServiceTeamCapability.capability_key,
        )
        .outerjoin(
            scope,
            scope.id == ServiceTeamRoutingPolicy.scope_binding_id,
        )
        .outerjoin(geo_area, geo_area.id == scope.geo_area_id)
        .where(
            ServiceTeamRoutingPolicy.domain == normalized_domain,
            ServiceTeamRoutingPolicy.route_key == normalized_key,
            ServiceTeamRoutingPolicy.is_active.is_(True),
            ServiceTeam.is_active.is_(True),
            ServiceTeamCapability.capability_key == required_capability.value,
            ServiceTeamCapability.is_active.is_(True),
            ServiceTeamCapabilityDefinition.is_active.is_(True),
        )
    )
    if geo_area_id is None:
        statement = statement.where(
            or_(
                ServiceTeamRoutingPolicy.scope_binding_id.is_(None),
                (
                    (scope.is_active.is_(True))
                    & (scope.scope_type == ServiceTeamScopeType.global_.value)
                ),
            )
        )
    else:
        statement = statement.where(
            or_(
                ServiceTeamRoutingPolicy.scope_binding_id.is_(None),
                (
                    (scope.is_active.is_(True))
                    & (
                        or_(
                            scope.scope_type == ServiceTeamScopeType.global_.value,
                            (
                                (
                                    scope.scope_type
                                    == ServiceTeamScopeType.geo_area.value
                                )
                                & (scope.geo_area_id == geo_area_id)
                                & (geo_area.is_active.is_(True))
                            ),
                        )
                    )
                ),
            )
        )
    candidates = db.scalars(
        statement.order_by(
            ServiceTeamRoutingPolicy.priority.asc(),
            ServiceTeamRoutingPolicy.id.asc(),
        )
    ).all()
    if not candidates:
        return None
    winner_priority = candidates[0].priority
    winners = [row for row in candidates if row.priority == winner_priority]
    if len(winners) != 1:
        raise _error(
            "service_team_routing_ambiguous",
            "Multiple active routing policies have the winning priority.",
            domain=normalized_domain,
            route_key=normalized_key,
            priority=winner_priority,
            policy_ids=tuple(str(row.id) for row in winners),
        )
    winner = winners[0]
    return RoutedServiceTeam(
        policy_id=winner.id,
        team_id=winner.team_id,
        domain=winner.domain,
        route_key=winner.route_key,
        priority=winner.priority,
        scope_binding_id=winner.scope_binding_id,
    )


def inspect_shadow_drift(db: Session) -> CompositionShadowDrift:
    """Five-pointer verification before the legacy scalar columns may contract."""

    missing_type_team_ids: set[UUID] = set()
    for legacy_type, capabilities in LEGACY_TYPE_CAPABILITIES.items():
        for capability in capabilities:
            capability_binding = aliased(ServiceTeamCapability)
            missing_type_team_ids.update(
                db.scalars(
                    select(ServiceTeam.id)
                    .outerjoin(
                        capability_binding,
                        (capability_binding.team_id == ServiceTeam.id)
                        & (capability_binding.capability_key == capability.value)
                        & (capability_binding.is_active.is_(True)),
                    )
                    .where(
                        ServiceTeam.team_type == legacy_type,
                        capability_binding.id.is_(None),
                    )
                ).all()
            )
    unknown_type_team_ids = db.scalars(
        select(ServiceTeam.id).where(
            ServiceTeam.team_type.is_not(None),
            ServiceTeam.team_type.not_in(tuple(LEGACY_TYPE_CAPABILITIES)),
        )
    ).all()
    missing_type_team_ids.update(unknown_type_team_ids)

    missing_role_member_ids: set[UUID] = set()
    for legacy_role, responsibilities in LEGACY_ROLE_RESPONSIBILITIES.items():
        for responsibility in responsibilities:
            responsibility_binding = aliased(ServiceTeamMemberResponsibility)
            missing_role_member_ids.update(
                db.scalars(
                    select(ServiceTeamMember.id)
                    .outerjoin(
                        responsibility_binding,
                        (responsibility_binding.membership_id == ServiceTeamMember.id)
                        & (
                            responsibility_binding.responsibility_key
                            == responsibility.value
                        )
                        & (responsibility_binding.is_active.is_(True)),
                    )
                    .where(
                        ServiceTeamMember.role == legacy_role,
                        responsibility_binding.id.is_(None),
                    )
                ).all()
            )
    unknown_role_member_ids = db.scalars(
        select(ServiceTeamMember.id).where(
            ServiceTeamMember.role.is_not(None),
            ServiceTeamMember.role.not_in(tuple(LEGACY_ROLE_RESPONSIBILITIES)),
        )
    ).all()
    missing_role_member_ids.update(unknown_role_member_ids)

    manager_responsibility = db.scalar(
        select(func.count(ServiceTeam.id))
        .outerjoin(
            ServiceTeamMember,
            (ServiceTeamMember.team_id == ServiceTeam.id)
            & (ServiceTeamMember.person_id == ServiceTeam.manager_person_id)
            & (ServiceTeamMember.is_active.is_(True)),
        )
        .outerjoin(
            ServiceTeamMemberResponsibility,
            (ServiceTeamMemberResponsibility.membership_id == ServiceTeamMember.id)
            & (
                ServiceTeamMemberResponsibility.responsibility_key
                == ServiceTeamResponsibilityKey.accountable_manager.value
            )
            & (ServiceTeamMemberResponsibility.is_active.is_(True)),
        )
        .where(
            ServiceTeam.manager_person_id.is_not(None),
            ServiceTeamMemberResponsibility.id.is_(None),
        )
    )
    region_scope = db.scalar(
        select(func.count(ServiceTeam.id))
        .outerjoin(
            ServiceTeamScopeBinding,
            (ServiceTeamScopeBinding.team_id == ServiceTeam.id)
            & (
                ServiceTeamScopeBinding.scope_type
                == ServiceTeamScopeType.geo_area.value
            )
            & (ServiceTeamScopeBinding.is_active.is_(True)),
        )
        .where(
            ServiceTeam.region.is_not(None),
            ServiceTeamScopeBinding.id.is_(None),
        )
    )
    workforce_reference = db.scalar(
        select(func.count(ServiceTeam.id))
        .outerjoin(
            ServiceTeamExternalReference,
            (ServiceTeamExternalReference.team_id == ServiceTeam.id)
            & (
                ServiceTeamExternalReference.provider
                == func.lower(ServiceTeam.workforce_system)
            )
            & (
                ServiceTeamExternalReference.external_id
                == ServiceTeam.workforce_department_reference
            )
            & (ServiceTeamExternalReference.is_active.is_(True)),
        )
        .where(
            ServiceTeam.workforce_system.is_not(None),
            ServiceTeamExternalReference.id.is_(None),
        )
    )
    return CompositionShadowDrift(
        legacy_type_without_capability=len(missing_type_team_ids),
        legacy_role_without_responsibility=len(missing_role_member_ids),
        legacy_manager_without_responsibility=int(manager_responsibility or 0),
        legacy_region_without_geo_scope=int(region_scope or 0),
        legacy_workforce_without_external_reference=int(workforce_reference or 0),
    )
