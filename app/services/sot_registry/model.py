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
