"""One forward owner advances the anchor; one reviewed reconciler repairs drift.

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


# `financial.payments` still advances the anchor in exactly one place, while
# re-anchoring a lapsed prepaid invoice's documentary period. That is a KNOWN,
# DELIBERATE exception, not an approved second owner: it is reached by every
# `_finalize_invoice_payment_effects` caller, several of which emit no
# funding-change event, so retiring it is a wider change that needs its own
# architecture decision. It is recorded by name in docs/SOT_RELATIONSHIP_MAP.md
# under "Billing-anchor writer boundary".
PAYMENTS_ANCHOR_WRITER_EXCEPTIONS = {"_reanchor_paid_prepaid_invoice_if_lapsed"}


def test_payments_anchor_writers_are_a_named_shrinking_exception() -> None:
    """Pin the one remaining payment-side writer so no new one can appear.

    Equality, not a subset: adding another `next_billing_at` writer to
    `financial.payments` fails here, and retiring this one requires deleting
    its name from the exception set.
    """
    assert _assigns_next_billing_at(PAYMENTS) == PAYMENTS_ANCHOR_WRITER_EXCEPTIONS

    documented = (PROJECT_ROOT / "docs" / "SOT_RELATIONSHIP_MAP.md").read_text(
        encoding="utf-8"
    )
    for name in PAYMENTS_ANCHOR_WRITER_EXCEPTIONS:
        assert name in documented, (
            f"{name} writes the billing anchor but is not documented as a named "
            "exception in the relationship map"
        )


def test_the_owner_defines_the_single_anchor_projection() -> None:
    names = _module_function_names(OWNER)
    assert "project_prepaid_billing_anchor_for_invoice" in names
    assert "retract_prepaid_billing_anchors_after_funding_reversal" in names
    # Advancement and retraction both flow through the one projection.
    assert _assigns_next_billing_at(OWNER) <= {
        "project_prepaid_billing_anchor_for_invoice",
        "apply_due_prepaid_service_after_funding_change",
        "apply_stale_prepaid_billing_anchor_repair",
        "confirm_prepaid_service_renewal",
        "run_due_prepaid_service_renewals",
    }


def test_reviewed_calendar_reconciler_has_one_named_anchor_repair() -> None:
    assert _assigns_next_billing_at(CALENDAR_RECONCILER) == {
        "operation",
        "reconcile_prepaid_billing_calendar",
    }
    source = CALENDAR_RECONCILER.read_text(encoding="utf-8")
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
