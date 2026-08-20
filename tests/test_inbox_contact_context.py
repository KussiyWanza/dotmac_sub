from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from app.db import finish_read_transaction
from app.models.party import Party, PartyContactPoint, PartyType
from app.models.sales import Lead, Pipeline
from app.models.subscriber import Reseller
from app.models.team_inbox import (
    InboxContactLink,
    InboxConversation,
    InboxConversationParticipant,
    InboxParticipantAdmissionSource,
)
from app.services import (
    conversation_lead_relationships,
    inbox_lead_actions,
    team_inbox_contact_context,
)
from app.services.owner_commands import CommandContext

PERMISSIONS = inbox_lead_actions.InboxActionPermissions(
    can_read_profile=True,
    can_edit_profile=True,
    can_read_leads=True,
    can_write_leads=True,
)


def _conversation(
    db_session,
    *,
    address: str,
    channel_type: str = "email",
    subscriber_id: UUID | None = None,
    subject: str = "Service enquiry",
    last_message_at: datetime | None = None,
) -> InboxConversation:
    row = InboxConversation(
        subscriber_id=subscriber_id,
        channel_type=channel_type,
        contact_address=address,
        subject=subject,
        last_message_at=last_message_at,
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _bind_party(db_session, conversation: InboxConversation) -> Party:
    party = Party(party_type=PartyType.person.value, display_name="Exact Prospect")
    db_session.add(party)
    db_session.flush()
    point = PartyContactPoint(
        party_id=party.id,
        channel_type=conversation.channel_type,
        normalized_value=conversation.contact_address,
        display_value=conversation.contact_address,
        is_primary=True,
    )
    db_session.add(point)
    db_session.flush()
    db_session.add(
        InboxConversationParticipant(
            conversation_id=conversation.id,
            channel_type=conversation.channel_type,
            normalized_endpoint=conversation.contact_address,
            party_contact_point_id=point.id,
            party_contact_point_bound_at=datetime.now(UTC),
            party_contact_point_binding_source="pytest",
            party_contact_point_binding_reason="Reviewed exact Party fixture",
            admission_source=InboxParticipantAdmissionSource.inbound_from.value,
        )
    )
    db_session.commit()
    return party


def _bind_existing_party(
    db_session,
    conversation: InboxConversation,
    party: Party,
) -> PartyContactPoint:
    point = PartyContactPoint(
        party_id=party.id,
        channel_type=conversation.channel_type,
        normalized_value=conversation.contact_address,
        display_value=conversation.contact_address,
        is_primary=False,
    )
    db_session.add(point)
    db_session.flush()
    db_session.add(
        InboxConversationParticipant(
            conversation_id=conversation.id,
            channel_type=conversation.channel_type,
            normalized_endpoint=conversation.contact_address,
            party_contact_point_id=point.id,
            party_contact_point_bound_at=datetime.now(UTC),
            party_contact_point_binding_source="pytest",
            party_contact_point_binding_reason="Reviewed shared Party fixture",
            admission_source=InboxParticipantAdmissionSource.inbound_from.value,
        )
    )
    db_session.commit()
    return point


def test_drawer_source_contains_no_customer_placeholder_values():
    drawer = Path("templates/admin/inbox/_contact_drawer.html").read_text()
    context = Path("templates/admin/inbox/_authoritative_context.html").read_text()
    combined = drawer + context
    for fabricated in (
        "Dummy data",
        "Profile completeness",
        "Retention risk",
        "INC-1042",
        "Plan upgrade enquiry",
        "Invoice clarification",
        "Projects · 2",
        "Tasks · 3",
    ):
        assert fabricated not in combined
    assert "availability.value == 'empty'" in context
    assert ">0</strong>" in context
    assert "Not calculated" in context
    assert "Unavailable" in context
    assert "Restricted" in context


def test_unmatched_conversation_resolves_new_prospect_without_creating(db_session):
    conversation = _conversation(db_session, address=f"new-{uuid4()}@example.com")

    action = inbox_lead_actions.resolve_action(
        db_session,
        conversation_id=conversation.id,
        intent=inbox_lead_actions.InboxActionIntent.lead,
        permissions=PERMISSIONS,
    )

    assert (
        action.action_type
        is inbox_lead_actions.InboxResolvedActionType.create_party_and_lead
    )
    assert action.destination == (
        f"/admin/sales/leads/new?inbox_conversation_id={conversation.id}"
    )
    assert (
        conversation_lead_relationships.active_link(db_session, conversation.id) is None
    )


def test_exact_party_lead_is_reused_and_durably_linked(db_session):
    conversation = _conversation(db_session, address=f"exact-{uuid4()}@example.com")
    party = _bind_party(db_session, conversation)
    pipeline = Pipeline(name=f"Inbox Pipeline {uuid4()}", is_active=True)
    db_session.add(pipeline)
    db_session.flush()
    lead = Lead(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Exact Party Lead fixture",
        pipeline_id=pipeline.id,
        title="Existing exact Party Lead",
        status="new",
        is_active=True,
    )
    db_session.add(lead)
    db_session.commit()

    unresolved = inbox_lead_actions.resolve_action(
        db_session,
        conversation_id=conversation.id,
        intent=inbox_lead_actions.InboxActionIntent.lead,
        permissions=PERMISSIONS,
    )
    assert (
        unresolved.action_type
        is inbox_lead_actions.InboxResolvedActionType.select_pipeline
    )

    resolved = inbox_lead_actions.resolve_action(
        db_session,
        conversation_id=conversation.id,
        intent=inbox_lead_actions.InboxActionIntent.lead,
        permissions=PERMISSIONS,
        selected_pipeline_id=pipeline.id,
    )
    assert resolved.lead_id == lead.id
    assert resolved.requires_link is True

    conversation_id = conversation.id
    party_id = party.id
    lead_id = lead.id
    finish_read_transaction(db_session)
    outcome = inbox_lead_actions.link_existing_lead(
        db_session,
        inbox_lead_actions.LinkExistingLeadCommand(
            context=CommandContext.system(
                actor="pytest",
                scope="inbox:lead-link",
                reason="Focused exact Party Lead reuse test",
                idempotency_key=f"pytest:{conversation_id}:{lead_id}",
            ),
            conversation_id=conversation_id,
            party_id=party_id,
            lead_id=lead_id,
            actor_person_id=None,
            source=conversation_lead_relationships.ConversationLeadLinkSource.exact_party_lead,
        ),
    )

    assert outcome.created is False
    link = conversation_lead_relationships.active_link(db_session, conversation_id)
    assert link is not None
    assert link.lead_id == lead_id
    assert link.party_id == party_id


def test_contact_context_uses_zero_only_for_successful_empty_query(db_session):
    conversation = _conversation(db_session, address=f"party-{uuid4()}@example.com")
    _bind_party(db_session, conversation)

    projection = team_inbox_contact_context.build_contact_context(
        db_session,
        conversation_id=conversation.id,
        permissions=team_inbox_contact_context.InboxContactContextPermissions(
            can_read_profile=True,
            can_edit_profile=False,
            can_read_leads=True,
            can_write_leads=False,
            can_read_tickets=True,
            can_read_projects=True,
            can_read_project_tasks=True,
        ),
    )

    assert projection is not None
    assert (
        projection.leads.availability
        is team_inbox_contact_context.ContextAvailability.empty
    )
    assert projection.leads.total_count == 0
    assert (
        projection.projects.availability
        is team_inbox_contact_context.ContextAvailability.not_applicable
    )
    assert projection.projects.total_count is None


def test_contact_context_projects_cross_agent_customer_conversation_history(
    db_session, subscriber
):
    observed_at = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    current = _conversation(
        db_session,
        address=subscriber.email,
        subscriber_id=subscriber.id,
        subject="Current relocation request",
        last_message_at=observed_at,
    )
    previous = tuple(
        _conversation(
            db_session,
            address=f"personal-{index}-{uuid4()}@example.com",
            subscriber_id=subscriber.id,
            subject=f"Previous conversation {index}",
            last_message_at=observed_at - timedelta(days=index),
        )
        for index in range(1, 7)
    )
    _conversation(
        db_session,
        address=f"other-{uuid4()}@example.com",
        subject="Another customer's conversation",
        last_message_at=observed_at + timedelta(hours=1),
    )

    projection = team_inbox_contact_context.build_contact_context(
        db_session,
        conversation_id=current.id,
        permissions=team_inbox_contact_context.InboxContactContextPermissions(
            can_read_profile=True,
            can_edit_profile=False,
            can_read_leads=True,
            can_write_leads=False,
            can_read_tickets=True,
            can_read_projects=True,
            can_read_project_tasks=True,
        ),
    )

    assert projection is not None
    history = projection.recent_conversations
    assert (
        history.availability is team_inbox_contact_context.ContextAvailability.available
    )
    assert history.total_count == 6
    assert tuple(item.id for item in history.items) == tuple(
        row.id for row in previous[:5]
    )
    assert all(item.id != current.id for item in history.items)
    assert tuple(item.url for item in history.items) == tuple(
        f"/admin/inbox?c={row.id}" for row in previous[:5]
    )


def test_contact_context_projects_non_subscriber_history_by_exact_endpoint(db_session):
    observed_at = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    address = f"prospect-{uuid4()}@example.com"
    current = _conversation(
        db_session,
        address=address,
        subject="Current prospect enquiry",
        last_message_at=observed_at,
    )
    previous = _conversation(
        db_session,
        address=address,
        subject="Earlier prospect enquiry",
        last_message_at=observed_at - timedelta(days=2),
    )
    previous.status = "resolved"
    _conversation(
        db_session,
        address=f"different-{uuid4()}@example.com",
        subject="Different prospect",
        last_message_at=observed_at - timedelta(days=1),
    )
    db_session.commit()

    projection = team_inbox_contact_context.build_contact_context(
        db_session,
        conversation_id=current.id,
        permissions=team_inbox_contact_context.InboxContactContextPermissions(
            can_read_profile=True,
            can_edit_profile=False,
            can_read_leads=True,
            can_write_leads=False,
            can_read_tickets=True,
            can_read_projects=True,
            can_read_project_tasks=True,
        ),
    )

    assert projection is not None
    assert (
        projection.conversation_history_scope.kind
        is team_inbox_contact_context.ConversationHistoryMatchKind.exact_endpoint
    )
    assert projection.recent_conversations.total_count == 1
    assert tuple(item.id for item in projection.recent_conversations.items) == (
        previous.id,
    )


def test_contact_context_projects_party_history_across_reviewed_endpoints(db_session):
    observed_at = datetime(2026, 8, 20, 11, 0, tzinfo=UTC)
    current = _conversation(
        db_session,
        address=f"+234801{str(uuid4().int)[-7:]}",
        channel_type="whatsapp",
        subject="Work number enquiry",
        last_message_at=observed_at,
    )
    party = _bind_party(db_session, current)
    previous = _conversation(
        db_session,
        address=f"+234802{str(uuid4().int)[-7:]}",
        channel_type="whatsapp",
        subject="Personal number enquiry",
        last_message_at=observed_at - timedelta(days=1),
    )
    _bind_existing_party(db_session, previous, party)

    projection = team_inbox_contact_context.build_contact_context(
        db_session,
        conversation_id=current.id,
        permissions=team_inbox_contact_context.InboxContactContextPermissions(
            can_read_profile=True,
            can_edit_profile=False,
            can_read_leads=True,
            can_write_leads=False,
            can_read_tickets=True,
            can_read_projects=True,
            can_read_project_tasks=True,
        ),
    )

    assert projection is not None
    assert (
        projection.conversation_history_scope.kind
        is team_inbox_contact_context.ConversationHistoryMatchKind.party
    )
    assert tuple(item.id for item in projection.recent_conversations.items) == (
        previous.id,
    )


def test_contact_context_refuses_to_merge_ambiguous_party_history(db_session):
    current = _conversation(
        db_session,
        address=f"shared-{uuid4()}@example.com",
    )
    _bind_party(db_session, current)
    second_party = Party(
        party_type=PartyType.person.value,
        display_name="Second reviewed person",
    )
    db_session.add(second_party)
    db_session.flush()
    second_point = PartyContactPoint(
        party_id=second_party.id,
        channel_type=current.channel_type,
        normalized_value=f"second-{uuid4()}@example.com",
        display_value="Second participant",
        is_primary=True,
    )
    db_session.add(second_point)
    db_session.flush()
    db_session.add(
        InboxConversationParticipant(
            conversation_id=current.id,
            channel_type=current.channel_type,
            normalized_endpoint=second_point.normalized_value,
            party_contact_point_id=second_point.id,
            party_contact_point_bound_at=datetime.now(UTC),
            party_contact_point_binding_source="pytest",
            party_contact_point_binding_reason="Reviewed ambiguous Party fixture",
            admission_source=InboxParticipantAdmissionSource.inbound_from.value,
        )
    )
    db_session.commit()

    projection = team_inbox_contact_context.build_contact_context(
        db_session,
        conversation_id=current.id,
        permissions=team_inbox_contact_context.InboxContactContextPermissions(
            can_read_profile=True,
            can_edit_profile=False,
            can_read_leads=True,
            can_write_leads=False,
            can_read_tickets=True,
            can_read_projects=True,
            can_read_project_tasks=True,
        ),
    )

    assert projection is not None
    assert (
        projection.conversation_history_scope.kind
        is team_inbox_contact_context.ConversationHistoryMatchKind.ambiguous
    )
    assert (
        projection.recent_conversations.availability
        is team_inbox_contact_context.ContextAvailability.not_calculated
    )


def test_contact_context_projects_reseller_history_without_subscriber(db_session):
    reseller = Reseller(name=f"History Reseller {uuid4()}", is_active=True)
    db_session.add(reseller)
    db_session.flush()
    observed_at = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    current = _conversation(
        db_session,
        address=f"reseller-work-{uuid4()}@example.com",
        subject="Current reseller enquiry",
        last_message_at=observed_at,
    )
    previous = _conversation(
        db_session,
        address=f"reseller-personal-{uuid4()}@example.com",
        subject="Earlier reseller enquiry",
        last_message_at=observed_at - timedelta(days=1),
    )
    for conversation in (current, previous):
        conversation.metadata_ = {
            "contact_resolution": {
                "status": "linked_reseller",
                "normalized_contact": conversation.contact_address,
                "reseller_id": str(reseller.id),
            }
        }
        db_session.add(
            InboxContactLink(
                channel_type=conversation.channel_type,
                normalized_contact=conversation.contact_address,
                reseller_id=reseller.id,
                source="pytest_reviewed_reseller",
                is_active=True,
            )
        )
    db_session.commit()

    projection = team_inbox_contact_context.build_contact_context(
        db_session,
        conversation_id=current.id,
        permissions=team_inbox_contact_context.InboxContactContextPermissions(
            can_read_profile=True,
            can_edit_profile=False,
            can_read_leads=True,
            can_write_leads=False,
            can_read_tickets=True,
            can_read_projects=True,
            can_read_project_tasks=True,
        ),
    )

    assert projection is not None
    assert (
        projection.conversation_history_scope.kind
        is team_inbox_contact_context.ConversationHistoryMatchKind.reseller
    )
    assert tuple(item.id for item in projection.recent_conversations.items) == (
        previous.id,
    )


def test_contact_context_projects_party_bound_reseller_history(db_session):
    party = Party(
        party_type=PartyType.organization.value,
        display_name="Reviewed history reseller",
    )
    db_session.add(party)
    db_session.flush()
    reseller = Reseller(
        name=f"Party History Reseller {uuid4()}",
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Reviewed reseller Party fixture",
        is_active=True,
    )
    db_session.add(reseller)
    db_session.flush()
    current = _conversation(
        db_session,
        address=f"party-reseller-work-{uuid4()}@example.com",
    )
    previous = _conversation(
        db_session,
        address=f"party-reseller-personal-{uuid4()}@example.com",
    )
    for conversation in (current, previous):
        db_session.add(
            InboxContactLink(
                channel_type=conversation.channel_type,
                normalized_contact=conversation.contact_address,
                reseller_id=reseller.id,
                source="pytest_reviewed_party_reseller",
                is_active=True,
            )
        )
    db_session.commit()

    projection = team_inbox_contact_context.build_contact_context(
        db_session,
        conversation_id=current.id,
        permissions=team_inbox_contact_context.InboxContactContextPermissions(
            can_read_profile=True,
            can_edit_profile=False,
            can_read_leads=True,
            can_write_leads=False,
            can_read_tickets=True,
            can_read_projects=True,
            can_read_project_tasks=True,
        ),
    )

    assert projection is not None
    assert (
        projection.conversation_history_scope.kind
        is team_inbox_contact_context.ConversationHistoryMatchKind.party
    )
    assert tuple(item.id for item in projection.recent_conversations.items) == (
        previous.id,
    )
