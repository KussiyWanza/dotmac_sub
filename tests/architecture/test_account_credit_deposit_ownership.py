"""Pin Deposit Account Credit settlement to one typed owner boundary."""

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


def _calls(node: ast.AST) -> set[str]:
    return {
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    }


def _names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _named_calls(node: ast.AST) -> set[str]:
    return {
        child.func.id
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
    }


def test_account_credit_deposit_owner_has_complete_contract() -> None:
    service = sot_relationships.service_relationship(
        "financial.account_credit_deposits"
    )

    assert service.module == "app.services.account_credit_deposits"
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
            "verified Deposit Account Credit settlement command"
        )
        == service
    )
    baseline = (ROOT / "tests/architecture/sot_manifest_legacy_baseline.txt").read_text(
        encoding="utf-8"
    )
    assert "financial.account_credit_deposits" not in baseline.splitlines()


def test_public_settlement_is_one_typed_owner_command() -> None:
    source = (ROOT / "app/services/account_credit_deposits.py").read_text(
        encoding="utf-8"
    )
    public = _function("app/services/account_credit_deposits.py", "settle_verified")
    stage = _function(
        "app/services/account_credit_deposits.py", "stage_verified_settlement"
    )

    assert source.count("execute_owner_command(") == 1
    assert "SettleAccountCreditDepositCommand" in _names(public)
    assert "CommandContext" in source
    assert "PaymentGatewayTransaction" not in source
    assert "def create_intent(" not in source
    assert "commit" not in {argument.arg for argument in public.args.args}
    assert "commit" not in {argument.arg for argument in public.args.kwonlyargs}
    assert "commit" not in _calls(stage)
    assert "rollback" not in _calls(stage)


def test_adapters_select_root_or_participant_by_transaction_ownership() -> None:
    customer = _function(
        "app/services/customer_portal_flow_payments.py",
        "verify_and_record_topup",
    )
    reconciliation = _function(
        "app/services/payment_reconciliation.py",
        "_stage_verified_settlement",
    )
    webhook = _function(
        "app/services/payment_webhook_commands.py",
        "_stage_deposit_settlement",
    )
    proof = _function("app/services/payment_proofs.py", "_verify_proof")

    assert "settle_verified" in _calls(customer)
    assert "release_read_transaction" in _calls(customer)
    assert "stage_verified_settlement" in _calls(reconciliation)
    assert "settle_verified" not in _calls(reconciliation)
    assert "commit" not in _calls(reconciliation)
    assert "rollback" not in _calls(reconciliation)
    assert "stage_verified_settlement" in _calls(webhook)
    assert "commit" not in _calls(webhook)
    assert "rollback" not in _calls(webhook)
    assert "stage_verified_settlement" in _calls(proof)
    assert "settle_verified" not in _calls(proof)


def test_settlement_event_and_source_vocabulary_are_versioned() -> None:
    source = (ROOT / "app/services/account_credit_deposits.py").read_text(
        encoding="utf-8"
    )

    assert "class AccountCreditDepositSettlementSource" in source
    assert "SettleAccountCreditDepositCommand" in source
    assert "schema_version" in source
    assert "command_id" in source
    assert "correlation_id" in source
    assert "EventType.account_credit_deposited" in source


def test_customer_topup_page_uses_owner_active_request_state() -> None:
    page = _function(
        "app/services/customer_portal_flow_payments.py",
        "get_topup_page",
    )
    template = (ROOT / "templates/customer/billing/topup.html").read_text(
        encoding="utf-8"
    )

    assert "active_request" in _calls(page)
    assert "_latest_pending_direct_transfer_intent" not in _named_calls(page)
    assert "active_deposit_request.phase" in template
    assert 'active_deposit_request.next_action == "upload_receipt"' in template
    assert "Your transfer receipt is under review." in template
