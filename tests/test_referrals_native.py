"""Native referral service: capture → qualify → reward flows,
external_ref idempotency continuity with the CRM's payout path, the
subscriber-activation hook wiring, and read-shape compatibility with the
referral mirror."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.audit import AuditEvent
from app.models.billing import CreditNote
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.event_store import EventStore
from app.models.notification import CommunicationIntentRecord, Notification
from app.models.party import (
    Party,
    PartyContactPoint,
    PartyContactPointType,
    PartyIdentityStatus,
)
from app.models.referral_native import Referral, ReferralCode
from app.models.sales import Lead, LeadOriginCapture
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import crm_api
from app.services import party as party_service
from app.services import referrals as referral_program
from app.services.customer_lifecycle_audit import build_customer_lifecycle_audit
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.referrals import (
    _CODE_ALPHABET,
    ReferralAttachmentError,
    ReferralProgramError,
    referrals,
)
from tests.referral_program_testkit import (
    capture,
    ensure_code,
    issue_reward,
    qualify_for_subscriber,
    refer_friend,
    reject,
)


def _unique_email() -> str:
    return f"ref-{uuid.uuid4().hex[:10]}@example.com"


def _subscriber(db, *, status=SubscriberStatus.active, email=None) -> Subscriber:
    sub = Subscriber(
        first_name="Refer",
        last_name="Rer",
        email=email or _unique_email(),
        status=status,
        is_active=True,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _program(
    db,
    *,
    enabled: bool = True,
    amount: str = "2500",
    window_days: int | None = None,
    auto_approve: bool | None = None,
):
    rows = {
        "referral_program_enabled": (
            "true" if enabled else "false",
            SettingValueType.boolean,
        ),
        "referral_reward_amount": (amount, SettingValueType.string),
    }
    if window_days is not None:
        rows["referral_qualify_window_days"] = (
            str(window_days),
            SettingValueType.integer,
        )
    if auto_approve is not None:
        rows["referral_auto_approve_reward"] = (
            "true" if auto_approve else "false",
            SettingValueType.boolean,
        )
    for key, (text, value_type) in rows.items():
        db.add(
            DomainSetting(
                domain=SettingDomain.subscriber,
                key=key,
                value_type=value_type,
                value_text=text,
                is_active=True,
            )
        )
    db.commit()


# ── codes ────────────────────────────────────────────────────────────────────


def test_ensure_code_mints_and_reuses(db_session):
    referrer = _subscriber(db_session)
    code = ensure_code(db_session, referrer.id)
    assert len(code.code) == 8
    assert set(code.code) <= set(_CODE_ALPHABET)
    again = ensure_code(db_session, referrer.id)
    assert again.id == code.id  # one active code per referrer


def test_ensure_code_unknown_subscriber_404(db_session):
    with pytest.raises(ReferralProgramError) as exc:
        ensure_code(db_session, uuid.uuid4())
    assert exc.value.code == "referrals.program.subscriber_not_found"


def test_program_owner_rejects_an_active_caller_transaction(db_session):
    referrer = _subscriber(db_session)
    db_session.get(Subscriber, referrer.id)
    assert db_session.in_transaction()

    with pytest.raises(DomainError) as exc:
        referral_program.ensure_referral_code(
            db_session,
            referral_program.EnsureReferralCodeCommand(
                context=CommandContext.system(
                    actor="test_referral_program",
                    scope=referral_program.REFERRAL_PROGRAM_SCOPE,
                    reason="Prove the adapter cannot own the transaction",
                ),
                subscriber_id=referrer.id,
            ),
        )

    assert exc.value.code == "referrals.program.active_caller_transaction"
    assert not db_session.in_transaction()


# ── capture ──────────────────────────────────────────────────────────────────


def test_capture_creates_prospect_lead_and_referral(db_session):
    _program(db_session)
    referrer = _subscriber(db_session)
    code = ensure_code(db_session, referrer.id)

    email = _unique_email()
    subscriber_count = db_session.query(Subscriber).count()
    referral = capture(
        db_session,
        code=code.code,
        name="Ada Lovelace",
        email=email,
        phone="0803 000 0001",
        region="Abuja",
        source="public",
    )
    assert referral.status == "pending"
    assert referral.reward_status == "none"
    assert referral.referrer_subscriber_id == referrer.id
    assert referral.referral_code_id == code.id
    assert referral.source == "public"
    assert referral.referred_subscriber_id is None
    assert db_session.query(Subscriber).count() == subscriber_count
    assert not (referral.metadata_ or {}).get("capture")

    prospect = db_session.get(Party, referral.referred_party_id)
    assert prospect is not None
    assert prospect.display_name == "Ada Lovelace"
    assert prospect.status == PartyIdentityStatus.quarantined.value
    points = {
        point.channel_type: point
        for point in db_session.query(PartyContactPoint)
        .filter(PartyContactPoint.party_id == prospect.id)
        .all()
    }
    assert points[PartyContactPointType.email.value].normalized_value == email
    assert (
        points[PartyContactPointType.phone.value].normalized_value == "+2348030000001"
    )
    assert all(point.verification_status == "unverified" for point in points.values())

    # Attributed lead.
    lead = db_session.get(Lead, referral.referred_lead_id)
    assert lead is not None
    assert lead.party_id == prospect.id
    assert lead.subscriber_id is None
    assert lead.lead_source == "Referrer"
    assert lead.metadata_["referral_code"] == code.code
    assert lead.metadata_["referrer_subscriber_id"] == str(referrer.id)
    origin = db_session.query(LeadOriginCapture).filter_by(lead_id=lead.id).one()
    assert origin.capture_method == "referral"
    assert origin.source_platform == "referral"
    assert origin.lead_source == "Referrer"

    audit = build_customer_lifecycle_audit(db_session)
    assert audit["referrals"]["party_bound"] == 1
    assert audit["referrals"]["awaiting_account_conversion"] == 1
    assert audit["referrals"]["quarantined_awaiting_account_adjudication"] == 1
    assert audit["referrals"]["active_awaiting_account_conversion"] == 0
    assert audit["referrals"]["legacy_capture_pii_metadata"] == 0
    assert audit["referrals"]["aligned"] == 1


def test_capture_is_idempotent_per_referred_prospect(db_session):
    _program(db_session)
    referrer = _subscriber(db_session)
    code = ensure_code(db_session, referrer.id)
    email = _unique_email()

    first = capture(db_session, code=code.code, email=email, name="Once")
    second = capture(db_session, code=code.code, email=email, name="Twice")
    assert second.id == first.id

    # Phone-only exact retries dedupe too.
    phone_first = capture(db_session, code=code.code, phone="0812 345 6789")
    phone_second = capture(db_session, code=code.code, phone="+2348123456789")
    assert phone_second.id == phone_first.id


def test_capture_replay_has_one_pii_free_audit_and_event(db_session):
    _program(db_session)
    referrer = _subscriber(db_session)
    code = ensure_code(db_session, referrer.id)
    email = _unique_email()

    first = capture(
        db_session,
        code=code.code,
        name="Private Prospect",
        email=email,
        phone="0812 345 6701",
    )
    second = capture(
        db_session,
        code=code.code,
        name="Retry Name",
        email=email,
        phone="+2348123456701",
    )

    assert second.id == first.id
    events = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "referral.captured")
        .all()
    )
    audits = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "referrals.captured")
        .all()
    )
    assert len(events) == len(audits) == 1
    serialized = f"{events[0].payload} {audits[0].metadata_}".lower()
    assert email.lower() not in serialized
    assert "private prospect" not in serialized
    # Match the whole number, never a prefix. "0812" is four hex characters and
    # the payload carries UUIDs, so a subscriber_id such as
    # 3162c802-8173-4688-992f-dd0812c6f7bf fails this guard on its own fixture
    # a few runs in a thousand. A PII guard that fails randomly gets re-run
    # past, and eventually someone re-runs past a real leak. The E.164 form
    # cannot collide at all: '+' never appears in a UUID.
    assert "0812 345 6701" not in serialized
    assert "+2348123456701" not in serialized
    assert "8123456701" not in serialized
    assert events[0].payload["schema_version"] == 1


def test_late_capture_event_failure_rolls_back_party_lead_and_referral(
    db_session, monkeypatch
):
    from app.models.party import Party
    from app.services import referrals as referral_program

    _program(db_session)
    referrer = _subscriber(db_session)
    code = ensure_code(db_session, referrer.id)
    before = {
        "parties": db_session.query(Party).count(),
        "leads": db_session.query(Lead).count(),
        "referrals": db_session.query(Referral).count(),
    }

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("referral event unavailable")

    monkeypatch.setattr(referral_program, "emit_event", fail_event)
    with pytest.raises(RuntimeError, match="referral event unavailable"):
        capture(db_session, code=code.code, email=_unique_email())

    assert not db_session.in_transaction()
    assert db_session.query(Party).count() == before["parties"]
    assert db_session.query(Lead).count() == before["leads"]
    assert db_session.query(Referral).count() == before["referrals"]
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "referrals.captured")
        .count()
        == 0
    )


def test_capture_retry_requires_the_exact_submitted_contact_set(db_session):
    _program(db_session)
    referrer = _subscriber(db_session)
    code = ensure_code(db_session, referrer.id)
    email = _unique_email()

    both = capture(
        db_session,
        code=code.code,
        email=email,
        phone="0812 345 6790",
    )
    email_only = capture(db_session, code=code.code, email=email)

    assert email_only.id != both.id
    exact_retry = capture(
        db_session,
        code=code.code,
        email=email.upper(),
        phone="+2348123456790",
    )
    assert exact_retry.id == both.id


def test_capture_validations(db_session):
    referrer = _subscriber(db_session)

    with pytest.raises(ReferralProgramError) as exc:
        capture(db_session, code="NOPE1234", email=_unique_email())
    assert exc.value.code == "referrals.program.program_disabled"

    _program(db_session)
    with pytest.raises(ReferralProgramError) as exc:
        capture(db_session, code="NOPE1234", email=_unique_email())
    assert exc.value.code == "referrals.program.code_not_found"

    code = ensure_code(db_session, referrer.id)
    with pytest.raises(ReferralProgramError) as exc:
        capture(db_session, code=code.code, name="No Contact")
    assert exc.value.code == "referrals.program.contact_required"


def test_capture_self_referral_409(db_session):
    _program(db_session)
    referrer = _subscriber(db_session)
    code = ensure_code(db_session, referrer.id)
    with pytest.raises(ReferralProgramError) as exc:
        capture(db_session, code=code.code, email=referrer.email)
    assert exc.value.code == "referrals.program.self_referral"


def test_capture_already_active_customer_409(db_session):
    _program(db_session)
    referrer = _subscriber(db_session)
    existing_customer = _subscriber(db_session, status=SubscriberStatus.active)
    code = ensure_code(db_session, referrer.id)
    with pytest.raises(ReferralProgramError) as exc:
        capture(db_session, code=code.code, email=existing_customer.email)
    assert exc.value.code == "referrals.program.existing_customer"


# ── qualification ────────────────────────────────────────────────────────────


def _captured_referral(db, referrer=None, **capture_kwargs):
    referrer = referrer or _subscriber(db)
    code = ensure_code(db, referrer.id)
    capture_kwargs.setdefault("email", _unique_email())
    referral = capture(db, code=code.code, **capture_kwargs)
    prospect = _subscriber(db, status=SubscriberStatus.new)
    party_service.bind_subscriber_account(
        db,
        subscriber_id=prospect.id,
        party_id=referral.referred_party_id,
        source="test_review",
        reason="Test fixture reviewed referral conversion",
    )
    referrals.attach_subscriber_for_conversion(
        db,
        referral_id=str(referral.id),
        subscriber_id=str(prospect.id),
        source="test_review",
        reason="Test fixture reviewed referral conversion",
    )
    db.commit()
    db.refresh(referral)
    return referral, referrer


def test_qualify_on_activation(db_session):
    _program(db_session, amount="2500")
    referral, _referrer = _captured_referral(db_session)
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db_session.commit()

    result = qualify_for_subscriber(db_session, prospect)
    assert result is not None and result.id == referral.id
    assert result.status == "qualified"
    assert result.reward_status == "pending"  # no auto-approve
    assert result.reward_amount == Decimal("2500")
    assert result.qualified_at is not None
    assert result.referred_subscriber_id == prospect.id

    # Idempotent: a second activation event does nothing (no longer pending).
    assert qualify_for_subscriber(db_session, prospect) is None


def test_qualify_auto_approve(db_session):
    _program(db_session, auto_approve=True)
    referral, _ = _captured_referral(db_session)
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db_session.commit()
    result = qualify_for_subscriber(db_session, prospect)
    assert result is not None
    assert result.reward_status == "approved"


def test_qualify_expires_outside_window(db_session):
    _program(db_session, window_days=30)
    referral, _ = _captured_referral(db_session)
    referral.created_at = datetime.now(UTC) - timedelta(days=45)
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db_session.commit()

    result = qualify_for_subscriber(db_session, prospect)
    assert result is not None
    assert result.status == "expired"
    assert result.reward_status == "none"


def test_qualify_attaches_only_a_reviewed_matching_party(db_session):
    _program(db_session)
    email = _unique_email()
    referrer = _subscriber(db_session)
    code = ensure_code(db_session, referrer.id)
    referral = capture(db_session, code=code.code, email=email)

    signup = _subscriber(db_session, status=SubscriberStatus.active, email=email)
    assert signup.party_id is None
    assert qualify_for_subscriber(db_session, signup) is None
    assert referral.referred_subscriber_id is None

    party_service.bind_subscriber_account(
        db_session,
        subscriber_id=signup.id,
        party_id=referral.referred_party_id,
        source="signup_identity_review",
        reason="Signup was reviewed as the referred Party",
    )

    result = qualify_for_subscriber(db_session, signup)
    assert result is not None and result.id == referral.id
    assert result.status == "qualified"
    assert result.referred_subscriber_id == signup.id
    assert result.subscriber_link_source == "test_referral_program"
    lead = db_session.get(Lead, referral.referred_lead_id)
    assert lead.subscriber_id == signup.id


def test_attach_subscriber_refuses_a_different_party(db_session):
    _program(db_session)
    referrer = _subscriber(db_session)
    code = ensure_code(db_session, referrer.id)
    referral = capture(db_session, code=code.code, email=_unique_email())
    wrong_party = party_service.create_party(
        db_session,
        party_type="person",
        display_name="Different person",
    )
    subscriber = _subscriber(db_session, status=SubscriberStatus.new)
    party_service.bind_subscriber_account(
        db_session,
        subscriber_id=subscriber.id,
        party_id=wrong_party.id,
        source="test_review",
        reason="Test fixture reviewed a different identity",
    )

    with pytest.raises(ReferralAttachmentError) as exc:
        referrals.attach_subscriber_for_conversion(
            db_session,
            referral_id=str(referral.id),
            subscriber_id=str(subscriber.id),
            source="test_review",
            reason="Attempted mismatched conversion",
        )
    assert exc.value.code == "account_conflict"
    assert referral.referred_subscriber_id is None


def test_qualify_noops(db_session):
    _program(db_session)
    referral, referrer = _captured_referral(db_session)
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)

    # Not active yet → no-op.
    assert qualify_for_subscriber(db_session, prospect) is None
    assert db_session.get(Referral, referral.id).status == "pending"

    # Active subscriber with no referral → no-op.
    unrelated = _subscriber(db_session, status=SubscriberStatus.active)
    assert qualify_for_subscriber(db_session, unrelated) is None

    # The referrer activating themselves never qualifies their own referral.
    assert qualify_for_subscriber(db_session, referrer) is None


def test_qualify_noop_when_program_disabled(db_session):
    _program(db_session)
    referral, _ = _captured_referral(db_session)
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db_session.query(DomainSetting).filter(
        DomainSetting.key == "referral_program_enabled"
    ).update({"value_text": "false"})
    db_session.commit()
    assert qualify_for_subscriber(db_session, prospect) is None


# ── reward payout (external_ref continuity) ──────────────────────────────────


def _qualified_referral(db, amount="2500"):
    _program(db, amount=amount)
    referral, referrer = _captured_referral(db)
    prospect = db.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db.commit()
    referral = qualify_for_subscriber(db, prospect)
    return referral, referrer


def test_issue_reward_credits_wallet_with_referral_external_ref(db_session):
    referral, referrer = _qualified_referral(db_session)

    result = issue_reward(db_session, referral.id)
    assert result.status == "rewarded"
    assert result.reward_status == "issued"
    assert result.reward_issued_at is not None

    credit = db_session.get(CreditNote, result.metadata_["reward_credit_id"])
    assert credit is not None
    assert credit.total == Decimal("2500")
    assert f"[ref:referral:{referral.id}]" in str(credit.memo)
    assert result.metadata_["reward_subscriber_id"] == str(referrer.id)


def test_issue_reward_is_idempotent(db_session):
    referral, referrer = _qualified_referral(db_session)
    issue_reward(db_session, referral.id)
    again = issue_reward(db_session, referral.id)  # retry
    assert again.status == "rewarded"

    credits = (
        db_session.query(CreditNote)
        .filter(CreditNote.account_id == referrer.id)
        .filter(CreditNote.memo.ilike(f"%[ref:referral:{referral.id}]%"))
        .all()
    )
    assert len(credits) == 1  # never double-credited


def test_issue_reward_external_ref_continuity_with_crm_payout(db_session):
    """A reward the CRM already paid pre-cutover (via POST /crm/credits with
    external_ref='referral:{id}') is returned by the native path, not re-paid —
    the SAME key flows through the SAME dedupe."""
    referral, referrer = _qualified_referral(db_session)
    external_ref = f"referral:{referral.id}"

    # Simulate the CRM's historical payout through the /crm/credits service.
    crm_entry = crm_api.create_account_credit(
        db_session,
        subscriber_id=str(referrer.id),
        amount=Decimal("2500"),
        reason="Referral reward",
        external_ref=external_ref,
    )

    result = issue_reward(db_session, referral.id)
    assert result.status == "rewarded"
    assert result.reward_status == "issued"
    assert result.metadata_["reward_credit_id"] == str(crm_entry.id)

    reconciliation_event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "referral.reward_reconciled")
        .one()
    )
    assert reconciliation_event.subscriber_id == referrer.id

    assert (
        db_session.query(CreditNote)
        .filter(CreditNote.account_id == referrer.id)
        .filter(CreditNote.memo.ilike(f"%[ref:{external_ref}]%"))
        .count()
        == 1
    )


def test_issue_reward_guards(db_session):
    _program(db_session)
    referral, _ = _captured_referral(db_session)

    with pytest.raises(ReferralProgramError) as exc:
        issue_reward(db_session, referral.id)
    assert exc.value.code == "referrals.program.invalid_transition"

    with pytest.raises(ReferralProgramError) as exc:
        issue_reward(db_session, uuid.uuid4())
    assert exc.value.code == "referrals.program.referral_not_found"

    # Qualified but zero reward: never mark rewarded with no credit behind it.
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db_session.commit()
    qualified = qualify_for_subscriber(db_session, prospect)
    qualified.reward_amount = Decimal("0")
    db_session.commit()
    with pytest.raises(ReferralProgramError) as exc:
        issue_reward(db_session, referral.id)
    assert exc.value.code == "referrals.program.invalid_reward"


def test_reject_sets_void_and_notes(db_session):
    _program(db_session)
    referral, _ = _captured_referral(db_session)
    result = reject(db_session, referral.id, "Fraudulent capture")
    assert result.status == "rejected"
    assert result.reward_status == "void"
    assert "Rejected: Fraudulent capture" in (result.notes or "")


# ── activation hook wiring ───────────────────────────────────────────────────


def test_referral_handler_registered_in_dispatcher():
    from app.services.events.dispatcher import get_dispatcher, reset_dispatcher

    reset_dispatcher()
    try:
        dispatcher = get_dispatcher()
        names = [h.__class__.__name__ for h in dispatcher._handlers]
        assert "ReferralHandler" in names
    finally:
        reset_dispatcher()


def test_reward_event_uses_template_policy_and_deduplicated_intent(db_session):
    from app.services.events.handlers.notification import NotificationHandler
    from app.services.events.types import Event, EventType

    subscriber = _subscriber(db_session)
    event = Event(
        event_type=EventType.referral_reward_issued,
        payload={
            "schema_version": 1,
            "referral_id": str(uuid.uuid4()),
            "amount": "NGN 2500.00",
        },
        subscriber_id=subscriber.id,
    )

    NotificationHandler().handle(db_session, event)
    db_session.commit()
    NotificationHandler().handle(db_session, event)
    db_session.commit()

    dedupe_key = f"event-notification:{event.event_id}:referral_reward_issued:push"
    intents = (
        db_session.query(CommunicationIntentRecord)
        .filter(CommunicationIntentRecord.dedupe_key == dedupe_key)
        .all()
    )
    notifications = (
        db_session.query(Notification)
        .filter(Notification.event_type == "referral_reward_issued")
        .all()
    )
    assert len(intents) == len(notifications) == 1
    assert "NGN 2500.00" in str(notifications[0].body)


def test_activation_event_qualifies_referral(db_session):
    from app.services.events.handlers.referral import ReferralHandler
    from app.services.events.types import Event, EventType

    _program(db_session)
    referral, _ = _captured_referral(db_session)
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db_session.commit()

    # subscription.activated carries the subscriber UUID as account_id
    # (activate_subscription's emit).
    event = Event(
        event_type=EventType.subscription_activated,
        payload={"subscription_id": str(uuid.uuid4())},
        account_id=prospect.id,
    )
    ReferralHandler().handle(db_session, event)
    db_session.commit()

    assert db_session.get(Referral, referral.id).status == "qualified"


def test_handler_ignores_unrelated_events_and_missing_subscriber(db_session):
    from app.services.events.handlers.referral import ReferralHandler
    from app.services.events.types import Event, EventType

    _program(db_session)
    referral, _ = _captured_referral(db_session)

    handler = ReferralHandler()
    handler.handle(
        db_session,
        Event(event_type=EventType.invoice_paid, payload={}, account_id=uuid.uuid4()),
    )
    handler.handle(
        db_session,
        Event(
            event_type=EventType.subscription_activated,
            payload={},
            account_id=uuid.uuid4(),  # unknown subscriber
        ),
    )
    handler.handle(
        db_session,
        Event(event_type=EventType.subscription_activated, payload={}),  # no subject
    )
    assert db_session.get(Referral, referral.id).status == "pending"


# ── read-shape compatibility ────────────────────────────────────────────


def test_read_for_subscriber_matches_mirror_shape(db_session):
    from app.schemas.portal import MyReferralsResponse

    referral, referrer = _qualified_referral(db_session)
    issue_reward(db_session, referral.id)

    payload = referrals.read_for_subscriber(db_session, str(referrer.id))

    # Exact key sets of referrals_mirror.read_for_subscriber.
    assert set(payload) == {"code", "share_url", "program", "totals", "referrals"}
    assert set(payload["program"]) == {"enabled", "reward_amount", "reward_currency"}
    assert set(payload["totals"]) == {
        "total",
        "pending",
        "qualified",
        "rewarded",
        "total_earned",
    }
    assert set(payload["referrals"][0]) == {
        "id",
        "status",
        "referred_name",
        "reward_amount",
        "reward_currency",
        "reward_status",
        "created_at",
        "qualified_at",
    }
    # And the payload validates against the portal response schema.
    parsed = MyReferralsResponse.model_validate(payload)
    assert parsed.code == payload["code"]
    assert payload["share_url"].endswith(f"/r/{payload['code']}")

    item = payload["referrals"][0]
    assert item["id"] == str(referral.id)  # id = referral UUID
    assert item["status"] == "rewarded"
    assert item["reward_status"] == "issued"  # native standardizes on issued
    totals = payload["totals"]
    assert (
        totals["total"],
        totals["pending"],
        totals["qualified"],
        totals["rewarded"],
    ) == (1, 0, 0, 1)
    # Numeric(12,2) round-trips with scale (the mirror serialized the same way).
    assert Decimal(totals["total_earned"]) == Decimal("2500")
    assert payload["program"]["enabled"] is True
    assert Decimal(payload["program"]["reward_amount"]) == Decimal("2500")


def test_share_url_uses_the_canonical_configured_base(db_session):
    configured_base = "https://referrals.example.test/customer"
    _program(db_session)
    db_session.add(
        DomainSetting(
            domain=SettingDomain.subscriber,
            key="referral_share_base_url",
            value_type=SettingValueType.string,
            value_text=configured_base,
            is_active=True,
        )
    )
    db_session.commit()
    referrer = _subscriber(db_session)
    ensure_code(db_session, referrer.id)

    payload = referrals.read_for_subscriber(db_session, str(referrer.id))

    assert payload["share_url"].startswith(f"{configured_base}/r/")


def test_refer_a_friend_portal_shape(db_session):
    from app.schemas.portal import ReferAFriendResponse

    _program(db_session)
    referrer = _subscriber(db_session)
    result = refer_friend(
        db_session,
        str(referrer.id),
        name="Friend",
        email=_unique_email(),
        note="from the app",
    )
    assert set(result) == {"id", "status", "message"}
    parsed = ReferAFriendResponse.model_validate(result)
    assert parsed.status == "pending"

    # The referrer now has an active code (minted on first use).
    code = (
        db_session.query(ReferralCode)
        .filter(ReferralCode.subscriber_id == referrer.id)
        .one()
    )
    assert code.is_active
