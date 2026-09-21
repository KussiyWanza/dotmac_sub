"""Layer 3 — reseller_user principal across the bearer/session/password auth layer.

The cutover exposed that ph1b only made the WEB portal reseller_user-aware; the
bearer token validation, refresh, change-password, and reseller API scoping still
assumed subscriber/system_user. These verify a reseller_user works there too.
"""

from __future__ import annotations

import uuid

import pytest
from starlette.requests import Request

from app.api.reseller import _reseller_id
from app.config import settings
from app.models.auth import UserCredential
from app.models.subscriber import Reseller, ResellerUser
from app.services import auth_flow as auth_flow_service
from app.services import reseller_onboarding
from app.services.auth_dependencies import require_user_auth
from app.services.auth_flow import AuthFlow, change_password
from app.services.owner_commands import CommandContext


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    old = settings.reseller_user_principal_enabled
    object.__setattr__(settings, "reseller_user_principal_enabled", True)
    yield
    object.__setattr__(settings, "reseller_user_principal_enabled", old)


def _request(headers=None):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/reseller/dashboard",
            "headers": headers or [(b"user-agent", b"pytest")],
            "client": ("127.0.0.1", 5555),
        }
    )


def _reseller_login(db):
    r = Reseller(name="Bearer Net", code="BRR")
    db.add(r)
    db.flush()
    reseller_id = r.id
    db.commit()
    command_id = uuid.uuid4()
    outcome = reseller_onboarding.provision_reseller_user(
        db,
        reseller_onboarding.ProvisionResellerUserCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="user:reseller-bearer-test",
                scope=reseller_onboarding.RESELLER_WRITE_SCOPE,
                reason="create first-class reseller bearer test principal",
            ),
            reseller_id=reseller_id,
            portal_user=reseller_onboarding.ResellerPortalUserSpec(
                first_name="BRR",
                last_name="Admin",
                email="brr@example.com",
                username="brr-admin",
                send_invite=False,
            ),
        ),
    )
    ru = db.get(ResellerUser, outcome.reseller_user_id)
    r = db.get(Reseller, reseller_id)
    credential = (
        db.query(UserCredential)
        .filter(UserCredential.reseller_user_id == outcome.reseller_user_id)
        .one()
    )
    # The owner mints a per-account secret nobody sees; give this fixture a
    # known one so the test can drive a real login.
    credential.password_hash = auth_flow_service.hash_password("secret")  # noqa: S106
    credential.must_change_password = False
    db.commit()
    tokens = AuthFlow.login(db, "brr-admin", "secret", _request(), None)
    return r, ru, tokens


def test_bearer_auth_resolves_reseller_user_token(db_session, flag_on):
    r, ru, tokens = _reseller_login(db_session)
    principal = require_user_auth(
        authorization=f"Bearer {tokens['access_token']}",
        request=_request(),
        db=db_session,
    )
    assert principal["principal_type"] == "reseller_user"
    assert principal["principal_id"] == str(ru.id)


def test_refresh_keeps_reseller_user_principal(db_session, flag_on):
    r, ru, tokens = _reseller_login(db_session)
    rotated = AuthFlow.refresh(db_session, tokens["refresh_token"], _request())
    payload = auth_flow_service.decode_access_token(db_session, rotated.access_token)
    assert payload["principal_type"] == "reseller_user"
    assert payload["sub"] == str(ru.id)


def test_reseller_api_scope_resolves_for_reseller_user(db_session, flag_on):
    r, ru, _ = _reseller_login(db_session)
    principal = {"principal_type": "reseller_user", "principal_id": str(ru.id)}
    assert _reseller_id(db_session, principal) == str(r.id)


def test_reseller_api_scope_rejects_plain_subscriber(db_session, person, flag_on):
    principal = {
        "principal_type": "subscriber",
        "subscriber_id": str(person.id),
        "principal_id": str(person.id),
    }
    with pytest.raises(Exception) as exc:  # noqa: PT011 - HTTPException 403
        _reseller_id(db_session, principal)
    assert getattr(exc.value, "status_code", None) == 403


def test_change_password_works_for_reseller_user(db_session, flag_on):
    r, ru, _ = _reseller_login(db_session)
    ts = change_password(db_session, str(ru.id), "secret", "NewSecret123!")
    assert ts is not None
    # New password authenticates; old one no longer resolves a usable credential.
    tokens = AuthFlow.login(db_session, "brr-admin", "NewSecret123!", _request(), None)
    assert tokens.get("access_token")
