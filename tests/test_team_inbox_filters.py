from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
    InboxTeamRole,
    InboxTeamSource,
)
from app.services import team_inbox_filters, team_inbox_read

_FILTER_TEAM_ID = UUID("33333333-3333-3333-3333-333333333333")


def _team(db_session, name: str, *, is_active: bool = True) -> ServiceTeam:
    team = ServiceTeam(
        name=name,
        team_type=ServiceTeamType.support.value,
        is_active=is_active,
    )
    db_session.add(team)
    db_session.flush()
    return team


def _conversation(db_session, subject: str) -> InboxConversation:
    conversation = InboxConversation(
        channel_type="email",
        status=InboxConversationStatus.open.value,
        subject=subject,
        contact_address=f"{subject.lower().replace(' ', '-')}@example.test",
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def _channel_conversation(
    db_session,
    subject: str,
    *,
    channel_type: str,
    status: str = InboxConversationStatus.open.value,
    team: ServiceTeam | None = None,
    last_inbound_at=None,
    qualifying: bool | None = None,
) -> InboxConversation:
    conversation = InboxConversation(
        channel_type=channel_type,
        status=status,
        subject=subject,
        contact_address=f"{subject.lower().replace(' ', '-')}@example.test",
        primary_service_team_id=team.id if team else None,
    )
    db_session.add(conversation)
    db_session.flush()
    if team is not None:
        _link(db_session, conversation, team)
    if last_inbound_at is not None:
        metadata = {}
        if qualifying is not None:
            metadata["reply_window_qualifying"] = qualifying
        db_session.add(
            InboxMessage(
                conversation_id=conversation.id,
                channel_type=channel_type,
                direction=InboxMessageDirection.inbound.value,
                body="Customer message",
                received_at=last_inbound_at,
                metadata_=metadata,
            )
        )
        db_session.flush()
    return conversation


def _link(
    db_session,
    conversation: InboxConversation,
    team: ServiceTeam,
    *,
    is_active: bool = True,
) -> None:
    db_session.add(
        InboxConversationTeam(
            conversation_id=conversation.id,
            service_team_id=team.id,
            role=InboxTeamRole.owner.value,
            source=InboxTeamSource.routing_rule.value,
            is_active=is_active,
        )
    )
    db_session.flush()


def _payload(
    operator: str, value: object
) -> team_inbox_filters.InboxAdvancedFilterPayload:
    return team_inbox_filters.InboxAdvancedFilterPayload(
        raw_json=json.dumps([["InboxConversation", "service_team_id", operator, value]])
    )


@pytest.mark.parametrize(
    ("raw_json", "message"),
    (
        ("not json", "Invalid JSON"),
        (
            json.dumps([["Ticket", "service_team_id", "=", str(_FILTER_TEAM_ID)]]),
            "not allowed",
        ),
        (
            json.dumps([["InboxConversation", "subject", "=", "x"]]),
            "not available",
        ),
        (
            json.dumps(
                [
                    [
                        "InboxConversation",
                        "service_team_id",
                        "like",
                        str(_FILTER_TEAM_ID),
                    ]
                ]
            ),
            "not allowed",
        ),
        (
            json.dumps([["InboxConversation", "service_team_id", "=", "not-a-uuid"]]),
            "valid team identifiers",
        ),
    ),
)
def test_invalid_advanced_team_filters_fail_closed(raw_json, message):
    with pytest.raises(team_inbox_filters.InboxFilterError) as exc_info:
        team_inbox_filters.parse_filter_payload(
            team_inbox_filters.InboxAdvancedFilterPayload(raw_json=raw_json)
        )

    assert exc_info.value.code == (
        "communications.team_inbox_projection.invalid_filter"
    )
    assert message in exc_info.value.message


def test_unknown_or_inactive_team_ids_are_rejected(db_session):
    inactive = _team(db_session, "Retired team", is_active=False)

    for team_id in (inactive.id, uuid4()):
        with pytest.raises(team_inbox_filters.InboxFilterError) as exc_info:
            team_inbox_filters.resolve_filter_query(
                db_session,
                _payload("=", str(team_id)),
            )
        assert "active Service Team" in exc_info.value.message


def test_service_team_operators_use_active_relationship_semantics(db_session):
    support = _team(db_session, "Support")
    billing = _team(db_session, "Billing")
    retired = _team(db_session, "Retired", is_active=False)

    support_only = _conversation(db_session, "Support only")
    _link(db_session, support_only, support)
    billing_only = _conversation(db_session, "Billing only")
    _link(db_session, billing_only, billing)
    both = _conversation(db_session, "Both teams")
    _link(db_session, both, support)
    _link(db_session, both, billing)
    unassigned = _conversation(db_session, "No team")
    inactive_only = _conversation(db_session, "Inactive team")
    _link(db_session, inactive_only, retired, is_active=False)
    db_session.commit()

    def matching_subjects(operator: str, value: object) -> set[str]:
        query, _options = team_inbox_filters.resolve_filter_query(
            db_session,
            _payload(operator, value),
        )
        result = team_inbox_read.list_conversations(
            db_session,
            advanced_filters=query,
            limit=50,
        )
        return {row.subject for row in result.items}

    assert matching_subjects("=", str(billing.id)) == {
        billing_only.subject,
        both.subject,
    }
    assert matching_subjects("!=", str(billing.id)) == {
        support_only.subject,
        unassigned.subject,
        inactive_only.subject,
    }
    assert matching_subjects("in", [str(support.id), str(billing.id)]) == {
        support_only.subject,
        billing_only.subject,
        both.subject,
    }
    assert matching_subjects("not in", [str(support.id), str(billing.id)]) == {
        unassigned.subject,
        inactive_only.subject,
    }
    assert matching_subjects("is", None) == {
        unassigned.subject,
        inactive_only.subject,
    }
    assert matching_subjects("is not", None) == {
        support_only.subject,
        billing_only.subject,
        both.subject,
    }


def test_filter_query_preserves_and_or_groups_in_canonical_json():
    support_id = UUID("11111111-1111-1111-1111-111111111111")
    billing_id = UUID("22222222-2222-2222-2222-222222222222")
    raw_json = json.dumps(
        {
            "and": [["InboxConversation", "service_team_id", "!=", str(support_id)]],
            "or": [
                ["InboxConversation", "service_team_id", "=", str(billing_id)],
                ["InboxConversation", "service_team_id", "is", None],
            ],
        }
    )

    query = team_inbox_filters.parse_filter_payload(
        team_inbox_filters.InboxAdvancedFilterPayload(raw_json=raw_json)
    )

    assert query.canonical_json() == json.dumps(
        [
            ["InboxConversation", "service_team_id", "!=", str(support_id)],
            {
                "or": [
                    ["InboxConversation", "service_team_id", "=", str(billing_id)],
                    ["InboxConversation", "service_team_id", "is", None],
                ]
            },
        ],
        separators=(",", ":"),
        sort_keys=True,
    )


def test_expired_reply_window_filter_is_calculated_for_meta_channels(db_session):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    expired_at = now - timedelta(hours=25)
    open_at = now - timedelta(hours=2)
    support = _team(db_session, "Support")
    expired_whatsapp = _channel_conversation(
        db_session,
        "Expired WhatsApp",
        channel_type=InboxChannelType.whatsapp.value,
        team=support,
        last_inbound_at=expired_at,
    )
    expired_messenger = _channel_conversation(
        db_session,
        "Expired Messenger",
        channel_type=InboxChannelType.facebook_messenger.value,
        team=support,
        last_inbound_at=expired_at,
    )
    expired_instagram = _channel_conversation(
        db_session,
        "Expired Instagram",
        channel_type=InboxChannelType.instagram_dm.value,
        team=support,
        last_inbound_at=expired_at,
    )
    _channel_conversation(
        db_session,
        "Open WhatsApp",
        channel_type=InboxChannelType.whatsapp.value,
        team=support,
        last_inbound_at=open_at,
    )
    _channel_conversation(
        db_session,
        "Unavailable WhatsApp",
        channel_type=InboxChannelType.whatsapp.value,
        team=support,
    )
    _channel_conversation(
        db_session,
        "Email",
        channel_type=InboxChannelType.email.value,
        team=support,
        last_inbound_at=expired_at,
    )
    _channel_conversation(
        db_session,
        "Facebook Comment",
        channel_type=InboxChannelType.facebook_comment.value,
        team=support,
        last_inbound_at=expired_at,
    )
    db_session.commit()

    result = team_inbox_read.list_conversations(
        db_session,
        reply_window_status="expired",
        service_team_ids=(str(support.id),),
        limit=10,
    )

    subjects = {row.subject for row in result.items}
    assert subjects == {
        expired_whatsapp.subject,
        expired_messenger.subject,
        expired_instagram.subject,
    }
    assert result.count == 3
    assert {row.reply_window_status for row in result.items} == {"expired"}
    assert {row.status for row in result.items} == {InboxConversationStatus.open.value}


def test_expired_reply_window_filter_defaults_to_unresolved_workflow_statuses(
    db_session,
):
    from datetime import UTC, datetime, timedelta

    support = _team(db_session, "Support")
    expired_at = datetime.now(UTC) - timedelta(hours=25)
    open_at = datetime.now(UTC) - timedelta(hours=2)
    expired_open = _channel_conversation(
        db_session,
        "Expired Open",
        channel_type=InboxChannelType.whatsapp.value,
        status=InboxConversationStatus.open.value,
        team=support,
        last_inbound_at=expired_at,
    )
    expired_pending = _channel_conversation(
        db_session,
        "Expired Pending",
        channel_type=InboxChannelType.whatsapp.value,
        status=InboxConversationStatus.pending.value,
        team=support,
        last_inbound_at=expired_at,
    )
    expired_snoozed = _channel_conversation(
        db_session,
        "Expired Snoozed",
        channel_type=InboxChannelType.whatsapp.value,
        status=InboxConversationStatus.snoozed.value,
        team=support,
        last_inbound_at=expired_at,
    )
    expired_resolved = _channel_conversation(
        db_session,
        "Expired Resolved",
        channel_type=InboxChannelType.whatsapp.value,
        status=InboxConversationStatus.resolved.value,
        team=support,
        last_inbound_at=expired_at,
    )
    _channel_conversation(
        db_session,
        "Open Window Pending",
        channel_type=InboxChannelType.whatsapp.value,
        status=InboxConversationStatus.pending.value,
        team=support,
        last_inbound_at=open_at,
    )
    _channel_conversation(
        db_session,
        "Unavailable Snoozed",
        channel_type=InboxChannelType.whatsapp.value,
        status=InboxConversationStatus.snoozed.value,
        team=support,
    )
    db_session.commit()

    default_result = team_inbox_read.list_conversations(
        db_session,
        reply_window_status="expired",
        service_team_ids=(str(support.id),),
        limit=10,
    )
    resolved_result = team_inbox_read.list_conversations(
        db_session,
        status=InboxConversationStatus.resolved.value,
        reply_window_status="expired",
        service_team_ids=(str(support.id),),
        limit=10,
    )

    assert {row.subject for row in default_result.items} == {
        expired_open.subject,
        expired_pending.subject,
        expired_snoozed.subject,
    }
    assert default_result.count == 3
    assert {row.status for row in default_result.items} == {
        InboxConversationStatus.open.value,
        InboxConversationStatus.pending.value,
        InboxConversationStatus.snoozed.value,
    }
    assert {row.subject for row in resolved_result.items} == {expired_resolved.subject}


def test_expired_reply_window_filter_excludes_nonqualifying_inbound_rows(db_session):
    from datetime import UTC, datetime, timedelta

    support = _team(db_session, "Support")
    _channel_conversation(
        db_session,
        "Non qualifying receipt",
        channel_type=InboxChannelType.whatsapp.value,
        team=support,
        last_inbound_at=datetime.now(UTC) - timedelta(hours=25),
        qualifying=False,
    )
    db_session.commit()

    result = team_inbox_read.list_conversations(
        db_session,
        reply_window_status="expired",
        service_team_ids=(str(support.id),),
        limit=10,
    )

    assert result.items == []
    assert result.count == 0


def test_expired_reply_window_filter_counts_after_pagination_and_team_scope(
    db_session,
):
    from datetime import UTC, datetime, timedelta

    support = _team(db_session, "Support")
    billing = _team(db_session, "Billing")
    expired_at = datetime.now(UTC) - timedelta(hours=25)
    for index in range(3):
        _channel_conversation(
            db_session,
            f"Support expired {index}",
            channel_type=InboxChannelType.whatsapp.value,
            team=support,
            last_inbound_at=expired_at,
        )
    _channel_conversation(
        db_session,
        "Billing expired",
        channel_type=InboxChannelType.whatsapp.value,
        team=billing,
        last_inbound_at=expired_at,
    )
    db_session.commit()

    result = team_inbox_read.list_conversations(
        db_session,
        reply_window_status="expired",
        service_team_ids=(str(support.id),),
        limit=2,
        offset=0,
    )

    assert len(result.items) == 2
    assert result.count == 3
    assert all(row.primary_service_team_id == str(support.id) for row in result.items)
