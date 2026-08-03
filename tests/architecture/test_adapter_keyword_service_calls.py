"""Adapters must not call owning services with long positional argument lists.

The route/service boundary has no arity check. When a service signature gains a
parameter in the middle, every positional argument after it silently rebinds:
`UserCredentials.list` gained `person_id`, so `order_dir="desc"` arrived as
`order_by` and `/api/v1/user-credentials`, `/api/v1/api-keys` and
`/api/v1/mfa-methods` answered 400 to every request until issue #1895 found it.
No unit test saw it, because each side was individually correct.

This guard ratchets the remaining debt down: existing modules may shrink but
never grow, and a module not listed in the baseline must have none.
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.adapter_keyword_service_call import (
    MAX_POSITIONAL_ARGS,
    counts_by_module,
    violations_for,
)

BASELINE = Path("tests/architecture/adapter_keyword_service_call_baseline.txt")


def _baseline() -> dict[str, int]:
    allowed: dict[str, int] = {}
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path, _, count = line.rpartition(" ")
        allowed[path] = int(count)
    return allowed


def test_no_new_positional_service_calls_in_adapters() -> None:
    allowed = _baseline()
    current = counts_by_module()

    new_modules = sorted(set(current) - set(allowed))
    assert not new_modules, (
        "These adapters call services with more than "
        f"{MAX_POSITIONAL_ARGS} positional arguments. Pass keywords instead — "
        "a mid-signature parameter insertion silently rebinds positional "
        f"arguments (see issue #1895): {new_modules}"
    )

    grew = sorted(
        f"{path}: {count} > {allowed[path]} allowed"
        for path, count in current.items()
        if count > allowed[path]
    )
    assert not grew, (
        "New long positional service calls were added to modules that still "
        f"carry this debt; pass keywords in the new call sites: {grew}"
    )


def test_baseline_has_no_stale_entries() -> None:
    """A module that no longer offends must leave the baseline, so the debt
    cannot silently reappear under cover of an old allowance."""

    current = counts_by_module()
    stale = sorted(
        f"{path} (recorded {count}, now {current.get(path, 0)})"
        for path, count in _baseline().items()
        if current.get(path, 0) < count
    )
    assert not stale, (
        "These baseline counts are higher than reality — lower or remove them "
        f"so the ratchet keeps its grip: {stale}"
    )


def test_repaired_auth_routes_stay_keyword_only() -> None:
    """The routes that produced the original defect must not regress."""

    assert not violations_for(Path("app/api/auth.py"))
