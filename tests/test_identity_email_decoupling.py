"""Identity / email decoupling (Layers 1+2).

Email is non-unique contact information, not canonical identity. A unique
eligible customer email may be used as a safe alias for the canonical local
credential; shared email must never select an account.
"""

from __future__ import annotations

import pytest

from app.models.auth import AuthProvider, UserCredential
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services import auth_flow as auth_flow_service
from app.services.auth_flow import (
    _resolve_login_credential,
    hash_password,
    request_password_reset,
    set_subscriber_email,
)
from app.services.customer_login_identity import (
    AMBIGUOUS_EMAIL_CODE,
    CustomerLoginIdentityError,
)
from app.services.validation_api import validate_email_unique


def _sub(db, email: str, **kw) -> Subscriber:
    sub = Subscriber(first_name="A", last_name="B", email=email, **kw)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _local_cred(db, *, subscriber=None, system_user=None, username, password="secret"):  # noqa: S107  # test fixture credential, not a real secret
    cred = UserCredential(
        subscriber_id=subscriber.id if subscriber else None,
        system_user_id=system_user.id if system_user else None,
        provider=AuthProvider.local,
        username=username,
        password_hash=hash_password(password),
        is_active=True,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return cred


# --- Layer 1: email is non-unique contact info -----------------------------


def test_two_subscribers_can_share_an_email(db_session):
    shared = "owner@abcnetworks.com"
    a = _sub(db_session, shared)
    b = _sub(db_session, shared)
    assert a.id != b.id
    rows = db_session.query(Subscriber).filter(Subscriber.email == shared).all()
    assert {r.id for r in rows} == {a.id, b.id}


def test_validate_email_unique_never_blocks(db_session):
    _sub(db_session, "shared@example.com")
    valid, message = validate_email_unique(db_session, "shared@example.com")
    assert valid is True
    assert message is None


# --- Layer 2: email is only an unambiguous login alias ---------------------


def test_login_resolves_by_username_and_refuses_shared_subscriber_email(db_session):
    shared = "shared@example.com"
    a = _sub(db_session, shared)
    b = _sub(db_session, shared)
    _local_cred(db_session, subscriber=a, username="alice")
    _local_cred(db_session, subscriber=b, username="bob")

    # Each resolves by its own username.
    assert (
        _resolve_login_credential(
            db_session, provider=AuthProvider.local, identifier="alice"
        ).subscriber_id
        == a.id
    )
    assert (
        _resolve_login_credential(
            db_session, provider=AuthProvider.local, identifier="bob"
        ).subscriber_id
        == b.id
    )

    # Shared contact email is not allowed to select either credential.
    with pytest.raises(CustomerLoginIdentityError) as exc:
        _resolve_login_credential(
            db_session, provider=AuthProvider.local, identifier=shared
        )
    assert exc.value.code == AMBIGUOUS_EMAIL_CODE


def test_admin_system_user_still_logs_in_by_email(db_session):
    admin = SystemUser(first_name="Ad", last_name="Min", email="admin@corp.com")
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    # Username deliberately differs from the email to prove email-resolution.
    _local_cred(db_session, system_user=admin, username="admin-login")

    resolved = _resolve_login_credential(
        db_session, provider=AuthProvider.local, identifier="admin@corp.com"
    )
    assert resolved is not None
    assert resolved.system_user_id == admin.id


# --- Layer 2 risk surface: lookups tolerate shared emails ------------------


def test_password_reset_for_shared_email_is_deterministic(db_session, monkeypatch):
    monkeypatch.setattr(
        auth_flow_service, "_issue_password_reset_token", lambda *a, **k: "tok"
    )
    shared = "shared@example.com"
    a = _sub(db_session, shared)
    b = _sub(db_session, shared)
    _local_cred(db_session, subscriber=a, username="alice")
    cred_b = _local_cred(db_session, subscriber=b, username="bob")

    result = request_password_reset(db_session, shared, ttl_minutes=30)
    assert result is not None
    # Most recent credential wins; b's credential was created last.
    assert result["principal_id"] == str(b.id)
    assert cred_b.subscriber_id == b.id


def test_set_subscriber_email_allows_sharing(db_session):
    a = _sub(db_session, "a@example.com")
    b = _sub(db_session, "b@example.com")
    # Pointing b at a's address must NOT raise (no global uniqueness anymore).
    changed = set_subscriber_email(db_session, str(b.id), "a@example.com")
    assert changed is True
    db_session.refresh(b)
    assert b.email == "a@example.com"
