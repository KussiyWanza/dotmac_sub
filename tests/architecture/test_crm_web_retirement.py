"""Guard the complete route-level Dotmac CRM retirement control."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.architecture import crm_web_retirement

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_crm_web_retirement_ledger_is_complete_and_valid() -> None:
    ledger = crm_web_retirement.load_ledger()

    assert crm_web_retirement.ledger_validation_errors(ledger) == ()
    assert ledger["source"]["module_count"] == 73
    assert ledger["source"]["route_count"] == 813
    assert ledger["source"]["method_counts"] == {
        "DELETE": 3,
        "GET": 430,
        "POST": 380,
    }
    assert ledger["schema_version"] == 2
    assert ledger["zero_traffic_evidence_contract"] == (
        crm_web_retirement.ZERO_TRAFFIC_EVIDENCE_CONTRACT
    )
    assert ledger["target"] == {
        "merged_pull_requests_reviewed": list(
            crm_web_retirement.REVIEWED_SUB_PULL_REQUESTS
        ),
        "repository": "dotmac_sub",
        "revision": crm_web_retirement.DEFAULT_SUB_REVISION,
    }


def test_every_crm_web_module_and_route_has_migration_tracking() -> None:
    ledger = crm_web_retirement.load_ledger()
    defaults = ledger["tracking_defaults"]

    assert defaults["module"]["assessment_state"] == "inventory_only"
    assert defaults["route"]["assessment_state"] == "inventory_only"
    assert all("tracking" in module for module in ledger["modules"])
    assert all("tracking" in route for route in ledger["routes"])
    assert all(route["source"]["mounted"] for route in ledger["routes"])


def test_marketing_sales_owner_map_exists_without_verifying_campaigns() -> None:
    owner_map = PROJECT_ROOT / "docs" / "designs" / "MARKETING_SALES_SOT.md"
    text = owner_map.read_text(encoding="utf-8")

    assert "SALES_TO_SERVICE_LIFECYCLE_SOT.md" in text
    assert "The reusable sales owner stops at an **accepted Quote**" in text
    assert "It does not import `dotmac-orders`" in text
    assert "Campaigns, campaign steps" in text
    assert "**Unverified**" in text
    assert "Retention engagement history" in text
    assert "**Unresolved**" in text

    ledger = crm_web_retirement.load_ledger()
    defaults = ledger["tracking_defaults"]["module"]
    campaigns = next(
        module
        for module in ledger["modules"]
        if module["file"] == "app/web/admin/campaigns.py"
    )
    tracking = crm_web_retirement._deep_merge(defaults, campaigns["tracking"])

    assert tracking["decision"]["state"] != "verified"
    assert "not verified" in tracking["decision"]["notes"]


def test_evidence_free_route_cannot_be_declared_retired() -> None:
    ledger = deepcopy(crm_web_retirement.load_ledger())
    route = ledger["routes"][0]
    route["tracking"] = {"assessment_state": "retired"}

    errors = crm_web_retirement.ledger_validation_errors(ledger)

    assert any("needs production usage evidence" in error for error in errors)
    assert any("needs a replacement disposition" in error for error in errors)
    assert any("must be verified before retirement" in error for error in errors)


def test_evidence_free_helper_module_cannot_be_declared_retired() -> None:
    ledger = deepcopy(crm_web_retirement.load_ledger())
    helper = next(module for module in ledger["modules"] if not module["route_count"])
    helper["tracking"] = {"assessment_state": "retired"}

    errors = crm_web_retirement.ledger_validation_errors(ledger)

    assert any(
        f"module {helper['file']} needs a verified owner decision" in error
        for error in errors
    )
    assert any(
        f"module {helper['file']}.retirement" in error
        and "must be verified before retirement" in error
        for error in errors
    )


def _zero_traffic_record(*, days: int = 30) -> dict[str, object]:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        "window_started_at": started_at.isoformat(),
        "window_ended_at": (started_at + timedelta(days=days)).isoformat(),
        "loki_query": "reviewed LogQL query",
        "loki_request_count": 0,
        "victoriametrics_query": "reviewed PromQL query",
        "victoriametrics_request_count": 0,
        "telemetry_health_evidence": ["ops/telemetry-health-record"],
        "operator_record": "ops/change-record",
    }


def test_verified_zero_traffic_gate_requires_two_healthy_observation_sources() -> None:
    ledger = deepcopy(crm_web_retirement.load_ledger())
    route = ledger["routes"][0]
    route["tracking"] = {
        "retirement": {
            "zero_traffic": {
                "evidence": [
                    {
                        **_zero_traffic_record(),
                        "telemetry_health_evidence": [],
                    }
                ],
                "state": "verified",
            }
        }
    }

    errors = crm_web_retirement.ledger_validation_errors(ledger)

    assert any("telemetry_health_evidence" in error for error in errors)
    assert any(
        "needs a compliant zero-traffic observation record" in error for error in errors
    )


def test_verified_zero_traffic_gate_rejects_short_observation_window() -> None:
    ledger = deepcopy(crm_web_retirement.load_ledger())
    route = ledger["routes"][0]
    route["tracking"] = {
        "retirement": {
            "zero_traffic": {
                "evidence": [_zero_traffic_record(days=29)],
                "state": "verified",
            }
        }
    }

    errors = crm_web_retirement.ledger_validation_errors(ledger)

    assert any("must cover at least 30 days" in error for error in errors)


def test_verified_zero_traffic_gate_accepts_complete_observation_record() -> None:
    ledger = deepcopy(crm_web_retirement.load_ledger())
    route = ledger["routes"][0]
    route["tracking"] = {
        "retirement": {
            "zero_traffic": {
                "evidence": [_zero_traffic_record()],
                "state": "verified",
            }
        }
    }

    errors = crm_web_retirement.ledger_validation_errors(ledger)

    assert not any(".retirement.zero_traffic" in error for error in errors)


def test_module_cannot_retire_before_all_of_its_routes() -> None:
    ledger = deepcopy(crm_web_retirement.load_ledger())
    module = next(module for module in ledger["modules"] if module["route_count"])
    module["tracking"] = {
        "assessment_state": "retired",
        "decision": {
            "notes": "Reviewed for this guard test.",
            "owner_service": "test.owner",
            "state": "verified",
        },
    }

    errors = crm_web_retirement.ledger_validation_errors(ledger)

    assert f"module {module['file']} still has routes that are not retired" in errors


def test_reviewed_target_slices_keep_exact_owner_state() -> None:
    ledger = crm_web_retirement.load_ledger()
    defaults = ledger["tracking_defaults"]["module"]
    modules = {
        module["file"]: crm_web_retirement._deep_merge(
            defaults,
            module["tracking"],
        )
        for module in ledger["modules"]
    }

    service_teams = modules["app/web/admin/service_teams.py"]
    assert service_teams["assessment_state"] == "cutover_ready"
    assert (
        service_teams["decision"]["owner_service"]
        == "operations.service_team_lifecycle"
    )
    assert service_teams["decision"]["state"] == "verified"
    assert (
        service_teams["target_slice"]
        == "service-team-production-cutover-and-crm-retirement"
    )

    workqueue = modules["app/web/agent/workqueue.py"]
    assert workqueue["assessment_state"] == "cutover_ready"
    assert workqueue["decision"]["owner_service"] == "operations.agent_workqueue"
    assert workqueue["decision"]["state"] == "verified"
    assert (
        workqueue["target_slice"]
        == "agent-workqueue-production-cutover-and-crm-retirement"
    )

    projects = modules["app/web/admin/projects.py"]
    assert projects["assessment_state"] == "implementation_in_progress"
    assert projects["decision"]["owner_service"] == "operations.project_lifecycle"
    assert projects["decision"]["state"] == "verified"


def test_service_team_routes_record_native_replacements_without_retirement() -> None:
    ledger = crm_web_retirement.load_ledger()
    defaults = ledger["tracking_defaults"]["route"]
    routes = {
        route["source"]["handler"]: crm_web_retirement._deep_merge(
            defaults,
            route["tracking"],
        )
        for route in ledger["routes"]
        if route["source"]["file"] == "app/web/admin/service_teams.py"
    }

    assert set(routes) == set(crm_web_retirement.SERVICE_TEAM_ROUTE_REPLACEMENTS)
    assert {route["assessment_state"] for route in routes.values()} == {"cutover_ready"}
    assert {route["replacement"]["owner_service"] for route in routes.values()} == {
        "operations.service_team_lifecycle"
    }
    assert routes["service_team_delete"]["replacement"]["kind"] == "explicit_removal"
    assert all(
        route["migration"]["callers"]["state"] == "verified"
        and route["migration"]["data"]["state"] == "in_progress"
        and route["migration"]["cutover"]["state"] == "in_progress"
        and "alembic/versions/426_service_team_lifecycle.py"
        in route["migration"]["data"]["evidence"]
        and "tests/playwright/e2e/test_service_teams.py"
        in route["parity"]["behavior"]["evidence"]
        and "tests/playwright/e2e/test_service_teams.py"
        in route["parity"]["permissions"]["evidence"]
        and route["retirement"]["zero_traffic"]["state"] == "unassessed"
        and route["retirement"]["crm_route_deleted"]["state"] == "unassessed"
        for route in routes.values()
    )


def test_workqueue_routes_record_native_replacements_without_retirement() -> None:
    ledger = crm_web_retirement.load_ledger()
    defaults = ledger["tracking_defaults"]["route"]
    routes = {
        route["source"]["handler"]: crm_web_retirement._deep_merge(
            defaults,
            route["tracking"],
        )
        for route in ledger["routes"]
        if route["source"]["file"] == "app/web/agent/workqueue.py"
    }

    assert set(routes) == set(crm_web_retirement.WORKQUEUE_ROUTE_REPLACEMENTS)
    assert {route["assessment_state"] for route in routes.values()} == {"cutover_ready"}
    assert {route["replacement"]["owner_service"] for route in routes.values()} == {
        "operations.agent_workqueue"
    }
    assert all(
        route["migration"]["callers"]["state"] == "verified"
        and route["migration"]["data"]["state"] == "in_progress"
        and route["migration"]["shadow_verification"]["state"] == "in_progress"
        and route["migration"]["cutover"]["state"] == "in_progress"
        and route["retirement"]["crm_route_deleted"]["state"] == "in_progress"
        for route in routes.values()
    )
    for handler in crm_web_retirement.WORKQUEUE_READ_HANDLERS:
        assert routes[handler]["parity"]["audit"]["state"] == "not_applicable"
        assert routes[handler]["parity"]["events"]["state"] == "not_applicable"
        assert routes[handler]["parity"]["idempotency"]["state"] == "not_applicable"
    for handler in (
        set(crm_web_retirement.WORKQUEUE_ROUTE_REPLACEMENTS)
        - crm_web_retirement.WORKQUEUE_READ_HANDLERS
    ):
        assert routes[handler]["parity"]["audit"]["state"] == "verified"
        assert routes[handler]["parity"]["events"]["state"] == "verified"
        assert routes[handler]["parity"]["idempotency"]["state"] == "verified"


def test_reviewed_inbox_modules_are_not_mistaken_for_retired() -> None:
    ledger = crm_web_retirement.load_ledger()
    defaults = ledger["tracking_defaults"]["module"]
    modules = {
        module["file"]: crm_web_retirement._deep_merge(
            defaults,
            module["tracking"],
        )
        for module in ledger["modules"]
        if module["file"] in crm_web_retirement.TEAM_INBOX_MODULES
    }

    assert set(modules) == crm_web_retirement.TEAM_INBOX_MODULES
    assert {module["assessment_state"] for module in modules.values()} == {
        "implementation_in_progress"
    }
    assert {module["decision"]["state"] for module in modules.values()} == {
        "in_progress"
    }
    assert {module["target_slice"] for module in modules.values()} == {
        "team-inbox-history-channel-and-crm-cutover"
    }
