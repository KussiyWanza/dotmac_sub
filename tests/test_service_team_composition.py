from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.gis import GeoArea, GeoAreaType
from app.models.party import (
    Party,
    PartyDataClassification,
    PartyIdentityStatus,
    PartyType,
)
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamCapabilityDefinition,
    ServiceTeamCapabilityKey,
    ServiceTeamMember,
    ServiceTeamRelationshipKind,
    ServiceTeamResponsibilityKey,
    ServiceTeamScopeType,
)
from app.models.system_user import SystemUser
from app.services import service_team_composition
from app.services.owner_commands import CommandContext


@pytest.fixture(autouse=True)
def _plain_reads_after_commit(db_session):
    # Owner commands require a transaction-free session at entry. Keep fixture
    # attributes loaded across commits (as app adapters do via fresh_reads) so
    # reading `team.id` between commands does not autobegin a transaction.
    db_session.expire_on_commit = False


def _context(operation: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{uuid4()}",
        scope=f"operations.service_team_composition:{operation}",
        reason=f"test {operation}",
        idempotency_key=f"{operation}:{command_id}",
    )


def _seed_capability_vocabulary(db_session) -> None:
    for contract in service_team_composition.CAPABILITY_CONTRACTS.values():
        db_session.add(
            ServiceTeamCapabilityDefinition(
                key=contract.key.value,
                display_name=contract.display_name,
                contract_owner=contract.contract_owner,
                contract_version=contract.contract_version,
                description=f"Test definition for {contract.key.value}",
                is_active=True,
            )
        )
    db_session.commit()


def _team_and_member(db_session):
    person = Party(
        party_type=PartyType.person.value,
        display_name="Composable Operator",
        status=PartyIdentityStatus.active.value,
        data_classification=PartyDataClassification.test.value,
    )
    db_session.add(person)
    db_session.flush()
    user = SystemUser(
        first_name="Composable",
        last_name="Operator",
        display_name="Composable Operator",
        email=f"{uuid4()}@example.com",
        is_active=True,
        person_party_id=person.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="composition-test",
        party_binding_reason="Reviewed fixture identity",
    )
    team = ServiceTeam(name=f"Composable {uuid4()}", is_active=True)
    db_session.add_all([user, team])
    db_session.flush()
    member = ServiceTeamMember(
        team_id=team.id,
        person_id=person.id,
        role=None,
        is_active=True,
    )
    db_session.add(member)
    db_session.commit()
    return team, user, member


def test_team_can_hold_many_capabilities_and_member_responsibilities(db_session):
    _seed_capability_vocabulary(db_session)
    team, user, member = _team_and_member(db_session)

    for capability in (
        ServiceTeamCapabilityKey.customer_support,
        ServiceTeamCapabilityKey.field_service,
        ServiceTeamCapabilityKey.outage_response,
    ):
        service_team_composition.set_capability(
            db_session,
            service_team_composition.SetTeamCapability(
                context=_context(f"capability-{capability.value}"),
                team_id=team.id,
                capability=capability,
                is_active=True,
            ),
        )
    for responsibility in (
        ServiceTeamResponsibilityKey.agent,
        ServiceTeamResponsibilityKey.dispatcher,
        ServiceTeamResponsibilityKey.on_call,
    ):
        service_team_composition.set_responsibility(
            db_session,
            service_team_composition.SetMemberResponsibility(
                context=_context(f"responsibility-{responsibility.value}"),
                team_id=team.id,
                membership_id=member.id,
                responsibility=responsibility,
                is_active=True,
            ),
        )

    scope = service_team_composition.resolve_staff_capability_scope(
        db_session,
        user.id,
        capabilities=(
            ServiceTeamCapabilityKey.customer_support,
            ServiceTeamCapabilityKey.field_service,
        ),
        responsibilities=(
            ServiceTeamResponsibilityKey.agent,
            ServiceTeamResponsibilityKey.dispatcher,
        ),
    )

    assert set(service_team_composition.capabilities_for_team(db_session, team.id)) == {
        ServiceTeamCapabilityKey.customer_support,
        ServiceTeamCapabilityKey.field_service,
        ServiceTeamCapabilityKey.outage_response,
    }
    assert scope.team_ids == (team.id,)
    assert scope.capability_team_ids[ServiceTeamCapabilityKey.customer_support] == (
        team.id,
    )
    assert scope.capability_team_ids[ServiceTeamCapabilityKey.field_service] == (
        team.id,
    )
    assert scope.responsibility_team_ids[ServiceTeamResponsibilityKey.dispatcher] == (
        team.id,
    )


def test_composition_owns_no_outbound_delivery_decision():
    # Delivery is a transport concern: domain callers declare the notification
    # activity explicitly, and capability never derives sender behavior.
    assert not hasattr(service_team_composition, "outbound_activity_for_team")


def test_routing_policy_selects_exact_capability_eligible_team(db_session):
    _seed_capability_vocabulary(db_session)
    team, _user, _member = _team_and_member(db_session)
    service_team_composition.set_capability(
        db_session,
        service_team_composition.SetTeamCapability(
            context=_context("outage-capability"),
            team_id=team.id,
            capability=ServiceTeamCapabilityKey.outage_response,
            is_active=True,
        ),
    )
    policy_id = uuid4()
    service_team_composition.set_routing_policy(
        db_session,
        service_team_composition.SetTeamRoutingPolicy(
            context=_context("outage-route"),
            policy_id=policy_id,
            domain="network.outage",
            route_key="incident.primary",
            team_id=team.id,
            priority=10,
            scope_binding_id=None,
            is_active=True,
        ),
    )

    routed = service_team_composition.resolve_routing_team(
        db_session,
        domain="network.outage",
        route_key="incident.primary",
    )

    assert routed is not None
    assert routed.policy_id == policy_id
    assert routed.team_id == team.id

    # Release the read transaction before entering the next public owner command.
    db_session.commit()
    disabled = service_team_composition.set_routing_policy(
        db_session,
        service_team_composition.SetTeamRoutingPolicy(
            context=_context("outage-route-disable"),
            policy_id=policy_id,
            domain="network.outage",
            route_key="incident.primary",
            team_id=team.id,
            priority=10,
            scope_binding_id=None,
            is_active=False,
        ),
    )
    assert disabled.replayed is False
    assert (
        service_team_composition.resolve_routing_team(
            db_session,
            domain="network.outage",
            route_key="incident.primary",
        )
        is None
    )


def test_routing_policy_requires_registered_domain_contract_and_team_capability(
    db_session,
):
    _seed_capability_vocabulary(db_session)
    team, _user, _member = _team_and_member(db_session)

    with pytest.raises(service_team_composition.ServiceTeamCompositionError) as error:
        service_team_composition.set_routing_policy(
            db_session,
            service_team_composition.SetTeamRoutingPolicy(
                context=_context("unregistered-route"),
                policy_id=uuid4(),
                domain="arbitrary.domain",
                route_key="guess-a-team",
                team_id=team.id,
                priority=10,
                scope_binding_id=None,
                is_active=True,
            ),
        )
    assert error.value.code == "service_team_routing_unregistered"

    with pytest.raises(service_team_composition.ServiceTeamCompositionError) as error:
        service_team_composition.set_routing_policy(
            db_session,
            service_team_composition.SetTeamRoutingPolicy(
                context=_context("ineligible-route"),
                policy_id=uuid4(),
                domain="network.outage",
                route_key="incident.primary",
                team_id=team.id,
                priority=10,
                scope_binding_id=None,
                is_active=True,
            ),
        )
    assert error.value.code == "service_team_routing_capability_missing"


def test_unregistered_capability_fails_closed(db_session):
    team, _user, _member = _team_and_member(db_session)

    with pytest.raises(service_team_composition.ServiceTeamCompositionError) as error:
        service_team_composition.set_capability(
            db_session,
            service_team_composition.SetTeamCapability(
                context=_context("unregistered"),
                team_id=team.id,
                capability=ServiceTeamCapabilityKey.customer_support,
                is_active=True,
            ),
        )

    assert error.value.code == "service_team_capability_unregistered"


def test_scope_relationship_and_external_observations_are_independent_facts(
    db_session,
):
    _seed_capability_vocabulary(db_session)
    team, _user, _member = _team_and_member(db_session)
    child = ServiceTeam(name=f"Composable child {uuid4()}", is_active=True)
    area = GeoArea(
        name=f"Reviewed service area {uuid4()}",
        area_type=GeoAreaType.service_area,
        is_active=True,
    )
    second_area = GeoArea(
        name=f"Second reviewed service area {uuid4()}",
        area_type=GeoAreaType.service_area,
        is_active=True,
    )
    db_session.add_all([child, area, second_area])
    db_session.commit()

    scope = service_team_composition.set_scope(
        db_session,
        service_team_composition.SetTeamScope(
            context=_context("geo-scope"),
            team_id=team.id,
            scope_type=ServiceTeamScopeType.geo_area,
            geo_area_id=area.id,
            is_active=True,
        ),
    )
    second_scope = service_team_composition.set_scope(
        db_session,
        service_team_composition.SetTeamScope(
            context=_context("second-geo-scope"),
            team_id=team.id,
            scope_type=ServiceTeamScopeType.geo_area,
            geo_area_id=second_area.id,
            is_active=True,
        ),
    )
    relationship = service_team_composition.set_relationship(
        db_session,
        service_team_composition.SetTeamRelationship(
            context=_context("relationship"),
            parent_team_id=team.id,
            child_team_id=child.id,
            kind=ServiceTeamRelationshipKind.parent_child,
            is_active=True,
        ),
    )
    external = service_team_composition.record_external_reference(
        db_session,
        service_team_composition.RecordTeamExternalReference(
            context=_context("external-observation"),
            team_id=team.id,
            provider="Workforce Provider",
            account_scope="Lagos account",
            external_id="department-42",
            provenance="reviewed provider import",
            observed_at=datetime.now(UTC),
        ),
    )
    second_external = service_team_composition.record_external_reference(
        db_session,
        service_team_composition.RecordTeamExternalReference(
            context=_context("second-external-observation"),
            team_id=team.id,
            provider="Dispatch Provider",
            account_scope="Nigeria operations",
            external_id="queue-18",
            provenance="reviewed dispatch import",
            observed_at=datetime.now(UTC),
        ),
    )

    assert scope.team_id == team.id
    assert relationship.team_id == team.id
    assert external.team_id == team.id
    assert (
        len(
            {
                scope.record_id,
                second_scope.record_id,
                relationship.record_id,
                external.record_id,
                second_external.record_id,
            }
        )
        == 5
    )
    scope_bindings = service_team_composition.scope_bindings_for_team(
        db_session, team.id
    )
    assert {binding.binding_id for binding in scope_bindings} == {
        scope.record_id,
        second_scope.record_id,
    }
    assert {binding.geo_area_id for binding in scope_bindings} == {
        area.id,
        second_area.id,
    }
    assert (
        service_team_composition.relationships_for_team(db_session, child.id)[
            0
        ].parent_team_id
        == team.id
    )
    observations = service_team_composition.external_references_for_team(
        db_session, team.id
    )
    assert len(observations) == 2
    assert {(item.provider, item.external_id) for item in observations} == {
        ("dispatch provider", "queue-18"),
        ("workforce provider", "department-42"),
    }

    # Release the read transaction before entering the next public owner command.
    db_session.commit()
    with pytest.raises(service_team_composition.ServiceTeamCompositionError) as error:
        service_team_composition.set_relationship(
            db_session,
            service_team_composition.SetTeamRelationship(
                context=_context("relationship-cycle"),
                parent_team_id=child.id,
                child_team_id=team.id,
                kind=ServiceTeamRelationshipKind.parent_child,
                is_active=True,
            ),
        )
    assert error.value.code == "service_team_relationship_cycle"

    second_area.is_active = False
    db_session.commit()
    with pytest.raises(service_team_composition.ServiceTeamCompositionError) as error:
        service_team_composition.scope_bindings_for_team(db_session, team.id)
    assert error.value.code == "service_team_scope_geo_area_inactive"


def test_equal_priority_scoped_and_unscoped_routes_resolve_as_ambiguous(db_session):
    """A genuine winning-priority tie is refused, never broken by row age.

    The partial-unique index only forbids two active *unscoped* policies at the
    same (domain, route_key, priority). An unscoped policy and a global-scoped
    one may legally coexist at the same priority, and both match a resolution
    without a geo area — so the resolver must refuse to pick one.
    """
    _seed_capability_vocabulary(db_session)
    unscoped_team, _user_a, _member_a = _team_and_member(db_session)
    scoped_team, _user_b, _member_b = _team_and_member(db_session)

    for index, team in enumerate((unscoped_team, scoped_team)):
        service_team_composition.set_capability(
            db_session,
            service_team_composition.SetTeamCapability(
                context=_context(f"tie-capability-{index}"),
                team_id=team.id,
                capability=ServiceTeamCapabilityKey.outage_response,
                is_active=True,
            ),
        )
    global_scope = service_team_composition.set_scope(
        db_session,
        service_team_composition.SetTeamScope(
            context=_context("tie-global-scope"),
            team_id=scoped_team.id,
            scope_type=ServiceTeamScopeType.global_,
            geo_area_id=None,
            is_active=True,
        ),
    )
    service_team_composition.set_routing_policy(
        db_session,
        service_team_composition.SetTeamRoutingPolicy(
            context=_context("tie-unscoped-route"),
            policy_id=uuid4(),
            domain="network.outage",
            route_key="incident.primary",
            team_id=unscoped_team.id,
            priority=10,
            scope_binding_id=None,
            is_active=True,
        ),
    )
    service_team_composition.set_routing_policy(
        db_session,
        service_team_composition.SetTeamRoutingPolicy(
            context=_context("tie-scoped-route"),
            policy_id=uuid4(),
            domain="network.outage",
            route_key="incident.primary",
            team_id=scoped_team.id,
            priority=10,
            scope_binding_id=global_scope.record_id,
            is_active=True,
        ),
    )

    with pytest.raises(service_team_composition.ServiceTeamCompositionError) as error:
        service_team_composition.resolve_routing_team(
            db_session,
            domain="network.outage",
            route_key="incident.primary",
        )
    assert error.value.code == "service_team_routing_ambiguous"
    assert error.value.details["priority"] == 10
    assert len(error.value.details["policy_ids"]) == 2
