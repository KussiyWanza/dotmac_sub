"""Staff authentication resolves through one named owner — stage 1.

Four entry points authenticate a staff principal: login, refresh, per-request
session validation, and vendor admission. Before the cutover each resolved the
principal itself, straight from `credential.system_user_id` or
`session.system_user_id`. That is four authorities wearing one name.

This guard fixes the shape for deploy 1:

- all four call `staff_party_authentication`
- `resolve_staff_principal_assertion` — the ONE-DEPLOY compatibility bridge —
  is referenced only by the owner that defines it; readers use the typed
  session resolver, so the bridge cannot spread while it exists
- nothing outside the owner resolves a staff principal by handing
  `credential.system_user_id` or `session.system_user_id` to `db.get`

Deploy 2 strengthens this: once `sessions.party_id` is backfilled and required,
`resolve_staff_principal_assertion` is deleted and
`test_the_compatibility_bridge_is_confined` becomes a test that it does not
exist at all. The bridge is temporary by construction, not by intention.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP = PROJECT_ROOT / "app"

OWNER_MODULE = "staff_party_authentication"
OWNER_PATH = Path("app/services/staff_party_authentication.py")
BRIDGE = "resolve_staff_principal_assertion"

#: The FOUR staff authentication entry points, as (module, function). Named
#: individually so deleting one is a failure, not a silent pass. The vendor one
#: was found by this guard's legacy-key scan, not by reading the auth services —
#: it lives under `field/` and resolves a staff principal to decide vendor
#: admission. Vendor ELIGIBILITY remains owned by that module; only IDENTITY
#: resolution moved to the shared resolver.
ENTRY_POINTS = (
    (Path("app/services/auth_flow.py"), "_principal_for_credential"),
    (Path("app/services/auth_flow.py"), "refresh"),
    (Path("app/services/auth_flow.py"), "validate_active_session"),
    (Path("app/services/field/vendor_auth.py"), "resolve_vendor_login_eligibility"),
)

#: Legacy resolution: handing a principal foreign key straight to `db.get`.
LEGACY_RESOLUTION = re.compile(
    r"db\.get\(\s*SystemUser\s*,\s*(?:credential|session)\.system_user_id",
)


def _reportable(path: Path) -> Path:
    try:
        return path.relative_to(PROJECT_ROOT)
    except ValueError:
        return path


def _modules_referencing(name: str, root: Path = APP) -> set[str]:
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if name in source:
            found.add(str(_reportable(path)))
    return found


def _legacy_resolvers(root: Path = APP) -> dict[str, list[int]]:
    offenders: dict[str, list[int]] = {}
    for path in sorted(root.rglob("*.py")):
        relative = _reportable(path)
        if relative == OWNER_PATH:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if LEGACY_RESOLUTION.search(line):
                offenders.setdefault(str(relative), []).append(number)
    return offenders


def test_the_owner_defines_the_party_primitive_and_transition_entry_points() -> None:
    """A rename must fail here rather than silently empty every other check."""

    source = (PROJECT_ROOT / OWNER_PATH).read_text(encoding="utf-8")

    assert "def resolve_staff_principal_by_party(" in source
    assert "def resolve_staff_principal(" in source
    assert "def resolve_staff_session_principal(" in source
    assert f"def {BRIDGE}(" in source


def test_every_staff_entry_point_delegates_to_the_owner() -> None:
    """All four staff authentication entry points call the named owner.

    Checked per function, so removing one call fails even while the other three
    keep the module import alive.
    """

    missing: list[str] = []
    undelegated: list[str] = []
    for module_path, function_name in ENTRY_POINTS:
        tree = ast.parse((PROJECT_ROOT / module_path).read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        node = functions.get(function_name)
        if node is None:
            missing.append(f"{module_path}::{function_name}")
            continue
        # Attribute access on the owner module shows up in the dump.
        if OWNER_MODULE not in ast.dump(node):
            undelegated.append(f"{module_path}::{function_name}")

    assert not missing, (
        f"these entry points no longer exist, so this guard is measuring "
        f"nothing for them: {missing}"
    )
    assert undelegated == [], (
        f"these staff authentication entry points resolve a principal without "
        f"{OWNER_MODULE}: {undelegated}"
    )


def test_the_compatibility_bridge_is_confined() -> None:
    """The assertion-first bridge must not spread while it exists.

    It is a one-deploy allowance for sessions that predate `sessions.party_id`.
    Deploy 2 deletes it; until then only the owner and the reader may name it.
    """

    referencing = _modules_referencing(BRIDGE)

    assert referencing <= {str(OWNER_PATH)}, (
        f"{BRIDGE} is the temporary compatibility bridge and must stay confined "
        f"to the owner; also referenced by {referencing - {str(OWNER_PATH)}}"
    )


def test_no_module_resolves_a_staff_principal_from_the_legacy_key() -> None:
    offenders = _legacy_resolvers()

    assert offenders == {}, (
        "these modules resolve a staff principal by handing the legacy foreign "
        f"key to db.get instead of {OWNER_MODULE}: {offenders}"
    )


#: The primitive. Identity in, staff context out — the only correct query
#: direction for a session that carries a Party.
PRIMITIVE = "resolve_staff_principal_by_party"
CREDENTIAL_RESOLVER = "resolve_staff_principal"
SESSION_RESOLVER = "resolve_staff_session_principal"

#: Functions that must reach the primitive, because they resolve a principal
#: for a subject that carries `party_id`.
PARTY_KEYED = (
    (Path("app/services/auth_flow.py"), "validate_active_session"),
    (Path("app/services/auth_flow.py"), "refresh"),
    (Path("app/services/auth_flow.py"), "_issue_tokens"),
    (Path("app/services/auth_flow.py"), "mfa_verify"),
    (Path("app/services/auth_flow.py"), "_principal_for_credential"),
)


def _resolves_party_keyed(source: str, function_name: str) -> bool:
    """Does this function reach a resolver whose query starts at Party?"""

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == function_name
        ):
            owner_calls = {
                call.func.attr
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == OWNER_MODULE
            }
            return bool(
                owner_calls & {PRIMITIVE, CREDENTIAL_RESOLVER, SESSION_RESOLVER}
            )
    return False


def test_party_carrying_subjects_resolve_through_the_primitive() -> None:
    """Query direction, not just delegation.

    A session with `party_id` must be resolved BY that Party. Falling back to
    assertion-first here would still delegate to the owner, still return the
    right principal on healthy data, and still pass every parity test — while
    quietly restoring the legacy key as the authority.
    """

    undirected = [
        f"{module}::{name}"
        for module, name in PARTY_KEYED
        if not _resolves_party_keyed(
            (PROJECT_ROOT / module).read_text(encoding="utf-8"), name
        )
    ]

    assert undirected == [], (
        f"these resolve a Party-carrying subject without {PRIMITIVE}, which "
        f"leaves the legacy key authoritative: {undirected}"
    )


def test_the_guard_detects_assertion_first_regression() -> None:
    """Direction-sensitivity proof: plant the exact regression.

    This is the one an ordinary parity test cannot make. Both directions return
    the same principal on healthy data, so comparing answers proves nothing
    about which key was authoritative. Only planting the reversed direction and
    showing the guard rejects it establishes that.
    """

    regressed = (
        "def refresh(db, session):\n"
        "    # the exact regression: party_id ignored, legacy key resolves\n"
        "    return staff_party_authentication.resolve_staff_principal_assertion(\n"
        "        db, session.system_user_id\n"
        "    )\n"
    )

    assert not _resolves_party_keyed(regressed, "refresh"), (
        "the guard accepted assertion-first resolution for a Party-carrying "
        "session — it cannot tell query direction, so it certifies nothing"
    )

    correct = (
        "def refresh(db, session):\n"
        "    return staff_party_authentication.resolve_staff_session_principal(\n"
        "        db, party_id=session.party_id, system_user_id=session.system_user_id\n"
        "    )\n"
    )

    assert _resolves_party_keyed(correct, "refresh"), (
        "the guard rejected correct Party-keyed resolution"
    )


def test_the_guard_detects_a_planted_legacy_resolver(tmp_path: Path) -> None:
    """Sensitivity proof: a guard that cannot fail is not a guard."""

    planted = tmp_path / "app"
    planted.mkdir()
    (planted / "offender.py").write_text(
        "def authenticate(db, credential):\n"
        "    return db.get(SystemUser, credential.system_user_id)\n",
        encoding="utf-8",
    )

    assert _legacy_resolvers(root=planted)


def test_the_guard_detects_the_session_shaped_variant(tmp_path: Path) -> None:
    """The other spelling of the same defect.

    Validation resolves from a session, not a credential. A detector that only
    knew the credential spelling would certify the path that carries 1,240 live
    sessions.
    """

    planted = tmp_path / "app"
    planted.mkdir()
    (planted / "offender.py").write_text(
        "def validate(db, session):\n"
        "    return db.get(SystemUser, session.system_user_id)\n",
        encoding="utf-8",
    )

    assert _legacy_resolvers(root=planted)


def test_the_guard_accepts_delegation(tmp_path: Path) -> None:
    """Both directions: a compliant module is not flagged."""

    planted = tmp_path / "app"
    planted.mkdir()
    (planted / "compliant.py").write_text(
        "from app.services import staff_party_authentication\n"
        "def authenticate(db, credential):\n"
        "    return staff_party_authentication.resolve_staff_principal(db, credential)\n",
        encoding="utf-8",
    )

    assert _legacy_resolvers(root=planted) == {}
