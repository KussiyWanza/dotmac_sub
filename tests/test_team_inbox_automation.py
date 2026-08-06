from uuid import uuid4

from app.models.service_team import ServiceTeam
from app.models.team_inbox import (
    InboxAutomationActionType,
    InboxAutomationRule,
    InboxAutomationTrigger,
    InboxConversation,
    InboxConversationLabel,
    InboxConversationQueueEntry,
)
from app.services.team_inbox_automation import evaluate_rules, execute_matching_rules


def _conversation(db_session, *, channel: str = "chat_widget") -> InboxConversation:
    conversation = InboxConversation(channel_type=channel, status="open", priority=25)
    db_session.add(conversation)
    db_session.flush()
    return conversation


def test_trigger_matching_uses_supported_conversation_conditions(db_session):
    conversation = _conversation(db_session)
    matching = InboxAutomationRule(
        name="Widget high priority",
        trigger=InboxAutomationTrigger.inbound_message_received,
        conditions={"channel_type": "chat_widget", "priority": 25},
        action_type=InboxAutomationActionType.add_tag,
        action_value={"tag": "Widget"},
    )
    wrong_channel = InboxAutomationRule(
        name="Email only",
        trigger=InboxAutomationTrigger.inbound_message_received,
        conditions={"channel_type": "email"},
        action_type=InboxAutomationActionType.add_tag,
        action_value={"tag": "Email"},
    )
    db_session.add_all([matching, wrong_channel])
    db_session.flush()

    proposals = evaluate_rules(
        db_session,
        conversation=conversation,
        trigger=InboxAutomationTrigger.inbound_message_received,
    )
    assert [proposal.rule_id for proposal in proposals] == [matching.id]


def test_actions_tag_and_auto_assign_to_fifo_queue(db_session):
    team = ServiceTeam(name=f"Automation {uuid4()}", team_type="support")
    db_session.add(team)
    db_session.flush()
    conversation = _conversation(db_session)
    conversation.primary_service_team_id = team.id
    tag_rule = InboxAutomationRule(
        name="Tag widget",
        trigger=InboxAutomationTrigger.conversation_created,
        conditions={"channel_type": "chat_widget"},
        action_type=InboxAutomationActionType.add_tag,
        action_value={"tag": "Live chat"},
        sort_order=10,
    )
    assign_rule = InboxAutomationRule(
        name="Route widget",
        trigger=InboxAutomationTrigger.conversation_created,
        conditions={"channel_type": "chat_widget"},
        action_type=InboxAutomationActionType.auto_assign,
        action_value={"service_team_id": str(team.id)},
        sort_order=20,
    )
    db_session.add_all([tag_rule, assign_rule])
    db_session.flush()

    result = execute_matching_rules(
        db_session,
        conversation=conversation,
        trigger=InboxAutomationTrigger.conversation_created,
    )

    assert result.executed_rule_ids == (tag_rule.id, assign_rule.id)
    assert db_session.query(InboxConversationLabel).count() == 1
    queued = db_session.query(InboxConversationQueueEntry).one()
    assert queued.conversation_id == conversation.id
    assert queued.service_team_id == team.id
