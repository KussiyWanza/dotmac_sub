from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
    InboxConversationTeam,
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
