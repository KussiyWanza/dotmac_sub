"""One canonical writer projects every subscription billing anchor.

`financial.prepaid_service_renewals` owns "prepaid subscription paid-through
advancement". `financial.payments` commits cash, allocation and entitlement
evidence and then emits a durable funding-change event; it must not project the
anchor itself. An earlier attempt that called the projection inline from
`_finalize_invoice_payment_effects` was reverted for exactly this reason.
`financial.prepaid_billing_calendar_reconciliation` is the explicit historical
repair owner. It may write only a preview-bound UTC-to-WAT or proved lapsed-
payment correction, and delegates any access consequence to the canonical
lifecycle protocol.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PAYMENTS = PROJECT_ROOT / "app" / "services" / "billing" / "payments.py"
CANONICAL_WRITER = PROJECT_ROOT / "app" / "services" / "account_lifecycle.py"
ENTITLEMENTS = PROJECT_ROOT / "app" / "services" / "service_entitlements.py"
OWNER = PROJECT_ROOT / "app" / "services" / "prepaid_service_renewals.py"
HANDLER = (
    PROJECT_ROOT / "app" / "services" / "events" / "handlers" / "prepaid_renewal.py"
)
CALENDAR_RECONCILER = (
    PROJECT_ROOT / "app" / "services" / "prepaid_billing_calendar_reconciliation.py"
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_function_names(path: Path) -> set[str]:
    return {
        node.name
        for node in _tree(path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _assigns_next_billing_at(path: Path) -> set[str]:
    """Return enclosing function names that assign ``.next_billing_at``."""
    tree = _tree(path)
    offenders: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            targets: list[ast.expr] = []
            if isinstance(inner, ast.Assign):
                targets = list(inner.targets)
            elif isinstance(inner, ast.AugAssign):
                targets = [inner.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "next_billing_at"
                ):
                    offenders.add(node.name)
    return offenders


def _assigns_active_subscription_status(path: Path) -> set[str]:
    """Return functions that directly set a subscription status active."""
    offenders: set[str] = set()
    for node in ast.walk(_tree(path)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Assign):
                continue
            if not (
                isinstance(inner.value, ast.Attribute)
                and isinstance(inner.value.value, ast.Name)
                and inner.value.value.id == "SubscriptionStatus"
                and inner.value.attr == "active"
            ):
                continue
            if any(
                isinstance(target, ast.Attribute) and target.attr == "status"
                for target in inner.targets
            ):
                offenders.add(node.name)
    return offenders


def test_retired_inline_anchor_projection_is_gone() -> None:
    """The helper `financial.payments` used to call inline no longer exists."""
    assert "project_paid_invoice_billing_anchors" not in _module_function_names(
        ENTITLEMENTS
    )
    for path in (PAYMENTS, ENTITLEMENTS):
        source = path.read_text(encoding="utf-8")
        assert "project_paid_invoice_billing_anchors(" not in source, (
            f"{path.name} still calls the retired inline anchor projection"
        )


def test_entitlement_evidence_module_does_not_write_the_anchor() -> None:
    """`service_entitlements` writes entitlement facts, never the projection."""
    assert _assigns_next_billing_at(ENTITLEMENTS) == set()


def test_payments_does_not_call_the_owner_projection_inline() -> None:
    """Payment may orchestrate via events; it may not project the anchor."""
    source = PAYMENTS.read_text(encoding="utf-8")
    assert "project_prepaid_billing_anchor_for_invoice" not in source
    assert "retract_prepaid_billing_anchors_after_funding_reversal" not in source


def test_payment_reanchor_requests_the_canonical_writer() -> None:
    source = PAYMENTS.read_text(encoding="utf-8")
    assert _assigns_next_billing_at(PAYMENTS) == set()
    assert "stage_subscription_billing_anchor(" in source
    assert "BillingAnchorProjectionSource.prepaid_settlement_reanchor" in source


def test_canonical_writer_is_the_only_service_assignment() -> None:
    assert _assigns_next_billing_at(CANONICAL_WRITER) == {
        "stage_subscription_billing_anchor"
    }
    offenders = {
        str(path.relative_to(PROJECT_ROOT)): sorted(_assigns_next_billing_at(path))
        for path in (PROJECT_ROOT / "app" / "services").rglob("*.py")
        if path != CANONICAL_WRITER and _assigns_next_billing_at(path)
    }
    assert offenders == {}


def test_every_active_lifecycle_transition_stages_the_required_anchor() -> None:
    writers = _assigns_active_subscription_status(CANONICAL_WRITER)
    assert writers == {
        "activate_subscription",
        "enable_subscription",
        "restore_subscription_detailed",
        "unsuspend_account_override",
    }
    tree = _tree(CANONICAL_WRITER)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in writers:
            continue
        calls = {
            inner.func.id
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
        }
        assert calls.intersection(
            {
                "_stage_missing_activation_billing_anchor",
                "stage_subscription_billing_anchor",
            }
        ), f"{node.name} can make a service active without staging its anchor"


def test_the_owner_defines_the_single_anchor_projection() -> None:
    names = _module_function_names(OWNER)
    assert "project_prepaid_billing_anchor_for_invoice" in names
    assert "retract_prepaid_billing_anchors_after_funding_reversal" in names
    # Advancement and retraction both flow through the one projection.
    assert _assigns_next_billing_at(OWNER) == set()
    assert "stage_subscription_billing_anchor(" in OWNER.read_text(encoding="utf-8")


def test_reviewed_calendar_reconciler_has_one_named_anchor_repair() -> None:
    assert _assigns_next_billing_at(CALENDAR_RECONCILER) == set()
    source = CALENDAR_RECONCILER.read_text(encoding="utf-8")
    assert "stage_subscription_billing_anchor(" in source
    assert "execute_owner_command(" in source
    assert "preview_fingerprint" in source
    assert "restore_subscription_detailed(" in source
    assert "reason=EnforcementReason.prepaid" in source


def test_payment_allocation_emits_the_funding_change_event() -> None:
    """Regression guard for the defect: the class emitted no event at all."""
    tree = _tree(PAYMENTS)
    allocations = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PaymentAllocations"
    )
    emitted = {
        node.func.id
        for node in ast.walk(allocations)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "emit_event" in emitted, (
        "PaymentAllocations emits no event, so the prepaid consequence owner "
        "is never invoked and the billing anchor goes stale"
    )


def test_the_renewal_handler_covers_funding_reversals() -> None:
    source = HANDLER.read_text(encoding="utf-8")
    assert "payment_refunded" in source
    assert "payment_reversed" in source
