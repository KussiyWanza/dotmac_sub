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


def test_exact_unique_contact_plan_repairs_each_matching_conversation(db_session):
    subscriber = _subscriber(db_session, email="ada@example.com")
    first = _conversation(db_session, email=subscriber.email)
    second = _conversation(db_session, email=subscriber.email)
    db_session.commit()

    plan = repair.build_plan(db_session, limit=100)
    assert len(plan.items) == 2
    assert {item.subscriber_id for item in plan.items} == {subscriber.id}

    result = repair.apply_plan(
        db_session,
        plan=plan,
        expected_digest=plan.digest,
        actor_person_id=uuid4(),
        reason="Reviewed exact email relationship",
        approval_reference="TEST-APPROVAL-1",
    )

    assert result["linked"] == 2
    assert set(result["repaired_conversation_ids"]) == {str(first.id), str(second.id)}
    assert db_session.get(InboxConversation, first.id).subscriber_id == subscriber.id
    assert db_session.get(InboxConversation, second.id).subscriber_id == subscriber.id

    replay_plan = repair.build_plan(db_session, limit=100)
    assert replay_plan.items == ()


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


def test_apply_never_overwrites_a_customer_link_added_after_preview(db_session):
    planned = _subscriber(db_session, email="planned@example.com")
    existing = _subscriber(db_session, email="existing@example.com")
    conversation = _conversation(db_session, email=planned.email)
    db_session.commit()
    plan = repair.build_plan(db_session, limit=100)

    conversation.subscriber_id = existing.id
    db_session.commit()
    result = repair.apply_plan(
        db_session,
        plan=plan,
        expected_digest=plan.digest,
        actor_person_id=uuid4(),
        reason="Race protection",
        approval_reference="TEST-APPROVAL-3",
    )

    assert result["conflicts"] == 1
    assert (
        db_session.get(InboxConversation, conversation.id).subscriber_id == existing.id
    )
