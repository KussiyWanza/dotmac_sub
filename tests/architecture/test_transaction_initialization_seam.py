"""`SET TRANSACTION` cannot be the first statement, so it must not be used.

`SET TRANSACTION` — in every spelling, isolation level or bare `READ ONLY` — is
only legal as the FIRST statement in a transaction. Since #2353 installs
`app.current_tenant` via `set_config` on `after_begin`, it never is: a statement
has always run before any caller SQL arrives. Twenty-four read-only operator
reports and one serializable writer raised ActiveSqlTransaction on every
PostgreSQL invocation, while SQLite unit coverage stayed green because it skips
the dialect branch entirely.

The repair is two seams in the transaction-authority layer. This guard stops the
raw pattern coming back, in two ways the first version of it could not:

- It matches `SET TRANSACTION` rather than `SET TRANSACTION ISOLATION LEVEL`.
  The narrower pattern was itself a half-repair: it certified thirteen callers
  while twenty-three uses of the bare `SET TRANSACTION READ ONLY` spelling sat
  outside its reach, equally broken and equally invisible.
- The exemption is a CHECKED PREMISE, not a list of filenames. A module holding
  a raw `Connection` from its own engine has no ORM session, so no `after_begin`
  hook runs and `SET TRANSACTION` genuinely is its first statement. That is true
  of the CRM import/drift scripts, which speak to a second database entirely.
  The guard re-derives that per file instead of trusting a name, so one of them
  adopting an ORM session is caught the moment it does.

Scanning is over string literals rather than raw text, so prose explaining the
defect — this docstring included — is not itself a violation. That is why the
guard needs no allowlist at all.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = (PROJECT_ROOT / "app", PROJECT_ROOT / "scripts")

#: Deliberately loose. It matches the statement's opening, not one exact
#: spelling, so a comma, casing, whitespace or READ ONLY/ISOLATION LEVEL
#: variant cannot slip through.
RAW_SET_TRANSACTION = re.compile(r"SET\s+TRANSACTION\b", re.IGNORECASE)

#: The premise under which a module may issue `SET TRANSACTION` at all: it never
#: obtains an ORM `Session`, so the root `after_begin` listener in
#: `app.services.session_hooks` cannot have run a statement ahead of it.
ORM_SESSION = re.compile(r"\bSessionLocal\b|\bsessionmaker\b|\bSession\(")


def _sql_literals(source: str) -> list[str]:
    """Every string literal that is not a docstring.

    SQL reaches the database as a literal; an explanation of SQL reaches only
    the reader. Distinguishing them is what lets this file describe the defect
    it forbids.
    """

    tree = ast.parse(source)
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            if node.body and isinstance(first := node.body[0], ast.Expr):
                if isinstance(first.value, ast.Constant) and isinstance(
                    first.value.value, str
                ):
                    docstring_ids.add(id(first.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_ids
    ]


def _reportable(path: Path) -> Path:
    """Name the offender relative to the repo when it lives there.

    The canaries below scan a planted tree under `tmp_path`, which is not under
    PROJECT_ROOT. Calling `relative_to` unconditionally raised ValueError there,
    so every canary that expected a DETECTION died before it could assert — the
    guard's proof that it can fail was itself unproven from the day it was
    written, and no run caught it because this job never completed on the branch.
    """

    try:
        return path.relative_to(PROJECT_ROOT)
    except ValueError:
        return path


def _offenders(roots=SCANNED_ROOTS) -> dict[Path, int]:
    found: dict[Path, int] = {}
    for root in roots:
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if not ORM_SESSION.search(source):
                continue
            hits = sum(
                1
                for literal in _sql_literals(source)
                if RAW_SET_TRANSACTION.search(literal)
            )
            if hits:
                found[_reportable(path)] = hits
    return found


def test_no_module_sets_transaction_characteristics_with_raw_sql() -> None:
    offenders = _offenders()

    assert offenders == {}, (
        "use app.db.read_only_snapshot_session / begin_read_only_snapshot "
        "(reports) or app.db.begin_serializable_write (writers) instead of raw "
        "SQL; SET TRANSACTION cannot be the first statement while the "
        f"operator-tenant hook is installed: {offenders}"
    )


def _plant(directory: Path, body: str) -> Path:
    directory.mkdir(exist_ok=True)
    planted = directory / "offender.py"
    planted.write_text(body, encoding="utf-8")
    return planted


def test_the_guard_detects_a_planted_violation(tmp_path: Path) -> None:
    """Sensitivity proof: a guard that cannot fail is not a guard.

    Without this, deleting the pattern from the detector — or scanning a
    directory the offenders do not live in — would look exactly like success.
    """

    _plant(
        tmp_path / "app",
        "db = SessionLocal()\n"
        'db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))\n',
    )

    assert _offenders(roots=(tmp_path / "app",))


def test_the_guard_detects_the_bare_read_only_spelling(tmp_path: Path) -> None:
    """The spelling the narrower first guard could not see.

    Twenty-three uses of `SET TRANSACTION READ ONLY` — no isolation level named
    — carried the identical defect while passing a detector that required the
    words ISOLATION LEVEL. A guard is only as wide as the defect it admits.
    """

    _plant(
        tmp_path / "app",
        'db = SessionLocal()\ndb.execute(text("SET TRANSACTION READ ONLY"))\n',
    )

    assert _offenders(roots=(tmp_path / "app",))


def test_the_guard_catches_the_typo_variant(tmp_path: Path) -> None:
    """The spelling that escaped the original exact-string search.

    `crm_network_map_point_migration.py` carried
    `REPEATABLE READ READ ONLY` — no comma — and was equally broken while
    matching no search anyone had run for it.
    """

    _plant(
        tmp_path / "app",
        "db = SessionLocal()\n"
        'db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))\n',
    )

    assert _offenders(roots=(tmp_path / "app",))


def test_the_premise_is_checked_rather_than_asserted(tmp_path: Path) -> None:
    """A raw-Connection module is out of scope only while it stays one.

    This is the difference between an exemption and a premise. The CRM scripts
    are not trusted by name: the same file becomes an offender the moment it
    acquires an ORM session, because that is the moment the `after_begin` hook
    starts running a statement ahead of its SQL.
    """

    planted_root = tmp_path / "app"
    raw_connection = (
        "engine = _engine_from_env('CRM_DATABASE_URL')\n"
        "with engine.connect() as crm:\n"
        '    crm.execute(text("SET TRANSACTION READ ONLY"))\n'
    )
    _plant(planted_root, raw_connection)
    assert _offenders(roots=(planted_root,)) == {}

    _plant(planted_root, raw_connection + "session = sessionmaker(bind=engine)()\n")
    assert _offenders(roots=(planted_root,))


def test_prose_about_the_defect_is_not_a_violation(tmp_path: Path) -> None:
    """Otherwise the guard would forbid explaining itself.

    `app/db.py` documents this defect at length and needed a filename exemption
    under the old text-scanning detector. Scanning literals instead of raw text
    removes the exemption rather than widening it.
    """

    _plant(
        tmp_path / "app",
        '"""A report must not issue SET TRANSACTION READ ONLY itself."""\n'
        "db = SessionLocal()\n"
        "# SET TRANSACTION ISOLATION LEVEL is illegal here; use the seam.\n"
        "begin_read_only_snapshot(db)\n",
    )

    assert _offenders(roots=(tmp_path / "app",)) == {}
