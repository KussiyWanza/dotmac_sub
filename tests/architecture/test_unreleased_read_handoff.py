"""Keep adapter decision-input reads out of owner-command entry."""

from __future__ import annotations

from collections import Counter

from scripts.architecture import sot_debt


def _current() -> Counter[tuple[str, str]]:
    return Counter(
        {
            (handoff.resolver, handoff.path): handoff.count
            for handoff in sot_debt.unreleased_read_handoffs()
        }
    )


def _format(entries: dict[tuple[str, str], int]) -> str:
    return "\n  ".join(
        f"{resolver} {count} {path}"
        for (resolver, path), count in sorted(entries.items())
    )


def test_adapter_identification_still_resolves() -> None:
    """Guard the guard.

    This scanner treats an undeclared ``app/services/web_*.py`` module as an
    adapter. If the registry declaration detector stopped matching, every
    presenter would be scanned as an adapter and the baseline would look
    enormous for a reason that has nothing to do with this rule.
    """

    declared = sot_debt.declared_service_modules()
    assert len(declared) > 50, (
        f"only {len(declared)} declared service modules found; the registry "
        "declaration format has probably changed and this scanner is measuring "
        "the wrong thing"
    )
    assert any(module.startswith("app.services.web_") for module in declared), (
        "no web_* module is declared a service; the split this scanner depends "
        "on has collapsed"
    )


def test_no_new_or_expanded_unreleased_read_handoffs() -> None:
    current = _current()
    baseline = sot_debt.read_count_baseline(sot_debt.UNRELEASED_READ_HANDOFF_BASELINE)
    expanded = {
        key: count for key, count in current.items() if count > baseline.get(key, 0)
    }

    assert not expanded, (
        "adapters gained decision-input reads that hand an open read "
        "transaction onward. Resolve the inputs, call "
        "db_session_adapter.release_read_transaction, then enter the owner "
        "command; do not expand the migration baseline:\n  " + _format(expanded)
    )


def test_unreleased_read_handoff_baseline_only_shrinks() -> None:
    current = _current()
    baseline = sot_debt.read_count_baseline(sot_debt.UNRELEASED_READ_HANDOFF_BASELINE)
    resolved = {
        key: baseline_count - current.get(key, 0)
        for key, baseline_count in baseline.items()
        if current.get(key, 0) < baseline_count
    }

    assert not resolved, (
        "unreleased read-handoff debt shrank; reduce or remove these baseline "
        "entries so the repair is permanent:\n  " + _format(resolved)
    )
