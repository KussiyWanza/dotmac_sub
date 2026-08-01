"""Architecture guard: only the assignment owner moves the served-address marker.

`IPAssignment.is_primary` records which held address RADIUS serves as
`Framed-IP-Address`. That is an ownership decision, so it has exactly one
writer: `network.ip_assignment_lifecycle`.

The failure this prevents is quiet. A service may legitimately hold several
addresses, so any adapter that sets the flag — a generic CRUD payload, an admin
form, an importer — can move a live customer's served address while looking
like it is only recording an allocation. The flag being writable through a
schema is enough: no adapter has to intend it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/ip_assignment_lifecycle.py"
GENERIC_CRUD = ROOT / "app/services/network/ip.py"
SCHEMAS = ROOT / "app/schemas/network.py"
MODEL = ROOT / "app/models/network.py"

#: Modules permitted to assign `is_primary`. Only the owner. This set may
#: SHRINK without discussion; growing it means a second writer to the
#: served-address decision and needs an architecture decision, not a green diff.
_PERMITTED_WRITERS = {"app/services/ip_assignment_lifecycle.py"}

_ASSIGN_PATTERN = re.compile(r"\.is_primary\s*=(?!=)")
_PAYLOAD_PATTERN = re.compile(r"['\"]is_primary['\"]\s*:")

#: Modules that write an `is_primary` belonging to a DIFFERENT model. The name
#: is common in this schema -- postal addresses and contact channels both carry
#: one -- and a pattern tight enough to tell them apart by text alone would be
#: tighter than the risk warrants. Listing them keeps the scan strict, so a
#: genuinely new IPAssignment writer still trips it and has to be triaged here
#: with a reason rather than silently matching a loosened regex.
_UNRELATED_IS_PRIMARY = {
    "app/services/subscriber.py": "Address.is_primary (primary postal address)",
    "app/services/web_customer_details.py": "contact channel is_primary",
}


def _python_sources() -> list[Path]:
    return [
        path for path in (ROOT / "app").rglob("*.py") if "__pycache__" not in path.parts
    ]


def test_only_the_owner_assigns_the_primary_marker() -> None:
    """Scoped to modules that touch IPAssignment.

    `is_primary` is a common field name across this codebase -- primary
    contact, primary email, primary POP site -- so an unscoped scan reports
    nine unrelated modules. A module can only write IPAssignment.is_primary if
    it references IPAssignment at all, which makes that the honest filter.
    """
    offenders = []
    for path in _python_sources():
        rel = path.relative_to(ROOT).as_posix()
        if rel in _PERMITTED_WRITERS or rel == "app/models/network.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "IPAssignment" not in source or rel in _UNRELATED_IS_PRIMARY:
            continue
        if _ASSIGN_PATTERN.search(source) or _PAYLOAD_PATTERN.search(source):
            offenders.append(rel)

    assert not offenders, (
        "`is_primary` may only be assigned by network.ip_assignment_lifecycle; "
        "these modules would become a second writer of the served-address "
        "decision:\n  " + "\n  ".join(sorted(offenders))
    )


def test_the_owner_exposes_a_single_marker_writer() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert "def mark_primary_ipv4_assignment(" in source
    # Demote-then-promote, because the partial unique index permits one active
    # primary per service and the reverse order violates it mid-statement.
    body = source[source.index("def mark_primary_ipv4_assignment(") :]
    body = body[: body.index("\ndef ", 1)]
    assert body.index("item.is_primary = False") < body.index(
        "target.is_primary = True"
    )
    assert "db.flush()" in body


def test_the_generic_crud_reads_the_marker_but_never_writes_it() -> None:
    source = GENERIC_CRUD.read_text(encoding="utf-8")

    # Reads it, to avoid re-addressing a customer on an additional holding...
    assert "if not assignment.is_primary:" in source
    # ...and never assigns it.
    assert not _ASSIGN_PATTERN.search(source)


def test_the_marker_is_not_writable_through_the_generic_api() -> None:
    """Readable on the way out, absent on the way in."""
    tree = ast.parse(SCHEMAS.read_text(encoding="utf-8"))
    fields: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            fields[node.name] = {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }

    assert "is_primary" not in fields.get("IPAssignmentBase", set())
    assert "is_primary" not in fields.get("IPAssignmentUpdate", set())
    assert "is_primary" in fields.get("IPAssignmentRead", set())


def test_the_constraint_permits_several_holdings_and_one_primary() -> None:
    """The invariant must not regress into forbidding the supported shape."""
    source = MODEL.read_text(encoding="utf-8")

    assert "uq_ip_assignments_primary_ipv4_active" in source
    assert "is_active AND is_primary AND ipv4_address_id IS NOT NULL" in source
    # A bare unique index on subscription_id would forbid holding several.
    assert '"uq_ip_assignments_subscription_ipv4_active"' not in source
