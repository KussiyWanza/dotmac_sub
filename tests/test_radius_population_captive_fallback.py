"""Captive-mode RADIUS projection must fail closed on an unusable credential.

`resolve_subscription_restriction`/`plan_login_radius_projections` can resolve
a blocked/suspended subscription's mode to ``"captive"`` (walled-garden
pay-page redirect) instead of a hard ``"reject"``. Captive still needs a
working password to let the login authenticate onto the walled network at
all, so an undecryptable/missing credential previously fell back to
``preserve_usernames`` -- leaving whatever RADIUS row already existed for
that username (possibly a fully permissive one from before the subscription
was blocked) completely untouched. That is a fail-OPEN path: the customer
keeps unrestricted access instead of being captive-redirected or blocked.

This must now downgrade to the SAME hard-reject projection the file already
builds for `mode == "reject"` (see `populate()`'s "A hard reject is a
complete RADIUS projection..." comment) -- not invent a new mechanism, and
not silently preserve stale state.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from app.models.catalog import (
    AccessCredential,
    AccessState,
    AccessType,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.services import radius_population
from app.services.credential_crypto import (
    encrypt_credential_with_key,
    get_encryption_key,
)
from app.services.radius_projection_planner import (
    LoginRadiusProjection,
    RadiusProjectionPlan,
)


@pytest.fixture()
def _radius_env(monkeypatch, db_session):
    target = {"target_name": "test", "target_fingerprint": "test-target"}
    monkeypatch.setattr(
        radius_population,
        "active_external_radius_targets",
        lambda _db, capability=None: [target],
    )
    monkeypatch.setattr(
        radius_population, "assert_legacy_target_alignment", lambda _db: []
    )
    monkeypatch.setattr(
        radius_population,
        "simultaneous_use_enforcement_enabled",
        lambda _db: True,
    )
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(radius_population, "SessionLocal", lambda: db_session)


def _offer(db) -> CatalogOffer:
    offer = CatalogOffer(
        name=f"Offer {uuid.uuid4().hex[:6]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.prepaid,
        billing_cycle="monthly",
        speed_download_mbps=100,
        speed_upload_mbps=100,
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return offer


def _account(db) -> Subscriber:
    account = Subscriber(
        first_name="T",
        last_name="User",
        email=f"t{uuid.uuid4().hex[:8]}@example.com",
        status="blocked",
        is_active=True,
        billing_mode=BillingMode.prepaid,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _subscription(
    db, account, offer, *, login, status=SubscriptionStatus.blocked
) -> Subscription:
    subscription = Subscription(
        subscriber_id=account.id,
        offer_id=offer.id,
        status=status,
        billing_mode=BillingMode.prepaid,
        login=login,
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return subscription


def _blocked_subscription(db, account, offer, *, login) -> Subscription:
    return _subscription(db, account, offer, login=login)


def _force_projection(
    monkeypatch, subscription: Subscription, login: str, *, mode: str
):
    """Bypass walled-garden eligibility resolution and pin an exact mode.

    This isolates the exact behavior under test (what `populate()` does once
    a login's projection is a given mode) from the separate,
    independently-tested question of *when* `resolve_subscription_restriction`
    decides that mode is the effective one.
    """
    is_captive = mode == "captive"
    plan = RadiusProjectionPlan(
        mode=mode,
        access_state=AccessState.captive if is_captive else AccessState.active,
        blocked=is_captive,
        radius_allowed=True,
        write_password=mode in {"active", "captive"},
        write_radreply=mode in {"active", "captive"},
        captive=is_captive,
        block_reason="subscription_status_blocked" if is_captive else None,
        billing_access_state=None,
    )
    projection = LoginRadiusProjection(
        login=login,
        subscription_id=str(subscription.id),
        subscription_status=subscription.status,
        plan=plan,
    )
    monkeypatch.setattr(
        radius_population,
        "plan_login_radius_projections",
        lambda _db, _rows=None: {login: projection},
    )


def _force_captive_projection(monkeypatch, subscription: Subscription, login: str):
    _force_projection(monkeypatch, subscription, login, mode="captive")


def _add_credential(db, account, subscription, login, *, key):
    db.add(
        AccessCredential(
            subscriber_id=account.id,
            subscription_id=subscription.id,
            username=login,
            is_active=True,
            secret_hash=encrypt_credential_with_key("pw-" + login, key),
        )
    )
    db.commit()


def _add_ambiguous_ipv4_ledger(db, account, subscription):
    """Two active, non-primary IPv4 assignments for the same subscription --
    the exact condition `_single_active_ipv4` refuses to guess an owner for."""
    for address in ("192.0.2.10", "192.0.2.20"):
        ipv4 = IPv4Address(address=address)
        db.add(ipv4)
        db.flush()
        db.add(
            IPAssignment(
                subscriber_id=account.id,
                subscription_id=subscription.id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=ipv4.id,
                is_primary=False,
                is_active=True,
            )
        )
    db.commit()


def test_captive_with_undecryptable_credential_downgrades_to_reject(
    _radius_env, monkeypatch, db_session
):
    offer = _offer(db_session)
    account = _account(db_session)
    login = f"captive-bad-{uuid.uuid4().hex[:8]}"
    subscription = _blocked_subscription(db_session, account, offer, login=login)
    db_session.add(
        AccessCredential(
            subscriber_id=account.id,
            subscription_id=subscription.id,
            username=login,
            is_active=True,
            # `decrypt_credential_with_key` treats any value WITHOUT an
            # `enc:`/`plain:` prefix as legacy plaintext and returns it
            # verbatim (no exception raised) -- so this must carry the
            # `enc:` prefix to actually be routed into the Fernet-decrypt
            # path and fail there, simulating a corrupted secret /
            # mismatched encryption key.
            secret_hash="enc:not-a-real-fernet-token",
        )
    )
    db_session.commit()
    _force_captive_projection(monkeypatch, subscription, login)

    stats = radius_population.populate(dry_run=True)

    assert stats["rejected_users_written"] == 1
    assert stats["captive_users_written"] == 0
    assert stats["captive_downgraded_to_reject"] == 1
    assert stats["projected_logins"] == 1
    # The login was actively rewritten to reject (not skipped as unbuildable
    # with nothing written): projected_logins counts only `work` items, and
    # rejected_users_written == 1 already proves a reject row was built for
    # it above -- unbuildable_logins still reflects that the *intended*
    # captive projection could not be built as-is.
    assert stats["unbuildable_logins"] == 1


def test_captive_with_no_credential_at_all_downgrades_to_reject(
    _radius_env, monkeypatch, db_session
):
    offer = _offer(db_session)
    account = _account(db_session)
    login = f"captive-nocred-{uuid.uuid4().hex[:8]}"
    subscription = _blocked_subscription(db_session, account, offer, login=login)
    # No AccessCredential row at all.
    _force_captive_projection(monkeypatch, subscription, login)

    stats = radius_population.populate(dry_run=True)

    assert stats["rejected_users_written"] == 1
    assert stats["captive_users_written"] == 0
    assert stats["captive_downgraded_to_reject"] == 1


def test_captive_with_working_credential_is_unaffected(
    _radius_env, monkeypatch, db_session
):
    """Regression guard: a captive login whose credential DOES decrypt must
    keep getting the normal captive-portal treatment -- the fix only changes
    the undecryptable-credential branch."""
    offer = _offer(db_session)
    account = _account(db_session)
    login = f"captive-good-{uuid.uuid4().hex[:8]}"
    subscription = _blocked_subscription(db_session, account, offer, login=login)
    key = get_encryption_key()
    db_session.add(
        AccessCredential(
            subscriber_id=account.id,
            subscription_id=subscription.id,
            username=login,
            is_active=True,
            secret_hash=encrypt_credential_with_key("pw-" + login, key),
        )
    )
    db_session.commit()
    _force_captive_projection(monkeypatch, subscription, login)

    stats = radius_population.populate(dry_run=True)

    assert stats["captive_users_written"] == 1
    assert stats["rejected_users_written"] == 0
    assert stats["captive_downgraded_to_reject"] == 0
    assert stats["projection_complete"] is True


def test_captive_with_ambiguous_ipv4_ledger_downgrades_to_reject(
    _radius_env, monkeypatch, db_session
):
    """The ambiguous-active-IPv4-ledger branch is reachable by a captive-mode
    login too (it fires after credential decryption succeeds, once the
    subscription's own `ipv4_address` column is empty). It is exactly as
    unusable to captive as an undecryptable credential -- no routable
    Framed-IP means no real walled-garden session -- so it must get the same
    fail-closed downgrade, not `preserve_usernames`."""
    offer = _offer(db_session)
    account = _account(db_session)
    login = f"captive-ambiguous-{uuid.uuid4().hex[:8]}"
    subscription = _blocked_subscription(db_session, account, offer, login=login)
    assert subscription.ipv4_address is None
    key = get_encryption_key()
    _add_credential(db_session, account, subscription, login, key=key)
    _add_ambiguous_ipv4_ledger(db_session, account, subscription)
    _force_captive_projection(monkeypatch, subscription, login)

    stats = radius_population.populate(dry_run=True)

    assert stats["skipped_ambiguous_ipv4_ledger"] == 1
    assert stats["rejected_users_written"] == 1
    assert stats["captive_users_written"] == 0
    assert stats["captive_downgraded_to_reject"] == 1


def test_active_with_ambiguous_ipv4_ledger_still_preserves(
    _radius_env, monkeypatch, db_session
):
    """Regression guard: the documented ownership-refusal preserve-and-report
    behavior for a NON-captive (active-mode) login hitting the same ambiguous
    ledger must be completely unchanged -- this fix only closes the
    captive-mode gap."""
    offer = _offer(db_session)
    account = _account(db_session)
    login = f"active-ambiguous-{uuid.uuid4().hex[:8]}"
    subscription = _subscription(
        db_session, account, offer, login=login, status=SubscriptionStatus.active
    )
    key = get_encryption_key()
    _add_credential(db_session, account, subscription, login, key=key)
    _add_ambiguous_ipv4_ledger(db_session, account, subscription)
    _force_projection(monkeypatch, subscription, login, mode="active")

    stats = radius_population.populate(dry_run=True)

    assert stats["skipped_ambiguous_ipv4_ledger"] == 1
    assert stats["captive_downgraded_to_reject"] == 0
    assert stats["rejected_users_written"] == 0
    assert stats["captive_users_written"] == 0
    # Nothing was written for this login at all -- it was preserved, not
    # rewritten as reject or as active.
    assert stats["projected_logins"] == 0
    assert stats["unbuildable_logins"] == 1
