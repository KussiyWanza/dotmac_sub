"""Structural types for the canonical SOT registry."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.sot_manifest import SOTService


@dataclass(frozen=True)
class DomainSOT:
    domain: str
    services: tuple[SOTService, ...]
    entrypoints: tuple[str, ...]
    rule: str
    #: Setting domains this SOT domain OWNS — the ``domain_settings.domain``
    #: values its services are the canonical writer for. Declared here rather
    #: than enumerated in ``app.models.domain_settings`` so that adding a
    #: setting domain is a declaration by its owner, never an ``ALTER TYPE``
    #: in the hosting layer (ADR-0008). Exactly one SOT domain may declare a
    #: given setting domain; ``registry_validation_errors`` rejects a
    #: duplicate. An annotated field on this frozen record is also what the
    #: governance schema-v3 ``declaration_field`` gate resolves.
    setting_domains: tuple[str, ...] = ()
    #: Authentication mechanism codes implemented by this domain. This is an
    #: open, owner-declared vocabulary (ADR-0008), not a host enum. Exactly one
    #: SOT domain may declare a code; the registry rejects duplicates.
    authentication_mechanisms: tuple[str, ...] = ()
