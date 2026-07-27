"""Pin direct-transfer proof linkage to one typed intent participant."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services import sot_relationships
from app.services.sot_manifest import TransactionMode, contract_validation_errors

ROOT = Path(__file__).resolve().parents[2]


def _function(path: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    source_path = ROOT / path
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def _attribute_calls(node: ast.AST) -> set[str]:
    return {
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    }


def _intent_assignment_paths(attributes: set[str]) -> set[str]:
    paths: set[str] = set()
    for path in (ROOT / "app/services").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr not in attributes:
                continue
            if not isinstance(node.ctx, ast.Store):
                continue
            if isinstance(node.value, ast.Name) and node.value.id in {
                "intent",
                "topup_intent",
            }:
                paths.add(path.relative_to(ROOT).as_posix())
    return paths


def _topup_intent_constructor_paths() -> set[str]:
    paths: set[str] = set()
    for path in (ROOT / "app/services").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "TopupIntent"
            for node in ast.walk(tree)
        ):
            paths.add(path.relative_to(ROOT).as_posix())
    return paths


def test_direct_transfer_intent_participant_has_complete_contract() -> None:
    service = sot_relationships.service_relationship("financial.topup_intents")

    assert service.module == "app.services.topup_intents"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.PARTICIPANT
    assert (
        contract_validation_errors(
            service,
            service_names={item.name for item in sot_relationships.all_services()},
        )
        == ()
    )
    assert (
        sot_relationships.owning_service_for(
            "direct-transfer top-up intent proof submission transition"
        )
        == service
    )
    assert (
        sot_relationships.owning_service_for(
            "direct-transfer reviewed-proof resolution projection"
        )
        == service
    )


def test_direct_transfer_creation_coordinator_has_complete_contract() -> None:
    service = sot_relationships.service_relationship(
        "financial.direct_transfer_intent_commands"
    )

    assert service.module == "app.services.direct_transfer_intents"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert (
        contract_validation_errors(
            service,
            service_names={item.name for item in sot_relationships.all_services()},
        )
        == ()
    )
    assert (
        sot_relationships.owning_service_for(
            "customer direct-transfer intent creation coordination"
        )
        == service
    )


def test_terminal_proof_reconciliation_has_complete_contract() -> None:
    service = sot_relationships.service_relationship(
        "financial.topup_intent_proof_reconciliation"
    )

    assert service.module == "app.services.topup_intent_proof_reconciliation"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert (
        contract_validation_errors(
            service,
            service_names={item.name for item in sot_relationships.all_services()},
        )
        == ()
    )
    assert (
        sot_relationships.owning_service_for(
            "submitted intent terminal-proof reconciliation"
        )
        == service
    )


def test_gateway_creation_coordinator_has_complete_contract() -> None:
    service = sot_relationships.service_relationship(
        "financial.gateway_topup_intent_commands"
    )

    assert service.module == "app.services.gateway_topup_intents"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert (
        contract_validation_errors(
            service,
            service_names={item.name for item in sot_relationships.all_services()},
        )
        == ()
    )
    assert (
        sot_relationships.owning_service_for("saved-card charge failure coordination")
        == service
    )


def test_payment_proof_owner_composes_locked_intent_participant() -> None:
    payment_function = _function(
        "app/services/payment_proofs.py",
        "_submit_direct_transfer_proof",
    )
    calls = _attribute_calls(payment_function)

    assert "lock_direct_transfer_intent_for_proof" in calls
    assert "stage_direct_transfer_proof_submission" in calls
    assert "commit" not in calls
    assert "rollback" not in calls


def test_payment_proof_review_composes_intent_resolution_participant() -> None:
    for function_name in ("_verify_proof", "_reject_proof"):
        review_function = _function(
            "app/services/payment_proofs.py",
            function_name,
        )
        calls = _attribute_calls(review_function)
        assert "stage_direct_transfer_proof_resolution" in calls
        assert "commit" not in calls
        assert "rollback" not in calls


def test_customer_portal_does_not_write_direct_transfer_intent_state() -> None:
    portal_function = _function(
        "app/services/customer_portal_flow_payments.py",
        "submit_direct_transfer_topup",
    )
    calls = _attribute_calls(portal_function)
    constants = {
        child.value
        for child in ast.walk(portal_function)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }

    assert "submit_direct_transfer_proof" in calls
    assert "set_topup_intent_status" not in calls
    assert "add" not in calls
    assert "commit" not in calls
    assert "payment_proof_id" not in constants
    assert "selected_bank_account" not in constants


def test_customer_portal_creation_delegates_without_policy_or_record_writes() -> None:
    portal_function = _function(
        "app/services/customer_portal_flow_payments.py",
        "create_direct_transfer_topup_intent",
    )
    calls = _attribute_calls(portal_function)
    referenced_names = {
        child.id for child in ast.walk(portal_function) if isinstance(child, ast.Name)
    }

    assert "create_direct_transfer_intent" in calls
    assert "release_read_transaction" in calls
    assert "TopupIntent" not in referenced_names
    assert "AccountCreditDeposits" not in referenced_names
    assert "resolve_value" not in referenced_names
    assert "add" not in calls
    assert "flush" not in calls
    assert "commit" not in calls


def test_direct_transfer_configuration_has_one_domain_resolver() -> None:
    portal_source = (ROOT / "app/services/customer_portal_flow_payments.py").read_text(
        encoding="utf-8"
    )
    owner_source = (ROOT / "app/services/topup_intents.py").read_text(encoding="utf-8")
    settings_source = (ROOT / "app/services/settings_spec.py").read_text(
        encoding="utf-8"
    )

    assert "def direct_transfer_configuration(" in owner_source
    assert "resolve_values_atomic(" in owner_source
    assert "DomainSetting" not in owner_source
    assert "select(DomainSetting)" not in portal_source
    assert "control_registry.is_enabled(" not in owner_source
    assert "collection_account_directory.enabled_transfer_accounts(" in owner_source
    assert 'key="direct_bank_transfer_enabled"' not in settings_source
    assert 'key="direct_bank_transfer_intent_ttl_days"' in settings_source
    assert "_DIRECT_TRANSFER_TTL" not in portal_source


def test_creation_coordinator_and_deposit_participant_complete_no_transactions() -> (
    None
):
    coordinator = ROOT / "app/services/direct_transfer_intents.py"
    coordinator_source = coordinator.read_text(encoding="utf-8")
    coordinator_tree = ast.parse(coordinator_source, filename=str(coordinator))
    stage = _function("app/services/account_credit_deposits.py", "stage_intent")

    assert coordinator_source.count("execute_owner_command(") == 1
    assert "commit" not in _attribute_calls(coordinator_tree)
    assert "rollback" not in _attribute_calls(coordinator_tree)
    assert "commit" not in _attribute_calls(stage)
    assert "rollback" not in _attribute_calls(stage)


def test_completion_projection_fields_have_one_canonical_writer() -> None:
    assert _intent_assignment_paths(
        {"completed_payment_id", "completed_at", "actual_amount", "external_id"}
    ) == {"app/services/topup_intents.py"}


def test_gateway_creation_has_only_canonical_record_writers() -> None:
    assert _topup_intent_constructor_paths() == {
        "app/services/account_credit_deposits.py",
        "app/services/topup_intents.py",
    }


def test_gateway_adapters_delegate_without_lifecycle_policy_or_writes() -> None:
    customer_source = (
        ROOT / "app/services/customer_portal_flow_payments.py"
    ).read_text(encoding="utf-8")
    reseller_source = (ROOT / "app/services/reseller_portal_billing.py").read_text(
        encoding="utf-8"
    )
    coordinator_path = ROOT / "app/services/gateway_topup_intents.py"
    coordinator_source = coordinator_path.read_text(encoding="utf-8")
    settings_source = (ROOT / "app/services/settings_spec.py").read_text(
        encoding="utf-8"
    )
    coordinator_calls = _attribute_calls(
        ast.parse(coordinator_source, filename=str(coordinator_path))
    )

    assert "create_customer_gateway_topup_intent(" in customer_source
    assert "create_reseller_gateway_topup_intent(" in reseller_source
    assert "fail_saved_card_charge(" in customer_source
    assert "fail_saved_card_charge(" in reseller_source
    assert "_TOPUP_INTENT_TTL" not in customer_source
    assert "_INTENT_TTL" not in reseller_source
    assert "gateway_topup_intent_ttl_minutes" in coordinator_source
    assert 'key="gateway_topup_intent_ttl_minutes"' in settings_source
    assert "timedelta(minutes=30)" not in customer_source
    assert "timedelta(minutes=30)" not in reseller_source
    assert coordinator_source.count("execute_owner_command(") == 3
    assert "commit" not in coordinator_calls
    assert "rollback" not in coordinator_calls


def test_completion_and_expiry_callers_delegate_to_intent_participant() -> None:
    expected_calls = {
        "app/services/account_credit_deposits.py": "stage_topup_intent_completion",
        "app/services/payment_proofs.py": "stage_direct_transfer_proof_resolution",
        "app/services/payment_webhook_commands.py": "stage_topup_intent_completion",
        "app/services/customer_portal_flow_payments.py": (
            "stage_topup_intent_completion"
        ),
        "app/services/payment_reconciliation.py": "stage_topup_intent_completion",
        "app/services/reseller_portal_billing.py": "stage_topup_intent_completion",
    }
    for path, call in expected_calls.items():
        assert call in (ROOT / path).read_text(encoding="utf-8")
    reconciliation = (ROOT / "app/services/payment_reconciliation.py").read_text(
        encoding="utf-8"
    )
    assert "stage_topup_intent_expiry" in reconciliation
    assert "_EXPIRE_GRACE" not in reconciliation
    assert "DEFAULT_EXPIRY_GRACE_HOURS" not in reconciliation


def test_topup_intent_participant_never_completes_its_transaction() -> None:
    path = ROOT / "app/services/topup_intents.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    calls = _attribute_calls(tree)

    assert "commit" not in calls
    assert "rollback" not in calls
    assert "begin_nested" not in calls
    assert "HTTPException" not in source
    assert "topup_intent.direct_transfer_submitted" in (
        ROOT / "app/services/events/types.py"
    ).read_text(encoding="utf-8")
    assert "topup_intent.direct_transfer_proof_rejected" in (
        ROOT / "app/services/events/types.py"
    ).read_text(encoding="utf-8")
    assert "topup_intent.completed" in (
        ROOT / "app/services/events/types.py"
    ).read_text(encoding="utf-8")
    assert "topup_intent.expired" in (ROOT / "app/services/events/types.py").read_text(
        encoding="utf-8"
    )
