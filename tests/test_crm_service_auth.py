"""CRM inbound API accepts only scoped, rotatable Sub API keys."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.crm import CRM_INTEGRATION_PERMISSION, require_crm_service_auth
from app.models.auth import ApiKey
from app.services.auth import hash_api_key


def _make_key(db, *, scopes, raw="raw-crm-key"):
    key = ApiKey(
        label="crm-service",
        key_hash=hash_api_key(raw),
        scopes=scopes,
        is_active=True,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db.add(key)
    db.commit()
    return key, raw


def _call(db, *, x_api_key=None):
    return require_crm_service_auth(
        request=SimpleNamespace(state=SimpleNamespace(), cookies={}),
        x_api_key=x_api_key,
        db=db,
    )


def test_scoped_api_key_is_accepted(db_session):
    _make_key(db_session, scopes=[CRM_INTEGRATION_PERMISSION])
    assert _call(db_session, x_api_key="raw-crm-key") is None


def test_wildcard_scope_satisfies(db_session):
    _make_key(db_session, scopes=["integration:*"], raw="raw-wild")
    _call(db_session, x_api_key="raw-wild")


def test_key_without_scope_fails_closed(db_session):
    _make_key(db_session, scopes=["billing:read"], raw="raw-wrong")
    with pytest.raises(HTTPException) as exc:
        _call(db_session, x_api_key="raw-wrong")
    assert exc.value.status_code == 401


def test_unknown_key_is_rejected(db_session):
    with pytest.raises(HTTPException) as exc:
        _call(db_session, x_api_key="no-such-key")
    assert exc.value.status_code == 401


def test_missing_key_is_rejected(db_session):
    with pytest.raises(HTTPException) as exc:
        _call(db_session)
    assert exc.value.status_code == 401
