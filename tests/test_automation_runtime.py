from __future__ import annotations

import pytest

from app.services import automation_capabilities, automation_runtime
from app.services.automation_contracts import (
    AutomationConditionField,
    AutomationDomainCapabilities,
    AutomationOperator,
    AutomationTriggerCapability,
    AutomationValueType,
)
from app.services.sot_registry.model import DomainSOT


@pytest.fixture
def declared_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    declaration = DomainSOT(
        domain="test_runtime_domain",
        services=(),
        entrypoints=(),
        rule="test only",
        automation=AutomationDomainCapabilities(
            target_types=("test.ticket",),
            triggers=(
                AutomationTriggerCapability(
                    key="test.ticket.created",
                    label="Ticket created",
                    event_type="subscriber.created",
                    event_schema_version=1,
                    entity_type="test.ticket",
                    tenant_id_field="tenant_id",
                    entity_id_field="ticket_id",
                    fields=(
                        AutomationConditionField(
                            key="priority",
                            label="Priority",
                            value_type=AutomationValueType.enum,
                            operators=(
                                AutomationOperator.equals,
                                AutomationOperator.in_values,
                            ),
                            enum_values=("normal", "urgent"),
                        ),
                        AutomationConditionField(
                            key="evidence.balance",
                            label="Balance",
                            value_type=AutomationValueType.decimal,
                            operators=(AutomationOperator.greater_than,),
                        ),
                    ),
                    author_permission="support:ticket:read",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        automation_capabilities, "DOMAIN_SOT_RELATIONSHIPS", (declaration,)
    )


def test_rule_conditions_use_only_declared_typed_fields(
    declared_trigger: None,
) -> None:
    assert automation_runtime._rule_matches(
        trigger_key="test.ticket.created",
        conditions=[
            {"field_key": "priority", "operator": "equals", "value": "urgent"},
            {
                "field_key": "evidence.balance",
                "operator": "greater_than",
                "value": "10.5",
            },
        ],
        payload={"priority": "urgent", "evidence": {"balance": "11.0"}},
    )


def test_undeclared_field_or_operator_fails_closed(declared_trigger: None) -> None:
    payload = {"priority": "urgent"}
    assert not automation_runtime._rule_matches(
        trigger_key="test.ticket.created",
        conditions=[{"field_key": "secret", "operator": "equals", "value": "x"}],
        payload=payload,
    )
    assert not automation_runtime._rule_matches(
        trigger_key="test.ticket.created",
        conditions=[
            {"field_key": "priority", "operator": "contains", "value": "urgent"}
        ],
        payload=payload,
    )
