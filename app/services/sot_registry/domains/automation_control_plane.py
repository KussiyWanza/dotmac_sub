"""Canonical SOT declarations for the automation control plane."""

from __future__ import annotations

from app.services.automation_contracts import AutomationDomainCapabilities
from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    EventContract,
    MigrationContract,
    OwnerRole,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="automation_control_plane",
    services=(
        SOTService(
            name="automation.capability_registry",
            module="app.services.automation_capabilities",
            owns=("automation capability declarations and compatibility validation",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=(
                            "automation capability declarations and compatibility "
                            "validation"
                        ),
                        role=OwnerRole.POLICY,
                        input_names=("SOT domain automation declarations",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="SOT domain automation declarations",
                        owner="automation.capability_registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "immutable AutomationDomainCapabilities declared by each "
                            "canonical DomainSOT"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="Pure in-process registry resolution; no database writes.",
                    locking="Not applicable because declarations are immutable code.",
                    idempotency="The same checked-in declarations resolve identically.",
                    retries="Retry the pure query after correcting an invalid manifest.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "automation_capability_undeclared",
                        "automation_capability_ambiguous",
                        "automation_capability_manifest_invalid",
                    ),
                    mapping_owner="automation authoring adapters",
                    fail_closed_on=(
                        "unknown module, trigger, action, target, or contract version",
                        "duplicate capability ownership",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="automation.capability_registry",
                    verification="automation capability registry architecture tests",
                    cutover_gate=(
                        "only registered capabilities can be stored or executed by the "
                        "Automation Center"
                    ),
                    fallback_retirement="No runtime-editable capability registry exists.",
                ),
                steward="platform automation",
                design_refs=(
                    "docs/designs/AUTOMATION_CENTER_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/architecture/test_automation_capability_registry.py",
                ),
            ),
        ),
        SOTService(
            name="automation.rule_definitions",
            module="app.services.automation_rules",
            owns=("automation rule definitions and immutable versions",),
            depends_on=("automation.capability_registry",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="automation rule definitions and immutable versions",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "typed automation rule lifecycle command",
                            "declared automation capabilities",
                            "tenant-scoped automation rule records",
                        ),
                        canonical_writer="automation.rule_definitions",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed automation rule lifecycle command",
                        owner="automation.rule_definitions",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed create, replace-draft, publish, pause, resume, "
                            "and retire commands with actor and permission evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="declared automation capabilities",
                        owner="automation.capability_registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "closed trigger, field, action, schema-version, permission, "
                            "and legacy-conflict declarations"
                        ),
                    ),
                    AuthorityInput(
                        name="tenant-scoped automation rule records",
                        owner="automation.rule_definitions",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="AutomationRule and AutomationRuleVersion rows",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "each public lifecycle command enters execute_owner_command "
                        "once and commits its rule, version, and event atomically"
                    ),
                    locking=(
                        "tenant and rule identity are rechecked; existing rules and "
                        "draft versions are locked before mutation"
                    ),
                    idempotency=(
                        "tenant rule keys, rule-version numbers, and one-draft indexes "
                        "converge duplicate commands"
                    ),
                    retries="retry the complete lifecycle command after rollback",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "automation.rule_definitions.not_found",
                        "automation.rule_definitions.permission_denied",
                        "automation.rule_definitions.identity_invalid",
                        "automation.rule_definitions.key_conflict",
                        "automation.rule_definitions.condition_field_undeclared",
                        "automation.rule_definitions.condition_operator_unsupported",
                        "automation.rule_definitions.condition_value_invalid",
                        "automation.rule_definitions.action_inputs_invalid",
                        "automation.rule_definitions.action_target_mismatch",
                        "automation.rule_definitions.legacy_scope_conflict",
                        "automation.rule_definitions.status_conflict",
                        *owner_command_boundary_error_codes(
                            "automation.rule_definitions"
                        ),
                    ),
                    mapping_owner="automation web and API adapters",
                    fail_closed_on=(
                        "unknown or stale capability contract",
                        "missing central or module permission",
                        "legacy-exclusive capability conflict",
                    ),
                ),
                events=EventContract(
                    event_types=("automation.rule_changed",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 carries tenant, rule, version, status, and change "
                        "identity without rule values."
                    ),
                    replay=(
                        "Rule and immutable version rows reconstruct lifecycle state; "
                        "event-store rows reconstruct emitted transitions."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="automation.rule_definitions",
                    verification=(
                        "automation rule owner, migration, permission, and architecture "
                        "tests"
                    ),
                    cutover_gate=(
                        "all Automation Center rule writes use typed owner commands"
                    ),
                    fallback_retirement=(
                        "no generic CRUD or direct ORM rule writer is admitted"
                    ),
                ),
                steward="platform automation",
                design_refs=(
                    "docs/designs/AUTOMATION_CENTER_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_automation_rules.py",
                    "tests/architecture/test_automation_rule_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="automation.execution",
            module="app.services.automation_runtime",
            owns=("automation execution decisions and run evidence",),
            depends_on=(
                "automation.capability_registry",
                "automation.rule_definitions",
                "events.store",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="automation execution decisions and run evidence",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "durable domain event evidence",
                            "published automation rule versions",
                            "declared automation runtime adapters",
                            "tenant-scoped automation run evidence",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="durable domain event evidence",
                        owner="events.store",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "EventStore identity, type, payload, actor, and durable "
                            "handler retry evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="published automation rule versions",
                        owner="automation.rule_definitions",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active immutable AutomationRuleVersion conditions and "
                            "ordered action declarations"
                        ),
                    ),
                    AuthorityInput(
                        name="declared automation runtime adapters",
                        owner="automation.capability_registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "closed code registry whose action keys exactly match "
                            "typed idempotent executors"
                        ),
                    ),
                    AuthorityInput(
                        name="tenant-scoped automation run evidence",
                        owner="automation.execution",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="AutomationRun and AutomationStepRun rows",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "event planning, each step claim, each module owner command, "
                        "and each step completion use separate committed owner "
                        "boundaries so external effects never share a transaction"
                    ),
                    locking=(
                        "rule-version/event uniqueness converges replay; run and step "
                        "rows are locked before claim and completion transitions"
                    ),
                    idempotency=(
                        "each action receives the stable event/version/step key and "
                        "module adapters must honor their declared idempotency contract"
                    ),
                    retries=(
                        "EventStore retries only the failed automation handler; "
                        "succeeded steps are skipped and expired claims are reclaimed"
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "automation.execution.trigger_event_mismatch",
                        "automation.execution.trigger_target_mismatch",
                        "automation.execution.step_not_found",
                        "automation.execution.run_not_found",
                        "automation.execution.action_failed",
                        *owner_command_boundary_error_codes("automation.execution"),
                    ),
                    mapping_owner="automation event and web adapters",
                    fail_closed_on=(
                        "missing or mismatched event tenant and target identity",
                        "capability-to-executor registry mismatch",
                        "busy or failed ordered action step",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="automation.execution",
                    verification=(
                        "automation runtime, handler registration, RLS, replay, and "
                        "static adapter registry tests"
                    ),
                    cutover_gate=(
                        "a module receives no automation traffic until its trigger, "
                        "action, and exact executor are admitted together"
                    ),
                    fallback_retirement=(
                        "remove the module declaration and executor; existing legacy "
                        "automation surfaces remain independently owned"
                    ),
                ),
                steward="platform automation",
                design_refs=(
                    "docs/designs/AUTOMATION_CENTER_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_automation_runtime.py",
                    "tests/architecture/test_automation_runtime_boundary.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.services.automation_capabilities",
        "app.services.automation_rules",
        "app.services.automation_runtime",
        "app.services.events.handlers.automation",
        "app.services.web_automation_center",
        "app.web.admin.automation_center",
    ),
    rule=(
        "Every SOT domain is visible to the Automation Center, but only a closed, "
        "versioned domain declaration may expose executable triggers or actions."
    ),
    automation=AutomationDomainCapabilities(target_types=("automation.rule",)),
)
