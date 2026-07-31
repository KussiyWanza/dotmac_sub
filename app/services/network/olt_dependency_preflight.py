"""Fail-fast OLT profile dependency preflight checks."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Any

from sqlalchemy.orm import Session

from app.services.network.olt_config_pack_live_audit import (
    OltDependencyAuditScope,
    audit_olt_config_pack_live,
)

_SUCCESS_CACHE_TTL_SECONDS = 5 * 60
_success_cache_lock = Lock()
_success_cache: dict[
    tuple[str, OltDependencyAuditScope],
    tuple[float, OltDependencyPreflightResult],
] = {}


@dataclass
class OltDependencyPreflightResult:
    """Blocking result for OLT dependency validation."""

    success: bool
    message: str
    audit: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)


def _clone_result(
    result: OltDependencyPreflightResult,
) -> OltDependencyPreflightResult:
    return OltDependencyPreflightResult(
        success=result.success,
        message=result.message,
        audit=deepcopy(result.audit),
        errors=list(result.errors),
    )


def get_cached_olt_dependency_validation(
    olt_id: str,
    *,
    scope: OltDependencyAuditScope = OltDependencyAuditScope.FULL,
    max_age_sec: int = _SUCCESS_CACHE_TTL_SECONDS,
) -> OltDependencyPreflightResult | None:
    """Return a recent successful live audit result when available."""
    cache_key = (str(olt_id), OltDependencyAuditScope(scope))
    with _success_cache_lock:
        cached = _success_cache.get(cache_key)
        if cached is None:
            return None
        cached_at, result = cached
        if monotonic() - cached_at > max_age_sec:
            _success_cache.pop(cache_key, None)
            return None
        return _clone_result(result)


def format_dependency_audit_errors(errors: list[str]) -> str:
    """Return compact, UI-safe audit error text."""
    return "; ".join(error for error in errors if error)[:800]


def validate_olt_profile_dependencies(
    db: Session,
    *,
    olt_id: str,
    operation: str,
    scope: OltDependencyAuditScope = OltDependencyAuditScope.FULL,
) -> OltDependencyPreflightResult:
    """Validate live OLT profile dependencies before any OLT write operation."""
    scope = OltDependencyAuditScope(scope)
    cache_key = (str(olt_id), scope)
    cached = get_cached_olt_dependency_validation(olt_id, scope=scope)
    if cached is not None:
        return cached

    audit = audit_olt_config_pack_live(db, olt_id, scope=scope)
    if audit.is_valid:
        result = OltDependencyPreflightResult(
            success=True,
            message="OLT profile dependencies are valid.",
            audit=audit.to_dict(),
        )
        with _success_cache_lock:
            _success_cache[cache_key] = (monotonic(), _clone_result(result))
        return result

    errors = audit.errors or ["OLT profile dependency audit failed"]
    return OltDependencyPreflightResult(
        success=False,
        message=(
            f"OLT {operation} dependency audit failed: "
            + format_dependency_audit_errors(errors)
        ),
        audit=audit.to_dict(),
        errors=errors,
    )
