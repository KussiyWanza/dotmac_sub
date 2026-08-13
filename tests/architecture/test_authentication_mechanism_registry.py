"""Authentication mechanisms are open declarations, not a host enum."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.models.auth import AuthProvider
from app.services.authentication_mechanism_registry import (
    UndeclaredAuthenticationMechanismError,
    declared_authentication_mechanisms,
    owner_of,
    require_declared_mechanism,
)
from app.services.sot_registry import registry as registry_module
from app.services.sot_registry.registry import (
    authentication_mechanism_declaration_errors,
    domain_relationship,
)


def test_only_implemented_mechanisms_are_declared_by_one_owner() -> None:
    assert declared_authentication_mechanisms() == {"local", "radius"}
    assert owner_of("local") == "authorization_control_plane"
    assert owner_of("radius") == "network_access_control_plane"
    assert "sso" in {item.value for item in AuthProvider}
    assert owner_of("sso") is None
    with pytest.raises(UndeclaredAuthenticationMechanismError):
        require_declared_mechanism("sso")


def test_duplicate_mechanism_declaration_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    party = domain_relationship("party_identity")
    monkeypatch.setattr(
        registry_module,
        "DOMAIN_SOT_RELATIONSHIPS",
        (
            *registry_module.DOMAIN_SOT_RELATIONSHIPS,
            replace(party, authentication_mechanisms=("local",)),
        ),
    )

    errors = authentication_mechanism_declaration_errors()

    assert any("local" in error and "both" in error for error in errors)
