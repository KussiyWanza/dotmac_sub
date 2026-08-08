"""Setting domains are DECLARED by their owning SOT domain, not enumerated here.

ADR-0008, fleet-wide: a vocabulary whose members belong to modules is declared
by those modules and validated by a registry — never an enum, fixed list, or
native-enum column in the hosting layer.

The tests that matter most are the two negative ones, because they are what a
regression would silently pass:

- ``test_a_new_domain_needs_no_change_to_the_host_module`` — declaring a domain
  must not require editing ``app/models/domain_settings.py``. If the accessors
  were asserted EQUAL to the declared set instead of a subset, adding a domain
  would still force a host edit and the registry would be decoration.
- ``test_the_accessors_are_a_subset_not_the_definition`` — the same property
  stated directly.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import CheckConstraint

from app.models.domain_settings import (
    _ACCESSOR_NAMES,
    DomainSetting,
    SettingDomain,
    SettingDomainType,
)
from app.services import settings_spec
from app.services.setting_domain_registry import (
    SETTING_DOMAIN_OWNERS,
    UndeclaredSettingDomainError,
    declared_setting_domains,
    is_declared,
    owner_of,
    require_declared_domain,
)
from app.services.sot_registry.registry import (
    DOMAIN_SOT_RELATIONSHIPS,
    registry_validation_errors,
    setting_domain_declaration_errors,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_MODULE = REPO_ROOT / "app" / "models" / "domain_settings.py"
RETIRED_DOMAIN = "subscription_engine"


def test_the_declarations_are_structurally_sound() -> None:
    assert setting_domain_declaration_errors() == ()
    assert registry_validation_errors() == ()


def test_every_declared_domain_has_exactly_one_owner() -> None:
    declared = declared_setting_domains()
    assert declared
    for domain in sorted(declared):
        owner = owner_of(domain)
        assert owner is not None, f"{domain} has no owning SOT domain"
        claimants = [
            sot.domain
            for sot in DOMAIN_SOT_RELATIONSHIPS
            if domain in sot.setting_domains
        ]
        assert claimants == [owner], (
            f"{domain} is claimed by {claimants}; exactly one SOT domain may own it"
        )


def test_one_owner_may_declare_several_domains() -> None:
    """Guard against re-introducing a ``len(domains) == len(owners)`` assertion.

    That equality quietly forbids a multi-domain owner, and Sub has four of
    them — it would have failed the moment ``network`` declared its second.
    """

    owners = {owner_of(domain) for domain in declared_setting_domains()}
    assert len(owners) < len(declared_setting_domains())


def test_the_accessors_are_a_subset_not_the_definition() -> None:
    declared = declared_setting_domains()
    accessors = set(_ACCESSOR_NAMES)
    assert accessors <= declared, (
        "every accessor on SettingDomain must be a declared domain; "
        f"undeclared: {sorted(accessors - declared)}"
    )
    for name in _ACCESSOR_NAMES:
        assert getattr(SettingDomain, name) == name


def test_a_new_domain_needs_no_change_to_the_host_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declare a domain the host module has never heard of, and use it."""

    invented = "totally_new_domain_for_this_test"
    assert invented not in declared_setting_domains()
    assert not hasattr(SettingDomain, invented)

    monkeypatch.setattr(
        "app.services.setting_domain_registry.SETTING_DOMAIN_OWNERS",
        {**SETTING_DOMAIN_OWNERS, invented: "some_owning_domain"},
    )

    assert is_declared(invented)
    resolved = require_declared_domain(invented)
    assert isinstance(resolved, SettingDomain)
    assert resolved == invented
    assert resolved.value == invented


def test_an_undeclared_domain_is_rejected() -> None:
    with pytest.raises(UndeclaredSettingDomainError):
        require_declared_domain("no_module_declares_this")


def test_the_retired_domain_is_undeclared_and_unroutable() -> None:
    """``subscription_engine`` is dead: no spec, no route, no reader, no writer.

    Its rows survive the migration as text and simply become unwritable. The
    generic dispatcher must not still be able to reach it.
    """

    assert not is_declared(RETIRED_DOMAIN)
    assert RETIRED_DOMAIN not in {
        str(domain) for domain in settings_spec.DOMAIN_SETTINGS_SERVICE
    }


def test_every_spec_names_a_declared_domain() -> None:
    undeclared = sorted(
        {
            str(spec.domain)
            for spec in settings_spec.SETTINGS_SPECS
            if not is_declared(spec.domain)
        }
    )
    assert not undeclared, (
        "these setting domains have specs but no SOT domain declares them: "
        f"{undeclared}. Add them to that domain's `setting_domains` in "
        "app/services/sot_registry/domains/."
    )


def test_every_dispatchable_domain_is_declared() -> None:
    undeclared = sorted(
        {
            str(domain)
            for domain in settings_spec.DOMAIN_SETTINGS_SERVICE
            if not is_declared(domain)
        }
    )
    assert not undeclared, (
        f"DOMAIN_SETTINGS_SERVICE can write undeclared domains: {undeclared}"
    )


def test_the_host_module_holds_no_second_vocabulary() -> None:
    """``SettingDomain`` must not be an enum again, nor gain a CHECK constraint.

    A static read, because the point is the SHAPE of the declaration, not a
    runtime value: an ``enum.Enum`` here would put the vocabulary back in the
    hosting layer even if the registry kept working.
    """

    tree = ast.parse(MODEL_MODULE.read_text(encoding="utf-8"))
    setting_domain = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SettingDomain"
    )
    bases = {base.id for base in setting_domain.bases if isinstance(base, ast.Name)} | {
        base.attr for base in setting_domain.bases if isinstance(base, ast.Attribute)
    }
    assert bases == {"str"}, (
        "SettingDomain must stay an open `str` subclass; an Enum base returns "
        "the vocabulary to the hosting layer (ADR-0008)"
    )

    domain_column = DomainSetting.__table__.c.domain
    assert isinstance(domain_column.type, SettingDomainType), (
        "the domain column must store text through SettingDomainType, never a "
        "native enum"
    )
    closing_checks = [
        constraint
        for constraint in DomainSetting.__table__.constraints
        if isinstance(constraint, CheckConstraint)
        and "domain" in str(constraint.sqltext)
        and "value_type" not in str(constraint.sqltext)
    ]
    assert not closing_checks, (
        f"a CHECK constraint re-closes the domain column: {closing_checks}"
    )


def test_the_registry_is_derived_from_the_sot_registry() -> None:
    """No second ownership list to drift from the canonical one."""

    from_sot = {
        setting_domain: sot.domain
        for sot in DOMAIN_SOT_RELATIONSHIPS
        for setting_domain in sot.setting_domains
    }
    assert dict(SETTING_DOMAIN_OWNERS) == from_sot


def _revision_module(revision: str) -> ModuleType:
    path = REPO_ROOT / "alembic" / "versions" / f"{revision}.py"
    spec = importlib.util.spec_from_file_location(revision, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_two_legacy_enum_lists_agree() -> None:
    """001 rebuilds the retired type; 502 rebuilds it on downgrade.

    They describe the same historical fact from opposite directions, and a
    mismatch would only surface as a failed cast on a real database — 001
    building a type that 502's downgrade cannot restore, or vice versa. Both
    lists are frozen history and neither should ever change again.
    """

    base = _revision_module("001_squashed_initial_schema")
    retirement = _revision_module("502_open_setting_domain_vocabulary")
    assert tuple(base._LEGACY_SETTING_DOMAIN_MEMBERS) == tuple(
        retirement.LEGACY_MEMBERS
    )
