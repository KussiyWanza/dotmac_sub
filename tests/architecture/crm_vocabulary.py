"""Shared analysis for the CRM/Omni vocabulary freeze.

The `dotmac_crm` deployment ("Omni") was decommissioned on 2026-08-29. Its
surface inside Sub is being replaced domain by domain — Inbox/Chat, then
Support/Ticketing, Sales/Quotes, Party/Customer/Reseller, ERP modules, then a
final secrets/deployment/observability pass — each slice deleting residue
BESIDE its replacement owner rather than in a sweep.

A slice-by-slice replacement only works if the surface holds still between
slices. This freeze is what holds it: it fails when a NEW CRM dependency lands,
and it fails when one disappears without the baseline being lowered in the same
change. A silent removal is as damaging as a silent addition here, because the
whole programme is a sequence of deliberate, recorded removals.

## Why the match is on identifier FORM, not on the bare word

`\\b` is the trap. A word boundary does not fire beside `_` or a digit, so a
naive `\\bcrm\\b` reads `crm_subscriber_id`, `crm_ticket_pull` and `CRMClient`
as clean — which is nearly the entire real surface. This module instead splits
camelCase into `_`, splits on every non-alphanumeric run, lowercases, and
compares whole TOKENS against the frozen vocabulary.

Token equality also buys specificity for free: a base64 digest or a long opaque
string is one token and never equals `crm`, so it cannot false-positive the way
a substring search would.

## What counts as a member

A file is in the surface if the vocabulary appears in its CONTENT **or in its
PATH**. The path half is not decorative: `app/services/reseller_crm_views.py`
is named for the CRM and contains no `crm` token in its body, so a
content-only scan misses a file whose whole subject is the CRM.

Tests that assert a CRM/Omni alias is *refused* are members too — for example
the ERP contract tests pinning the retired `omni_id`. They reference the
vocabulary, and deleting one silently is exactly what the falling direction of
this ratchet exists to catch.

## How a literal is CONSUMED decides dependency vs. mention, for code

This freeze asks a different question than "does executable code consume
this module?" — it asks "did the retired surface grow?" — so it stays
deliberately conservative almost everywhere: prose, identifiers, and
container literals (a registry list, a name->value dict —
`app/services/infrastructure_health.py` keys a real health-check dispatch
off a `"crm"` string in a plain dict) all keep counting exactly as a plain
token match would. There is exactly ONE narrow exemption, for `.py` files: a
literal that is used to LOCATE a path and NOTHING ELSE — never imported,
never read, opened, executed, or subprocess-invoked. Locating a path to
describe or compare it is characterization; touching what it names is
exactly the growth this freeze exists to prevent, so the exemption is
withdrawn the moment that happens.

`_is_characterization_only` draws that line, for `.py` files:

- A real `import`/`from ... import` statement is ALWAYS a dependency, full
  stop, regardless of what it names — importing a well-established,
  long-frozen module is still importing it.
- A string literal passed to a runtime import operation
  (`importlib.import_module(...)`, `__import__(...)`) is a dynamic
  dependency, same as a static import.
- A vocabulary-bearing path — directly, or via a variable it was earlier
  assigned to — that is actually read, opened, executed, or handed to a
  subprocess call (`.read_text()`, `.read_bytes()`, `.read()`, `.open()`,
  `open(...)`, `subprocess.run/call/Popen/check_output/check_call(...)`) is
  a dependency: a production operation that touches the retired surface,
  even though the path appears only as a string argument, never an import.
  The ONE exception is the freeze's OWN ledger file
  (`crm_vocabulary_baseline.txt`) — reading it is reading the freeze's own
  characterization record, not a piece of retired application code, and
  that is the single narrow case this module reads without counting itself.
- A vocabulary-bearing string that is the SOLE argument to `Path(...)`, and
  is never subsequently touched by any of the above, is characterization —
  locating a path to describe or compare it, nothing more.
- Anything else — a bare identifier, an f-string, prose, a docstring, a
  plain assignment, a container literal — keeps counting exactly as before:
  this exemption only ever SUBTRACTS membership for the one specifically
  recognized, unambiguously-inert, never-touched location shape, never
  grants a general "it's just a mention" allowance elsewhere.

The deciding question is how the literal is consumed, not whether it looks
like a module or file path — the identical name can be a dependency in one
file and a mention in another (see the paired plants in
`test_crm_vocabulary_freeze.py`:
`test_a_real_import_of_a_crm_module_still_counts_as_a_dependency`,
`test_a_production_read_of_a_crm_path_still_counts_as_a_dependency`, and
`test_the_identical_name_used_only_to_locate_a_path_does_not_count`).
"""

from __future__ import annotations

import ast
import re
import subprocess
from functools import cache
from pathlib import Path

#: The frozen vocabulary. `omni` is here because the deployment was named Omni
#: and its identifiers outlived the CRM name in several contracts.
CRM_TERMS: frozenset[str] = frozenset({"crm", "omni"})

#: Entry-point families. Application code with its Celery tasks and workers
#: (`app`), schema migrations (`alembic`), operator CLI, seeds and one-offs
#: (`scripts`), the test suite, and the documentation that carries the
#: programme's contracts.
LANES: tuple[str, ...] = ("app", "alembic", "docs", "scripts", "tests")

# Split `fooBar` and `CRMClient` alike: the second alternative is what turns
# `CRMClient` into `CRM_Client` rather than leaving one opaque token.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def tokens(text: str) -> frozenset[str]:
    """Lowercase identifier tokens, with camelCase treated as a separator."""

    split = _CAMEL_BOUNDARY.sub("_", text)
    return frozenset(part.lower() for part in _NON_ALNUM.split(split) if part)


def mentions_crm(text: str) -> bool:
    """Whether a fragment carries the frozen vocabulary as a whole token."""

    return bool(tokens(text) & CRM_TERMS)


@cache
def tracked_files() -> tuple[str, ...]:
    """Every tracked path, from git.

    Tracked rather than on-disk on purpose: a freeze is a statement about the
    repository, and an untracked scratch file in `docs/` must not be able to
    trip it.
    """

    result = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    )
    return tuple(sorted(path for path in result.stdout.split("\0") if path))


def _reads_as_text(path: Path) -> str | None:
    """Decode a tracked file, or None if it is binary or unreadable.

    The binary check is load-bearing. `docs/My Map.kmz` is a zip archive whose
    bytes, forced through a lossy UTF-8 decode, happen to yield a `crm` token
    and put a map file in the CRM surface. A scanner that decodes binary does
    not find things; it invents them.
    """

    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="ignore")


_DYNAMIC_IMPORT_CALLEES: frozenset[str] = frozenset({"import_module", "__import__"})

#: A file whose content is actually read, executed, or spawned counts as a
#: real dependency — "locating" a path and "touching" what it names are
#: different operations, and only the first is characterization.
_CONTENT_TOUCHING_ATTRS: frozenset[str] = frozenset(
    {"read_text", "read_bytes", "read", "open", "exec_module"}
)
_SUBPROCESS_CALLEES: frozenset[str] = frozenset(
    {"run", "call", "Popen", "check_output", "check_call"}
)

#: The freeze's own ledger — the ONE file this exemption may read without
#: that read counting as touching the retired surface, because the ledger
#: IS characterization data by definition (a list of paths, forever, never
#: application code). Reading any OTHER vocabulary-bearing path — even one
#: this exemption would otherwise treat as a bare `Path(...)` location —
#: still counts the moment that path's content is actually consumed. This
#: is a narrow, explicit, singular exception, not a general "the ledger
#: family is exempt" allowance: nothing else gets it.
_LEDGER_LITERAL = "tests/architecture/crm_vocabulary_baseline.txt"


def _path_literal_assignments(tree: ast.Module) -> dict[str, tuple[int, str]]:
    """Map `VAR` -> `(lineno, literal)` for `VAR = Path("literal")`
    assignments whose literal bears the vocabulary, so a read performed on
    `VAR` elsewhere in the file — not right where the literal was written —
    can still be traced back to the line that named it."""

    assignments: dict[str, tuple[int, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "Path"
            and len(value.args) == 1
        ):
            continue
        arg = value.args[0]
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            continue
        if not mentions_crm(arg.value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                assignments[target.id] = (arg.lineno, arg.value)
    return assignments


def _content_touching_lines(
    tree: ast.Module, path_assignments: dict[str, tuple[int, str]]
) -> frozenset[int]:
    """Lines whose vocabulary-bearing literal — directly, or via a variable
    it was earlier assigned to — is fed to a real content-reading,
    executing, or subprocess operation. Returns the line that NAMED the
    literal (what `_is_characterization_only` actually scans line-by-line),
    not necessarily the line the operation itself sits on. The ledger's own
    literal is exempt from this; every other vocabulary-bearing literal is
    not, regardless of how innocuous the operation looks.
    """

    lines: set[int] = set()

    def _mark(literal: str, lineno: int) -> None:
        if literal != _LEDGER_LITERAL:
            lines.add(lineno)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        if isinstance(func, ast.Attribute) and func.attr in _CONTENT_TOUCHING_ATTRS:
            base = func.value
            if (
                isinstance(base, ast.Call)
                and isinstance(base.func, ast.Name)
                and base.func.id == "Path"
                and len(base.args) == 1
                and isinstance(base.args[0], ast.Constant)
                and isinstance(base.args[0].value, str)
                and mentions_crm(base.args[0].value)
            ):
                _mark(base.args[0].value, base.args[0].lineno)
            elif isinstance(base, ast.Name) and base.id in path_assignments:
                lineno, literal = path_assignments[base.id]
                _mark(literal, lineno)

        elif isinstance(func, ast.Name) and func.id == "open":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if mentions_crm(arg.value):
                        _mark(arg.value, arg.lineno)

        elif isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_CALLEES:
            for arg_node in ast.walk(node):
                if isinstance(arg_node, ast.Constant) and isinstance(
                    arg_node.value, str
                ):
                    if mentions_crm(arg_node.value):
                        _mark(arg_node.value, arg_node.lineno)

    return frozenset(lines)


def _dependency_lines(tree: ast.Module) -> frozenset[int]:
    """Lines where a vocabulary-bearing literal is genuinely CONSUMED as
    code, or where its target's CONTENT is actually touched: a real
    `import`/`from ... import` statement (always, regardless of what module
    it names — this is what makes the exemption below non-overridable by a
    real import); a string literal passed to a runtime import operation
    (`importlib.import_module(...)`, `__import__(...)`); or a
    read/open/exec/subprocess operation on a vocabulary-bearing path other
    than the freeze's own ledger (`_content_touching_lines`) — "production
    code reading
    `open('scripts/migration/backfill_crm_subscriber_links.py')`" is a live
    dependency on the retired surface even though the path appears only as
    a string argument, never an import.
    """

    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            end = getattr(node, "end_lineno", None) or node.lineno
            lines.update(range(node.lineno, end + 1))
        elif isinstance(node, ast.Import) and node.names:
            end = getattr(node, "end_lineno", None) or node.lineno
            lines.update(range(node.lineno, end + 1))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in _DYNAMIC_IMPORT_CALLEES:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        lines.add(arg.lineno)
    lines |= _content_touching_lines(tree, _path_literal_assignments(tree))
    return frozenset(lines)


def _characterization_data_lines(tree: ast.Module) -> frozenset[int]:
    """Lines where a vocabulary-bearing string literal is CONSUMED as the
    sole argument to `Path(...)` — locating a path, not touching what it
    names. `_dependency_lines` is always checked first by
    `_is_characterization_only`, so a `Path(...)` construction whose result
    is later read, opened, executed, or subprocess-invoked is caught there
    and never reaches this exemption, regardless of matching this shape.

    Deliberately narrower than "any list/tuple/set/dict literal": a
    registry list or a name->value dict can be exactly as live a dependency
    as an import — `app/services/infrastructure_health.py` keys a real
    health-check dispatch off a `"crm"` string in a plain dict, which is
    unambiguously still a dependency and must keep counting. A bare,
    never-read `Path("literal/path.txt")` construction has no such
    reading: it can only ever locate a file, which is what makes it safe to
    treat as characterization input rather than a general "container
    literal" exemption.
    """

    lines: set[int] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Path"
            and len(node.args) == 1
        ):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            lines.add(arg.lineno)
    return frozenset(lines)


def _is_characterization_only(path: Path, text: str) -> bool:
    """Whether every vocabulary-bearing line in a `.py` file is DATA
    consumption, never a real or dynamic import.

    True means this file adds no new coupling to the frozen surface: it
    only names the vocabulary inside a recognized data shape (see
    `_characterization_data_lines`). False — the default, unconditional
    posture — means at least one occurrence is a real dependency
    (`_dependency_lines`) or an unrecognized shape (a bare identifier, an
    f-string, prose, a plain assignment), and the file is a genuine surface
    member exactly as before this exemption existed. A dependency line
    always wins over a data-shaped line on the same file: this exemption
    only ever subtracts membership for a specifically recognized data
    shape, never adds a blanket allowance. Only `.py` files can qualify —
    a `.md`/`.txt`/`.json` file has no AST to classify.
    """

    if path.suffix != ".py":
        return False
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return False
    dependency_lines = _dependency_lines(tree)
    data_lines = _characterization_data_lines(tree)
    saw_any = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not mentions_crm(line):
            continue
        saw_any = True
        if lineno in dependency_lines:
            return False
        if lineno not in data_lines:
            return False
    return saw_any


@cache
def surface_by_lane() -> dict[str, frozenset[str]]:
    """Files carrying the CRM/Omni vocabulary, grouped by entry-point family."""

    found: dict[str, set[str]] = {lane: set() for lane in LANES}
    for tracked in tracked_files():
        lane, _, _ = tracked.partition("/")
        if lane not in found:
            continue
        if mentions_crm(tracked):
            found[lane].add(tracked)
            continue
        text = _reads_as_text(Path(tracked))
        if text is None or not any(mentions_crm(line) for line in text.splitlines()):
            continue
        if _is_characterization_only(Path(tracked), text):
            continue
        found[lane].add(tracked)
    return {lane: frozenset(paths) for lane, paths in found.items()}


def surface_paths() -> frozenset[str]:
    """The whole frozen surface as one set."""

    return frozenset().union(*surface_by_lane().values())
