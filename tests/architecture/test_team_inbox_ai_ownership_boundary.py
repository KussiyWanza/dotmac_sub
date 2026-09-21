"""Pin authoritative AI ownership and explicit human takeover boundaries."""

from __future__ import annotations

from pathlib import Path

from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_ai_ownership_resolver_uses_active_session_not_conversation_metadata() -> None:
    source = _source("app/services/ai_conversation_ownership.py")
    resolver = source[
        source.index("def resolve_ai_conversation_ownership(") : source.index(
            "\ndef ownership_by_conversation_ids("
        )
    ]

    assert "AiIntakeSession.completed_at.is_(None)" in source
    assert "metadata_" not in resolver
    assert "AiConversationOwnership" in resolver


def test_human_commands_guard_and_takeover_is_an_explicit_typed_command() -> None:
    source = _source("app/services/team_inbox_commands.py")

    assert "class TakeOverConversationCommand" in source
    assert "def take_over_conversation(" in source
    assert "expected_ai_session_id" in source
    assert "expected_ai_session_state" in source
    assert "stopped_human_takeover" in source
    assert "HumanConversationMutation.reply" in source
    assert "HumanConversationMutation.private_note" in source
    assert "HumanConversationMutation.assignment" in source
    assert "HumanConversationMutation.status" in source
    assert "HumanConversationMutation.ticket" not in source
    assert "HumanConversationMutation.macro" in source
    assert "HumanConversationMutation.bulk" in source


def test_implicit_fail_open_takeover_does_not_return() -> None:
    assignment = _source("app/services/team_inbox_assignment.py")
    outbound = _source("app/services/team_inbox_outbound.py")

    assert "InboxAssignmentProvenance.ai_intake_handoff" in assignment
    assert "InboxAssignmentProvenance.explicit_human_takeover" in assignment
    assert "complete_session(" not in assignment
    assert "stopped_human_takeover" not in outbound
    assert (
        "except Exception"
        not in outbound[
            outbound.index("def send_inbox_reply(") : outbound.index(
                "\ndef send_ai_intake_follow_up("
            )
        ]
    )


def test_delivery_revalidates_ai_session_before_provider_contact() -> None:
    worker = _source("app/tasks/notifications.py")
    owner = _source("app/services/team_inbox_commands.py")
    suppression = owner[owner.index("def suppress_ai_outbound_without_ownership(") :]
    delivery = worker[
        worker.index("def _deliver_notification_queue_stats(") : worker.index(
            "\ndef _deliver_notification_queue("
        )
    ]

    assert "decide_ai_outbound_delivery" in suppression
    assert "suppress_notification_delivery" in suppression
    assert delivery.index(
        "team_inbox_commands.suppress_ai_outbound_without_ownership"
    ) < delivery.index("if notification.channel == NotificationChannel.email:")


def test_registry_names_ai_session_authority_for_commands_and_projection() -> None:
    ai = service_relationship("ai.intake")
    commands = service_relationship("communications.team_inbox_commands")
    projection = service_relationship("communications.team_inbox_projection")

    assert "active AI conversation ownership resolution" in ai.owns
    assert commands.contract is not None
    assert projection.contract is not None
    assert any(
        item.name == "active AI conversation ownership" and item.owner == "ai.intake"
        for item in commands.contract.authoritative_inputs
    )
    assert any(
        item.name == "active AI conversation ownership" and item.owner == "ai.intake"
        for item in projection.contract.authoritative_inputs
    )
