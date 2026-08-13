from datetime import date
from uuid import uuid4

from app.models.subscriber import (
    Gender,
    Reseller,
    Subscriber,
    SubscriberCategory,
    UserType,
)
from app.models.subscriber_field_verification import SubscriberFieldVerification
from app.services.owner_commands import CommandContext
from app.services.subscriber_profile_cleanup import (
    ProfileCleanupOutcome,
    SubmitProfileCleanupCommand,
    submit_profile_cleanup,
)


def _house_reseller(db_session) -> Reseller:
    row = Reseller(name=f"House {uuid4()}", is_house=True)
    db_session.add(row)
    db_session.flush()
    return row


def _subscriber(db_session, reseller: Reseller) -> Subscriber:
    row = Subscriber(
        email=f"customer-{uuid4()}@example.test",
        full_name="Residential Customer",
        user_type=UserType.customer,
        reseller_id=reseller.id,
        gender=Gender.unknown,
    )
    row.category = SubscriberCategory.residential
    db_session.add(row)
    db_session.flush()
    return row


def _context() -> CommandContext:
    return CommandContext.system(
        actor="system:ai-intake",
        scope="customer:profile-cleanup",
        reason="test AI collected NCC profile fields",
    )


def test_profile_cleanup_is_disabled_by_default(db_session):
    subscriber = _subscriber(db_session, _house_reseller(db_session))

    result = submit_profile_cleanup(
        db_session,
        SubmitProfileCleanupCommand(
            context=_context(),
            subscriber_id=subscriber.id,
            source_conversation_id=uuid4(),
            candidate_gender="female",
            activation_enabled=False,
        ),
    )

    assert result.outcome is ProfileCleanupOutcome.no_change
    assert db_session.get(Subscriber, subscriber.id).gender is Gender.unknown


def test_profile_cleanup_saves_missing_fields_and_audit_evidence(db_session):
    subscriber = _subscriber(db_session, _house_reseller(db_session))

    result = submit_profile_cleanup(
        db_session,
        SubmitProfileCleanupCommand(
            context=_context(),
            subscriber_id=subscriber.id,
            source_conversation_id=uuid4(),
            candidate_gender="male",
            candidate_date_of_birth=date(1990, 1, 2),
            consent_text="Customer provided details in DM",
            activation_enabled=True,
        ),
    )

    updated = db_session.get(Subscriber, subscriber.id)
    verifications = db_session.query(SubscriberFieldVerification).all()
    assert result.outcome is ProfileCleanupOutcome.saved
    assert set(result.saved_fields) == {"gender", "date_of_birth"}
    assert updated.gender is Gender.male
    assert updated.date_of_birth == date(1990, 1, 2)
    assert {row.field_key for row in verifications} == {"gender", "date_of_birth"}
    assert all(row.source == "ai_intake" for row in verifications)


def test_profile_cleanup_excludes_non_house_reseller_customer(db_session):
    reseller = Reseller(name=f"Partner {uuid4()}", is_house=False)
    db_session.add(reseller)
    db_session.flush()
    subscriber = _subscriber(db_session, reseller)

    result = submit_profile_cleanup(
        db_session,
        SubmitProfileCleanupCommand(
            context=_context(),
            subscriber_id=subscriber.id,
            source_conversation_id=uuid4(),
            candidate_gender="female",
            activation_enabled=True,
        ),
    )

    assert result.outcome is ProfileCleanupOutcome.ineligible
