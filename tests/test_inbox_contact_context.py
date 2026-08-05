from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.db import finish_read_transaction
from app.models.party import Party, PartyContactPoint, PartyType
from app.models.sales import Lead, Pipeline
from app.models.team_inbox import (
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


def _conversation(db_session, *, address: str) -> InboxConversation:
    row = InboxConversation(
        channel_type="email",
        contact_address=address,
        subject="Service enquiry",
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
        channel_type="email",
        normalized_value=conversation.contact_address,
        display_value=conversation.contact_address,
        is_primary=True,
    )
    db_session.add(point)
    db_session.flush()
    db_session.add(
        InboxConversationParticipant(
            conversation_id=conversation.id,
            channel_type="email",
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
