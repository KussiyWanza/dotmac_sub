from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.db import get_db
from app.models.service_team import (
    ServiceTeamCapabilityDefinition,
    ServiceTeamCapabilityKey,
)
from app.services import service_team_composition, service_team_lifecycle
from app.services.owner_commands import CommandContext
from app.web.admin import service_teams


def test_service_team_web_exposes_native_capabilities_without_hard_delete():
    routes = {
        (route.path, frozenset(route.methods))
        for route in service_teams.router.routes
        if isinstance(route, APIRoute)
    }

    assert ("/system/service-teams", frozenset({"GET"})) in routes
    assert ("/system/service-teams", frozenset({"POST"})) in routes
    assert ("/system/service-teams/{team_id}", frozenset({"GET"})) in routes
    assert (
        "/system/service-teams/{team_id}/active",
        frozenset({"POST"}),
    ) in routes
    assert (
        "/system/service-teams/{team_id}/members",
        frozenset({"POST"}),
    ) in routes
    assert (
        "/system/service-teams/{team_id}/capabilities",
        frozenset({"POST"}),
    ) in routes
    assert (
        "/system/service-teams/{team_id}/members/{member_id}/responsibilities",
        frozenset({"POST"}),
    ) in routes
    assert (
        "/system/service-teams/{team_id}/scopes",
        frozenset({"POST"}),
    ) in routes
    assert (
        "/system/service-teams/{team_id}/relationships",
        frozenset({"POST"}),
    ) in routes
    assert (
        "/system/service-teams/{team_id}/external-references",
        frozenset({"POST"}),
    ) in routes
    assert (
        "/system/service-teams/{team_id}/routing-policies",
        frozenset({"POST"}),
    ) in routes
    assert not any(path.endswith("/role") for path, _methods in routes)
    assert not any(path.endswith("/delete") for path, _methods in routes)


def test_service_team_forms_are_csrf_protected_and_keep_identity_evidence():
    template_root = Path("templates/admin/system/service_teams")
    form = (template_root / "form.html").read_text(encoding="utf-8")
    detail = (template_root / "detail.html").read_text(encoding="utf-8")
    index = (template_root / "index.html").read_text(encoding="utf-8")

    assert "components/forms/csrf_input.html" in form
    assert 'name="request_id"' in form
    assert 'name="expected_updated_at"' in form
    assert detail.count("components/forms/csrf_input.html") >= 4
    assert 'name="reason"' in detail
    assert "/delete" not in detail
    assert "detail.actions.can_add_member" in detail
    assert "detail.actions.can_edit" in detail
    assert "detail.actions.can_activate or detail.actions.can_deactivate" in detail
    assert "detail.actions.lifecycle_block_reason" in detail
    assert "can(request, service_team_permissions.membership)" in detail
    assert "can(request, service_team_permissions.retire)" in detail
    assert 'name="responsibility"' in detail
    assert 'name="capability"' in detail
    assert 'name="scope_type"' in detail
    assert 'name="geo_area_id"' in detail
    assert 'name="child_team_id"' in detail
    assert 'name="kind"' in detail
    assert 'name="provider"' in detail
    assert 'name="account_scope"' in detail
    assert 'name="external_id"' in detail
    assert 'name="provenance"' in detail
    assert 'name="route"' in detail
    assert 'name="priority"' in detail
    assert 'name="scope_binding_id"' in detail
    assert detail.count("components/forms/csrf_input.html") >= 8
    # Routing routes are select-only options sourced from registered contracts.
    assert "routing_policy_contracts" in detail
    assert 'name="role"' not in detail
    assert 'name="team_type"' not in form
    assert 'name="region"' not in form
    assert 'name="manager_system_user_id"' not in form
    assert "can(request, service_team_permissions.create)" in index
    assert "Page {{ page }} of {{ total_pages }}" in index
    assert "result.search|urlencode" in index
    assert "Capabilities" in index
    assert "Accountable managers" in index
    assert "role_region_groups" not in index
    settings_hub = Path("templates/admin/system/settings_hub.html").read_text(
        encoding="utf-8"
    )
    assert 'link.get("permission")' in settings_hub
    assert "can(request, link.permission)" in settings_hub
    assert "cat.links | map" not in settings_hub


# ── HTTP adapter seam for the composition owner commands ─────────────────────


@pytest.fixture(autouse=True)
def _plain_reads_after_commit(db_session):
    # Owner commands require a transaction-free session at entry; keep fixture
    # attributes readable across the commits the adapters perform.
    db_session.expire_on_commit = False


def _client(db_session) -> TestClient:
    app = FastAPI()
    app.include_router(service_teams.router)
    app.dependency_overrides[get_db] = lambda: db_session
    auth = {"principal_id": str(uuid4()), "principal_type": "user"}
    # Permission guards are per-route closures created at import time, so they
    # are overridden by identity rather than by key.
    for route in service_teams.router.routes:
        for dependency in route.dependant.dependencies:
            if dependency.call is not None and dependency.call is not get_db:
                app.dependency_overrides[dependency.call] = lambda: auth
    return TestClient(app, raise_server_exceptions=False)


def _post(client: TestClient, path: str, data: dict):
    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
    ):
        return client.post(path, data=data, follow_redirects=False)


def _create_team(db_session, name: str) -> UUID:
    team_id = uuid4()
    command_id = uuid4()
    db_session.commit()
    service_team_lifecycle.create_team(
        db_session,
        service_team_lifecycle.CreateServiceTeam(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"user:{uuid4()}",
                scope="operations:service_team:create",
                reason="web adapter test",
                idempotency_key=f"create:{command_id}",
            ),
            team_id=team_id,
            name=name,
        ),
    )
    return team_id


def _register_capability(db_session, capability: ServiceTeamCapabilityKey) -> None:
    contract = service_team_composition.CAPABILITY_CONTRACTS[capability]
    db_session.add(
        ServiceTeamCapabilityDefinition(
            key=contract.key.value,
            display_name=contract.display_name,
            contract_owner=contract.contract_owner,
            contract_version=contract.contract_version,
            description=f"Web adapter test definition for {contract.key.value}",
            is_active=True,
        )
    )
    db_session.commit()


def test_scope_route_records_global_scope_and_redirects(db_session):
    team_id = _create_team(db_session, f"Scope Web {uuid4()}")
    response = _post(
        _client(db_session),
        f"/system/service-teams/{team_id}/scopes",
        {
            "request_id": str(uuid4()),
            "scope_type": "global",
            "geo_area_id": "",
            "is_active": "true",
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/system/service-teams/{team_id}"
    bindings = service_team_composition.scope_bindings_for_team(db_session, team_id)
    assert [
        (binding.scope_type.value, binding.geo_area_id, binding.is_active)
        for binding in bindings
    ] == [("global", None, True)]


def test_scope_route_rerenders_detail_with_domain_error(db_session):
    team_id = _create_team(db_session, f"Scope Error Web {uuid4()}")
    response = _post(
        _client(db_session),
        f"/system/service-teams/{team_id}/scopes",
        {
            "request_id": str(uuid4()),
            "scope_type": "geo_area",
            "geo_area_id": "",
            "is_active": "true",
        },
    )

    assert response.status_code == 400
    assert "Choose either global scope or one GeoArea." in response.text
    assert service_team_composition.scope_bindings_for_team(db_session, team_id) == ()


def test_relationship_route_records_edge_with_path_team_as_parent(db_session):
    parent_id = _create_team(db_session, f"Parent Web {uuid4()}")
    child_id = _create_team(db_session, f"Child Web {uuid4()}")
    response = _post(
        _client(db_session),
        f"/system/service-teams/{parent_id}/relationships",
        {
            "request_id": str(uuid4()),
            "child_team_id": str(child_id),
            "kind": "escalation",
            "is_active": "true",
        },
    )

    assert response.status_code == 303
    edges = service_team_composition.relationships_for_team(db_session, parent_id)
    assert [
        (edge.parent_team_id, edge.child_team_id, edge.kind.value, edge.is_active)
        for edge in edges
    ] == [(parent_id, child_id, "escalation", True)]


def test_external_reference_route_records_observation(db_session):
    team_id = _create_team(db_session, f"Reference Web {uuid4()}")
    response = _post(
        _client(db_session),
        f"/system/service-teams/{team_id}/external-references",
        {
            "request_id": str(uuid4()),
            "provider": "Slack",
            "account_scope": "dotmac.slack.com",
            "external_id": "C0AGENTS",
            "provenance": "web adapter test observation",
            "is_active": "true",
        },
    )

    assert response.status_code == 303
    references = service_team_composition.external_references_for_team(
        db_session, team_id
    )
    assert [
        (reference.provider, reference.account_scope, reference.external_id)
        for reference in references
    ] == [("slack", "dotmac.slack.com", "C0AGENTS")]


def test_routing_policy_route_records_registered_route(db_session):
    _register_capability(db_session, ServiceTeamCapabilityKey.outage_response)
    team_id = _create_team(db_session, f"Routing Web {uuid4()}")
    client = _client(db_session)
    capability_response = _post(
        client,
        f"/system/service-teams/{team_id}/capabilities",
        {
            "request_id": str(uuid4()),
            "capability": "outage_response",
            "is_active": "true",
        },
    )
    assert capability_response.status_code == 303

    policy_id = uuid4()
    response = _post(
        client,
        f"/system/service-teams/{team_id}/routing-policies",
        {
            "request_id": str(uuid4()),
            "policy_id": str(policy_id),
            "route": "network.outage|incident.primary",
            "priority": "10",
            "scope_binding_id": "",
            "is_active": "true",
        },
    )

    assert response.status_code == 303
    policies = service_team_composition.routing_policies_for_team(db_session, team_id)
    assert [
        (policy.policy_id, policy.domain, policy.route_key, policy.priority)
        for policy in policies
    ] == [(policy_id, "network.outage", "incident.primary", 10)]


def test_routing_policy_route_rejects_unregistered_route(db_session):
    team_id = _create_team(db_session, f"Routing Error Web {uuid4()}")
    response = _post(
        _client(db_session),
        f"/system/service-teams/{team_id}/routing-policies",
        {
            "request_id": str(uuid4()),
            "policy_id": str(uuid4()),
            "route": "network.outage|not.registered",
            "priority": "10",
            "scope_binding_id": "",
            "is_active": "true",
        },
    )

    assert response.status_code == 400
    assert "no registered routing contract" in response.text
    assert service_team_composition.routing_policies_for_team(db_session, team_id) == ()
