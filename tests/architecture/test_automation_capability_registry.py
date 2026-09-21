from __future__ import annotations

import pytest

from app.services import automation_capabilities
from app.services.automation_contracts import (
    AutomationConditionField,
    AutomationDomainCapabilities,
    AutomationOperator,
    AutomationTriggerCapability,
    AutomationValueType,
)
from app.services.sot_registry.model import DomainSOT


def test_every_sot_domain_is_visible_in_module_catalogue() -> None:
    modules = automation_capabilities.all_module_manifests()
    assert modules
    assert len({item.module_key for item in modules}) == len(modules)
    assert "automation_control_plane" in {item.module_key for item in modules}


def test_unregistered_domain_cannot_resolve_a_trigger() -> None:
    with pytest.raises(automation_capabilities.AutomationCapabilityError):
        automation_capabilities.trigger_capability("support.ticket.created")


def test_checked_in_registry_is_structurally_valid() -> None:
    assert automation_capabilities.capability_registry_errors() == ()


def test_invalid_enum_field_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    declaration = DomainSOT(
        domain="invalid_test_domain",
        services=(),
        entrypoints=(),
        rule="test only",
        automation=AutomationDomainCapabilities(
            target_types=("test.entity",),
            triggers=(
                AutomationTriggerCapability(
                    key="test.entity.created",
                    label="Entity created",
                    event_type="test.entity.created",
                    event_schema_version=1,
                    entity_type="test.entity",
                    tenant_id_field="tenant_id",
                    entity_id_field="entity_id",
                    fields=(
                        AutomationConditionField(
                            key="status",
                            label="Status",
                            value_type=AutomationValueType.enum,
                            operators=(AutomationOperator.equals,),
                        ),
                    ),
                    author_permission="test:entity:read",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        automation_capabilities,
        "DOMAIN_SOT_RELATIONSHIPS",
        (declaration,),
    )
    assert automation_capabilities.capability_registry_errors() == (
        "trigger 'test.entity.created' enum field 'status' has no values",
    )
