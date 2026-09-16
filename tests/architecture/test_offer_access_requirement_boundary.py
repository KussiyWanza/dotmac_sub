"""Architecture guards for service_intent.offer_access_requirement.

Sensitivity is proven both ways using an ISOLATED temp directory (never the
real source tree — planting files into the live repo is unsafe under this
repo's 4-worker parallel test execution, since another worker's clean-tree
scan could observe the planted file mid-test): a planted leak into forbidden
territory must be caught, and an unrelated near-miss must not be flagged.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.rbac_catalog import _PERMISSION_KEY_PATTERN
from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]

# Every location allowed to reference the guarded tokens (the model enum
# class name and the field/column name) as a real field/column/permission
# concept.
_ALLOWED_PATHS = (
    "app/services/catalog/offer_access_requirement.py",
    "app/models/catalog.py",
    "app/schemas/catalog.py",
    "app/services/catalog/offers.py",
    "app/api/catalog.py",
    "app/services/sot_registry/domains/service_intent_control_plane.py",
    "app/services/events/types.py",
    "alembic/versions/608_offer_access_requirement.py",
    "alembic/versions/609_offer_access_requirement_classify_permission.py",
    "alembic/versions/610_offer_version_admission_permission.py",
    "alembic/versions/611_offer_versions_unique_version_number.py",
    "scripts/catalog/classify_offer_access_requirement.py",
    "docs/SOT_RELATIONSHIP_MAP.md",
    "docs/designs/CATALOG_ACCESS_REQUIREMENT_AUTHORITY.md",
)

# Directories that must NEVER mention either guarded token, per the brief:
# connection-type/PPPoE/RADIUS/enforcement/missing_login code. Checked
# explicitly (not just implied by _ALLOWED_PATHS) so this list is a positive,
# exercised assertion rather than a declared-but-unused constant.
_FORBIDDEN_PATHS = (
    "app/models/network.py",
    "app/services/radius.py",
    "app/services/catalog/radius.py",
    "app/services/network",
    "app/services/provisioning",
)

#: The class-name token and the lowercase field/column-name token. Both are
#: guarded — a leak can show up as either an import of the enum type or a
#: raw attribute/column/string reference to the field it backs.
_GUARDED_TOKENS = ("AccessRequirement", "access_requirement")

#: The specific cross-domain vocabulary a forbidden fallback would need to
#: reference: the exact regression the brief names — an access-requirement-
#: to-PPPoE/connection-type fallback hiding INSIDE an already-allowed file
#: (e.g. near ``ConnectionType``/``pppoe`` in ``app/models/catalog.py``),
#: which the wholesale allowlist skip in ``_find_leaks`` cannot see.
_CROSS_DOMAIN_TOKENS = ("ConnectionType", "pppoe", "PPPoE", "PPPOE")

#: How many lines of slack either side of a guarded-token line still count
#: as "nearby" for the content guard below — wide enough to catch a
#: same-block ``if``/``return`` fallback, narrow enough that unrelated
#: mentions elsewhere in a large allowed file (e.g. a docstring explaining
#: the prohibition, or an unrelated class hundreds of lines away) do not
#: false-positive. Verified against every current allowed ``*.py`` file.
_NEARBY_WINDOW = 3


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _find_leaks(
    root: Path, *, tokens: tuple[str, ...], allowed: tuple[str, ...]
) -> list[str]:
    """Pure scan: every ``*.py`` file under ``root`` outside ``tests/`` and
    ``allowed`` that mentions any of ``tokens``. No side effects, no mutation
    of ``root`` — callers plant fixtures into an isolated directory, never
    the real source tree.
    """

    leaks: list[str] = []
    for path in root.rglob("*.py"):
        if "/.venv/" in str(path) or "/node_modules/" in str(path):
            continue
        relative = str(path.relative_to(root))
        if relative.startswith("tests/") or relative in allowed:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(token in text for token in tokens):
            leaks.append(relative)
    return leaks


def _enclosing_function_span(tree: ast.AST, lineno: int) -> tuple[int, int] | None:
    """The smallest (innermost) function/async-function body containing
    ``lineno`` (1-based), as an inclusive ``(start_line, end_line)`` span, or
    ``None`` if ``lineno`` sits outside every function (e.g. a module-level
    import, class-body field, or top-level constant)."""

    best: tuple[int, int] | None = None
    best_size = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", None) or node.lineno
        if start <= lineno <= end:
            size = end - start
            if best_size is None or size < best_size:
                best = (start, end)
                best_size = size
    return best


def _find_cross_domain_leaks_within_allowed_files(
    root: Path,
    *,
    guarded: tuple[str, ...],
    cross_domain: tuple[str, ...],
    allowed: tuple[str, ...],
    window: int = _NEARBY_WINDOW,
) -> list[str]:
    """Content-level scan INSIDE the allowed files themselves.

    ``_find_leaks`` treats every path in ``allowed`` as entirely out of
    scope — that is correct for the guarded token itself (this is where it
    is meant to live), but it also means a hypothetical
    access-requirement-to-PPPoE/connection-type fallback added inside one of
    those already-allowed files (e.g. near the existing
    ``ConnectionType``/PPPoE definitions in ``app/models/catalog.py``) would
    never be scanned at all.

    For a guarded-token line that sits inside a function/async-function
    body, this checks the ENTIRE enclosing function body for a cross-domain
    token — an AST scope check, not a fixed line-window, so a fallback split
    across an ``if``/``elif`` (condition on one line, the forbidden token
    several lines later in the body) is caught exactly the same as one
    planted on a single line. A guarded-token line with no enclosing
    function (e.g. a class-body field or a top-level import) falls back to
    the narrow ``window``-line check, which is verified against every
    current allowed file's real content: none of them trip this today, and
    such lines are far less likely to hide a derived fallback than a
    function body is.

    Restricted to ``*.py`` allowed paths: the design docs in ``allowed``
    (``docs/...md``) legitimately DISCUSS this exact prohibition in prose
    (e.g. "never treated like PPPoE or any connection-type fallback") and
    are out of scope here, matching ``_find_leaks``'s own ``*.py``-only
    scope.
    """

    leaks: list[str] = []
    for relative in allowed:
        if not relative.endswith(".py"):
            continue
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        guarded_line_indexes = [
            index
            for index, line in enumerate(lines)
            if any(token in line for token in guarded)
        ]
        for index in guarded_line_indexes:
            lineno = index + 1
            span = _enclosing_function_span(tree, lineno) if tree is not None else None
            if span is not None:
                start_line, end_line = span
                scope_text = "\n".join(lines[start_line - 1 : end_line])
            else:
                start = max(0, index - window)
                end = min(len(lines), index + window + 1)
                scope_text = "\n".join(lines[start:end])
            if any(token in scope_text for token in cross_domain):
                leaks.append(f"{relative}:{index + 1}")
    return leaks


def test_access_requirement_reference_is_confined_to_the_owner_and_declared_seams():
    leaks = _find_leaks(ROOT, tokens=_GUARDED_TOKENS, allowed=_ALLOWED_PATHS)
    assert leaks == [], f"guarded token leaked outside its owner: {leaks}"


def test_forbidden_connection_type_and_enforcement_paths_never_mention_it():
    """Positive, exercised use of ``_FORBIDDEN_PATHS`` (not a declared-only
    constant): the exact areas the brief named — connection-type, PPPoE,
    RADIUS, enforcement, missing_login — never reference either token, and
    never treat ``unclassified`` as a connection-type/PPPoE fallback value.
    """

    extra_tokens = _GUARDED_TOKENS + ("network_access", "no_network_access")
    leaks: list[str] = []
    for forbidden in _FORBIDDEN_PATHS:
        target = ROOT / forbidden
        if not target.exists():
            continue
        paths = [target] if target.is_file() else list(target.rglob("*.py"))
        for path in paths:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(token in text for token in extra_tokens):
                leaks.append(str(path.relative_to(ROOT)))
    assert leaks == [], f"forbidden territory references a guarded token: {leaks}"


def test_confinement_guard_catches_a_planted_leak_in_an_isolated_tree(tmp_path):
    """Sensitivity proof: a planted leak is caught — using an isolated temp
    tree, never the real source tree, so no parallel worker can observe it."""

    (tmp_path / "app" / "services" / "network").mkdir(parents=True)
    leaking = tmp_path / "app" / "services" / "network" / "leaky.py"
    leaking.write_text("from app.models.catalog import AccessRequirement\n")

    leaks = _find_leaks(tmp_path, tokens=_GUARDED_TOKENS, allowed=_ALLOWED_PATHS)
    assert "app/services/network/leaky.py" in leaks


def test_confinement_guard_does_not_flag_an_allowed_path_in_an_isolated_tree(
    tmp_path,
):
    """Near-miss proof: a file at an ALLOWED relative path is not flagged
    even though it mentions the guarded token — using an isolated temp tree."""

    (tmp_path / "app" / "services" / "catalog").mkdir(parents=True)
    allowed_file = (
        tmp_path / "app" / "services" / "catalog" / "offer_access_requirement.py"
    )
    allowed_file.write_text("access_requirement = 'unclassified'\n")

    leaks = _find_leaks(tmp_path, tokens=_GUARDED_TOKENS, allowed=_ALLOWED_PATHS)
    assert leaks == []


def test_no_allowed_file_hides_a_connection_type_or_pppoe_fallback():
    """The allowlist skip does not create a blind spot in the real tree:
    none of today's allowed files contain an access-requirement-to-PPPoE/
    connection-type fallback."""

    leaks = _find_cross_domain_leaks_within_allowed_files(
        ROOT,
        guarded=_GUARDED_TOKENS,
        cross_domain=_CROSS_DOMAIN_TOKENS,
        allowed=_ALLOWED_PATHS,
    )
    assert leaks == [], (
        "an access-requirement-to-connection-type/PPPoE fallback leaked "
        f"inside an already-allowed file: {leaks}"
    )


def test_content_guard_catches_a_fallback_planted_inside_an_allowed_file(
    tmp_path,
):
    """Sensitivity proof for the content-level guard: a hypothetical
    access-requirement-to-PPPoE fallback planted INSIDE an already-allowed
    file (mirroring the brief's own example: near ``ConnectionType``/PPPoE
    in ``app/models/catalog.py``) is caught, proving the wholesale
    allowlist skip in ``_find_leaks`` no longer hides this regression."""

    (tmp_path / "app" / "models").mkdir(parents=True)
    planted = tmp_path / "app" / "models" / "catalog.py"
    planted.write_text(
        "class ConnectionType(enum.Enum):\n"
        "    pppoe = 'pppoe'\n"
        "\n"
        "def resolve_default_connection(access_requirement):\n"
        "    fallback = ConnectionType.pppoe if access_requirement == "
        "AccessRequirement.unclassified else None\n"
        "    return fallback\n"
    )

    leaks = _find_cross_domain_leaks_within_allowed_files(
        tmp_path,
        guarded=_GUARDED_TOKENS,
        cross_domain=_CROSS_DOMAIN_TOKENS,
        allowed=("app/models/catalog.py",),
    )
    assert any(leak.startswith("app/models/catalog.py:") for leak in leaks)


def test_content_guard_catches_a_fallback_split_across_an_if_elif_block(
    tmp_path,
):
    """Sensitivity proof for the AST-scope broadening: a fallback whose two
    halves are more than ``_NEARBY_WINDOW`` (3) physical lines apart — the
    guarded token only in an ``if``/``elif`` CONDITION, the cross-domain
    token several lines later in the body — used to pass the old fixed
    line-window check undetected. It must still be caught because both
    tokens live inside the SAME enclosing function."""

    (tmp_path / "app" / "models").mkdir(parents=True)
    planted = tmp_path / "app" / "models" / "catalog.py"
    planted.write_text(
        "def resolve_default_connection(offer_version):\n"
        "    if offer_version.access_requirement == 'unclassified':\n"
        "        # filler line 1\n"
        "        # filler line 2\n"
        "        # filler line 3\n"
        "        # filler line 4\n"
        "        fallback = 'pppoe'\n"
        "    elif offer_version.access_requirement == 'network_access':\n"
        "        fallback = None\n"
        "    else:\n"
        "        fallback = None\n"
        "    return fallback\n"
    )

    leaks = _find_cross_domain_leaks_within_allowed_files(
        tmp_path,
        guarded=_GUARDED_TOKENS,
        cross_domain=_CROSS_DOMAIN_TOKENS,
        allowed=("app/models/catalog.py",),
    )
    assert any(leak.startswith("app/models/catalog.py:2") for leak in leaks), leaks


def test_content_guard_does_not_flag_unrelated_definitions_sharing_one_file(
    tmp_path,
):
    """Near-miss proof: real, unrelated definitions merely sharing one
    allowed file — the ``AccessRequirement`` enum's own declaration, and,
    many lines away, an unrelated ``ConnectionType``/``pppoe`` enum that
    never appears near a guarded-token line — are not flagged. Distinguishes
    a genuine same-block fallback from two unrelated concepts that happen to
    live in the same large file."""

    (tmp_path / "app" / "models").mkdir(parents=True)
    planted = tmp_path / "app" / "models" / "catalog.py"
    filler = "\n".join(f"# unrelated filler line {i}" for i in range(20))
    planted.write_text(
        "class AccessRequirement(enum.Enum):\n"
        "    network_access = 'network_access'\n"
        "    no_network_access = 'no_network_access'\n"
        "    unclassified = 'unclassified'\n"
        f"\n{filler}\n\n"
        "class ConnectionType(enum.Enum):\n"
        "    pppoe = 'pppoe'\n"
    )

    leaks = _find_cross_domain_leaks_within_allowed_files(
        tmp_path,
        guarded=_GUARDED_TOKENS,
        cross_domain=_CROSS_DOMAIN_TOKENS,
        allowed=("app/models/catalog.py",),
    )
    assert leaks == []


def test_offer_access_requirement_permission_key_matches_the_repo_pattern():
    assert _PERMISSION_KEY_PATTERN.fullmatch(
        "catalog:offer_access_requirement:classify"
    )
    assert not _PERMISSION_KEY_PATTERN.fullmatch(
        "catalog:offer-access-requirement:classify"
    )


def test_offer_access_requirement_permission_is_not_seeded_into_any_role():
    migration = _source(
        "alembic/versions/609_offer_access_requirement_classify_permission.py"
    )
    assert "INSERT INTO role_permissions" not in migration
    assert "catalog:billing_write" not in migration


def test_offer_version_admission_permission_is_not_seeded_into_any_role():
    """Regression for the shrunk 610 migration: admission is an OR-alternative
    to catalog:billing_write at the route (combined with the router's own
    catalog:write gate — see the migration's own docstring), never a hard
    requirement, so there is no existing-caller regression to prevent by
    copying grants — this migration's ``upgrade()`` seeds the permission row
    only, with no grant-copying logic, exactly like 609's pattern.

    Checked structurally (no ``INSERT INTO role_permissions`` in
    ``upgrade()``), not by banning the substrings ``role_permissions``/
    ``catalog:billing_write`` outright: both appear legitimately elsewhere in
    this migration — ``role_permissions`` in the downgrade's direct-grant
    refusal/cleanup, and ``catalog:billing_write`` in the docstring
    explaining the OR-alternative relationship and the compound requirement
    with ``catalog:write``. A naive substring ban would fail on those
    accurate mentions, not on a real grant-copying regression.
    """

    migration = _source("alembic/versions/610_offer_version_admission_permission.py")
    upgrade_source, _, downgrade_source = migration.partition("def downgrade")
    assert "INSERT INTO role_permissions" not in upgrade_source
    assert "role_permissions" in downgrade_source, (
        "the downgrade's own direct-grant refusal/cleanup should still "
        "reference role_permissions — if this ever goes false, the seed-only "
        "shape of upgrade() changed and this test's premise needs revisiting"
    )


def test_billing_write_is_checked_from_exactly_one_place_in_this_module():
    """Round 11 gave the admission command a real, in-transaction
    authorization decision (``_verify_admission_authorization`` /
    ``_admission_permission_granted``), and that decision LEGITIMATELY
    checks ``catalog:billing_write`` as one leg of the compound OR — see the
    module's own docstring ("there is exactly one place that decides what a
    role/scope means"). The prior version of this test asserted the
    pre-round-11 premise ("this module never checks catalog:billing_write at
    all"), which round 11 made false, and it did so by matching ONLY a
    literal string argument — a check written as a named constant
    (``BILLING_WRITE_PERMISSION``, the actual current shape) silently evaded
    it even before round 11's redesign made the underlying premise obsolete.

    The invariant actually worth guarding now is narrower and still real:
    ``catalog:billing_write`` (by literal string OR the ``BILLING_WRITE_
    PERMISSION`` constant) is checked from exactly ONE call site in this
    module — inside ``_admission_permission_granted`` — never from a second,
    independently-written check elsewhere that could drift out of sync with
    it (e.g. inside ``_verify_classify_permission``, which must never grow
    its own billing-write check).

    Checked via real AST ``Call`` inspection of every ``has_permission(...)``
    call site's arguments, resolving BOTH a literal string and a reference to
    the named constant — not by banning the substring outright, which fails
    on the module's own accurate docstring mentions of the permission name
    (there are several, all legitimate).
    """

    owner = _source("app/services/catalog/offer_access_requirement.py")
    tree = ast.parse(owner)

    def _checks_billing_write(node: ast.Call) -> bool:
        func = node.func
        is_has_permission = (
            isinstance(func, ast.Name) and func.id == "has_permission"
        ) or (isinstance(func, ast.Attribute) and func.attr == "has_permission")
        if not is_has_permission:
            return False
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Constant) and arg.value == "catalog:billing_write":
                return True
            if isinstance(arg, ast.Name) and arg.id == "BILLING_WRITE_PERMISSION":
                return True
        return False

    checking_functions = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if any(
            _checks_billing_write(call)
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        ):
            checking_functions.add(node.name)

    assert checking_functions == {"_admission_permission_granted"}, (
        "catalog:billing_write must be checked from exactly "
        "_admission_permission_granted and nowhere else in this module; "
        f"found it checked in: {sorted(checking_functions)}"
    )


#: A name-matching AST guard used to live here, asserting that a specific
#: helper existed and was called from ``_admit``. It went blind the moment
#: the helper it named was renamed
#: (``_verify_admission_permission`` -> ``_verify_admission_authorization``)
#: while the property it meant to protect ("the command makes a real
#: authorization decision") stayed true — a guard built from a spelling
#: cannot survive a rename, and a rename is exactly what happened. It is
#: deliberately NOT re-pointed at the current name; that would only
#: reproduce the same blind spot under a new label.
#:
#: The replacement lives in ``tests/test_offer_version_admission_asgi.py::
#: test_authorization_owner_refuses_identically_through_route_and_command``.
#: It injects a sentinel refusal at the shared authorization owner
#: (monkeypatching ``erp_staff_access.staff_write_restricted``) and drives
#: BOTH a real, mounted HTTP request through the route's admission
#: dependency and a direct ``admit_offer_version`` call through it — it
#: fails if EITHER adapter stops delegating to the one owner, regardless
#: of what anything is named.


#: There is no production call site allowed to construct ``SystemAdmission``
#: at all: ``offers.py``'s admission adapter FAILS CLOSED for any
#: unrecognized actor instead of falling back to it (see
#: ``OfferVersions._resolve_admission_principal``); the only way to admit
#: with no authenticated actor is a caller passing ``principal=
#: SystemAdmission(...)`` explicitly, and the only current callers that do
#: that are test fixtures. Every ``tests/`` file is unconditionally exempt
#: from this scan (matching ``_find_leaks``'s own convention above): a test
#: fixture may freely construct ``SystemAdmission`` directly. The tuple stays
#: as an explicit, reviewable allowlist parameter (rather than a hardcoded
#: empty scan) so a future genuine internal production call site is added by
#: EDITING this declaration, not by silently becoming invisible to the guard.
_SYSTEM_ADMISSION_ALLOWED_PATHS: tuple[str, ...] = ()

#: Every name a ``SystemAdmission`` construction could resolve through, given
#: a (possibly aliased) import of the class.
_SYSTEM_ADMISSION_CLASS_NAME = "SystemAdmission"


def _bound_system_admission_names(tree: ast.AST) -> set[str]:
    """Local names bound to the ``SystemAdmission`` class in one module,
    including an aliased ``from ... import SystemAdmission as X``. The bare
    class name is always included: a module that never imports it under that
    name simply never matches on a bare ``ast.Name`` call."""

    names = {_SYSTEM_ADMISSION_CLASS_NAME}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == _SYSTEM_ADMISSION_CLASS_NAME:
                    names.add(alias.asname or alias.name)
    return names


def _calls_construct_system_admission(tree: ast.AST, bound_names: set[str]) -> bool:
    """True if ``tree`` contains a real ``ast.Call`` node that constructs
    ``SystemAdmission`` — either a direct/aliased bare name (``SystemAdmission(...)``
    or ``X(...)`` after ``import ... as X``), or a dotted attribute access
    ending in ``.SystemAdmission(...)`` (e.g. ``offer_access_requirement.
    SystemAdmission(...)``, which stays ``SystemAdmission`` as the attribute
    name regardless of how the containing module itself was imported)."""

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in bound_names:
            return True
        if (
            isinstance(func, ast.Attribute)
            and func.attr == _SYSTEM_ADMISSION_CLASS_NAME
        ):
            return True
    return False


def _find_system_admission_construction_leaks(
    root: Path, *, allowed: tuple[str, ...]
) -> list[str]:
    """Real AST ``Call``-node scan (not a substring search): a string match
    on ``"SystemAdmission("`` misses an aliased import and flags any merely
    textual mention (a comment, a docstring, a string literal). Parsing each
    file and inspecting actual ``ast.Call`` nodes catches genuine
    construction only, whitespace/formatting notwithstanding."""

    leaks: list[str] = []
    for path in root.rglob("*.py"):
        if "/.venv/" in str(path) or "/node_modules/" in str(path):
            continue
        relative = str(path.relative_to(root))
        if relative.startswith("tests/") or relative in allowed:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _SYSTEM_ADMISSION_CLASS_NAME not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        bound_names = _bound_system_admission_names(tree)
        if _calls_construct_system_admission(tree, bound_names):
            leaks.append(relative)
    return leaks


def test_system_admission_construction_is_confined_to_the_declared_allowlist():
    """SystemAdmission is an admission with no authenticated end-user context
    at all. This is a BUILD-TIME/reviewed-call-site guarantee, not an
    unforgeable runtime credential (see the class's own docstring): it proves
    no committed, non-test file outside the allowlist constructs this type
    (the allowlist is currently empty — there is no production call site at
    all), so a new "no actor" admission path is visible in review instead of
    silently added anywhere in the tree."""

    leaks = _find_system_admission_construction_leaks(
        ROOT, allowed=_SYSTEM_ADMISSION_ALLOWED_PATHS
    )
    assert leaks == [], f"SystemAdmission constructed outside its allowlist: {leaks}"


def test_system_admission_confinement_guard_catches_a_planted_leak(tmp_path):
    """Sensitivity proof: a planted construction outside the allowlist, in an
    isolated temp tree, is caught."""

    (tmp_path / "app" / "services" / "network").mkdir(parents=True)
    leaking = tmp_path / "app" / "services" / "network" / "leaky.py"
    leaking.write_text(
        "from app.services.catalog.offer_access_requirement import "
        "SystemAdmission\n"
        "principal = SystemAdmission(reason='bypass')\n"
    )

    leaks = _find_system_admission_construction_leaks(
        tmp_path, allowed=_SYSTEM_ADMISSION_ALLOWED_PATHS
    )
    assert "app/services/network/leaky.py" in leaks


def test_system_admission_confinement_guard_catches_an_aliased_import_construction(
    tmp_path,
):
    """Sensitivity proof for the aliasing fix: ``from ... import
    SystemAdmission as X`` followed by ``X(...)`` is still caught — a plain
    substring search on ``"SystemAdmission("`` would miss this entirely."""

    (tmp_path / "app" / "services" / "network").mkdir(parents=True)
    leaking = tmp_path / "app" / "services" / "network" / "leaky_alias.py"
    leaking.write_text(
        "from app.services.catalog.offer_access_requirement import "
        "SystemAdmission as _Bypass\n"
        "principal = _Bypass(reason='aliased bypass')\n"
    )

    leaks = _find_system_admission_construction_leaks(
        tmp_path, allowed=_SYSTEM_ADMISSION_ALLOWED_PATHS
    )
    assert "app/services/network/leaky_alias.py" in leaks


def test_system_admission_confinement_guard_does_not_flag_the_allowed_call_site(
    tmp_path,
):
    """Near-miss proof: a file at a DECLARED allowed relative path is not
    flagged even though it constructs ``SystemAdmission`` — exercised with a
    synthetic allowlist entry (the real allowlist is empty today) so this
    test still proves the allowlist-skip branch itself works."""

    (tmp_path / "app" / "services" / "catalog").mkdir(parents=True)
    allowed_file = tmp_path / "app" / "services" / "catalog" / "offers.py"
    allowed_file.write_text("principal = SystemAdmission(reason='no actor supplied')\n")

    leaks = _find_system_admission_construction_leaks(
        tmp_path, allowed=("app/services/catalog/offers.py",)
    )
    assert leaks == []


def test_system_admission_confinement_guard_does_not_flag_a_mere_textual_mention(
    tmp_path,
):
    """Near-miss proof for the AST-vs-substring fix: a file that merely
    MENTIONS the name (a comment, docstring, or string literal) — with no
    actual ``ast.Call`` construction — is not flagged."""

    (tmp_path / "app" / "services" / "network").mkdir(parents=True)
    mentioning = tmp_path / "app" / "services" / "network" / "mentions_only.py"
    mentioning.write_text(
        '"""Never construct SystemAdmission here."""\n'
        "# SystemAdmission( is not a real call, just a comment example\n"
        "label = 'SystemAdmission(reason=...)'\n"
    )

    leaks = _find_system_admission_construction_leaks(
        tmp_path, allowed=_SYSTEM_ADMISSION_ALLOWED_PATHS
    )
    assert leaks == []


def test_offer_versions_create_delegates_the_actual_persist_to_the_new_owner():
    offers_service = _source("app/services/catalog/offers.py")
    owner_service = _source("app/services/catalog/offer_access_requirement.py")
    assert "offer_access_requirement.admit_offer_version(" in offers_service
    assert "offer_access_requirement.AdmitOfferVersionCommand(" in offers_service
    # The adapter must not construct the row itself.
    assert "OfferVersion(**data)" not in offers_service
    # Mutation now delegates too: the owner, not its adapter, checks the
    # decision-bearing field immediately before applying the update.
    assert "assert_access_requirement_immutable(data)" in _function_source(
        owner_service, "_update"
    )


def test_offer_access_requirement_owner_actually_performs_the_write():
    """Round 13 correction: the earlier version of this test banned
    ``db.commit(``/``db.rollback(`` anywhere in the WHOLE file. That is too
    broad — the round-13 bug was an in-transaction ``db.commit()`` inside
    ``authorize_offer_version_admission``, rejected by
    ``owner_commands._reject_helper_commit``, which turned a
    ``permission_denied`` refusal into ``nested_transaction_completion``
    while still losing the audit evidence.

    Scoped to the actual owner-managed transaction functions instead: ONLY
    ``_admit``, ``_classify``, ``authorize_offer_version_admission``, and
    ``verify_admission_authorization`` (the ones that either run inside
    ``execute_owner_command``'s transaction or are the shared decision this
    round moved the commit responsibility OUT of) may never call
    ``db.commit(``/``db.rollback(`` themselves — completing or discarding
    the transaction is exclusively the public command boundary's job."""

    owner = _source("app/services/catalog/offer_access_requirement.py")
    assert "execute_owner_command(" in owner
    assert "OfferVersion(**data)" in owner
    assert "db.add(version)" in owner

    for function_name in (
        "_admit",
        "_classify",
        "authorize_offer_version_admission",
        "verify_admission_authorization",
    ):
        function_source = _function_source(owner, function_name)
        assert "commit" not in _db_completion_calls(function_source), (
            f"{function_name} must never commit its own transaction"
        )
        assert "rollback" not in _db_completion_calls(function_source), (
            f"{function_name} must never roll back its own transaction"
        )


def test_leave_denial_evidence_is_a_registered_command_not_a_committing_helper():
    """Round 14 finding 1: ``record_leave_denial_evidence`` used to call
    ``db.commit()`` directly, making it a nested helper that completed its
    own transaction — exactly what ``docs/CODING_STANDARD.md`` § 3 forbids
    ("nested domain helpers... never call commit() or rollback()
    independently"). It now delegates to a genuinely separate REGISTERED
    owner command (``_RECORD_LEAVE_DENIAL_COMMAND`` /
    ``execute_owner_command``), which begins and completes that
    transaction itself.

    Checked precisely: the function must call ``execute_owner_command(``
    (proving delegation, not merely defining a command constant that is
    never used) and must NEVER call ``db.commit(`` itself (proving it does
    not complete its own transaction).

    ``db.rollback(`` is explicitly PERMITTED exactly ONE or exactly TWO
    times, in a checked order (round 15 finding 4 correction — the prior
    version of this check only compared the FIRST occurrence's position,
    so a second, undisclosed rollback anywhere in the function went
    completely unchecked):

    1. The defensive pre-delegation cleanup — clearing a lingering caller
       transaction so the registered command can actually run — MUST sit
       BEFORE ``execute_owner_command(``.
    2. An OPTIONAL second rollback — the outer failure handler's own
       defensive cleanup for when ``execute_owner_command`` itself raises
       — MUST sit AFTER ``execute_owner_command(`` if present. This is
       genuinely a second, distinct call site (the ``except Exception:``
       block), not the same rollback counted twice.

    A THIRD occurrence, or a second occurrence positioned before the
    delegation call, fails this test — neither is a documented, permitted
    shape."""

    owner = _source("app/services/catalog/offer_access_requirement.py")
    function_source = _function_source(owner, "record_leave_denial_evidence")

    assert "execute_owner_command(" in function_source, (
        "record_leave_denial_evidence must actually delegate to a "
        "registered owner command, not merely define one"
    )
    assert "commit" not in _db_completion_calls(function_source), (
        "record_leave_denial_evidence must never complete its own "
        "transaction — that is execute_owner_command's job"
    )

    rollback_count = function_source.count("db.rollback(")
    assert rollback_count in (1, 2), (
        f"record_leave_denial_evidence has {rollback_count} db.rollback( "
        "calls; expected exactly one (defensive pre-delegation cleanup) or "
        "two (that cleanup, plus a second, post-delegation rollback in the "
        "outer failure handler) — never more, and never used as a "
        "substitute for the command boundary's own transaction completion"
    )

    execute_index = function_source.index("execute_owner_command(")
    first_rollback_index = function_source.index("db.rollback(")
    assert first_rollback_index < execute_index, (
        "the FIRST db.rollback( must be the defensive pre-delegation "
        "cleanup, before execute_owner_command( runs"
    )
    if rollback_count == 2:
        second_rollback_index = function_source.index(
            "db.rollback(", first_rollback_index + 1
        )
        assert second_rollback_index > execute_index, (
            "the SECOND db.rollback( must sit strictly after "
            "execute_owner_command( — it is the outer failure handler's "
            "own defensive cleanup for when the registered command itself "
            "raises, never a duplicate of the pre-delegation guard"
        )


def test_classify_command_carries_no_free_text_actor_field():
    """Sensitivity proof for the actor-spoofing fix: the command dataclass
    has no separate free-text actor field — only the authenticated
    ``SystemUser`` id, and CommandContext.actor is not what gets recorded."""

    owner = _source("app/services/catalog/offer_access_requirement.py")
    assert "authorized_system_user_id: UUID" in owner
    assert "permission_granted: bool" not in owner
    assert "classified_by=command.context.actor" not in owner
    assert "actor_id=command.context.actor" not in owner
    cli = _source("scripts/catalog/classify_offer_access_requirement.py")
    assert '"--actor"' not in cli


def test_classify_permission_is_reverified_inside_the_command_transaction():
    owner = _source("app/services/catalog/offer_access_requirement.py")
    assert "_verify_classify_permission(db, command.authorized_system_user_id)" in (
        owner
    )


def test_offer_access_requirement_is_registered_as_a_new_contracted_owner():
    service = service_relationship("service_intent.offer_access_requirement")
    assert service.module == "app.services.catalog.offer_access_requirement"
    assert service.contract is not None
    assert set(service.owns) == {
        "access-classified offer-version admission",
        "immutable access requirement for an exact offer version",
        "reviewed classification of legacy/unclassified versions",
        "mutation of an already-admitted offer version",
    }


def test_catalog_policy_is_left_completely_untouched():
    """Negative control: the deliberately separate owner is unchanged."""

    policies = _source("app/services/catalog/policies.py")
    assert "offer_access_requirement" not in policies
    assert "AccessRequirement" not in policies
    catalog_policy = service_relationship("service_intent.catalog_policy")
    assert catalog_policy.module == "app.services.catalog.policies"
    assert catalog_policy.owns == (
        "catalog policy lookup",
        "offer policy interpretation",
    )


def test_migration_downgrade_locks_each_table_before_counting_that_table():
    """Round 12 finding 6: the prior version of this test compared only the
    FIRST ``LOCK TABLE`` occurrence against the FIRST ``SELECT count(*)``
    occurrence — true for the file as a whole even if ONE of the two locks
    were deleted, so long as the other lock still happened to precede
    whichever count came first in the source. Deleting ``608``'s
    ``offer_versions`` lock while leaving the classifications lock in place
    (or vice versa) would still have passed.

    This checks EACH count query is preceded by an ACCESS EXCLUSIVE lock on
    the EXACT table it counts, and proves that both ways: planting a
    removal of either lock alone is caught, and the real, legitimate
    ordering (both locks acquired up front, in the fixed deadlock-avoiding
    order, before either count) is not flagged.
    """

    migration = _source("alembic/versions/608_offer_access_requirement.py")
    downgrade_source = _function_source(migration, "downgrade")

    # offer_versions is a literal table name; the classifications table is
    # referenced via the f-string placeholder {_CLASSIFICATIONS_TABLE} in
    # BOTH its lock and its count statement's source text.
    checks = (
        (
            "LOCK TABLE offer_versions IN ACCESS EXCLUSIVE MODE",
            "SELECT count(*) FROM offer_versions",
        ),
        (
            "LOCK TABLE {_CLASSIFICATIONS_TABLE} IN ACCESS EXCLUSIVE MODE",
            "SELECT count(*) FROM {_CLASSIFICATIONS_TABLE}",
        ),
    )

    def _locked_before_its_own_count(
        source: str, lock_text: str, count_text: str
    ) -> bool:
        count_index = source.find(count_text)
        if count_index == -1:
            return True  # nothing to protect if this table is never counted
        lock_index = source.find(lock_text)
        return lock_index != -1 and lock_index < count_index

    for lock_text, count_text in checks:
        assert _locked_before_its_own_count(downgrade_source, lock_text, count_text), (
            f"{count_text!r} must be preceded by {lock_text!r} in downgrade()"
        )

    # Sensitivity, both directions: planting a removal of EITHER lock alone
    # (leaving the other lock and both counts intact) must be caught, even
    # though "a LOCK TABLE statement exists somewhere before a count"
    # remains true for the file as a whole.
    for lock_text, count_text in checks:
        planted = downgrade_source.replace(lock_text, "-- lock removed")
        assert not _locked_before_its_own_count(planted, lock_text, count_text), (
            f"planted removal of {lock_text!r} was not caught"
        )

    # Near-miss: the real, unmodified ordering must not be flagged.
    assert downgrade_source.index(
        "LOCK TABLE offer_versions IN ACCESS EXCLUSIVE MODE"
    ) < downgrade_source.index("SELECT count(*) FROM offer_versions")
    assert downgrade_source.index(
        "LOCK TABLE {_CLASSIFICATIONS_TABLE} IN ACCESS EXCLUSIVE MODE"
    ) < downgrade_source.index("SELECT count(*) FROM {_CLASSIFICATIONS_TABLE}")


def test_609_and_610_downgrade_lock_every_grant_table_before_counting_it():
    """Round 14 finding 5: without a lock, a grant inserted into
    ``system_user_permissions``/``subscriber_permissions``/
    ``role_permissions`` AFTER the zero-count checks in ``downgrade()`` but
    BEFORE the ``DELETE FROM permissions`` produced an opaque FK integrity
    failure instead of the promised ``DowngradeRefused`` — data stayed
    safe either way (the FK still blocks the delete), but the documented
    failure semantics did not hold.

    Checks each migration's ``downgrade()`` locks ALL THREE grant tables,
    in ``ACCESS EXCLUSIVE MODE``, before its first count query — proven
    both ways: the real, unmodified ordering is not flagged, and a planted
    removal of any one lock (leaving the other two and every count intact)
    is caught."""

    expected_tables = {
        "system_user_permissions",
        "subscriber_permissions",
        "role_permissions",
    }
    lock_template = ast.dump(
        ast.parse('f"LOCK TABLE {grant_table} IN ACCESS EXCLUSIVE MODE"').body[0].value
    )

    def _locked_before_first_count(source: str) -> bool:
        """Inspect the executable loop, not three strings it interpolates."""
        function = ast.parse(source).body[0]
        count_lines = [
            node.lineno
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_direct_grant_count"
        ]
        if not count_lines:
            return False
        for conditional in ast.walk(function):
            if not (
                isinstance(conditional, ast.If)
                and isinstance(conditional.test, ast.Name)
                and conditional.test.id == "is_postgres"
            ):
                continue
            for loop in conditional.body:
                if not (
                    isinstance(loop, ast.For)
                    and isinstance(loop.target, ast.Name)
                    and loop.target.id == "grant_table"
                    and isinstance(loop.iter, ast.Tuple)
                ):
                    continue
                tables = {
                    item.value
                    for item in loop.iter.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                }
                if (
                    tables != expected_tables
                    or len(loop.iter.elts) != len(expected_tables)
                    or loop.lineno >= min(count_lines)
                ):
                    continue
                for guard in loop.body:
                    if not (
                        isinstance(guard, ast.If)
                        and isinstance(guard.test, ast.Compare)
                        and isinstance(guard.test.left, ast.Name)
                        and guard.test.left.id == "grant_table"
                        and len(guard.test.ops) == 1
                        and isinstance(guard.test.ops[0], ast.In)
                        and len(guard.test.comparators) == 1
                        and isinstance(guard.test.comparators[0], ast.Name)
                        and guard.test.comparators[0].id == "table_names"
                    ):
                        continue
                    if any(
                        isinstance(statement, ast.Expr)
                        and isinstance(statement.value, ast.Call)
                        and isinstance(statement.value.func, ast.Attribute)
                        and isinstance(statement.value.func.value, ast.Name)
                        and statement.value.func.value.id == "op"
                        and statement.value.func.attr == "execute"
                        and statement.value.args
                        and ast.dump(statement.value.args[0]) == lock_template
                        for statement in guard.body
                    ):
                        return True
        return False

    for migration_path in (
        "alembic/versions/609_offer_access_requirement_classify_permission.py",
        "alembic/versions/610_offer_version_admission_permission.py",
    ):
        migration = _source(migration_path)
        downgrade_source = _function_source(migration, "downgrade")
        assert _locked_before_first_count(downgrade_source), (
            f"{migration_path}: the real, unmodified source unexpectedly "
            "failed its own lock-before-count check"
        )
        for table in expected_tables:
            original = f'"{table}",'
            assert original in downgrade_source
            planted = downgrade_source.replace(original, "", 1)
            assert not _locked_before_first_count(planted), (
                f"{migration_path}: planted removal of {table!r} was "
                "not caught by the lock-before-count check"
            )
        original_call = (
            'op.execute(f"LOCK TABLE {grant_table} IN ACCESS EXCLUSIVE MODE")'
        )
        assert original_call in downgrade_source
        planted = downgrade_source.replace(original_call, "pass", 1)
        assert not _locked_before_first_count(planted)
        indented_call = "                " + original_call
        assert indented_call in downgrade_source
        planted = downgrade_source.replace(
            indented_call,
            "                if False:\n                    " + original_call,
            1,
        )
        assert not _locked_before_first_count(planted), (
            f"{migration_path}: an unreachable lock must not satisfy the guard"
        )


def _function_source(module_source: str, function_name: str) -> str:
    """The exact source text of one top-level function, by name.

    Slicing on ``def upgrade()``/``def downgrade()`` string offsets (the
    prior shape of this check) cannot tell the two functions apart — a
    timeout statement anywhere in the file satisfied a membership test that
    read as "both functions carry it". Parsing to an AST and returning only
    the named function's own line range makes each function's assertion
    incapable of being satisfied by the OTHER function's statements.
    """

    tree = ast.parse(module_source)
    lines = module_source.splitlines(keepends=True)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return "".join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f"no top-level function named {function_name!r} found")


def _db_completion_calls(function_source: str) -> set[str]:
    """Find executable `db.commit()`/`db.rollback()` calls, not prose.

    A source-substring check was fooled by an owner docstring explaining why
    `db.commit()` is forbidden: it saw the warning as a live transaction call.
    """
    tree = ast.parse(function_source)
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "db"
        and node.func.attr in {"commit", "rollback"}
    }


def test_db_completion_detector_ignores_prose_but_sees_a_real_call():
    assert (
        _db_completion_calls(
            'def owner(db):\n    """Never call db.commit() here."""\n    return None\n'
        )
        == set()
    )
    assert _db_completion_calls("def owner(db):\n    db.commit()\n") == {"commit"}


def test_migration_uses_set_local_not_a_bare_set_for_timeouts():
    migration = _source("alembic/versions/608_offer_access_requirement.py")
    assert "SET LOCAL lock_timeout" in migration
    assert "RESET lock_timeout" not in migration
    assert "RESET statement_timeout" not in migration


def test_migration_downgrade_sets_the_identical_timeout_budget_as_upgrade():
    """``downgrade()`` acquires ACCESS EXCLUSIVE locks — at least as
    contention-prone as ``upgrade()``'s ADD COLUMN — so it must carry the
    identical ``SET LOCAL`` timeout budget, not merely SOME timeout
    statement somewhere in the file.

    Sensitivity: this test is written against the current (fixed) 608, where
    both functions carry the budget — it would have failed against the prior
    downgrade(), which had neither statement, while
    ``test_migration_uses_set_local_not_a_bare_set_for_timeouts`` above
    (matching anywhere in the file) stayed green throughout because
    upgrade() alone satisfied it.
    """

    migration = _source("alembic/versions/608_offer_access_requirement.py")
    upgrade_source = _function_source(migration, "upgrade")
    downgrade_source = _function_source(migration, "downgrade")
    for label, function_source in (
        ("upgrade", upgrade_source),
        ("downgrade", downgrade_source),
    ):
        assert "SET LOCAL lock_timeout = '5s'" in function_source, (
            f"{label}() is missing the lock_timeout budget"
        )
        assert "SET LOCAL statement_timeout = '15min'" in function_source, (
            f"{label}() is missing the statement_timeout budget"
        )


def test_migration_declares_check_constraints_for_the_legal_transition_shape():
    migration = _source("alembic/versions/608_offer_access_requirement.py")
    assert "previous_access_requirement = 'unclassified'" in migration
    assert "new_access_requirement IN ('network_access', 'no_network_access')" in (
        migration
    )


def test_admission_principal_resolver_maps_every_auth_shape_correctly():
    """Unit-level proof of ``app.api.catalog._admission_principal``'s own
    mapping logic — a pure function from an auth dict's shape to a typed
    ``AdmissionPrincipal`` — NOT a claim about route/dependency-graph
    behavior (see ``tests/test_offer_version_admission_asgi.py`` for that;
    calling this resolver directly here is testing the function's own
    contract, the same as any other pure helper, not simulating
    authorization).

    Covers the machine-vs-legacy-API-key distinction specifically: a
    ``credential_kind == "machine"`` auth resolves to the shadow-only
    ``MachineCredentialPrincipal`` with its scopes preserved; every other
    ``api_key`` shape (with or without an explicit
    ``credential_kind == "legacy_api_key"``) resolves to the enforced
    ``ApiKeyPrincipal`` — deleting or inverting that branch would silently
    turn a real machine request into a caller hunting a nonexistent local
    ``ApiKey`` row.
    """

    import uuid

    import pytest
    from fastapi import HTTPException

    from app.api import catalog as api_catalog
    from app.services.catalog import offer_access_requirement

    system_user_auth = {
        "principal_id": str(uuid.uuid4()),
        "principal_type": "system_user",
    }
    api_key_auth = {"principal_id": str(uuid.uuid4()), "principal_type": "api_key"}
    legacy_api_key_auth = {
        "principal_id": str(uuid.uuid4()),
        "principal_type": "api_key",
        "credential_kind": "legacy_api_key",
    }
    machine_auth = {
        "principal_id": str(uuid.uuid4()),
        "principal_type": "api_key",
        "credential_kind": "machine",
        "scopes": ["catalog:write", offer_access_requirement.ADMISSION_SCOPE],
    }
    subscriber_auth = {
        "principal_id": str(uuid.uuid4()),
        "principal_type": "subscriber",
    }
    other_auth = {"principal_id": str(uuid.uuid4()), "principal_type": "reseller_user"}

    assert isinstance(
        api_catalog._admission_principal(system_user_auth),
        offer_access_requirement.StaffPrincipal,
    )
    assert isinstance(
        api_catalog._admission_principal(api_key_auth),
        offer_access_requirement.ApiKeyPrincipal,
    )
    assert isinstance(
        api_catalog._admission_principal(legacy_api_key_auth),
        offer_access_requirement.ApiKeyPrincipal,
    )
    machine_principal = api_catalog._admission_principal(machine_auth)
    assert isinstance(
        machine_principal, offer_access_requirement.MachineCredentialPrincipal
    )
    assert machine_principal.credential_id == uuid.UUID(machine_auth["principal_id"])
    assert set(machine_principal.scopes) == set(machine_auth["scopes"])
    assert isinstance(
        api_catalog._admission_principal(subscriber_auth),
        offer_access_requirement.SubscriberPrincipal,
    )
    with pytest.raises(HTTPException) as excinfo:
        api_catalog._admission_principal(other_auth)
    assert excinfo.value.status_code == 403


def test_credential_kind_is_stamped_by_both_api_key_authentication_paths():
    """No test asserts ``credential_kind`` at the AUTHENTICATION layer
    behaviorally — ``test_admission_principal_resolver_maps_every_auth_
    shape_correctly`` above builds its own auth dicts by hand, and the
    machine-shadow tests in ``tests/test_offer_access_requirement.py``
    construct ``MachineCredentialPrincipal`` directly, bypassing both the
    dict stamp AND the route conversion. Deleting the stamp in either
    ``auth_dependencies._machine_principal`` or ``_api_key_principal``
    would leave every one of those tests green while a real machine
    request silently resolved to ``ApiKeyPrincipal`` and 403'd hunting a
    nonexistent local row.

    Checked via AST dict-literal inspection of each function's own body —
    not a behavioral call, since ``_machine_principal`` requires a
    configured kernel HMAC key and a live
    ``dotmac_kernel.machine_auth.authenticate_machine`` call this test
    environment cannot provide (``dotmac_kernel`` is not vendored or
    importable here at all — there is no behavioral alternative to this
    check for this specific property)."""

    def _stamps_credential_kind(function_name: str, expected_value: str) -> bool:
        source = _source("app/services/auth_dependencies.py")
        tree = ast.parse(source)
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == function_name
        )
        for node in ast.walk(function):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "credential_kind"
                    and isinstance(value, ast.Constant)
                    and value.value == expected_value
                ):
                    return True
        return False

    assert _stamps_credential_kind("_machine_principal", "machine")
    assert _stamps_credential_kind("_api_key_principal", "legacy_api_key")
