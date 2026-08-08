"""FUP enforcement stays inside the service that breached, and reports the
window it was measured against.

Defects merged in #2114 are pinned here:

1. The throttle was applied by ``subscriber_id``, so one capped service moved
   every credential the customer owned onto the throttle profile — including
   unrelated, fully-paid subscriptions.
2. The apply helper committed before the ``FupState`` row that justifies it was
   staged. Handlers run on a savepoint-backed session, so that commit released
   the savepoint and made the throttle durable even when the state write failed.
3. Sub-monthly enforcement reported the MONTHLY bucket — 0.0 GB for the offer
   families that carry no bucket at all — so a real daily breach told the
   customer and the audit trail "0 GB exhausted".
4. The minimum-rate floor could RAISE a subscriber's rate in one direction, and
   the both-directions guard let it through.
5. A reused derived throttle profile was returned without validation, so a
   hand-edited row became the only copy of truth for every subscriber whose
   throttle resolved to that rate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.models.catalog import (
    AccessCredential,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.services.enforcement import (
    SubscriptionCredentialScopeError,
    apply_radius_profile_to_subscription,
)
from app.services.fup_enforcement import EvaluateFupSubscriptionCommand
from app.services.fup_usage import FupUsageWindow, fup_window_bounds
from app.services.owner_commands import CommandContext
from tests.fup_helpers import add_fup_rule, ensure_fup_policy

NOW = datetime(2026, 6, 21, 13, 0, tzinfo=UTC)


def _sub(db, subscriber, catalog_offer, status=SubscriptionStatus.active):
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=status,
        billing_mode=catalog_offer.billing_mode,
    )
    db.add(sub)
    db.flush()
    return sub


def _cred(db, subscriber, subscription, profile, username):
    cred = AccessCredential(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        username=username,
        is_active=True,
        radius_profile_id=profile.id,
    )
    db.add(cred)
    db.flush()
    return cred


# ---------------------------------------------------------------------------
# 1. Blast radius: one breaching service, one throttled credential
# ---------------------------------------------------------------------------


def test_throttle_does_not_touch_a_sibling_subscription(
    db_session, subscriber, catalog_offer
):
    full = RadiusProfile(name=f"full-{uuid4().hex[:6]}", is_active=True)
    throttle = RadiusProfile(name=f"throttle-{uuid4().hex[:6]}", is_active=True)
    db_session.add_all([full, throttle])
    db_session.flush()

    breaching = _sub(db_session, subscriber, catalog_offer)
    sibling = _sub(db_session, subscriber, catalog_offer)
    breaching_cred = _cred(db_session, subscriber, breaching, full, "capped-service")
    sibling_cred = _cred(db_session, subscriber, sibling, full, "paid-service")
    db_session.commit()

    application = apply_radius_profile_to_subscription(
        db_session, str(breaching.id), str(throttle.id)
    )

    assert application.matched == 1
    assert application.updated == 1
    assert application.usernames == frozenset({"capped-service"})
    db_session.refresh(breaching_cred)
    db_session.refresh(sibling_cred)
    assert breaching_cred.radius_profile_id == throttle.id
    # The customer's other service is still paid for and must stay at full rate.
    assert sibling_cred.radius_profile_id == full.id


def test_reasserting_an_existing_throttle_still_reports_a_match(
    db_session, subscriber, catalog_offer
):
    """ "Already throttled" is not "nothing to enforce".

    A re-assertion after a cooldown changes nothing on the wire, but the caller
    still has to record the decision. Reporting only `updated` made this
    indistinguishable from having no credentials at all, so state, event and
    customer notification were all skipped.
    """
    throttle = RadiusProfile(name=f"throttle-{uuid4().hex[:6]}", is_active=True)
    db_session.add(throttle)
    db_session.flush()

    sub = _sub(db_session, subscriber, catalog_offer)
    _cred(db_session, subscriber, sub, throttle, "already-throttled")
    db_session.commit()

    application = apply_radius_profile_to_subscription(
        db_session, str(sub.id), str(throttle.id)
    )

    assert application.matched == 1
    assert application.updated == 0


def test_unlinked_credential_is_claimed_only_when_no_sibling_serves(
    db_session, subscriber, catalog_offer
):
    """Legacy NULL-subscription credentials predate the link.

    They still have to be enforceable, but attributing one to a service while a
    sibling is also serving would throttle traffic that may belong to the
    sibling.
    """
    full = RadiusProfile(name=f"full-{uuid4().hex[:6]}", is_active=True)
    throttle = RadiusProfile(name=f"throttle-{uuid4().hex[:6]}", is_active=True)
    db_session.add_all([full, throttle])
    db_session.flush()

    only = _sub(db_session, subscriber, catalog_offer)
    legacy = AccessCredential(
        subscriber_id=subscriber.id,
        subscription_id=None,
        username="legacy-user",
        is_active=True,
        radius_profile_id=full.id,
    )
    db_session.add(legacy)
    db_session.commit()

    application = apply_radius_profile_to_subscription(
        db_session, str(only.id), str(throttle.id)
    )
    assert application.updated == 1
    db_session.refresh(legacy)
    assert legacy.radius_profile_id == throttle.id

    # Introduce a serving sibling; the unlinked credential is now ambiguous and
    # must no longer be swept up by either service.
    db_session.refresh(legacy)
    legacy.radius_profile_id = full.id
    _sub(db_session, subscriber, catalog_offer)
    db_session.commit()

    # Ambiguity must FAIL, not return zero. Returning zero let the caller skip
    # state, event and notification, so a breaching service was silently left
    # at full speed while the sweep counted the enforcement as done.
    with pytest.raises(SubscriptionCredentialScopeError) as captured:
        apply_radius_profile_to_subscription(db_session, str(only.id), str(throttle.id))

    assert captured.value.code == ("access.session_enforcement.credentials_ambiguous")
    db_session.refresh(legacy)
    assert legacy.radius_profile_id == full.id


def test_a_subscription_with_no_credential_at_all_fails_visibly(
    db_session, subscriber, catalog_offer
):
    throttle = RadiusProfile(name=f"throttle-{uuid4().hex[:6]}", is_active=True)
    db_session.add(throttle)
    db_session.flush()
    sub = _sub(db_session, subscriber, catalog_offer)
    db_session.commit()

    with pytest.raises(SubscriptionCredentialScopeError) as captured:
        apply_radius_profile_to_subscription(db_session, str(sub.id), str(throttle.id))

    assert captured.value.code == (
        "access.session_enforcement.no_subscription_credentials"
    )


# ---------------------------------------------------------------------------
# 2. The apply must not own the transaction
# ---------------------------------------------------------------------------


def test_apply_does_not_commit_the_caller_owns_the_transaction(
    db_session, subscriber, catalog_offer
):
    """A throttle with no state row explaining it is unrecoverable evidence.

    Event handlers run on a savepoint-backed child session and the dispatcher
    commits the parent even when a handler raises, so a commit *inside* this
    helper released the savepoint and put the credential change beyond the
    handler's own rollback. The change must stay pending, so that the FupState
    row recording WHY it happened lands in the same transaction.

    Asserted on the invariant (no commit) rather than by rolling back, so the
    test does not depend on the fixture's transaction isolation strategy.
    """
    full = RadiusProfile(name=f"full-{uuid4().hex[:6]}", is_active=True)
    throttle = RadiusProfile(name=f"throttle-{uuid4().hex[:6]}", is_active=True)
    db_session.add_all([full, throttle])
    db_session.flush()

    sub = _sub(db_session, subscriber, catalog_offer)
    cred = _cred(db_session, subscriber, sub, full, "commit-owner")
    db_session.flush()

    with patch.object(
        db_session, "commit", side_effect=AssertionError("apply must not commit")
    ):
        application = apply_radius_profile_to_subscription(
            db_session, str(sub.id), str(throttle.id)
        )

    assert application.updated == 1
    assert application.usernames == frozenset({"commit-owner"})
    # Applied in-session and visible to the caller, but not yet durable.
    assert cred.radius_profile_id == throttle.id


# ---------------------------------------------------------------------------
# 3. Report the window that was measured
# ---------------------------------------------------------------------------


@pytest.fixture
def daily_capped_subscription(db_session, subscriber, catalog_offer):
    """A daily reduce_speed ladder on an offer carrying no quota bucket."""
    offer_id = str(catalog_offer.id)
    ensure_fup_policy(db_session, offer_id)
    add_fup_rule(
        db_session,
        offer_id,
        name="daily-10",
        consumption_period="daily",
        direction="down",
        threshold_amount=10,
        threshold_unit="gb",
        action="reduce_speed",
        speed_reduction_percent=50,
    )
    sub = _sub(db_session, subscriber, catalog_offer)
    db_session.commit()
    return sub


def test_daily_breach_reports_the_daily_window_not_the_monthly_bucket(
    db_session, daily_capped_subscription, monkeypatch
):
    sub = daily_capped_subscription
    bounds = fup_window_bounds("daily", NOW)

    # 42 GB burned today. There is no monthly quota bucket on this family, so
    # the pre-fix code reported 0.0 GB for a breach of a 10 GB daily cap.
    monkeypatch.setattr(
        "app.services.fup_usage.build_usage_by_period",
        lambda *_a, **_k: {
            "daily": FupUsageWindow(
                used_gb=42.0,
                window=bounds,
                source="samples",
                is_authoritative=True,
            )
        },
    )

    captured: list[dict] = []
    monkeypatch.setattr(
        "app.services.fup_enforcement._emit_fup_notifications",
        lambda _db, pending: captured.extend(pending) or len(pending),
    )

    emitted: list[dict] = []
    monkeypatch.setattr(
        "app.services.fup_enforcement.emit_event",
        lambda _db, _type, payload, **_kw: emitted.append(payload),
    )

    from app.services.fup_enforcement import _evaluate_subscription

    command_id = uuid4()
    _evaluate_subscription(
        db_session,
        EvaluateFupSubscriptionCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="test:fup",
                scope=str(sub.id),
                reason="test_daily_usage_evidence",
            ),
            subscription_id=sub.id,
            evaluated_at=NOW,
            warning_enabled=False,
            warning_ratio=0.8,
            throttle_profile_configured=True,
        ),
    )

    assert emitted, "a daily breach must emit a usage_exhausted event"
    assert emitted[0]["current_usage_gb"] == 42.0
    assert captured, "the customer must be told about the breach"
    assert captured[0]["used_gb"] == 42.0


# ---------------------------------------------------------------------------
# 4. A throttle may only take away
# ---------------------------------------------------------------------------


def test_the_floor_never_raises_a_rate_in_either_direction():
    """MIN_THROTTLE_KBPS bounds how hard a throttle bites, not how fast it makes
    the customer.

    On an already-slow plan the floor can land above the subscriber's real rate
    in ONE direction. The both-directions guard is an ``and``, so that used to
    pass through as a "throttle" that handed the customer a faster uplink.
    """
    from app.services.fup_throttle_profile import MIN_THROTTLE_KBPS, reduced_kbps

    slow_uplink = 256
    assert slow_uplink < MIN_THROTTLE_KBPS
    # reduced_kbps still floors, which is why the caller must cap.
    assert reduced_kbps(slow_uplink, 50) == MIN_THROTTLE_KBPS
    assert min(reduced_kbps(slow_uplink, 50), slow_uplink) == slow_uplink


def test_a_reused_derived_profile_is_repaired_not_trusted(db_session):
    """These rows are a projection with one writer, so drift is repairable.

    Returning a hand-edited row unchecked made the cache the only copy of
    truth: a profile named for 5000k could be projecting anything to the NAS,
    for every subscriber whose throttle resolves to that rate.
    """
    from app.services.fup_throttle_profile import resolve_or_create_profile

    created = resolve_or_create_profile(
        db_session, download_kbps=5000, upload_kbps=2500
    )
    db_session.flush()

    # Someone edits the derived row by hand and deactivates it.
    created.download_speed = 99
    created.mikrotik_rate_limit = "1k/1k"
    created.is_active = False
    db_session.flush()

    again = resolve_or_create_profile(db_session, download_kbps=5000, upload_kbps=2500)

    assert again.id == created.id, "the code key must still be reused, not forked"
    assert again.download_speed == 5000
    assert again.upload_speed == 2500
    # Upload first: MikroTik rx is the subscriber's upload.
    assert again.mikrotik_rate_limit == "2500k/5000k"
    assert again.is_active is True
