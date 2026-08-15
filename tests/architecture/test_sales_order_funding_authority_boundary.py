"""Keep the sales-order funding escape hatch closed to everyone but its owner.

``sales_orders.assert_funding_authority`` refuses ``payment_status``,
``amount_paid`` and ``paid_at`` on a generic order edit. The refusal is lifted
by passing ``funding_authority=``, which makes that keyword the single most
security-relevant argument in the sales surface: whoever can pass it can create
a service contract without money arriving.

Three properties keep it honest, and each is asserted below.

1. It is not request-derived. No schema, model or route exposes it, so no
   payload, query string or form field can reach it.
2. Only the owning settlement/funding workflow may pass it. The allowlist is
   this file.
3. Its value is a ``FundingAuthority`` member, not a boolean or a string, so a
   caller cannot satisfy it with a generic truthy flag.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.services.sales_orders import (
    FUNDING_CONTROLLED_FIELDS,
    FundingAuthority,
    assert_funding_authority,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP = PROJECT_ROOT / "app"
OWNER = APP / "services" / "sales_orders.py"

#: Modules permitted to lift the funding refusal.
#:
#: ``sales_orders.py`` is the owner: it defines the guard and threads the
#: keyword through its own ``create``/``update``. Nothing else is listed yet —
#: the authoritative deposit path in ``sales/selfserve.py`` writes the order
#: directly rather than through the generic edit, so it never needs the hatch.
#:
#: Adding a module here is a security decision. It means that module has
#: independently confirmed money arrived. Do not add an adapter, a route, a
#: task or anything that takes its cue from a request.
ALLOWED_CALLERS: frozenset[str] = frozenset(
    {
        "app/services/sales_orders.py",
    }
)


def _modules_passing_funding_authority() -> set[str]:
    """Every file under ``app/`` that passes ``funding_authority=`` to a call."""
    found: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - a parse failure is a real break
            raise
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "funding_authority":
                    found.add(path.relative_to(PROJECT_ROOT).as_posix())
    return found


def test_only_the_owning_workflow_can_lift_the_funding_refusal():
    passers = _modules_passing_funding_authority()
    unexpected = passers - ALLOWED_CALLERS
    assert not unexpected, (
        "These modules lift the sales-order funding refusal but are not "
        f"allowlisted: {sorted(unexpected)}. Passing funding_authority asserts "
        "that money independently arrived. If that is genuinely true, add the "
        "module to ALLOWED_CALLERS in this file with a reviewer; if it is a "
        "request-shaped caller, it must record settlement instead."
    )


def test_the_allowlist_has_no_dead_entries():
    """A shrink-only ratchet in both directions (ADR-0018).

    An allowlist that outlives its callers silently widens the permitted set,
    so an entry that no longer passes the keyword must be removed.
    """
    passers = _modules_passing_funding_authority()
    dead = ALLOWED_CALLERS - passers
    assert not dead, (
        f"ALLOWED_CALLERS lists modules that no longer pass funding_authority: "
        f"{sorted(dead)}. Remove them — a stale entry is a standing permission."
    )


def test_the_detector_actually_finds_a_caller():
    """Sensitivity proof.

    Both assertions above pass trivially if the AST scan finds nothing. This
    proves the scan detects a real call site, so an empty result means "no
    unauthorised caller" rather than "the detector is broken".
    """
    assert _modules_passing_funding_authority(), (
        "The funding_authority scan found no call sites at all. The guard is "
        "not being measured — fix the detector before trusting the tests above."
    )


#: Word-boundary match, so the unrelated
#: ``uq_prepaid_funding_authority_cutover`` constraint name in
#: ``app/models/prepaid_funding.py`` is not a hit. A substring scan reports it
#: and trains the reader to ignore this test.
_IDENTIFIER = re.compile(r"\bfunding_authority\b")


def test_funding_authority_is_not_request_shaped():
    """No schema, model or route field can carry the hatch in from outside."""
    exposed: list[str] = []
    for area in ("schemas", "models", "api", "web"):
        for path in sorted((APP / area).rglob("*.py")):
            if _IDENTIFIER.search(path.read_text(encoding="utf-8")):
                exposed.append(path.relative_to(PROJECT_ROOT).as_posix())
    assert not exposed, (
        "funding_authority appears in a request-facing layer: "
        f"{exposed}. It must never be settable from a payload, query string "
        "or form field."
    )


def test_the_request_shape_detector_distinguishes_the_identifier():
    """Sensitivity proof for the regex above.

    It must match the real keyword and must not match the substring inside an
    unrelated constraint name — otherwise the test above is either blind or
    permanently red for the wrong reason.
    """
    assert _IDENTIFIER.search("funding_authority=FundingAuthority.settlement")
    assert not _IDENTIFIER.search("uq_prepaid_funding_authority_cutover")


def test_the_owner_declares_the_protected_fields():
    """The guarded set is the three that assert money arrived — and not more.

    ``total`` is deliberately absent: changing what an order is worth is a real
    sales edit, and coverage stays derived from it.
    """
    assert FUNDING_CONTROLLED_FIELDS == {"payment_status", "amount_paid", "paid_at"}
    assert OWNER.exists()


def test_a_truthy_non_member_cannot_satisfy_the_hatch():
    """A generic boolean or string must not lift the refusal."""
    forged = {"payment_status": "paid"}
    for impostor in (True, 1, "settlement", "yes", object()):
        with pytest.raises(TypeError):
            assert_funding_authority(forged, funding_authority=impostor)

    # The real member still works, so the check above is not simply refusing
    # everything.
    assert_funding_authority(forged, funding_authority=FundingAuthority.settlement)


# ---------------------------------------------------------------------------
# The waiver owner is on the other side of the same boundary
# ---------------------------------------------------------------------------

WAIVER_OWNER = APP / "services" / "sales_orders.py"

#: The waiver definitions inside the sales-orders module. The waiver lives in
#: `sales.orders` because the SOT registry maps one owner to exactly one
#: module — so this test cannot scan the whole file, which legitimately handles
#: coverage elsewhere. It scans exactly the waiver's own definitions.
_WAIVER_DEFINITIONS = ("SalesOrderWaivers", "active_waiver", "_grant_fingerprint")

#: Names that would mean the waiver had become a payment.
_FORBIDDEN_IN_WAIVER = (
    "payment_status",
    "amount_paid",
    "paid_at",
    "funding_authority",
    "stage_funding_transition",
    "funding_satisfied",
)


def _strip_docstrings(node: ast.AST) -> ast.AST:
    for child in ast.walk(node):
        if isinstance(
            child, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(child, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    return node


def _waiver_code() -> str:
    """Source of the waiver definitions only, with docstrings removed."""
    tree = ast.parse(
        WAIVER_OWNER.read_text(encoding="utf-8"), filename=str(WAIVER_OWNER)
    )
    found = [
        node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        and node.name in _WAIVER_DEFINITIONS
    ]
    assert len(found) == len(_WAIVER_DEFINITIONS), (
        "The waiver definitions moved or were renamed. Expected "
        f"{sorted(_WAIVER_DEFINITIONS)}, found {sorted(n.name for n in found)}. "
        "Fix this list — a scan that finds nothing proves nothing."
    )
    return "\n".join(ast.unparse(_strip_docstrings(node)) for node in found)


def test_the_waiver_owner_never_becomes_a_payment():
    """A waiver must not touch coverage, funding or the escape hatch.

    This is the whole point of splitting waiver out of ``payment_status``: a
    waived order was NOT paid, so nothing downstream may treat it as funded.
    """
    code = _waiver_code()
    leaked = [name for name in _FORBIDDEN_IN_WAIVER if name in code]
    assert not leaked, (
        f"The waiver definitions reference {leaked}. A waiver records a "
        "decision not to pursue an amount; it never asserts coverage, never "
        "stages funding, and never lifts the funding refusal."
    )


def test_the_waiver_scan_reads_real_code():
    """Sensitivity proof.

    The test above passes trivially if the extraction returns nothing, and the
    surrounding module DOES contain every forbidden term — so this pins that
    real waiver code was extracted and that the extraction is narrower than
    the file.
    """
    code = _waiver_code()
    assert "def grant" in code and "SalesOrderWaiver" in code
    whole_file = WAIVER_OWNER.read_text(encoding="utf-8")
    assert "payment_status" in whole_file, (
        "The sales-orders module no longer mentions payment_status at all, so "
        "the scan above is no longer discriminating anything."
    )
