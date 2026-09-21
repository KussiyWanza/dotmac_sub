from __future__ import annotations

from pathlib import Path

from app.services import automation_actions
from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_execution_coordinator_is_fully_contracted() -> None:
    service = service_relationship("automation.execution")
    assert service.module == "app.services.automation_runtime"
    assert service.is_contracted
    assert service.depends_on == (
        "automation.capability_registry",
        "automation.rule_definitions",
        "events.store",
    )


def test_runtime_adapter_registry_is_closed_and_currently_inert() -> None:
    source = _source("app/services/automation_actions.py")
    assert "MappingProxyType" in source
    assert automation_actions.runtime_registry_errors() == ()


def test_runtime_handler_is_registered_with_explicit_event_scope() -> None:
    dispatcher = _source("app/services/events/dispatcher.py")
    controls = _source("app/services/control_relationships.py")
    assert "dispatcher.register_handler(AutomationEventHandler())" in dispatcher
    assert '"AutomationEventHandler": HandlerControl(' in controls
    assert 'handler_name == "AutomationEventHandler"' in controls


def test_runtime_ledger_is_tenant_isolated_and_permissions_are_granular() -> None:
    migration = _source("alembic/versions/614_automation_runtime_ledger.py")
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "app_current_tenant_id()" in migration
    for permission in (
        "automation:hub:read",
        "automation:run:read",
        "automation:run:redrive",
    ):
        assert permission in migration


def test_runtime_does_not_mutate_legacy_rule_models() -> None:
    source = _source("app/services/automation_runtime.py")
    for legacy_model in (
        "TicketAssignmentRule",
        "AlertRule",
        "FupRule",
        "InboxAutomationRule",
        "NasConnectionRule",
        "DispatchRule",
    ):
        assert legacy_model not in source
