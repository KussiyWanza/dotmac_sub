"""Deactivating an authentication principal must close its access.

Setting `SystemUser.is_active = False` is the FLAG. The consequence — every
credential mechanism deactivated, every live session revoked — is owned by
`staff_provisioning.close_principal_access`. A writer that flips the flag
without invoking the consequence leaves an authenticable account behind: no
membership, no active principal, and a still-usable credential.

That is not hypothetical. `vendor_user_provisioning.revoke_vendor_user` did
exactly this until the commit that added this guard, and its own docstring
warned about "the same class of half-revocation" one level up while leaving
this one open.

The scan resolves names bound from a `SystemUser` lookup and then flags
assignments to `<name>.is_active`, rather than matching every `.is_active` in
the tree — `app/` carries ~60 of those across roles, tasks, surveys and
memberships, and a guard that shouts about all of them would be ignored.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP = PROJECT_ROOT / "app"

#: The consequence owner, and the one module allowed to flip the flag without
#: calling it — because it IS the call.
CONSEQUENCE = "close_principal_access"
OWNER = Path("app/services/staff_provisioning.py")


def _system_user_names(tree: ast.AST, source: str) -> set[str]:
    """Names bound from a SystemUser lookup in this module."""

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        segment = ast.get_source_segment(source, node.value) or ""
        if "SystemUser" not in segment and "_locked_user" not in segment:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _reportable(path: Path) -> Path:
    """Name a file relative to the repo when it lives there.

    The canaries below scan a planted tree under `tmp_path`, which is not under
    PROJECT_ROOT. Calling `relative_to` unconditionally raises ValueError there,
    killing every canary that expects a DETECTION before it can assert — the
    exact defect the transaction-initialization guard shipped with.
    """

    try:
        return path.relative_to(PROJECT_ROOT)
    except ValueError:
        return path


def _offenders(root: Path = APP) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "SystemUser" not in source:
            continue
        relative = _reportable(path)
        tree = ast.parse(source)
        principals = _system_user_names(tree, source)
        if not principals:
            continue
        invokes_consequence = CONSEQUENCE in source
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "is_active"
                    and isinstance(target.value, ast.Name)
                    and target.value.id in principals
                    and not invokes_consequence
                ):
                    found.setdefault(str(relative), []).append(node.lineno)
    return found


def test_no_writer_deactivates_a_principal_without_closing_its_access() -> None:
    offenders = _offenders()

    assert offenders == {}, (
        "these modules write SystemUser.is_active without calling "
        f"staff_provisioning.{CONSEQUENCE}, leaving credentials active and "
        f"sessions live on a deactivated principal: {offenders}"
    )


def test_the_owner_still_owns_the_consequence() -> None:
    """A two-directional ratchet: the allowlist must not outlive its reason.

    If `close_principal_access` is renamed or moved, the scan above silently
    passes for every module — nothing calls a function that no longer exists,
    but nothing flips the flag either, so `_offenders` returns empty for the
    wrong reason.
    """

    owner_source = (PROJECT_ROOT / OWNER).read_text(encoding="utf-8")

    assert f"def {CONSEQUENCE}(" in owner_source, (
        f"{OWNER} no longer defines {CONSEQUENCE}; this guard is measuring nothing"
    )


def test_the_guard_detects_a_planted_half_revocation(tmp_path: Path) -> None:
    """Sensitivity proof: a guard that cannot fail is not a guard."""

    planted = tmp_path / "app"
    planted.mkdir()
    (planted / "offender.py").write_text(
        "from app.models.system_user import SystemUser\n"
        "def revoke(db, user_id):\n"
        "    principal = db.get(SystemUser, user_id)\n"
        "    principal.is_active = False\n"
        "    db.flush()\n",
        encoding="utf-8",
    )

    assert _offenders(root=planted)


def test_the_guard_accepts_a_writer_that_closes_access(tmp_path: Path) -> None:
    """The other direction: invoking the consequence clears the module.

    Without this, a detector that flagged every writer unconditionally would
    look identical to a correct one on the offending case.
    """

    planted = tmp_path / "app"
    planted.mkdir()
    (planted / "compliant.py").write_text(
        "from app.models.system_user import SystemUser\n"
        "from app.services import staff_provisioning\n"
        "def revoke(db, user_id):\n"
        "    principal = db.get(SystemUser, user_id)\n"
        "    principal.is_active = False\n"
        "    staff_provisioning.close_principal_access(db, principal.id)\n",
        encoding="utf-8",
    )

    assert _offenders(root=planted) == {}
