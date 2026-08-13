"""Grace timing has one precedence and one date-based decision contract."""

from datetime import UTC, datetime

import pytest

from app.models.catalog import BillingMode, PolicySet
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.collections.grace_policy import (
    EffectiveGracePolicy,
    GracePhase,
    GracePolicyError,
    GracePolicySetSource,
    GracePolicySource,
    decide_grace,
    resolve_effective_grace_policy,
    resolve_policy_set_decision,
)
from app.services.web_catalog_settings import policy_set_form_context


def _billing_grace_default(db, *, mode: BillingMode, days: int) -> None:
    key = f"{mode.value}_default_grace_period_days"
    db.query(DomainSetting).filter(
        DomainSetting.domain == SettingDomain.billing,
        DomainSetting.key == key,
    ).delete(synchronize_session=False)
    db.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key=key,
            value_type=SettingValueType.integer,
            value_text=str(days),
            is_active=True,
        )
    )
    db.flush()


def test_grace_precedence_is_account_then_policy_then_billing_default(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscription.billing_mode = BillingMode.prepaid
    policy = PolicySet(name="Prepaid grace", grace_days=5, is_active=True)
    db_session.add(policy)
    db_session.flush()
    subscriber_account.policy_set_id = policy.id
    _billing_grace_default(db_session, mode=BillingMode.prepaid, days=9)

    inherited = resolve_effective_grace_policy(db_session, subscriber_account)
    assert (inherited.days, inherited.source, inherited.policy_set_id) == (
        5,
        GracePolicySource.POLICY_SET,
        policy.id,
    )
    assert inherited.policy_set_source is GracePolicySetSource.ACCOUNT

    subscriber_account.grace_period_days = 2
    overridden = resolve_effective_grace_policy(db_session, subscriber_account)
    assert (overridden.days, overridden.source) == (
        2,
        GracePolicySource.ACCOUNT_OVERRIDE,
    )

    subscriber_account.grace_period_days = None
    subscriber_account.policy_set_id = None
    policy.is_active = False
    defaulted = resolve_effective_grace_policy(
        db_session,
        subscriber_account,
        policy_set_id=policy.id,
    )
    assert (defaulted.days, defaulted.source) == (
        9,
        GracePolicySource.BILLING_MODE_DEFAULT,
    )
    assert defaulted.policy_set_source is GracePolicySetSource.EXPLICIT


def test_dunning_offsets_begin_after_grace_end():
    policy = EffectiveGracePolicy(
        days=7,
        source=GracePolicySource.POLICY_SET,
        billing_mode=BillingMode.postpaid,
        policy_set_id=None,
    )
    due_at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

    last_grace_day = decide_grace(
        policy,
        starts_at=due_at,
        as_of=datetime(2026, 7, 8, 23, 59, tzinfo=UTC),
    )
    first_action_day = decide_grace(
        policy,
        starts_at=due_at,
        as_of=datetime(2026, 7, 9, 0, 1, tzinfo=UTC),
    )

    assert last_grace_day.phase is GracePhase.IN_GRACE
    assert last_grace_day.elapsed_days_after_grace == 0
    assert last_grace_day.ends_at == datetime(2026, 7, 9, 0, 0, tzinfo=UTC)
    assert first_action_day.phase is GracePhase.ACTIONABLE
    assert first_action_day.elapsed_days_after_grace == 1


def test_configured_zero_grace_is_immediately_actionable():
    policy = EffectiveGracePolicy(
        days=0,
        source=GracePolicySource.BILLING_MODE_DEFAULT,
        billing_mode=BillingMode.prepaid,
        policy_set_id=None,
    )
    starts_at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

    decision = decide_grace(policy, starts_at=starts_at, as_of=starts_at)

    assert decision.phase is GracePhase.ACTIONABLE
    assert decision.ends_at == starts_at


def test_policy_form_preserves_explicit_zero_grace(db_session):
    policy = PolicySet(name="No grace", grace_days=0, is_active=True)
    db_session.add(policy)
    db_session.commit()

    context = policy_set_form_context(db_session, policy_id=str(policy.id))

    assert context is not None
    assert context["policy"]["grace_days"] == 0


def test_invalid_default_policy_set_id_is_a_stable_domain_failure(
    db_session, subscriber_account, monkeypatch
):
    monkeypatch.setattr(
        "app.services.collections.grace_policy.settings_spec.resolve_value",
        lambda *_args: "not-a-uuid",
    )

    with pytest.raises(GracePolicyError) as captured:
        resolve_policy_set_decision(db_session, subscriber_account)

    assert captured.value.code == "financial.grace_policy.invalid_policy_set_id"


def test_invalid_grace_days_fail_closed(db_session, subscriber_account, monkeypatch):
    def _setting(_db, domain, key):
        # `==`, not `is`: enum members were interned singletons, whereas the
        # open member type constructs a new object per call.
        if domain == SettingDomain.collections:
            return None
        if key.endswith("_default_grace_period_days"):
            return "invalid"
        return None

    monkeypatch.setattr(
        "app.services.collections.grace_policy.settings_spec.resolve_value",
        _setting,
    )

    with pytest.raises(GracePolicyError) as captured:
        resolve_effective_grace_policy(db_session, subscriber_account)

    assert captured.value.code == "financial.grace_policy.invalid_grace_days"


def test_naive_grace_timestamps_are_normalized_to_utc():
    policy = EffectiveGracePolicy(
        days=0,
        source=GracePolicySource.BILLING_MODE_DEFAULT,
        billing_mode=BillingMode.prepaid,
        policy_set_id=None,
    )
    starts_at = datetime(2026, 7, 1, 12, 0)

    decision = decide_grace(policy, starts_at=starts_at, as_of=starts_at)

    assert decision.phase is GracePhase.ACTIONABLE
    assert decision.starts_at == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assert decision.as_of == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
