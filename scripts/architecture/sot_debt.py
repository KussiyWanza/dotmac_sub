"""Reproducible source-of-truth governance debt inventory.

The report produced by this module is diagnostic evidence. Baseline files are
shrink-only migration ledgers; they do not grant ownership or approve a
violation. Architecture tests import the same scanners so the report and CI
cannot silently disagree about what they count.
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import Counter
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE_DIR = PROJECT_ROOT / "app" / "services"
ARCHITECTURE_TEST_DIR = PROJECT_ROOT / "tests" / "architecture"
WRITER_BASELINE = ARCHITECTURE_TEST_DIR / "sot_writer_baseline.txt"
DECISION_INPUT_BASELINE = ARCHITECTURE_TEST_DIR / "decision_input_bypass_baseline.txt"
ADAPTER_TRANSACTION_BASELINE = (
    ARCHITECTURE_TEST_DIR / "adapter_transaction_baseline.txt"
)
HTTP_EXCEPTION_BASELINE = ARCHITECTURE_TEST_DIR / "service_http_exception_baseline.txt"
LEGACY_MANIFEST_BASELINE = ARCHITECTURE_TEST_DIR / "sot_manifest_legacy_baseline.txt"
MIGRATION_ENUM_BASELINE = ARCHITECTURE_TEST_DIR / "migration_enum_creation_baseline.txt"
MIGRATION_DIR = PROJECT_ROOT / "alembic" / "versions"

ADAPTER_ROOTS = (
    PROJECT_ROOT / "app" / "api",
    PROJECT_ROOT / "app" / "tasks",
    PROJECT_ROOT / "app" / "web",
    PROJECT_ROOT / "app" / "services" / "events" / "handlers",
)

_PERSISTENCE_MUTATORS = {
    "add",
    "add_all",
    "bulk_insert_mappings",
    "bulk_save_objects",
    "bulk_update_mappings",
    "commit",
    "delete",
    "flush",
}
_PERSISTENCE_RECEIVER_TOKENS = {
    "cache",
    "conn",
    "connection",
    "db",
    "query",
    "redis",
    "session",
    "uow",
}
_ADAPTER_TRANSACTION_METHODS = {
    "begin",
    "begin_nested",
    "commit",
    "flush",
    "rollback",
}
_ADAPTER_TRANSACTION_HELPERS = {
    "UnitOfWork",
    "execute_owner_command",
    "form_write",
    "get_uow",
    "task_session",
}


@dataclass(frozen=True, order=True)
class AdapterTransactionUse:
    """One counted adapter-owned transaction operation."""

    operation: str
    count: int
    path: str


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def module_name(path: Path, *, project_root: Path = PROJECT_ROOT) -> str:
    """Return a Python module name for a file below ``project_root``."""

    relative = path.relative_to(project_root).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join(parts)


def receiver_tokens(node: ast.AST) -> set[str]:
    """Return normalized identifier tokens from a method-call receiver."""

    if isinstance(node, ast.Name):
        return set(node.id.lower().split("_"))
    if isinstance(node, ast.Attribute):
        return receiver_tokens(node.value) | set(node.attr.lower().split("_"))
    if isinstance(node, ast.Call):
        return receiver_tokens(node.func)
    if isinstance(node, ast.Subscript):
        return receiver_tokens(node.value)
    return set()


def has_persistence_mutation(path: Path) -> bool:
    """Return whether a module contains a persistence-like method call."""

    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _PERSISTENCE_MUTATORS
        and bool(receiver_tokens(node.func.value) & _PERSISTENCE_RECEIVER_TOKENS)
        for node in ast.walk(_tree(path))
    )


@cache
def persistence_writer_modules(
    *,
    service_dir: Path = SERVICE_DIR,
    project_root: Path = PROJECT_ROOT,
) -> set[str]:
    """Return service modules with persistence-like mutations."""

    return {
        module_name(path, project_root=project_root)
        for path in service_dir.rglob("*.py")
        if path.is_file()
        and path.name != "sot_relationships.py"
        and "__pycache__" not in path.parts
        and has_persistence_mutation(path)
    }


@cache
def served_ipv4_projection_writes(
    *,
    app_dir: Path = PROJECT_ROOT / "app",
    project_root: Path = PROJECT_ROOT,
) -> dict[str, int]:
    """Return per-file counts of direct writes to the served IPv4 projection.

    ``network.ip_assignment_lifecycle`` owns the desired IPv4 at exact service
    grain; ``Subscription.ipv4_address`` is its compatibility projection
    (``app/models/catalog.py``, ``docs/SOT_RELATIONSHIP_MAP.md``). A write that
    does not come from the owner creates a served address with no IPAM record —
    the ``assignment_missing`` cohort — or a served value that silently
    disagrees with its assignment.

    Only ``<something>.ipv4_address = ...`` counts. ``ipv4_address_id`` is the
    IPAM foreign key on ``IPAssignment``, not the projection, and is excluded by
    the exact attribute match.
    """

    counts: dict[str, int] = {}
    for path in sorted(app_dir.rglob("*.py")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            tree = _tree(path)
        except SyntaxError:  # pragma: no cover - syntax checked elsewhere
            continue
        hits = 0
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "ipv4_address":
                    hits += 1
        if hits:
            counts[str(path.relative_to(project_root))] = hits
    return counts


def declared_owner_modules(domains: Iterable[Any]) -> set[str]:
    """Return every module declared as an owner in the registry."""

    return {service.module for domain in domains for service in domain.services}


def undeclared_writer_modules(domains: Iterable[Any]) -> set[str]:
    """Return current writer modules absent from the ownership registry."""

    return persistence_writer_modules() - declared_owner_modules(domains)


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


@cache
def adapter_transaction_uses(
    *, roots: Sequence[Path] = ADAPTER_ROOTS
) -> tuple[AdapterTransactionUse, ...]:
    """Count transaction methods and legacy transaction helpers in adapters."""

    counts: Counter[tuple[str, str]] = Counter()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            for node in ast.walk(_tree(path)):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute):
                    method = node.func.attr
                    if (
                        method in _ADAPTER_TRANSACTION_METHODS
                        and receiver_tokens(node.func.value)
                        & _PERSISTENCE_RECEIVER_TOKENS
                    ):
                        counts[(method, relative)] += 1
                helper = _call_name(node.func)
                if helper in _ADAPTER_TRANSACTION_HELPERS:
                    counts[(f"helper:{helper}", relative)] += 1

    return tuple(
        AdapterTransactionUse(operation, count, path)
        for (operation, path), count in sorted(counts.items())
    )


@cache
def read_count_baseline(path: Path) -> Counter[tuple[str, str]]:
    """Read ``kind count path`` entries from a shrink-only baseline."""

    entries: Counter[tuple[str, str]] = Counter()
    if not path.exists():
        return entries
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            kind, raw_count, relative = line.split(maxsplit=2)
            count = int(raw_count)
        except ValueError as exc:
            raise ValueError(
                f"invalid baseline entry {path}:{line_number}: {raw_line!r}"
            ) from exc
        if count < 1:
            raise ValueError(f"baseline count must be positive at {path}:{line_number}")
        entries[(kind, relative)] += count
    return entries


@cache
def read_name_baseline(path: Path) -> set[str]:
    """Read one-name-per-line entries from a shrink-only baseline."""

    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


@cache
def service_http_exception_files() -> set[str]:
    """Return service modules that use a FastAPI ``HTTPException`` symbol."""

    offenders: set[str] = set()
    for path in SERVICE_DIR.rglob("*.py"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        tree = _tree(path)
        aliases: set[str] = set()
        fastapi_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in {
                "fastapi",
                "fastapi.exceptions",
            }:
                aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "HTTPException"
                )
            elif isinstance(node, ast.Import):
                fastapi_aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "fastapi"
                )
        used = any(
            isinstance(node, ast.Name)
            and node.id in aliases
            or isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in fastapi_aliases
            and node.attr == "HTTPException"
            for node in ast.walk(tree)
        )
        if used:
            offenders.add(path.relative_to(PROJECT_ROOT).as_posix())
    return offenders


def exact_concern_duplicates(domains: Iterable[Any]) -> dict[str, list[str]]:
    """Return normalized concern strings claimed by more than one service."""

    owners: dict[str, list[str]] = {}
    for domain in domains:
        for service in domain.services:
            for concern in service.owns:
                owners.setdefault(concern.strip().casefold(), []).append(service.name)
    return {
        concern: names for concern, names in sorted(owners.items()) if len(names) > 1
    }


def build_report(domains: Sequence[Any]) -> dict[str, Any]:
    """Build the deterministic governance-debt report."""

    services = [service for domain in domains for service in domain.services]
    undeclared = undeclared_writer_modules(domains)
    writer_baseline = read_name_baseline(WRITER_BASELINE)
    decision_baseline = read_count_baseline(DECISION_INPUT_BASELINE)
    adapter_uses = adapter_transaction_uses()
    adapter_counts = Counter(
        {(use.operation, use.path): use.count for use in adapter_uses}
    )
    adapter_baseline = read_count_baseline(ADAPTER_TRANSACTION_BASELINE)
    http_exception_files = service_http_exception_files()
    http_exception_baseline = read_name_baseline(HTTP_EXCEPTION_BASELINE)
    legacy_manifest = {
        service.name for service in services if not service.is_contracted
    }
    legacy_manifest_baseline = read_name_baseline(LEGACY_MANIFEST_BASELINE)

    return {
        "registry": {
            "domains": len(domains),
            "services": len(services),
            "duplicate_exact_concerns": exact_concern_duplicates(domains),
        },
        "manifest_contracts": {
            "contracted": len(services) - len(legacy_manifest),
            "legacy": len(legacy_manifest),
            "legacy_baseline": len(legacy_manifest_baseline),
            "new_legacy": sorted(legacy_manifest - legacy_manifest_baseline),
            "contracted_not_removed_from_baseline": sorted(
                legacy_manifest_baseline - legacy_manifest
            ),
        },
        "undeclared_service_writers": {
            "current": len(undeclared),
            "baseline": len(writer_baseline),
            "new": sorted(undeclared - writer_baseline),
            "resolved_not_removed_from_baseline": sorted(writer_baseline - undeclared),
        },
        "decision_input_bypasses": {
            "occurrences": sum(decision_baseline.values()),
            "files": len({path for _kind, path in decision_baseline}),
            "by_kind": dict(
                sorted(
                    Counter(
                        {
                            kind: sum(
                                count
                                for (
                                    entry_kind,
                                    _path,
                                ), count in decision_baseline.items()
                                if entry_kind == kind
                            )
                            for kind, _path in decision_baseline
                        }
                    ).items()
                )
            ),
        },
        "adapter_transactions": {
            "occurrences": sum(adapter_counts.values()),
            "files": len({path for _kind, path in adapter_counts}),
            "baseline_occurrences": sum(adapter_baseline.values()),
            "baseline_files": len({path for _kind, path in adapter_baseline}),
            "uses": [asdict(use) for use in adapter_uses],
        },
        "service_http_exceptions": {
            "current": len(http_exception_files),
            "baseline": len(http_exception_baseline),
            "new": sorted(http_exception_files - http_exception_baseline),
            "resolved_not_removed_from_baseline": sorted(
                http_exception_baseline - http_exception_files
            ),
        },
    }


@dataclass(frozen=True)
class MigrationEnumEmitter:
    """One place a migration can emit ``CREATE TYPE`` for a named enum."""

    enum_name: str
    path: str
    kind: str  # "explicit" (a checked .create()) or "auto" (a column declaration)


_ENUM_FACTORIES = {"Enum", "ENUM"}


def _enum_construction(node: ast.AST) -> tuple[str, bool] | None:
    """Return ``(type_name, auto_creates)`` for an Enum/ENUM construction."""

    if not isinstance(node, ast.Call):
        return None
    func = node.func
    attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if attr not in _ENUM_FACTORIES:
        return None
    type_name = None
    auto_creates = True
    for keyword in node.keywords:
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
            type_name = keyword.value.value
        # postgresql.ENUM(create_type=False) leaves creation to an explicit owner.
        if keyword.arg == "create_type" and isinstance(keyword.value, ast.Constant):
            auto_creates = bool(keyword.value.value)
    if not isinstance(type_name, str) or not type_name:
        return None
    return type_name, auto_creates


def _migration_enum_emitters_for(path: Path) -> list[MigrationEnumEmitter]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []

    # Only ``upgrade`` can emit CREATE TYPE. Scoping to it also stops a
    # ``downgrade`` that rebinds the same variable to a bare ``sa.Enum`` (for a
    # ``.drop()``) from shadowing the create_type=False definition upgrade used.
    upgrade_fn = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
        ),
        None,
    )
    if upgrade_fn is None:
        return []
    scopes: list[ast.AST] = [
        node for node in tree.body if not isinstance(node, ast.FunctionDef)
    ]
    scopes.append(upgrade_fn)

    bound: dict[str, tuple[str, bool]] = {}
    groups: dict[str, list[str]] = {}
    for scope in scopes:
        for node in ast.walk(scope):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            construction = _enum_construction(node.value)
            if construction is not None:
                bound[target.id] = construction
            elif isinstance(node.value, ast.Tuple | ast.List):
                members = [e.id for e in node.value.elts if isinstance(e, ast.Name)]
                if members:
                    groups[target.id] = members

    # Names an explicit `.create(...)` owns, directly or through a group tuple
    # the migration iterates (`for enum_type in _ALL_ENUMS: enum_type.create(...)`).
    explicit: set[str] = set()
    loop_sources: dict[str, str] = {}
    for node in ast.walk(upgrade_fn):
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and isinstance(node.iter, ast.Name)
        ):
            loop_sources[node.target.id] = node.iter.id

    created_vars: set[str] = set()
    for node in ast.walk(upgrade_fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create"
            and isinstance(node.func.value, ast.Name)
        ):
            created_vars.add(node.func.value.id)

    for var in created_vars:
        if var in bound:
            explicit.add(bound[var][0])
        source = loop_sources.get(var)
        if source:
            for member in groups.get(source, []):
                if member in bound:
                    explicit.add(bound[member][0])

    # Auto-creation fires from SQLAlchemy's `_on_table_create` hook, so only a
    # reference inside `create_table` emits CREATE TYPE. `add_column` does not —
    # which is why migrations that add an enum column must create the type
    # themselves, and why counting every reference would be a false positive.
    seen_auto: set[str] = set()
    for node in ast.walk(upgrade_fn):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"create_table", "Table"}
        ):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and inner.id in bound:
                type_name, auto_creates = bound[inner.id]
                if auto_creates:
                    seen_auto.add(type_name)
            construction = _enum_construction(inner)
            if construction is not None and construction[1]:
                seen_auto.add(construction[0])

    emitters: list[MigrationEnumEmitter] = []
    try:
        relative = path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:  # a scanner self-test fixture outside the project
        relative = path.name

    for type_name in sorted(explicit):
        emitters.append(MigrationEnumEmitter(type_name, relative, "explicit"))
    for type_name in sorted(seen_auto):
        emitters.append(MigrationEnumEmitter(type_name, relative, "auto"))
    return emitters


def migration_enum_emitters() -> list[MigrationEnumEmitter]:
    """Every place any migration can emit ``CREATE TYPE`` for a named enum.

    A named PostgreSQL type may only be created once. Alembic's fresh-database
    path hides duplicates: ``001`` builds the schema from model metadata, so
    ``_safe_create_table`` skips the tables whose creation would have emitted the
    second ``CREATE TYPE``. Only an incremental database reaches it, which is why
    this is enforced statically rather than left to the migration suite.
    """

    if not MIGRATION_DIR.exists():
        return []
    emitters: list[MigrationEnumEmitter] = []
    for path in sorted(MIGRATION_DIR.glob("*.py")):
        emitters.extend(_migration_enum_emitters_for(path))
    return emitters


def duplicated_migration_enums() -> Counter[tuple[str, str]]:
    """Emitters for enum names that more than one place can create."""

    emitters = migration_enum_emitters()
    per_name: Counter[str] = Counter(e.enum_name for e in emitters)
    duplicated: Counter[tuple[str, str]] = Counter()
    for emitter in emitters:
        if per_name[emitter.enum_name] > 1:
            duplicated[(emitter.enum_name, emitter.path)] += 1
    return duplicated


def main() -> int:
    """Print the current report as stable JSON."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact JSON instead of indented JSON",
    )
    args = parser.parse_args()

    from app.services.sot_registry.registry import DOMAIN_SOT_RELATIONSHIPS

    indent = None if args.compact else 2
    print(
        json.dumps(
            build_report(DOMAIN_SOT_RELATIONSHIPS), indent=indent, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
