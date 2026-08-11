"""Guard canonical decision-input resolvers against new parallel sources.

The baseline is migration debt, not an allowlist. A direct environment read or
raw ``DomainSetting`` reference outside the declared owners must not be added.
Existing debt is counted so it can only shrink; a resolved occurrence requires
the matching baseline count to be reduced.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
BASELINE = Path(__file__).with_name("decision_input_bypass_baseline.txt")

# These modules own deployment/bootstrap inputs or a single infrastructure or
# secret resolver. This list is intentionally narrow: adding an owner requires
# an explicit source-of-truth decision, not merely a baseline update.
DECLARED_ENV_INPUT_OWNERS = {
    "app/celery_app.py",
    "app/config.py",
    "app/monitoring.py",
    "app/services/credential_crypto.py",
    "app/services/radius_dsn.py",
    "app/services/redis_client.py",
    "app/services/scheduler_config.py",
    "app/services/secrets.py",
    "app/services/settings_seed.py",
    "app/services/wireguard_crypto.py",
    "app/telemetry.py",
    "app/version.py",
}

# Persistence, typed resolution/bootstrap, migration, capability composition,
# and the read-only admin projection are the only raw model consumers. Business
# callers use settings_spec/domain services instead.
DECLARED_RAW_SETTING_OWNERS = {
    "app/services/control_registry.py",
    "app/services/domain_settings.py",
    "app/services/settings_secret_cleanup.py",
    "app/services/settings_seed.py",
    "app/services/settings_spec.py",
    # One-time migration-cutover owner (operations.service_team_source_retirement
    # in the SOT registry): it retires the raw legacy setting source itself, so
    # it must read and deactivate the raw rows rather than use typed resolution.
    "app/services/service_team_source_retirement.py",
    "app/services/web_control_plane.py",
    # The history recorder OBSERVES that model rather than reading settings
    # through it. Its listeners are attached to `DomainSetting` precisely
    # because the service layer is not the only writer — the seed, the generic
    # settings API and several domain services persist rows directly, and a
    # history that covered only the typed resolver would read as a complete
    # record while missing most changes. It resolves no setting and decides
    # nothing; it records what a write did.
    "app/models/domain_setting_history.py",
}


def _relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


@cache
def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _environment_aliases(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    os_modules: set[str] = set()
    getenv_names: set[str] = set()
    environ_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "getenv":
                    getenv_names.add(alias.asname or alias.name)
                elif alias.name == "environ":
                    environ_names.add(alias.asname or alias.name)
    return os_modules, getenv_names, environ_names


def _is_os_environ(
    node: ast.AST, os_modules: set[str], environ_names: set[str]
) -> bool:
    return (
        (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in os_modules
            and node.attr == "environ"
        )
        or isinstance(node, ast.Name)
        and node.id in environ_names
    )


def _direct_env_read_count(path: Path) -> int:
    """Count direct stdlib environment reads, including imported aliases."""

    tree = _tree(path)
    os_modules, getenv_names, environ_names = _environment_aliases(tree)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id in os_modules
                    and func.attr == "getenv"
                )
                or isinstance(func, ast.Name)
                and func.id in getenv_names
            ):
                count += 1
                continue
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and _is_os_environ(func.value, os_modules, environ_names)
            ):
                count += 1
        elif isinstance(node, ast.Subscript) and _is_os_environ(
            node.value, os_modules, environ_names
        ):
            count += 1
    return count


def _raw_setting_reference_count(path: Path) -> int:
    tree = _tree(path)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module not in {
            "app.models",
            "app.models.domain_settings",
        }:
            continue
        for alias in node.names:
            if alias.name == "DomainSetting":
                names.add(alias.asname or alias.name)
    return sum(
        isinstance(node, ast.Name) and node.id in names for node in ast.walk(tree)
    )


@cache
def _actual_bypasses() -> dict[tuple[str, str], int]:
    actual: dict[tuple[str, str], int] = {}
    for path in APP_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        relative = _relative(path)
        env_count = _direct_env_read_count(path)
        if env_count and relative not in DECLARED_ENV_INPUT_OWNERS:
            actual[("env", relative)] = env_count
        setting_count = _raw_setting_reference_count(path)
        if setting_count and relative not in DECLARED_RAW_SETTING_OWNERS:
            actual[("setting", relative)] = setting_count
    return actual


@cache
def _baseline() -> dict[tuple[str, str], int]:
    entries: dict[tuple[str, str], int] = {}
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        kind, raw_count, path = stripped.split(maxsplit=2)
        entries[(kind, path)] = int(raw_count)
    return entries


def _display(entries: dict[tuple[str, str], int]) -> str:
    return "\n  ".join(
        f"{kind} {count} {path}" for (kind, path), count in sorted(entries.items())
    )


def test_no_new_decision_input_bypasses() -> None:
    actual = _actual_bypasses()
    baseline = _baseline()
    additions = {
        key: count
        for key, count in actual.items()
        if key not in baseline or count > baseline[key]
    }
    assert not additions, (
        "new direct decision-input bypasses were added. Use the named resolver; "
        "a new source owner requires an explicit SOT decision:\n  "
        + _display(additions)
    )


def test_decision_input_bypass_baseline_only_shrinks() -> None:
    actual = _actual_bypasses()
    baseline = _baseline()
    stale = {
        key: count
        for key, count in baseline.items()
        if key not in actual or actual[key] < count
    }
    assert not stale, (
        "decision-input bypass debt shrank; reduce or remove these baseline "
        "entries:\n  " + _display(stale)
    )


def test_runtime_setting_resolver_has_no_environment_source() -> None:
    resolver = PROJECT_ROOT / "app/services/settings_spec.py"
    assert _direct_env_read_count(resolver) == 0, (
        "settings_spec runtime resolution must remain DB-authoritative; "
        "environment values belong only to bootstrap/sync paths"
    )
