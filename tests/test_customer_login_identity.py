from __future__ import annotations

import pytest

from app.models.auth import AuthProvider, UserCredential
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.auth_flow import hash_password
from app.services.customer_login_identity import (
    CustomerLoginMatchSource,
    CustomerLoginResolutionStatus,
    ResolveCustomerLoginIdentity,
    resolve_customer_login_identity,
)


def _subscriber(
    db,
    *,
    email: str,
    status: SubscriberStatus = SubscriberStatus.active,
    is_active: bool = True,
) -> Subscriber:
    subscriber = Subscriber(
        first_name="Email",
        last_name="Login",
        email=email,
        status=status,
        is_active=is_active,
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def _credential(
    db,
    *,
    subscriber: Subscriber,
    username: str,
    is_active: bool = True,
) -> UserCredential:
    credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username=username,
        password_hash=hash_password("portal-secret"),
        is_active=is_active,
    )
    db.add(credential)
    db.commit()
    return credential


def _resolve(db, identifier: str):
    return resolve_customer_login_identity(
        db,
        ResolveCustomerLoginIdentity(identifier=identifier),
    )


def test_unique_customer_email_resolves_active_portal_credential_case_insensitively(
    db_session,
):
    subscriber = _subscriber(db_session, email="Unique.Customer@Example.com")
    credential = _credential(
        db_session,
        subscriber=subscriber,
        username="105000001",
    )

    resolution = _resolve(db_session, "  unique.customer@example.COM ")

    assert resolution.status is CustomerLoginResolutionStatus.matched
    assert resolution.source is CustomerLoginMatchSource.unique_customer_email
    assert resolution.credential_id == credential.id
    assert resolution.subscriber_id == subscriber.id


def test_shared_customer_email_is_ambiguous_but_existing_username_still_resolves(
    db_session,
):
    shared_email = "shared@example.com"
    first = _subscriber(db_session, email=shared_email)
    first_credential = _credential(
        db_session,
        subscriber=first,
        username=shared_email,
    )
    second = _subscriber(db_session, email=shared_email)
    _credential(db_session, subscriber=second, username="105000002")

    existing_login = _resolve(db_session, shared_email)
    assert existing_login.status is CustomerLoginResolutionStatus.matched
    assert existing_login.source is CustomerLoginMatchSource.exact_username
    assert existing_login.credential_id == first_credential.id

    first_credential.username = "105000001"
    db_session.commit()
    ambiguous = _resolve(db_session, shared_email)
    assert ambiguous.status is CustomerLoginResolutionStatus.ambiguous
    assert ambiguous.credential_id is None


def test_shared_email_is_ambiguous_even_when_one_customer_is_canceled(db_session):
    shared_email = "mixed-status-shared@example.com"
    active = _subscriber(db_session, email=shared_email)
    _credential(db_session, subscriber=active, username="105000021")
    canceled = _subscriber(
        db_session,
        email=shared_email,
        status=SubscriberStatus.canceled,
    )
    _credential(db_session, subscriber=canceled, username="105000022")

    resolution = _resolve(db_session, shared_email)

    assert resolution.status is CustomerLoginResolutionStatus.ambiguous
    assert resolution.credential_id is None


def test_customer_number_username_resolution_is_unchanged(db_session):
    subscriber = _subscriber(db_session, email="number@example.com")
    credential = _credential(
        db_session,
        subscriber=subscriber,
        username="105000003",
    )

    resolution = _resolve(db_session, "105000003")

    assert resolution.status is CustomerLoginResolutionStatus.matched
    assert resolution.source is CustomerLoginMatchSource.exact_username
    assert resolution.credential_id == credential.id


def test_suspended_customer_email_remains_eligible(db_session):
    subscriber = _subscriber(
        db_session,
        email="suspended@example.com",
        status=SubscriberStatus.suspended,
    )
    credential = _credential(
        db_session,
        subscriber=subscriber,
        username="105000004",
    )

    resolution = _resolve(db_session, subscriber.email)

    assert resolution.status is CustomerLoginResolutionStatus.matched
    assert resolution.credential_id == credential.id


@pytest.mark.parametrize(
    "status",
    (SubscriberStatus.disabled, SubscriberStatus.canceled),
)
def test_terminal_customer_email_is_not_eligible(db_session, status):
    subscriber = _subscriber(
        db_session,
        email=f"{status.value}@example.com",
        status=status,
    )
    _credential(
        db_session,
        subscriber=subscriber,
        username=f"105-{status.value}",
    )

    resolution = _resolve(db_session, subscriber.email)

    assert resolution.status is CustomerLoginResolutionStatus.not_found
    assert resolution.credential_id is None


def test_inactive_customer_or_portal_credential_cannot_use_email_alias(db_session):
    inactive_customer = _subscriber(
        db_session,
        email="inactive-customer@example.com",
        is_active=False,
    )
    _credential(
        db_session,
        subscriber=inactive_customer,
        username="105000005",
    )
    inactive_credential_customer = _subscriber(
        db_session,
        email="inactive-credential@example.com",
    )
    _credential(
        db_session,
        subscriber=inactive_credential_customer,
        username="105000006",
        is_active=False,
    )

    assert (
        _resolve(db_session, inactive_customer.email).status
        is CustomerLoginResolutionStatus.not_found
    )
    assert (
        _resolve(db_session, inactive_credential_customer.email).status
        is CustomerLoginResolutionStatus.not_found
    )
