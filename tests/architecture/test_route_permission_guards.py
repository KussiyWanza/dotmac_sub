"""Every staff-facing API route must declare an authorization guard.

The router-mount mode "user" only attaches ``require_user_auth`` — it proves
the caller is *some* authenticated principal, INCLUDING a customer subscriber.
A staff/admin endpoint mounted that way with no ``require_permission`` is open
to any logged-in customer. This test reconstructs the FULL production API
surface — both the core routers (mounted at import) and the DEFERRED routers
(mounted in the lifespan startup, e.g. settings, qualification) — and fails
the build on any ``/api/v1`` route that lacks a permission/role guard, unless
its path is on the self-scoped allowlist below.

Building from the spec lists (not the live ``app.routes``) is deliberate: the
deferred routers are NOT on ``app`` until startup runs, so an ``app.routes``
walk would silently skip exactly the routers most likely to be misconfigured.

RBAC-coverage counterpart of ``test_thin_wrappers``: makes the "forgot the
permission dependency" class of bug build-failing rather than shippable.
"""

from __future__ import annotations

import ast
import inspect

from fastapi import FastAPI

from app.api import catalog as api_catalog
from app.main import (
    _CORE_ROUTER_SPECS,
    _DEFERRED_API_ROUTER_SPECS,
    _load_router_object,
    _mount_router,
)

# Dependency callables that constitute a real authorization guard.
_GUARD_NAMES = {
    "_require_permission",
    "_require_role",
    "_require_any_permission",
    "_require_method_permission",
    "require_audit_auth",
    "require_scoped_permission",  # forward-compat (P1)
    "_require_scoped_permission",
    # Offer admission is a compound owner decision, not a blanket
    # catalog:write gate. Its delegation is asserted below against code.
    "_require_offer_version_admission",
}

# Self-scoped / public surfaces that legitimately need no staff permission.
_ALLOWLIST_PREFIXES = (
    "/api/v1/me",
    "/api/v1/reseller",
    "/api/v1/payment-proofs/me",
    "/api/v1/payment-proofs/reseller",
    "/api/v1/service-requests",
    "/api/v1/field",
    # Self-scoped vendor portal — authz is FieldVendorUser membership
    # (require_native_vendor_context), same pattern as /api/v1/field and
    # /api/v1/reseller. Behavior pinned by tests/test_vendor_portal_auth.py.
    "/api/v1/vendor",
    "/api/v1/auth",
    "/api/v1/health",
    "/api/v1/tables",
    # Customer self-service payment of their OWN invoices (_require_subscriber).
    "/api/v1/payments/initiate",
    "/api/v1/payments/verify",
)

# Quarantine: routes already unguarded when this rule was introduced
# (2026-06-10). The test fails on any route NOT in this set, so NEW holes are
# build-failing immediately. This list is the burn-down backlog — guarding a
# router removes its paths from here. Do not ADD to it.
_KNOWN_UNGUARDED: set[str] = set()


def _build_full_api() -> FastAPI:
    """Mount every API router from both spec lists onto a throwaway app, so
    the audit covers the true production surface deterministically."""
    test_app = FastAPI()
    for module_name, attr_name, mount_kind, mode in (
        _CORE_ROUTER_SPECS + _DEFERRED_API_ROUTER_SPECS
    ):
        if mount_kind != "api":
            continue
        router = _load_router_object(module_name, attr_name)
        _mount_router(test_app, router, mount_kind, mode)
    return test_app


def _dependency_calls(dependant) -> set[str]:
    names: set[str] = set()
    call = getattr(dependant, "call", None)
    if call is not None:
        names.add(getattr(call, "__name__", ""))
    for sub in getattr(dependant, "dependencies", []) or []:
        names |= _dependency_calls(sub)
    return names


def _is_allowlisted(path: str) -> bool:
    return any(path.startswith(p) for p in _ALLOWLIST_PREFIXES)


def test_offer_admission_guard_delegates_to_the_single_decision_owner() -> None:
    """The named mounted guard must execute the owner, not merely exist."""
    source = inspect.getsource(api_catalog._require_offer_version_admission)
    tree = ast.parse(source)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "offer_access_requirement"
        and node.func.attr == "authorize_offer_version_admission"
        for node in ast.walk(tree)
    )


def test_all_api_routes_declare_an_authorization_guard():
    app = _build_full_api()
    unguarded: list[str] = []
    seen_quarantined: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/v1"):
            continue
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        names = _dependency_calls(dependant)
        # "none"-mode routers carry no require_user_auth: intentional public
        # callbacks (webhooks, tr069). This test targets the authenticated-
        # but-unguarded gap, so only audit routes that DO require auth.
        if "require_user_auth" not in names:
            continue
        if names & _GUARD_NAMES:
            continue
        if _is_allowlisted(path):
            continue
        methods = ",".join(sorted(getattr(route, "methods", []) or []))
        entry = f"{methods} {path}"
        if entry in _KNOWN_UNGUARDED:
            seen_quarantined.add(entry)
            continue
        unguarded.append(entry)

    assert not unguarded, (
        "These authenticated /api/v1 routes have no permission/role guard "
        "(any logged-in customer can call them). Add a require_permission "
        "dependency, or allowlist the path if it is genuinely self-scoped:\n  "
        + "\n  ".join(sorted(unguarded))
    )

    # Burn-down hygiene: once a quarantined route is guarded (or removed), its
    # entry must be deleted from _KNOWN_UNGUARDED so the list shrinks honestly.
    stale = _KNOWN_UNGUARDED - seen_quarantined
    assert not stale, (
        "These routes are in the unguarded quarantine but are now guarded or "
        "gone — remove them from _KNOWN_UNGUARDED:\n  " + "\n  ".join(sorted(stale))
    )
