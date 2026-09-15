"""Safe customer local-login identity resolution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.auth import AuthProvider, UserCredential
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.domain_errors import DomainError

AMBIGUOUS_EMAIL_CODE = "auth.customer_login_identity.ambiguous_email"
INACTIVE_CREDENTIAL_CODE = "auth.customer_login_identity.inactive_credential"
AMBIGUOUS_EMAIL_MESSAGE = (
    "This email is linked to multiple customer accounts. "
    "Please use your customer number."
)
INACTIVE_CREDENTIAL_MESSAGE = "Account disabled. Please contact support."


class CustomerLoginResolutionStatus(StrEnum):
    """Closed result vocabulary for customer local-login identity lookup."""

    matched = "matched"
    not_found = "not_found"
    ambiguous = "ambiguous"
    inactive_credential = "inactive_credential"


class CustomerLoginMatchSource(StrEnum):
    """Authoritative reason a customer credential was selected."""

    exact_username = "exact_username"
    case_insensitive_email_username = "case_insensitive_email_username"
    unique_customer_email = "unique_customer_email"


@dataclass(frozen=True, slots=True)
class ResolveCustomerLoginIdentity:
    """Typed query for one entered customer login identifier."""

    identifier: str


@dataclass(frozen=True, slots=True)
class CustomerLoginIdentityResolution:
    """Typed, non-secret login identity decision."""

    status: CustomerLoginResolutionStatus
    credential_id: UUID | None = None
    subscriber_id: UUID | None = None
    source: CustomerLoginMatchSource | None = None
    candidate_count: int = 0


class CustomerLoginIdentityError(DomainError):
    """Safe refusal emitted when an identifier must not be selected."""


def resolution_error(
    resolution: CustomerLoginIdentityResolution,
) -> CustomerLoginIdentityError | None:
    """Translate a refusal outcome without leaking candidate identities."""

    if resolution.status is CustomerLoginResolutionStatus.ambiguous:
        return CustomerLoginIdentityError(
            code=AMBIGUOUS_EMAIL_CODE,
            message=AMBIGUOUS_EMAIL_MESSAGE,
            retryable=False,
        )
    if resolution.status is CustomerLoginResolutionStatus.inactive_credential:
        return CustomerLoginIdentityError(
            code=INACTIVE_CREDENTIAL_CODE,
            message=INACTIVE_CREDENTIAL_MESSAGE,
            retryable=False,
        )
    return None


def _matched(
    credential: UserCredential,
    *,
    source: CustomerLoginMatchSource,
) -> CustomerLoginIdentityResolution:
    subscriber_id = credential.subscriber_id
    if subscriber_id is None:
        return CustomerLoginIdentityResolution(
            status=CustomerLoginResolutionStatus.not_found
        )
    if not credential.is_active:
        return CustomerLoginIdentityResolution(
            status=CustomerLoginResolutionStatus.inactive_credential,
            credential_id=credential.id,
            subscriber_id=subscriber_id,
            source=source,
            candidate_count=1,
        )
    return CustomerLoginIdentityResolution(
        status=CustomerLoginResolutionStatus.matched,
        credential_id=credential.id,
        subscriber_id=subscriber_id,
        source=source,
        candidate_count=1,
    )


def resolve_customer_login_identity(
    db: Session,
    query: ResolveCustomerLoginIdentity,
) -> CustomerLoginIdentityResolution:
    """Resolve a customer credential without guessing from shared contact email.

    Stored credential usernames remain authoritative and are checked first.
    Contact email is only a login alias when exactly one eligible customer owns
    it and that customer has exactly one active local portal credential.
    """

    identifier = query.identifier.strip()
    if not identifier:
        return CustomerLoginIdentityResolution(
            status=CustomerLoginResolutionStatus.not_found
        )

    exact_credential = db.scalar(
        select(UserCredential)
        .where(UserCredential.provider == AuthProvider.local)
        .where(UserCredential.subscriber_id.is_not(None))
        .where(UserCredential.username == identifier)
        .limit(1)
    )
    if exact_credential is not None:
        return _matched(
            exact_credential,
            source=CustomerLoginMatchSource.exact_username,
        )

    # A customer number or PPPoE username is never interpreted as contact
    # information. Those established paths continue through their exact keys.
    if "@" not in identifier:
        return CustomerLoginIdentityResolution(
            status=CustomerLoginResolutionStatus.not_found
        )

    casefold_username_credentials = list(
        db.scalars(
            select(UserCredential)
            .where(UserCredential.provider == AuthProvider.local)
            .where(UserCredential.subscriber_id.is_not(None))
            .where(func.lower(UserCredential.username) == identifier.lower())
            .limit(2)
        )
    )
    if len(casefold_username_credentials) > 1:
        return CustomerLoginIdentityResolution(
            status=CustomerLoginResolutionStatus.ambiguous,
            candidate_count=2,
        )
    if casefold_username_credentials:
        return _matched(
            casefold_username_credentials[0],
            source=CustomerLoginMatchSource.case_insensitive_email_username,
        )

    matching_subscribers = list(
        db.scalars(
            select(Subscriber)
            .where(func.lower(Subscriber.email) == identifier.lower())
            .limit(2)
        )
    )
    if not matching_subscribers:
        return CustomerLoginIdentityResolution(
            status=CustomerLoginResolutionStatus.not_found
        )
    if len(matching_subscribers) > 1:
        return CustomerLoginIdentityResolution(
            status=CustomerLoginResolutionStatus.ambiguous,
            candidate_count=2,
        )

    subscriber = matching_subscribers[0]
    if not subscriber.is_active or subscriber.status in {
        SubscriberStatus.disabled,
        SubscriberStatus.canceled,
    }:
        return CustomerLoginIdentityResolution(
            status=CustomerLoginResolutionStatus.not_found,
            subscriber_id=subscriber.id,
            candidate_count=1,
        )

    credentials = list(
        db.scalars(
            select(UserCredential)
            .where(UserCredential.provider == AuthProvider.local)
            .where(UserCredential.subscriber_id == subscriber.id)
            .where(UserCredential.is_active.is_(True))
            .where(UserCredential.password_hash.is_not(None))
            .limit(2)
        )
    )
    if not credentials:
        return CustomerLoginIdentityResolution(
            status=CustomerLoginResolutionStatus.not_found,
            subscriber_id=subscriber.id,
            candidate_count=1,
        )
    if len(credentials) > 1:
        return CustomerLoginIdentityResolution(
            status=CustomerLoginResolutionStatus.ambiguous,
            subscriber_id=subscriber.id,
            candidate_count=2,
        )
    return _matched(
        credentials[0],
        source=CustomerLoginMatchSource.unique_customer_email,
    )
