"""Regression: the /admin web surface must default-deny non-staff principals.

Subscriber and reseller portal logins authenticate as ``principal_type ==
"subscriber"``; only staff are ``"system_user"``. The admin router previously
gated on authentication alone, so any authenticated principal could reach
admin-only routes (secret management, API-key minting, etc.). These tests pin
the staff gate in place.
"""

import pytest
from fastapi import HTTPException

from app.web.admin import router as admin_router
from app.web.auth.dependencies import require_admin_web_auth, require_web_auth


def _router_dependency_calls(router):
    return [getattr(dep, "dependency", None) for dep in router.dependencies]


def test_admin_router_uses_staff_gate_not_bare_auth():
    calls = _router_dependency_calls(admin_router)
    assert require_admin_web_auth in calls, "/admin router must require the staff gate"
    # The bare authn-only dependency must not be the admin gate anymore.
    assert require_web_auth not in calls


def test_admin_router_has_session_refresh_probe():
    paths = {getattr(route, "path", "") for route in admin_router.routes}
    assert "/admin/session/refresh" in paths


def test_staff_gate_rejects_subscriber():
    with pytest.raises(HTTPException) as exc:
        require_admin_web_auth({"principal_type": "subscriber", "principal_id": "p"})
    assert exc.value.status_code == 403


def test_staff_gate_rejects_missing_principal_type():
    with pytest.raises(HTTPException) as exc:
        require_admin_web_auth({"principal_id": "p"})
    assert exc.value.status_code == 403


def test_staff_gate_allows_system_user():
    auth = {"principal_type": "system_user", "principal_id": "p"}
    assert require_admin_web_auth(auth) is auth
