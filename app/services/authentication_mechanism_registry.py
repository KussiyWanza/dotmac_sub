"""Owner-declared authentication-mechanism vocabulary.

Mechanism codes are declared by the SOT domain that implements them and are
validated here. Installed verifier bindings remain database rows: a deployment
may have several bindings for one declared mechanism without changing this
registry or a migration.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from app.services.sot_registry.registry import DOMAIN_SOT_RELATIONSHIPS


class UndeclaredAuthenticationMechanismError(ValueError):
    """A write named a mechanism no SOT domain implements."""

    def __init__(self, mechanism_code: object) -> None:
        super().__init__(
            f"Authentication mechanism {str(mechanism_code)!r} is not declared "
            "by any SOT domain."
        )
        self.mechanism_code = str(mechanism_code)


def _build_owners() -> Mapping[str, str]:
    owners: dict[str, str] = {}
    for domain_sot in DOMAIN_SOT_RELATIONSHIPS:
        for mechanism in domain_sot.authentication_mechanisms:
            owners.setdefault(mechanism, domain_sot.domain)
    return MappingProxyType(owners)


AUTHENTICATION_MECHANISM_OWNERS: Mapping[str, str] = _build_owners()


def declared_authentication_mechanisms() -> frozenset[str]:
    return frozenset(AUTHENTICATION_MECHANISM_OWNERS)


def owner_of(mechanism_code: object) -> str | None:
    return AUTHENTICATION_MECHANISM_OWNERS.get(str(mechanism_code))


def require_declared_mechanism(mechanism_code: object) -> str:
    code = str(mechanism_code).strip()
    if code not in AUTHENTICATION_MECHANISM_OWNERS:
        raise UndeclaredAuthenticationMechanismError(code)
    return code


__all__ = [
    "AUTHENTICATION_MECHANISM_OWNERS",
    "UndeclaredAuthenticationMechanismError",
    "declared_authentication_mechanisms",
    "owner_of",
    "require_declared_mechanism",
]
