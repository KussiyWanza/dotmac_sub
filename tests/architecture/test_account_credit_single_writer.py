"""Unallocated account credit comes into existence in exactly one place.

An ``entry_type=credit, invoice_id=NULL`` ledger row IS account credit. Whoever
writes one has created money the customer can spend against their receivables,
and the consequence — offering it to the account's open invoices — belongs to
the account-credit owner.

The owner previously had a consume half and a read half but no creation half,
so callers reached past it to the generic ledger writer. The rule "offer newly
created credit" then had nowhere to live except replicated at each call site,
which is how it got missed: only the deposit path knew about it, and 27
accounts ended up holding credit while being dunned on an open invoice.

A comment cannot hold that line — the next caller does not read it. This does.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
BASELINE = Path(__file__).with_name("unallocated_credit_writer_baseline.txt")

OWNER_MODULE = "app/services/billing/account_credit.py"


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _may_be_credit(value: ast.expr | None) -> bool:
    """Could this entry be a credit?

    Deliberately conservative. A literal ``LedgerEntryType.credit`` is decided;
    a computed ``entry_type=preview.ledger_entry_type`` is not, and a guard that
    only catches the literal spelling is one refactor away from catching
    nothing. Unknown counts as yes, so evading it takes a baseline edit somebody
    has to justify.
    """
    if value is None:
        return False  # entry_type is required; this is not a ledger construction
    if isinstance(value, ast.Attribute):
        return value.attr == "credit"
    return True


def _may_be_unallocated(call: ast.Call) -> bool:
    """Could this entry have a NULL invoice link?

    An invoice-linked credit is a receivable reduction on that one invoice, not
    spendable account credit, so it is not this rule's business — but only when
    the link is provably present. ``invoice_id=invoice.id if invoice else None``
    is not, so it counts.
    """
    invoice_id = _keyword(call, "invoice_id")
    if invoice_id is None:
        return True
    if isinstance(invoice_id, ast.Constant):
        return invoice_id.value is None
    return True


def _constructs_unallocated_credit(call: ast.Call) -> bool:
    func = call.func
    direct = isinstance(func, ast.Name) and func.id in {
        "LedgerEntry",
        "LedgerEntryCreate",
    }
    staged = (
        isinstance(func, ast.Attribute)
        and func.attr == "create"
        and isinstance(func.value, ast.Name)
        and func.value.id == "LedgerEntries"
    )
    if not (direct or staged):
        return False
    return _may_be_credit(_keyword(call, "entry_type")) and _may_be_unallocated(call)


def _writers() -> set[str]:
    found: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - a parse failure is a lint problem
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _constructs_unallocated_credit(node):
                found.add(path.relative_to(ROOT).as_posix())
                break
    return found


def _baseline() -> set[str]:
    return {
        line.strip()
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def test_the_owner_is_a_writer() -> None:
    """If the owner stops minting, this guard is measuring the wrong thing."""
    assert OWNER_MODULE in _writers()


def test_no_new_module_mints_unallocated_account_credit() -> None:
    new = _writers() - _baseline()
    assert not new, (
        "These modules create unallocated account credit outside the "
        "account-credit owner:\n  "
        + "\n  ".join(sorted(new))
        + "\n\nCall AccountCreditApplications.record_credit instead. It mints the "
        "ledger evidence and offers the credit to the account's open receivables "
        "in one transaction, so the credit cannot be stranded. Minting here "
        "leaves the owner unaware the credit exists and overstates the "
        "receivable for as long as it goes unoffered."
    )


def test_baseline_shrinks_only() -> None:
    """Retired writers must be removed from the baseline, not left to rot."""
    stale = _baseline() - _writers()
    assert not stale, (
        "These modules no longer mint unallocated account credit; remove them "
        "from the baseline so it keeps meaning what it says:\n  "
        + "\n  ".join(sorted(stale))
    )
