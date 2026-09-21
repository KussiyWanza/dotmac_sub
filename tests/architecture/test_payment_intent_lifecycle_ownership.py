"""Pin gateway intent observations and UI projections to their named owners."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _function(path: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(_source(path), filename=path)
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _calls(node: ast.AST) -> set[str]:
    result: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            result.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            result.add(child.func.attr)
    return result


def test_gateway_adapter_only_reports_typed_observations() -> None:
    source = _source("app/services/payment_gateway_adapter.py")
    observe = _function(
        "app/services/payment_gateway_adapter.py", "observe_verification"
    )

    assert "PaymentGatewayVerificationObservation" in source
    assert "PaymentGatewayProviderStatus" in source
    assert "TopupIntent" not in source
    assert "set_topup_intent_status" not in source
    assert "commit" not in _calls(observe)
    assert "rollback" not in _calls(observe)


def test_lifecycle_owner_controls_status_blocker_and_retry_projection() -> None:
    owner = _source("app/services/topup_intents.py")
    deposit = _source("app/services/account_credit_deposits.py")
    management = _source("app/services/payment_intent_management.py")

    assert "def stage_gateway_topup_observation(" in owner
    assert "def project_topup_intent_lifecycle(" in owner
    assert "RecordGatewayTopupObservationCommand" in owner
    assert "blocks_another_attempt" in owner
    assert "customer_retry_allowed" in owner
    assert "project_topup_intent_lifecycle" in deposit
    assert "project_topup_intent_lifecycle" in management


def test_routes_tasks_and_templates_remain_projection_adapters() -> None:
    routes = _source("app/web/customer/routes.py")
    task = _source("app/tasks/payment_reconciliation.py")
    customer_template = _source("templates/customer/billing/payment_status.html")
    admin_template = _source("templates/admin/customers/payment_intents.html")

    assert "projection.customer_message" in routes
    assert "isinstance(exc" not in ast.unparse(
        _function("app/web/customer/routes.py", "_render_payment_return_status")
    )
    assert "RunTopupReconciliationCommand" in task
    assert "reconcile_pending_topups" in task
    assert "customer_retry_allowed" in customer_template
    for projected_field in (
        "provider_type",
        "reference",
        "requested_amount",
        "currency",
        "created_at",
        "expires_at",
        "status_label",
        "safe_reason_code",
        "last_verification_at",
        "blocks_another_attempt",
        "customer_retry_allowed",
        "cancellation_action",
    ):
        assert projected_field in admin_template
    assert "cancellation.impact" in admin_template
    assert "cancellation.confirmation_message" in admin_template
    assert "Review transfer proof" in admin_template
    assert "tojson" not in admin_template
    assert "metadata" not in admin_template


def test_required_customer_wording_is_owned_once() -> None:
    owner = _source("app/services/topup_intents.py")

    for message in (
        "Waiting for payment confirmation.",
        "Your payment is still processing. Please wait.",
        "Payment was not completed. You can try again.",
        "This payment attempt expired. Start a new payment.",
        "Payment confirmation is temporarily unavailable.",
    ):
        assert message in owner
