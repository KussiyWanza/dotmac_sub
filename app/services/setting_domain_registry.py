"""Declaration registry for setting domains.

The authority for "is this a real setting domain, and who owns it" — replacing
the closed ``enum.Enum`` that used to answer it from inside
``app.models.domain_settings``. Members are DECLARED by the SOT domain that
owns the settings (``DomainSOT.setting_domains``) and merely VALIDATED here,
per ADR-0008.

Two properties worth stating, because they are the ones that are easy to get
wrong:

- **Validation is at the WRITE boundary, never at construction.** A resolver
  must be able to name a domain in order to reject it, and rows written under
  a domain that has since been undeclared must still READ back. So
  ``SettingDomain`` stays constructible from anything and this module is
  consulted only when something persists a row.
- **The registry is derived from the SOT registry, not a second list.** There
  is no separate ownership table to drift: if a domain is declared here it is
  because a canonical SOT domain claims it, and
  ``registry_validation_errors`` fails the build when two claim the same one.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from app.models.domain_settings import SettingDomain
from app.services.sot_registry.registry import (
    DOMAIN_SOT_RELATIONSHIPS,
    setting_domain_declaration_errors,
)


class UndeclaredSettingDomainError(ValueError):
    """A setting domain no SOT domain declares was used on a write path.

    Deliberately not an ``HTTPException``: this is raised from an ORM event
    listener, where a transport-shaped exception would be wrong. Callers that
    take a domain from a URL translate it to a 404; everything else lets it
    surface as the programming error it is.
    """

    def __init__(self, domain: object) -> None:
        super().__init__(
            f"Setting domain {str(domain)!r} is not declared by any SOT domain. "
            "Declare it in that domain's `setting_domains` in "
            "app/services/sot_registry/domains/, or use an existing domain."
        )
        self.domain = str(domain)


def _build_owners() -> Mapping[str, str]:
    owners: dict[str, str] = {}
    for domain_sot in DOMAIN_SOT_RELATIONSHIPS:
        for setting_domain in domain_sot.setting_domains:
            # First declaration wins here; a duplicate is reported as a
            # structural error by `registry_validation_errors` rather than
            # silently merged, so ownership can never be ambiguous at rest.
            owners.setdefault(setting_domain, domain_sot.domain)
    return MappingProxyType(owners)


#: setting domain -> the ONE SOT domain that declares it.
SETTING_DOMAIN_OWNERS: Mapping[str, str] = _build_owners()


def declared_setting_domains() -> frozenset[str]:
    """Every setting domain some SOT domain declares."""

    return frozenset(SETTING_DOMAIN_OWNERS)


def owner_of(domain: object) -> str | None:
    """The SOT domain that declares ``domain``, or ``None`` if undeclared."""

    return SETTING_DOMAIN_OWNERS.get(str(domain))


def is_declared(domain: object) -> bool:
    return str(domain) in SETTING_DOMAIN_OWNERS


def require_declared_domain(domain: object) -> SettingDomain:
    """Return ``domain`` as a member, or raise if no module declares it."""

    if not is_declared(domain):
        raise UndeclaredSettingDomainError(domain)
    return SettingDomain(str(domain))


__all__ = [
    "SETTING_DOMAIN_OWNERS",
    "UndeclaredSettingDomainError",
    "declared_setting_domains",
    "is_declared",
    "owner_of",
    "require_declared_domain",
    "setting_domain_declaration_errors",
]
