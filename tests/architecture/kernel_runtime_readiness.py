"""Load and structurally validate `docs/kernel-runtime-readiness.json`.

This module is the shared analysis behind
`test_kernel_runtime_readiness_record.py`, mirroring the existing
`session_construction_inventory.py` / `test_session_construction_ratchet.py`
split in this directory: the *how do we know* lives here, the pytest
assertions live in the `test_*.py` file.

## Why this exists

The Kernel successor's compatibility gate lives in `dotmac_starter_mt`. It
used to take Sub's readiness facts as booleans authored in STARTER's own
JSON -- a producing repository asserting a fact about a consuming
repository it cannot verify. That is what this record replaces: Sub owns a
typed record, in Sub's own tree, and Sub's own CI (this test) is the only
thing that gets to say a claim in it is true. Starter's gate reads the
record from a Git blob at a pinned revision and parses it; it authors
nothing about Sub.

## What "checked" means here

Every `requirements[].id` in the record must have a registered checker in
`REQUIREMENT_CHECKS` below, and every `composition[].declaration` must have
a registered checker in `COMPOSITION_CHECKS`. Each checker re-derives its
fact directly from Sub's source tree via `ast` (never by re-reading the
JSON's own `statement` string, and never by trusting the JSON's
`source_reference` blindly) and returns the actual boolean. The test then
compares that actual value against the record's claimed `satisfied` (for
requirements) or simply asserts the checker found the declared fact (for
composition, which carries no boolean).

A requirement whose id has no registered checker is refused outright: an
unchecked `satisfied: true` is exactly the defect this record exists to
retire, and silently accepting one here would reintroduce it one repository
over.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.architecture.source_index import python_ast, python_nodes

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORD_PATH = REPO_ROOT / "docs" / "kernel-runtime-readiness.json"

REQUIRED_SCHEMA = "kernel-runtime-readiness.v1"
REQUIRED_PRODUCT = "dotmac_sub"
#: Canonical, owned by Starter's `PRODUCT_SPECS` -- not a phrase this record
#: coins. The richer semantics stay in `requirements[]`; encoding them into the
#: subject makes one identifier answer two questions, which is how three
#: repositories came to answer one question three different ways.
REQUIRED_SUBJECT = "sub-kernel-successor-readiness"

#: The envelope Starter owns (see the brief this record answers): exactly
#: these six top-level keys, never more. An extra key is refused rather than
#: ignored -- Starter's gate parses this envelope by name, and a field it
#: does not expect is exactly how a producer smuggles an unreviewed claim
#: past a consumer that only reads six named fields.
ENVELOPE_KEYS = frozenset(
    {"schema", "product", "subject", "requirements", "composition", "source_references"}
)
REQUIREMENT_KEYS = frozenset({"id", "statement", "satisfied", "source_reference"})
COMPOSITION_KEYS = frozenset({"declaration", "source_reference"})

_SOURCE_REF_RE = re.compile(r"^(?P<path>[\w./-]+):(?P<start>\d+)(-(?P<end>\d+))?$")


class RecordValidationError(AssertionError):
    """Raised for any structural or factual defect in the readiness record."""


def load_record(path: Path = RECORD_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Envelope / source_reference structural checks
# --------------------------------------------------------------------------


def validate_envelope(record: dict[str, Any]) -> None:
    extra = set(record.keys()) - ENVELOPE_KEYS
    if extra:
        raise RecordValidationError(
            f"unexpected top-level key(s) not in the owned envelope: {sorted(extra)!r}"
        )
    if record.get("schema") != REQUIRED_SCHEMA:
        raise RecordValidationError(
            f"schema must be {REQUIRED_SCHEMA!r}: {record.get('schema')!r}"
        )
    if record.get("product") != REQUIRED_PRODUCT:
        raise RecordValidationError(
            f"product must be {REQUIRED_PRODUCT!r}: {record.get('product')!r}"
        )
    if record.get("subject") != REQUIRED_SUBJECT:
        raise RecordValidationError(
            f"subject must be exactly {REQUIRED_SUBJECT!r}: {record.get('subject')!r}"
        )
    if not isinstance(record.get("requirements"), list) or not record["requirements"]:
        raise RecordValidationError("requirements must be a non-empty list")
    if not isinstance(record.get("composition"), list) or not record["composition"]:
        raise RecordValidationError("composition must be a non-empty list")
    if (
        not isinstance(record.get("source_references"), list)
        or not record["source_references"]
    ):
        raise RecordValidationError("source_references must be a non-empty list")

    seen_ids: set[str] = set()
    for entry in record["requirements"]:
        entry_extra = set(entry.keys()) - REQUIREMENT_KEYS
        if entry_extra:
            raise RecordValidationError(
                f"unexpected key(s) on a requirement entry: {sorted(entry_extra)!r}: {entry!r}"
            )
        for key in REQUIREMENT_KEYS:
            if key not in entry:
                raise RecordValidationError(
                    f"requirement missing key {key!r}: {entry!r}"
                )
        if not isinstance(entry["satisfied"], bool):
            raise RecordValidationError(
                f"requirement 'satisfied' must be a bool: {entry!r}"
            )
        if entry["id"] in seen_ids:
            raise RecordValidationError(f"duplicate requirement id: {entry['id']!r}")
        seen_ids.add(entry["id"])

    for entry in record["composition"]:
        entry_extra = set(entry.keys()) - COMPOSITION_KEYS
        if entry_extra:
            raise RecordValidationError(
                f"unexpected key(s) on a composition entry: {sorted(entry_extra)!r}: {entry!r}"
            )
        for key in COMPOSITION_KEYS:
            if key not in entry:
                raise RecordValidationError(
                    f"composition entry missing key {key!r}: {entry!r}"
                )

    for path in record["source_references"]:
        if not isinstance(path, str):
            raise RecordValidationError(
                f"source_references entries must be strings: {path!r}"
            )


def resolve_source_reference(ref: str) -> tuple[Path, int, int]:
    """Return `(path, start_line, end_line)` for a `"path:line"` /
    `"path:start-end"` reference, raising if it does not resolve inside the
    repository tree."""

    match = _SOURCE_REF_RE.match(ref)
    if not match:
        raise RecordValidationError(
            f"source_reference does not match path:line[-end]: {ref!r}"
        )
    rel_path = match.group("path")
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if end < start:
        raise RecordValidationError(f"source_reference end before start: {ref!r}")

    full_path = REPO_ROOT / rel_path
    if not full_path.is_file():
        raise RecordValidationError(f"source_reference path does not exist: {ref!r}")

    line_count = len(full_path.read_text(encoding="utf-8").splitlines())
    if start < 1 or end > line_count:
        raise RecordValidationError(
            f"source_reference line range {start}-{end} out of bounds for "
            f"{rel_path} ({line_count} lines): {ref!r}"
        )
    return full_path, start, end


def validate_source_references(record: dict[str, Any]) -> None:
    for entry in record["requirements"]:
        resolve_source_reference(entry["source_reference"])
    for entry in record["composition"]:
        resolve_source_reference(entry["source_reference"])
    for path in record["source_references"]:
        if not (REPO_ROOT / path).exists():
            raise RecordValidationError(
                f"top-level source_references path does not exist: {path!r}"
            )


# --------------------------------------------------------------------------
# Per-requirement fact checkers -- each re-derives its answer from the tree.
# --------------------------------------------------------------------------


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _decorator_is_listens_for(dec: ast.expr, target_name: str, event_name: str) -> bool:
    if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
        return False
    if dec.func.attr != "listens_for":
        return False
    if len(dec.args) < 2:
        return False
    target, event = dec.args[0], dec.args[1]
    return (
        isinstance(target, ast.Name)
        and target.id == target_name
        and isinstance(event, ast.Constant)
        and event.value == event_name
    )


def _session_imported_from_sqlalchemy(path: Path) -> bool:
    tree = python_ast(path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if not (module == "sqlalchemy" or module.startswith("sqlalchemy.")):
            continue
        if any(alias.name == "Session" for alias in node.names):
            return True
    return False


def check_tenant_guc_is_class_scoped_after_begin_listener() -> bool:
    path = REPO_ROOT / "app/services/session_hooks.py"
    tree = python_ast(path)
    func = _find_function(tree, "_apply_operator_tenant_scope")
    if func is None:
        return False
    if not any(
        _decorator_is_listens_for(dec, "Session", "after_begin")
        for dec in func.decorator_list
    ):
        return False
    return _session_imported_from_sqlalchemy(path)


def check_tenant_guc_fires_only_on_root_transactions() -> bool:
    path = REPO_ROOT / "app/services/session_hooks.py"
    tree = python_ast(path)
    func = _find_function(tree, "_apply_operator_tenant_scope")
    if func is None:
        return False
    for node in ast.walk(func):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        # `transaction.parent is not None`
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Attribute)
            and test.left.attr == "parent"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.IsNot)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
        ):
            has_return = any(isinstance(n, ast.Return) for n in ast.walk(node))
            if has_return:
                return True
    return False


def check_tenant_guc_value_is_transaction_local_set_config() -> bool:
    path = REPO_ROOT / "app/services/operator_tenant.py"
    tree = python_ast(path)
    func = _find_function(tree, "apply_operator_tenant_transaction_scope")
    if func is None:
        return False
    literal_found = False
    scalar_call_found = False
    for node in ast.walk(func):
        # The `true` third argument (PostgreSQL's `is_local` flag) is baked
        # into the SQL literal itself, not passed as a bound parameter --
        # finding this exact literal is therefore the whole check for both
        # the setting name and the SET LOCAL-equivalent semantics.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if (
                node.value
                == "SELECT set_config('app.current_tenant', :tenant_id, true)"
            ):
                literal_found = True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "scalar"
        ):
            scalar_call_found = True
    return literal_found and scalar_call_found


def check_isolation_modes_use_connection_execution_options_only() -> bool:
    path = REPO_ROOT / "app/db.py"
    tree = python_ast(path)
    for fn_name in ("begin_serializable_write", "begin_read_only_snapshot"):
        func = _find_function(tree, fn_name)
        if func is None:
            return False
        # First argument must be named `db`.
        if not func.args.args or func.args.args[0].arg != "db":
            return False
        calls_connection_with_execution_options = False
        forbidden_construction = False
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "connection"
                ):
                    if any(kw.arg == "execution_options" for kw in node.keywords):
                        calls_connection_with_execution_options = True
                fn_name_called = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else getattr(node.func, "attr", None)
                )
                if fn_name_called in {"create_engine", "sessionmaker"}:
                    forbidden_construction = True
        if not calls_connection_with_execution_options or forbidden_construction:
            return False
    return True


def _options_dict_values(name: str) -> dict[str, Any] | None:
    """Resolve `NAME: Final[...] = MappingProxyType({...})`'s dict literal.

    Handles both a plain `Assign` and an annotated `AnnAssign` (this
    module's constants carry a `Final[Mapping[str, object]]` annotation, so
    the real node is `AnnAssign`, not `Assign`).
    """

    path = REPO_ROOT / "app/db.py"
    tree = python_ast(path)
    for node in ast.walk(tree):
        value: ast.expr | None
        if isinstance(node, ast.Assign):
            if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                continue
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            if not (isinstance(node.target, ast.Name) and node.target.id == name):
                continue
            value = node.value
        else:
            continue
        if value is None:
            continue
        for sub in ast.walk(value):
            if isinstance(sub, ast.Dict):
                result: dict[str, Any] = {}
                for key, dict_value in zip(sub.keys, sub.values):
                    if isinstance(key, ast.Constant) and isinstance(
                        dict_value, ast.Constant
                    ):
                        result[key.value] = dict_value.value
                return result
    return None


def check_read_only_mode_is_repeatable_read_true() -> bool:
    values = _options_dict_values("READ_ONLY_SNAPSHOT_OPTIONS")
    return values == {"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}


def check_serializable_write_mode_is_serializable_false() -> bool:
    values = _options_dict_values("SERIALIZABLE_WRITE_OPTIONS")
    return values == {"isolation_level": "SERIALIZABLE", "postgresql_readonly": False}


def check_no_set_transaction_sql_is_issued_for_isolation_mode() -> bool:
    path = REPO_ROOT / "app/db.py"
    for node in python_nodes(path):
        if isinstance(node, ast.Call):
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if "SET TRANSACTION" in arg.value.upper():
                        return False
    return True


def _file_issues_no_set_transaction_sql(path: Path) -> bool:
    for node in python_nodes(path):
        if isinstance(node, ast.Call):
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if "SET TRANSACTION" in arg.value.upper():
                        return False
    return True


def check_tenant_guc_issues_no_set_transaction_sql() -> bool:
    """The Sub-side half of the declared ordering requirement below.

    The tenant-GUC `after_begin` listener itself -- `session_hooks.py`'s
    `_apply_operator_tenant_scope`, the function actually registered against
    `Session`'s `after_begin` event (see
    `tenant-guc-is-a-class-scoped-after-begin-listener`) -- and the function
    it calls to do the work, `operator_tenant.py`'s
    `apply_operator_tenant_transaction_scope`, between them never issue a
    literal `SET TRANSACTION` (they issue `set_config`, checked separately).
    Both files are swept: a `SET TRANSACTION` added directly to the listener
    in `session_hooks.py` -- bypassing `operator_tenant.py` entirely -- would
    be exactly as disqualifying as one added inside
    `apply_operator_tenant_transaction_scope`, and a checker that only swept
    `operator_tenant.py` would miss it. Because neither file issues one, the
    tenant-GUC hook can never compete for "first statement in the
    transaction" position with a Kernel-successor isolation pin applied via
    `execution_options` before `BEGIN`.

    This measures only the Sub-side half. It says nothing about, and cannot
    verify, when Starter's Kernel successor applies its isolation pin
    relative to `BEGIN` -- that fact lives in Starter's own tree.
    """

    hooks_path = REPO_ROOT / "app/services/session_hooks.py"
    tenant_path = REPO_ROOT / "app/services/operator_tenant.py"
    return _file_issues_no_set_transaction_sql(
        hooks_path
    ) and _file_issues_no_set_transaction_sql(tenant_path)


REQUIREMENT_CHECKS: dict[str, Callable[[], bool]] = {
    "tenant-guc-is-a-class-scoped-after-begin-listener": check_tenant_guc_is_class_scoped_after_begin_listener,
    "tenant-guc-fires-only-on-root-transactions": check_tenant_guc_fires_only_on_root_transactions,
    "tenant-guc-value-is-transaction-local-set-config": check_tenant_guc_value_is_transaction_local_set_config,
    "isolation-modes-apply-via-connection-execution-options-not-a-new-session-factory": (
        check_isolation_modes_use_connection_execution_options_only
    ),
    "read-only-mode-is-repeatable-read-plus-postgresql-readonly-true": check_read_only_mode_is_repeatable_read_true,
    "serializable-write-mode-is-serializable-plus-postgresql-readonly-false": (
        check_serializable_write_mode_is_serializable_false
    ),
    "no-set-transaction-sql-is-issued-for-isolation-mode": check_no_set_transaction_sql_is_issued_for_isolation_mode,
    "tenant-guc-ordering-is-compatible-with-a-pre-begin-kernel-isolation-pin": (
        check_tenant_guc_issues_no_set_transaction_sql
    ),
}


# --------------------------------------------------------------------------
# Per-composition fact checkers.
# --------------------------------------------------------------------------


def check_only_tenant_is_imported_from_kernel_models_no_database_runtime() -> bool:
    path = REPO_ROOT / "app/services/operator_tenant.py"
    tree = python_ast(path)
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "dotmac_kernel.models":
            imported_names.update(alias.name for alias in node.names)
    if imported_names != {"Tenant"}:
        return False
    # No DatabaseRuntime import anywhere under app/.
    for py_path in (REPO_ROOT / "app").rglob("*.py"):
        for node in python_nodes(py_path):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "dotmac_kernel.session_runtime"
            ):
                return False
            if isinstance(node, ast.Attribute) and node.attr == "DatabaseRuntime":
                return False
    return True


def check_kernel_pin_is_exact_a94() -> bool:
    import tomllib

    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    main_deps = data["project"]["dependencies"]
    kernel_main = [
        d for d in main_deps if d.replace(" ", "").startswith("dotmac-kernel")
    ]
    return kernel_main == ["dotmac-kernel==0.1.0a94"]


COMPOSITION_CHECKS: dict[str, Callable[[], bool]] = {
    (
        "Sub imports exactly one name from dotmac_kernel.models -- Tenant -- to "
        "model its single operator tenant; it does not import or construct "
        "dotmac_kernel.session_runtime.DatabaseRuntime anywhere, and app/db.py "
        "remains Sub's own session and transaction authority."
    ): check_only_tenant_is_imported_from_kernel_models_no_database_runtime,
    (
        "Sub pins dotmac-kernel at exactly 0.1.0a94 (no range) from the private "
        "forgejo index in [project.dependencies]."
    ): check_kernel_pin_is_exact_a94,
}


def validate_record(record: dict[str, Any]) -> None:
    """Full validation: envelope shape, source_reference resolution, and
    every requirement/composition fact re-derived and compared."""

    validate_envelope(record)
    validate_source_references(record)

    for entry in record["requirements"]:
        checker = REQUIREMENT_CHECKS.get(entry["id"])
        if checker is None:
            raise RecordValidationError(
                f"no registered checker for requirement id {entry['id']!r}"
            )
        actual = checker()
        if actual != entry["satisfied"]:
            raise RecordValidationError(
                f"requirement {entry['id']!r} claims satisfied={entry['satisfied']!r} "
                f"but the tree shows {actual!r}"
            )

    for entry in record["composition"]:
        checker = COMPOSITION_CHECKS.get(entry["declaration"])
        if checker is None:
            raise RecordValidationError(
                f"no registered checker for composition declaration: {entry['declaration']!r}"
            )
        if not checker():
            raise RecordValidationError(
                f"composition declaration not supported by the tree: {entry['declaration']!r}"
            )
