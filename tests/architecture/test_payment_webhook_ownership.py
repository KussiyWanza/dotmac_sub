"""Pin verified payment webhooks to one typed coordinator boundary."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services import sot_relationships
from app.services.sot_manifest import TransactionMode, contract_validation_errors

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/payment_webhook_commands.py"
ADAPTER = ROOT / "app/services/api_billing_webhooks.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(path: Path, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _calls(node: ast.AST) -> set[str]:
    return {
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    }


def _names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def test_payment_webhook_owner_has_complete_coordinator_contract() -> None:
    service = sot_relationships.service_relationship("financial.payment_webhooks")

    assert service.module == "app.services.payment_webhook_commands"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert not contract_validation_errors(
        service,
        service_names={item.name for item in sot_relationships.all_services()},
    )
    assert (
        sot_relationships.owning_service_for(
            "billing consequence submission from verified receipts"
        )
        == service
    )
    baseline = (ROOT / "tests/architecture/sot_manifest_legacy_baseline.txt").read_text(
        encoding="utf-8"
    )
    assert "financial.payment_webhooks" not in baseline.splitlines()


def test_public_webhook_command_owns_one_complete_transaction() -> None:
    source = OWNER.read_text(encoding="utf-8")
    public = _function(OWNER, "process_claimed_payment_webhook")
    implementation = _function(OWNER, "_process_claimed_payment_webhook")

    assert ast.unparse(public).count("execute_owner_command(") == 1
    assert "ProcessClaimedPaymentWebhookCommand" in _names(public)
    assert "CommandContext" in source
    assert "HTTPException" not in source
    assert "JSONResponse" not in source
    assert "fastapi" not in source
    assert "commit" not in _calls(public)
    assert "rollback" not in _calls(public)
    assert "commit" not in _calls(implementation)
    assert "rollback" not in _calls(implementation)
    assert "begin_nested" not in _calls(implementation)


def test_webhook_owner_composes_only_named_financial_participants() -> None:
    source = OWNER.read_text(encoding="utf-8")
    implementation = _function(OWNER, "_process_claimed_payment_webhook")
    calls = _calls(_tree(OWNER))

    assert "stage_verified_settlement" in calls
    assert "stage_verified_webhook_event" in calls
    assert "stage_topup_intent_completion" in _names(_tree(OWNER))
    assert "mark_processed" in calls
    assert "Payment(" not in source
    assert "PaymentAllocation(" not in source
    assert "PaymentProviderEvent(" not in source
    assert "TopupIntent(" not in source
    assert "restore_account_services" not in source
    assert "settle_prepaid_draft_invoices_from_credit" not in source
    assert "commit" not in _calls(implementation)
    assert "rollback" not in _calls(implementation)


def test_webhook_adapter_is_transport_only_and_transaction_neutral() -> None:
    source = ADAPTER.read_text(encoding="utf-8")
    process = _function(ADAPTER, "_process_webhook")
    calls = _calls(process)

    assert "verify_webhook_signature" in calls
    assert "identify_verified_payment_webhook" in _names(process)
    assert "receive_and_claim_verified" in calls
    assert "process_claimed_payment_webhook" in _names(process)
    assert "_record_processing_failure" in _names(process)
    assert "fail_claimed_consequence" in _calls(_tree(ADAPTER))
    assert "release_read_transaction" in calls
    assert not {"add", "delete", "flush", "commit", "rollback", "begin_nested"} & calls
    assert "app.models" not in source
    assert "PaymentProviderEventIngest" not in source
    assert "PaymentStatus" not in source
    assert "TopupIntent" not in source
    assert "_extract_settlement" not in source
    assert "_prepare_provider_event_ingest" not in source
    assert "_apply_post_settlement_bookkeeping" not in source
    assert "_settle_typed_account_credit_deposit" not in source


def test_paystack_vocabulary_is_named_and_non_vacuous() -> None:
    """Every Paystack event type the code claims to handle lives in one
    explicit named set, and an event type outside that set never reaches a
    path indistinguishable from a real settlement or reversal.

    This is the regression guard for the original bug this PR fixes: Paystack
    event names the code did not actually recognize were silently treated as
    a no-op success. A brand-new/unlisted event type must keep doing exactly
    that (informational no-op) -- never be misread as `charge.success` or a
    confirmed refund/reversal.
    """

    from app.services import payment_webhook_commands as owner

    recognized = owner._PAYSTACK_RECOGNIZED_EVENT_TYPES
    assert recognized == (
        owner._PAYSTACK_SETTLEMENT_EVENT_TYPES
        | owner._PAYSTACK_REFUND_EVENT_TYPES
        | owner._PAYSTACK_DISPUTE_EVENT_TYPES
    )
    assert "charge.success" in owner._PAYSTACK_SETTLEMENT_EVENT_TYPES
    assert recognized >= {
        "refund.processed",
        "refund.pending",
        "refund.processing",
        "refund.failed",
        "charge.dispute.create",
        "charge.dispute.resolve",
        "charge.dispute.remind",
    }
    # Payout events are explicitly out of scope; they must never be admitted
    # into the customer-charge refund/reversal vocabulary.
    assert "transfer.reversed" not in recognized
    assert "transfer.failed" not in recognized

    provider = owner.PaymentWebhookProvider.PAYSTACK

    # A synthetic, never-registered event type must resolve to the same
    # informational default as an unrecognized type always has -- not to a
    # settlement or a reversal.
    unknown = owner._settlement_observation(
        provider, event_type="charge.some_future_event_type", data={}
    )
    assert unknown is None

    # The one money-moving refund event type produces a real observation.
    settlement = owner._settlement_observation(
        provider,
        event_type="refund.processed",
        data={"amount": 1000, "currency": "NGN"},
    )
    assert settlement is not None
    assert settlement.status.value == "refunded"

    # Every other recognized-but-informational event type must NOT produce a
    # settlement observation (no money movement), while still being a named,
    # deliberate member of the vocabulary above (not an accident of a
    # catch-all).
    for informational in (
        "refund.pending",
        "refund.processing",
        "refund.failed",
        "charge.dispute.create",
        "charge.dispute.remind",
    ):
        assert (
            owner._settlement_observation(provider, event_type=informational, data={})
            is None
        )

    # `charge.dispute.resolve` is outcome-dependent and deliberately excluded
    # from the static event-type maps.
    from app.services.payment_provider_events import (
        _FINANCIAL_EFFECT_BY_EVENT_TYPE,
        _STATUS_BY_EVENT_TYPE,
    )

    assert "charge.dispute.resolve" not in _STATUS_BY_EVENT_TYPE
    assert "charge.dispute.resolve" not in _FINANCIAL_EFFECT_BY_EVENT_TYPE
