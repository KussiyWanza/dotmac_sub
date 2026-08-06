"""One owner for PON port identity, and no writer that invents a name.

Two writers used to synthesise ``pon-{port_number}`` when no name was supplied,
which is how 147 production rows acquired an identity nobody chose. A behaviour
test cannot stop a third from appearing -- a new fallback reads perfectly
sensibly at its own call site. This guard fails the build instead.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
OWNER = APP / "services" / "network" / "pon_port_identity.py"

#: The literal shape of the retired fallback, in an f-string or a concatenation.
_FABRICATED = re.compile(r"""["']pon-["']|f["']pon-\{""", re.IGNORECASE)


def _python_files() -> list[Path]:
    return [p for p in APP.rglob("*.py") if p.is_file()]


def test_no_module_fabricates_a_pon_name() -> None:
    """A generated name is indistinguishable from a real one once written."""
    offenders = []
    for path in _python_files():
        if path == OWNER:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue
            # A comparison or a strip is fine -- defensive prefix handling stays
            # until the repair slice has verified the data. Assignment is not.
            if _FABRICATED.search(line) and re.search(
                r"=\s*f?[\"']pon-|return\s+f?[\"']pon-", line
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")

    assert not offenders, (
        "These modules fabricate a PON port name instead of deriving it from "
        f"network.pon_port_identity: {offenders}. A port number alone does not "
        "identify a port, and an invented name cannot later be told apart from "
        "a real one."
    )


def test_the_owner_exports_the_identity_contract() -> None:
    tree = ast.parse(OWNER.read_text(encoding="utf-8"))
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}

    assert {"derive_identity", "assert_assignable", "canonical_name"} <= names
    assert {"PonPortIdentity", "PonPortIdentityError"} <= classes


def test_the_crud_helper_consumes_the_owner() -> None:
    source = (APP / "services" / "network" / "olt_crud_common.py").read_text(
        encoding="utf-8"
    )

    assert "derive_from_card_port" in source


def test_the_assignment_path_refuses_unidentifiable_pon_rows() -> None:
    """The guard belongs on the command path, not in a route handler."""
    source = (APP / "services" / "network" / "ont_assignment_commands.py").read_text(
        encoding="utf-8"
    )

    assert "assert_assignable" in source


def test_the_ambiguity_guard_scopes_competitors_to_active_rows() -> None:
    """Preserved inactive history must not become current identity authority."""
    tree = ast.parse(OWNER.read_text(encoding="utf-8"))
    guard = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "assert_assignable"
    )

    assert "PonPort.is_active.is_(True)" in ast.unparse(guard)


def test_the_create_schema_no_longer_invents_a_name() -> None:
    source = (APP / "schemas" / "network.py").read_text(encoding="utf-8")

    assert 'self.name = f"pon-' not in source
