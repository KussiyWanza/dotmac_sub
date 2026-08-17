"""Only the approved projection owner may populate a legacy session Party.

New-session construction remains with ``auth_flow`` and supplies the complete
bound pair at creation. This guard covers later mutation of an existing session:
assignment or bulk update must stay inside the named projection owner.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP = PROJECT_ROOT / "app"
OWNER = Path("app/services/staff_session_party_adoption.py")


def _reportable(path: Path) -> Path:
    try:
        return path.relative_to(PROJECT_ROOT)
    except ValueError:
        return path


def _writes_existing_session_party(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    source = path.read_text(encoding="utf-8")
    session_model_visible = "AuthSession" in source or "models.auth.Session" in source
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "party_id"
                    and isinstance(target.value, ast.Name)
                    and "session" in target.value.id.lower()
                ):
                    lines.append(node.lineno)
        if (
            session_model_visible
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "values"
            and any(keyword.arg == "party_id" for keyword in node.keywords)
        ):
            lines.append(node.lineno)
    return sorted(set(lines))


def _offenders(root: Path = APP) -> dict[str, list[int]]:
    offenders: dict[str, list[int]] = {}
    for path in sorted(root.rglob("*.py")):
        if _reportable(path) == OWNER:
            continue
        lines = _writes_existing_session_party(path)
        if lines:
            offenders[str(_reportable(path))] = lines
    return offenders


def test_only_the_projection_owner_mutates_existing_session_party() -> None:
    owner_source = (PROJECT_ROOT / OWNER).read_text(encoding="utf-8")

    assert "auth_session.party_id = party.id" in owner_source
    assert _offenders() == {}


def test_the_writer_guard_detects_assignment_and_bulk_update(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    assignment = app / "assignment.py"
    assignment.write_text(
        "def bypass(session, party_id):\n    session.party_id = party_id\n",
        encoding="utf-8",
    )
    bulk = app / "bulk.py"
    bulk.write_text(
        "from app.models.auth import Session as AuthSession\n"
        "def bypass(db, party_id):\n"
        "    db.execute(update(AuthSession).values(party_id=party_id))\n",
        encoding="utf-8",
    )

    assert _offenders(app) == {
        str(assignment): [2],
        str(bulk): [3],
    }


def test_the_writer_guard_accepts_new_session_bound_pair_construction(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    constructor = app / "constructor.py"
    constructor.write_text(
        "from app.models.auth import Session as AuthSession\n"
        "def mint(system_user_id, party_id):\n"
        "    return AuthSession(system_user_id=system_user_id, party_id=party_id)\n",
        encoding="utf-8",
    )

    assert _offenders(app) == {}
