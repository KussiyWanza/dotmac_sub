from __future__ import annotations

from decimal import Decimal

import pytest

from app.services import automation_capabilities, automation_rules
from app.services.automation_contracts import (
    AutomationActionCapability,
    AutomationActionInput,
    AutomationConditionField,
    AutomationDomainCapabilities,
    AutomationOperator,
    AutomationTriggerCapability,
    AutomationValueType,
    LegacyAutomationSurface,
)
from app.services.sot_registry.model import DomainSOT


@pytest.fixture
def declared_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    declaration = DomainSOT(
        domain="test_automation_domain",
        services=(),
        entrypoints=(),
        rule="test only",
        automation=AutomationDomainCapabilities(
            target_types=("test.ticket",),
            triggers=(
                AutomationTriggerCapability(
                    key="test.ticket.created",
                    label="Ticket created",
                    event_type="test.ticket.created",
                    event_schema_version=2,
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
                            key="balance",
                            label="Balance",
                            value_type=AutomationValueType.decimal,
                            operators=(AutomationOperator.greater_than,),
                        ),
                    ),
                    author_permission="support:ticket:read",
                ),
            ),
            actions=(
                AutomationActionCapability(
                    key="test.ticket.assign",
                    label="Assign ticket",
                    entity_type="test.ticket",
                    command_owner="test.ticket_owner",
                    command_name="assign_ticket",
                    input_schema_version=1,
                    inputs=(
                        AutomationActionInput(
                            key="team_id",
                            label="Team",
                            value_type=AutomationValueType.uuid,
                        ),
                    ),
                    author_permission="support:ticket:update",
                    runtime_scope="support:ticket:update",
                    idempotency="event, rule version, and step",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        automation_capabilities, "DOMAIN_SOT_RELATIONSHIPS", (declaration,)
    )


def test_definition_is_serialized_against_declared_contract(
    declared_capabilities: None,
) -> None:
    team_id = automation_rules.UUID("76a79707-c896-4db8-a802-6bce97cb0981")
    version, conditions, actions = automation_rules._validate_definition(
        trigger_key="test.ticket.created",
        conditions=(
            automation_rules.AutomationCondition(
                field_key="priority",
                operator=AutomationOperator.equals,
                value="urgent",
            ),
            automation_rules.AutomationCondition(
                field_key="balance",
                operator=AutomationOperator.greater_than,
                value=Decimal("10.50"),
            ),
        ),
        actions=(
            automation_rules.AutomationActionStep(
                action_key="test.ticket.assign",
                inputs=(
                    automation_rules.AutomationActionValue(
                        key="team_id", value=team_id
                    ),
                ),
            ),
        ),
        permission_keys=frozenset({"support:ticket:read", "support:ticket:update"}),
    )
    assert version == 2
    assert conditions[1]["value"] == "10.50"
    assert actions[0]["inputs"] == [{"key": "team_id", "value": str(team_id)}]


def test_definition_fails_without_module_permission(
    declared_capabilities: None,
) -> None:
    with pytest.raises(automation_rules.AutomationRuleError) as exc_info:
        automation_rules._validate_definition(
            trigger_key="test.ticket.created",
            conditions=(),
            actions=(
                automation_rules.AutomationActionStep(
                    action_key="test.ticket.assign",
                    inputs=(
                        automation_rules.AutomationActionValue(
                            key="team_id",
                            value=automation_rules.UUID(
                                "76a79707-c896-4db8-a802-6bce97cb0981"
                            ),
                        ),
                    ),
                ),
            ),
            permission_keys=frozenset({"support:ticket:read"}),
        )
    assert exc_info.value.code == "automation.rule_definitions.permission_denied"


def test_legacy_exclusive_scope_blocks_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declaration = DomainSOT(
        domain="legacy_test_domain",
        services=(),
        entrypoints=(),
        rule="test only",
        automation=AutomationDomainCapabilities(
            target_types=("test.ticket",),
            triggers=(
                AutomationTriggerCapability(
                    key="test.ticket.created",
                    label="Ticket created",
                    event_type="test.ticket.created",
                    event_schema_version=1,
                    entity_type="test.ticket",
                    tenant_id_field="tenant_id",
                    entity_id_field="ticket_id",
                    fields=(),
                    author_permission="support:ticket:read",
                ),
            ),
            actions=(
                AutomationActionCapability(
                    key="test.ticket.assign",
                    label="Assign ticket",
                    entity_type="test.ticket",
                    command_owner="test.ticket_owner",
                    command_name="assign_ticket",
                    input_schema_version=1,
                    inputs=(),
                    author_permission="support:ticket:update",
                    runtime_scope="support:ticket:update",
                    idempotency="event, rule version, and step",
                ),
            ),
            legacy_surfaces=(
                LegacyAutomationSurface(
                    key="legacy.ticket_assignment",
                    label="Legacy assignment",
                    owner_service="support.ticket_assignment_evaluation",
                    management_path="/admin/support/assignment-rules",
                    conflict_scopes=("test.ticket.assign",),
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        automation_capabilities, "DOMAIN_SOT_RELATIONSHIPS", (declaration,)
    )
    with pytest.raises(automation_rules.AutomationRuleError) as exc_info:
        automation_rules._validate_definition(
            trigger_key="test.ticket.created",
            conditions=(),
            actions=(
                automation_rules.AutomationActionStep(
                    action_key="test.ticket.assign", inputs=()
                ),
            ),
            permission_keys=frozenset({"support:ticket:read", "support:ticket:update"}),
        )
    assert exc_info.value.code == "automation.rule_definitions.legacy_scope_conflict"
