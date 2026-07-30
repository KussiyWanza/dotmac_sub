"""Guard: the mypy gate must keep meaning what it says.

`mypy app` printed "Success: no issues found in 1704 source files" while
`check_untyped_defs` was off (so it skipped the body of every untyped def,
which is most of this codebase), `warn_unused_ignores` was off (so a
`# type: ignore` outlived its error and masked the next one to land on that
line), and `ignore_errors` excused 101 modules — thirteen of which had not
existed for months. A green gate that checks nothing is worse than a red one.

These tests fix the honest shape: the two flags stay on, a mask entry must
name a module that exists, and the mask list may only shrink.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
BASELINE = Path(__file__).parent / "type_gate_masked_module_baseline.txt"


def _mypy_config() -> dict:
    return tomllib.loads(PYPROJECT.read_text())["tool"]["mypy"]


def _masked_modules() -> list[str]:
    """Application modules excused from type checking by `ignore_errors`.

    The `tests` / `tests.*` mask is a different decision (the suite is not
    type-checked at all) and is out of scope here.
    """
    masked: list[str] = []
    for override in _mypy_config().get("overrides", []):
        if not override.get("ignore_errors"):
            continue
        masked.extend(
            module
            for module in override["module"]
            if module == "app" or module.startswith("app.")
        )
    return masked


def _module_exists(module: str) -> bool:
    path = REPO_ROOT / Path(*module.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def _baseline_count() -> int:
    for line in BASELINE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return int(line)
    raise AssertionError(f"{BASELINE.name} holds no count")


def test_check_untyped_defs_and_warn_unused_ignores_stay_enabled():
    config = _mypy_config()
    disabled = [
        flag
        for flag in ("check_untyped_defs", "warn_unused_ignores")
        if config.get(flag) is not True
    ]
    assert not disabled, (
        f"[tool.mypy] {', '.join(disabled)} must stay true. Turning either off "
        "makes `mypy app` report success without checking untyped function "
        "bodies, or lets dead `# type: ignore` comments accumulate and mask "
        "real errors. Fix the errors instead."
    )


def test_every_masked_module_exists():
    missing = sorted({m for m in _masked_modules() if not _module_exists(m)})
    assert not missing, (
        f"ignore_errors names modules that do not exist: {missing}. Delete the "
        "entries — a stale mask implies debt that is already paid and would "
        "silently re-mask the name if it were ever reused."
    )


def test_masked_module_count_only_shrinks():
    masked = _masked_modules()
    baseline = _baseline_count()
    assert len(masked) <= baseline, (
        f"{len(masked)} app modules are masked by ignore_errors, above the "
        f"baseline of {baseline}. Type-check the new module instead of adding "
        "it to the mask."
    )
    assert len(masked) == baseline, (
        f"{len(masked)} app modules are masked, below the baseline of "
        f"{baseline} — lower the number in {BASELINE.name} so the gate holds "
        "the ground you just took."
    )


def test_masked_modules_are_not_listed_twice():
    masked = _masked_modules()
    duplicates = sorted({m for m in masked if masked.count(m) > 1})
    assert not duplicates, (
        f"Modules masked more than once: {duplicates}. A duplicate inflates "
        "the baseline and survives one deletion unnoticed."
    )
