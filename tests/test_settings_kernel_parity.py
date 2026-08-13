"""Every spec resolves to the same value through the kernel as it did before.

The cutover's evidence. Michael's ruling was one cutover rather than a staged
shadow, with parity proven over all specs rather than sampled — so this is that
proof, carried in the change that causes it rather than as a separate phase.

It compares the KERNEL's answer against Sub's former resolution rules, which
are reproduced here as `_legacy_resolution` rather than referenced, because the
point is to pin the behaviour that existed before `resolve_value` was rewritten.
When this test and the kernel disagree, one of them is wrong and the
disagreement is the finding.

Two deltas are expected and asserted as such rather than tolerated silently:

- **Unseeded env-declared specs.** The kernel materialises `env_var` into rows
  via `seed_settings_from_env`; a spec with no row and no seed still resolves
  to its default here because this test sets no environment.
- **Nothing else.** Any other difference is a defect.
"""

from __future__ import annotations

import pytest

from app.models.domain_settings import SettingDomain
from app.services.operator_tenant import provision_operator_tenant
from app.services.settings_kernel_bridge import kernel_specs, to_kernel_spec
from app.services.settings_spec import SETTINGS_SPECS, resolve_value


def _legacy_resolution(spec) -> object:
    """What Sub's resolver returned for a spec with NO stored row.

    The pre-cutover rules, reproduced: the default, coerced, with `allowed` and
    the integer bounds applied. A row-less resolution is the case every spec
    has in a fresh database, which is what makes it checkable for all 560.
    """

    from app.models.subscription_engine import SettingValueType
    from app.services.settings_spec import _coerce_int_value, coerce_value

    value, error = coerce_value(spec, spec.default)
    if error:
        value = spec.default
    if spec.allowed and value is not None and value not in spec.allowed:
        value = spec.default
    if spec.value_type == SettingValueType.integer and value is not None:
        parsed = _coerce_int_value(value)
        if parsed is None:
            parsed = spec.default if isinstance(spec.default, int) else None
        if (
            spec.min_value is not None
            and parsed is not None
            and parsed < spec.min_value
        ):
            parsed = spec.default if isinstance(spec.default, int) else None
        if (
            spec.max_value is not None
            and parsed is not None
            and parsed > spec.max_value
        ):
            parsed = spec.default if isinstance(spec.default, int) else None
        value = parsed
    return value


def test_every_spec_is_registered_with_the_kernel() -> None:
    """A spec the kernel does not know resolves to its default forever."""

    from dotmac_kernel.settings_resolver import all_specs

    registered = {(str(s.domain), s.key) for s in all_specs()}
    declared = {(str(s.domain), s.key) for s in SETTINGS_SPECS}
    missing = sorted(declared - registered)

    assert not missing, (
        f"{len(missing)} Sub specs are not registered with the kernel; each "
        f"would silently resolve to its default: {missing[:10]}"
    )


def test_the_translation_preserves_every_declared_fact() -> None:
    """Shape is the thing being handed over; a dropped bound is a silent bug."""

    mismatches: list[str] = []
    for spec in SETTINGS_SPECS:
        k = to_kernel_spec(spec)
        for field, mine, theirs in (
            ("value_type", spec.value_type.value, str(k.value_type)),
            ("default", spec.default, k.default),
            ("allowed", spec.allowed, k.allowed),
            ("min_value", spec.min_value, k.min_value),
            ("max_value", spec.max_value, k.max_value),
            ("is_secret", spec.is_secret, k.is_secret),
            ("env_var", spec.env_var, k.env_var),
        ):
            if mine != theirs:
                mismatches.append(
                    f"{spec.domain}.{spec.key}.{field}: {mine!r} != {theirs!r}"
                )

    assert not mismatches, (
        "translation dropped or changed declared facts:\n  "
        + "\n  ".join(mismatches[:20])
    )


def test_every_spec_resolves_identically_through_the_kernel(db_session) -> None:
    """The cutover's actual claim, over all 560 specs, not a sample."""

    provision_operator_tenant(db_session)

    differences: list[str] = []
    for spec in SETTINGS_SPECS:
        expected = _legacy_resolution(spec)
        actual = resolve_value(db_session, spec.domain, spec.key)
        if actual != expected:
            differences.append(
                f"{spec.domain}.{spec.key}: kernel={actual!r} legacy={expected!r}"
            )

    assert not differences, (
        f"{len(differences)} of {len(SETTINGS_SPECS)} specs resolve differently "
        "through the kernel:\n  " + "\n  ".join(differences[:25])
    )


def test_a_stored_row_still_wins_over_the_default(db_session) -> None:
    """Parity on defaults alone would pass with the database ignored."""

    from app.models.domain_settings import DomainSetting
    from app.models.subscription_engine import SettingValueType

    tenant = provision_operator_tenant(db_session)
    db_session.add(
        DomainSetting(
            tenant_id=tenant.id,
            scope_kind="tenant",
            domain=SettingDomain.imports,
            key="max_rows",
            value_type=SettingValueType.integer,
            value_text="4242",
            is_active=True,
        )
    )
    db_session.commit()

    assert resolve_value(db_session, SettingDomain.imports, "max_rows") == 4242


def test_an_unregistered_key_resolves_to_none(db_session) -> None:
    """The pre-cutover contract for an unknown key, preserved."""

    provision_operator_tenant(db_session)
    assert resolve_value(db_session, SettingDomain.imports, "not_a_setting") is None


@pytest.mark.parametrize("count", [len(SETTINGS_SPECS)])
def test_the_parity_sweep_is_not_vacuous(count: int) -> None:
    """Guard the guard: a sweep over an empty list proves nothing."""

    assert count > 500, f"only {count} specs swept; the registry looks truncated"
    assert len(kernel_specs()) == count


def test_no_setting_is_declared_twice() -> None:
    """A (domain, key) has ONE declaration, and Sub could not see otherwise.

    `get_spec` linear-scans and returns the FIRST match; `list_specs` returns
    every match. So a duplicate was invisible to every reader and visible to
    the admin settings screen, which rendered the key twice — and the second
    declaration's bounds were dead code that looked authoritative.

    Two such pairs existed (`notification.sms_api_timeout_seconds` and
    `notification.sms_max_length`, both from the SMS config split in #2193) and
    survived until the kernel's registry — a dict, so LAST wins — refused them
    outright rather than flipping the effective spec on import order. That flip
    would have tightened `sms_max_length` to `min_value=1`, silently forbidding
    the documented `0` that disables truncation.

    This guard is the cheap half of that lesson: it does not need the kernel,
    so it fails in a unit shard rather than at cutover.
    """

    from collections import Counter

    # Over the REGISTRY, not over one file. Specs are declared in two shapes:
    # literal `SettingSpec(...)` in `settings_spec.py`, and via a
    # `build_*_specs(setting_spec)` callable in `app/services/settings_specs/`.
    # I registered sixteen provisioning keys after sweeping only the first
    # shape, and duplicated `olt_write_mode_enabled` — which this guard caught
    # at import, in CI, rather than in review.
    counts = Counter((str(spec.domain), spec.key) for spec in SETTINGS_SPECS)
    duplicated = sorted(pair for pair, n in counts.items() if n > 1)

    assert not duplicated, (
        "these settings are declared more than once; the first declaration is "
        f"what `get_spec` returns and the rest are dead: {duplicated}"
    )
