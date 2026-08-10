"""A fact the kernel owns has exactly ONE expression under ``app/``.

Every defect the settings cutover surfaced was the same one wearing different
clothes: a fact belonging to `dotmac_kernel` restated locally, then drifting.

- the native ``settingvaluetype`` enum restated the value-type registry, so a
  kernel-declared type could not be stored (migration 512);
- ``CHECK (value_type = 'json' AND ...)`` restated ``ValueTypeSpec.storage``,
  and blocked the column conversion that removed the enum;
- ``DomainSettingUpdate`` and ``DomainSettingCreate`` restated it AGAIN one
  layer up, so a setting whose value is an array could not be written through
  the API at all — found only because a test happened to write one;
- ``SettingsCache`` restated the key model and dropped the scope segment, which
  is the cross-tenant leak `dotmac_kernel.settings_cache` names ERP for.

Four instances, one rule. Reviewing harder does not close that class; a test
does. This file is that test, and it is meant to grow one case per kernel-owned
fact rather than to stay as it is.

The rule it enforces today: **which column a value type is stored in is a
property of the TYPE**, held in `dotmac_kernel.setting_value_types` as
``ValueTypeSpec.storage``. Exactly one place under ``app/`` may ask; nobody may
answer for themselves by naming a type.
"""

from __future__ import annotations

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[2] / "app"

#: The one adapter permitted to map a value type to a storage column.
#: `_stores_as_json` asks `ValueTypeSpec.storage`; it does not decide.
STORAGE_ADAPTER = "app/schemas/settings.py"

#: Restatements that exist today, recorded so they cannot multiply.
#:
#: These branch on a type NAME to decide how to coerce or render a value.
#: `ValueTypeSpec.from_storage`/`to_storage` own that too — a value type is
#: "the only thing that knows how it is encoded", in the kernel's own words —
#: so these are the same defect as the storage ones, one slice behind. Sub has
#: not moved coercion to the kernel yet, and pretending otherwise by narrowing
#: this test to storage alone would hide the debt instead of bounding it.
#:
#: SHRINK-ONLY, like the other baselines in this suite: an entry may be deleted
#: when its site delegates, and none may be added. A new restatement is not a
#: line in a list, it is a defect with four merged instances behind it.
KNOWN_RESTATEMENTS = frozenset(
    {
        # `coerce_value`: parses a form value per type. Belongs to
        # `ValueTypeSpec.from_storage`.
        "app/services/settings_spec.py",
        # Chooses a form widget by json-ness. Belongs to `ValueTypeSpec` too,
        # via whatever presentation hint the kernel grows — or to a single
        # local adapter, but not to a comparison in a form builder.
        "app/services/web_system_settings_forms.py",
    }
)


#: Names a comparison may not test a `value_type` against in order to work out
#: where the value lives. Derived from the kernel registry rather than typed
#: out, so a type added there is covered the day it exists — including `money`,
#: which Sub does not use yet and which is exactly the kind of thing a
#: hand-written list would miss.
def _json_stored_type_names() -> frozenset[str]:
    from dotmac_kernel.setting_value_types import active_setting_value_types

    registry = active_setting_value_types()
    return frozenset(
        str(code)
        for code in registry.codes()
        if registry.require(code).storage == "json"
    )


def _mentions_value_type(node: ast.expr) -> bool:
    source = ast.unparse(node)
    return "value_type" in source or "ValueType" in source


def _is_type_name(node: ast.expr, names: frozenset[str]) -> bool:
    """`SettingValueType.json`, `"json"`, or `SettingValueType("json")`."""

    if isinstance(node, ast.Attribute) and node.attr in names:
        return "ValueType" in ast.unparse(node.value)
    if isinstance(node, ast.Constant) and node.value in names:
        return True
    return False


def test_only_the_adapter_maps_a_value_type_to_a_storage_column() -> None:
    """Nobody under `app/` decides where a value lives by naming its type.

    The failure this prevents is specific and has already happened twice: a
    validator (or a constraint, or a coercion branch) tests
    ``value_type == 'json'``, a SECOND json-stored type is declared, and every
    value of that type is rejected or filed in the wrong column — with the
    error blaming the caller.
    """

    names = _json_stored_type_names()
    assert "json" in names and "list" in names, (
        f"registry lookup returned {sorted(names)}; the guard would be vacuous"
    )

    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP.parent).as_posix()
        if rel == STORAGE_ADAPTER or rel in KNOWN_RESTATEMENTS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — a broken file fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            named = [o for o in operands if _is_type_name(o, names)]
            if not named:
                continue
            if any(_mentions_value_type(o) for o in operands if o not in named):
                offenders.append(f"{rel}:{node.lineno}: {ast.unparse(node)}")

    assert not offenders, (
        "these compare a value type against a literal type name to decide "
        "storage — which column a type uses belongs to `ValueTypeSpec.storage`, "
        f"and only {STORAGE_ADAPTER} may ask:\n  " + "\n  ".join(offenders)
    )


def test_the_adapter_asks_the_registry_rather_than_answering() -> None:
    """The one permitted expression must delegate, not hold its own opinion.

    An allowlist entry that stopped consulting the registry would satisfy the
    test above while reintroducing exactly what it forbids.
    """

    source = (APP.parent / STORAGE_ADAPTER).read_text(encoding="utf-8")
    assert "active_setting_value_types" in source, (
        f"{STORAGE_ADAPTER} is allowlisted as the one place that maps a value "
        "type to a column, on the basis that it ASKS the kernel registry. It "
        "no longer does, so the allowlist entry is now a hole."
    )
    assert ".storage" in source, (
        f"{STORAGE_ADAPTER} must read `ValueTypeSpec.storage` — the property "
        "that owns this decision — not re-derive it from the type's name."
    )


def test_the_restatement_baseline_only_shrinks() -> None:
    """Every recorded restatement is still real, and the list stays bounded.

    A baseline nobody prunes becomes a list of things that are fine. Each entry
    has to keep earning its place: if a file stopped restating, its entry is
    stale and the guard has quietly narrowed.
    """

    names = _json_stored_type_names()
    stale: list[str] = []
    for rel in sorted(KNOWN_RESTATEMENTS):
        tree = ast.parse((APP.parent / rel).read_text(encoding="utf-8"))
        restates = any(
            _is_type_name(operand, names)
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            for operand in [node.left, *node.comparators]
        )
        if not restates:
            stale.append(rel)

    assert not stale, (
        "these no longer restate a kernel-owned type name, so their baseline "
        f"entries are stale — delete them, do not leave the guard weaker than "
        f"the code: {stale}"
    )

    assert len(KNOWN_RESTATEMENTS) <= 2, (
        f"the baseline grew to {len(KNOWN_RESTATEMENTS)}; it is shrink-only"
    )


# ── The settings cache: key, TTL and invalidation belong to the kernel ───────

#: Modules under `app/` that may touch `SettingsCache`: NONE.
#:
#: It began as three — `module_manager`, `smart_defaults` and
#: `network/provisioning_settings` — recorded as a baseline while the
#: resolution path moved. Reviewing that record is what showed they were not
#: private caches at all: each queried `DomainSetting` directly, picked a column
#: by hand, applied none of the spec's coercion or bounds, filtered by no
#: tenant, and cached the result under the unscoped `settings:{domain}:{key}`.
#: Three more parallel readers, not three cache users.
#:
#: Leaving them would have meant fixing the key model on the resolution path
#: and leaving the defect it exists to prevent live in three modules. So the
#: baseline is empty, and this constant stays as the place a future entry would
#: have to be argued for.
PRIVATE_SETTINGS_CACHE_USERS: frozenset[str] = frozenset()

#: The transport, and the only module that installs a store.
CACHE_STORE_ADAPTER = "app/services/kernel_settings_cache_store.py"


def test_settings_resolution_does_not_carry_its_own_cache() -> None:
    """`SettingsCache` is off the resolution path, and stays off.

    Before this, ten invalidation call sites across six modules each remembered
    to drop an entry, and the key they dropped had no scope segment. The
    kernel owns key, TTL, what is never cached, and what a write invalidates;
    Sub owns a Redis transport and one invalidation listener on the model.
    """

    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP.parent).as_posix()
        if rel in PRIVATE_SETTINGS_CACHE_USERS or rel.endswith(
            ("services/settings_cache.py", "kernel_settings_cache_store.py")
        ):
            continue
        if "SettingsCache" in path.read_text(encoding="utf-8"):
            offenders.append(rel)

    assert not offenders, (
        "these reach for Sub's `SettingsCache`, which is no longer the settings "
        "cache — `dotmac_kernel.settings_cache` is, and it is consulted by the "
        f"resolver itself: {offenders}"
    )


def test_only_the_adapter_installs_a_settings_cache_store() -> None:
    """One installer, so "which store is active" has one answer."""

    # By IMPORT, not by substring: `settings_spec` imports the adapter's own
    # `install` under an alias containing the same characters, and a substring
    # match would call that a second installer.
    installers = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "dotmac_kernel.settings_cache"
                and any(a.name == "install_settings_cache" for a in node.names)
            ):
                installers.append(path.relative_to(APP.parent).as_posix())
    assert installers == [CACHE_STORE_ADAPTER], installers


def test_the_cache_store_builds_no_keys() -> None:
    """The transport must not know the key model.

    `CacheStore` is key-agnostic on purpose: a store that cannot construct a
    key cannot drop a scope segment from one, which is exactly how ERP served
    one organization's settings to every other.
    """

    source = (APP.parent / CACHE_STORE_ADAPTER).read_text(encoding="utf-8")
    for forbidden in ("setting_cache_key", "setting_key_prefix", "cache_key("):
        assert forbidden not in source, (
            f"{CACHE_STORE_ADAPTER} references {forbidden!r} — the store is a "
            "transport and must never build or parse a key"
        )
