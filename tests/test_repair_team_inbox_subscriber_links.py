from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.team_inbox import InboxConversation
from scripts.one_off import repair_team_inbox_subscriber_links as repair


def _subscriber(db_session, *, email: str) -> Subscriber:
    row = Subscriber(
        first_name="Ada",
        last_name="Nwosu",
        email=email,
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _conversation(db_session, *, email: str) -> InboxConversation:
    row = InboxConversation(
        channel_type="email",
        contact_address=email,
        status="resolved",
        is_active=True,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_exact_unique_contact_plan_repairs_all_matching_history(db_session):
    subscriber = _subscriber(db_session, email="ada@example.com")
    first = _conversation(db_session, email=subscriber.email)
    second = _conversation(db_session, email=subscriber.email)
    db_session.commit()

    plan = repair.build_plan(db_session, limit=100)
    assert len(plan.items) == 1
    assert plan.items[0].subscriber_id == subscriber.id

    repaired = repair.apply_plan(
        db_session,
        plan=plan,
        expected_digest=plan.digest,
        actor_person_id=uuid4(),
        reason="Reviewed exact email relationship",
        approval_reference="TEST-APPROVAL-1",
    )

    assert set(repaired) == {first.id, second.id}
    assert db_session.get(InboxConversation, first.id).subscriber_id == subscriber.id
    assert db_session.get(InboxConversation, second.id).subscriber_id == subscriber.id


def test_ambiguous_contact_is_not_eligible_for_repair(db_session):
    _subscriber(db_session, email="shared@example.com")
    _subscriber(db_session, email="shared@example.com")
    conversation = _conversation(db_session, email="shared@example.com")
    db_session.commit()

    plan = repair.build_plan(db_session, limit=100)

    assert plan.items == ()
    assert plan.ambiguous == 1
    assert db_session.get(InboxConversation, conversation.id).subscriber_id is None


def test_apply_refuses_a_changed_preview_digest(db_session):
    subscriber = _subscriber(db_session, email="ada@example.com")
    conversation = _conversation(db_session, email=subscriber.email)
    db_session.commit()
    plan = repair.build_plan(db_session, limit=100)

    with pytest.raises(ValueError, match="digest changed"):
        repair.apply_plan(
            db_session,
            plan=plan,
            expected_digest="wrong",
            actor_person_id=uuid4(),
            reason="Reviewed exact email relationship",
            approval_reference="TEST-APPROVAL-2",
        )

    assert db_session.get(InboxConversation, conversation.id).subscriber_id is None
