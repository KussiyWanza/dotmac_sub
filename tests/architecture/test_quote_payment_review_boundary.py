"""Architecture guard for staff-gated customer Quote payments."""

from pathlib import Path

from app.services.sot_manifest import contract_validation_errors
from app.services.sot_registry.registry import all_services, service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_quote_payment_review_is_a_complete_typed_command_owner() -> None:
    owner = service_relationship("sales.quote_payment_review")
    assert owner.module == "app.services.sales.quote_payment_review"
    assert owner.contract is not None
    assert (
        contract_validation_errors(
            owner, service_names={service.name for service in all_services()}
        )
        == ()
    )
    assert owner.contract.transaction.mode.value == "owner_managed"
    assert "auth.permission_gate" in owner.depends_on


def test_customer_mobile_routes_use_protected_typed_payment_boundary() -> None:
    routes = _source("app/api/me.py")
    initiate = routes[routes.index("def my_quote_deposit_initiate") :]
    initiate = initiate[: initiate.index("def my_quote_deposit_verify")]
    verify = routes[routes.index("def my_quote_deposit_verify") :]
    verify = verify[: verify.index('@router.post("/referrals"')]

    assert "initiate_quote_deposit(" in initiate
    assert "InitiateQuoteDepositCommand(" in initiate
    assert "initiate_deposit(" not in initiate
    assert "verify_quote_deposit(" in verify
    assert "VerifyQuoteDepositCommand(" in verify
    assert "verify_deposit(" not in verify


def test_payment_owner_checks_current_approval_before_money_moves() -> None:
    deposits = _source("app/services/quote_deposits.py")
    assert deposits.count("approval_required") >= 2
    assert "resolve_payment_review(quote)" in deposits
    assert deposits.index("approval_required") < deposits.index(
        "payments.verify_and_record_payment("
    )


def test_admin_review_route_has_dedicated_permission() -> None:
    routes = _source("app/web/admin/sales.py")
    review = routes[routes.index('"/quotes/{quote_id}/payment-review"') :]
    review = review[: review.index('@router.get(\n    "/quotes/{quote_id}/edit"')]
    assert 'require_permission("sales:quote:review")' in review
    assert "review_quote_payment(" in review
