"""Focused behavior contracts for Inbox Lead intake."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.ai_intake import AiIntakeConfig
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.lead_intake import LeadIntakeInvitation, LeadIntakePartyType
from app.models.party import Party, PartyContactPoint, PartyRole
from app.models.sales import Lead, LeadOriginCapture
from app.models.service_team import ServiceTeam
from app.models.subscription_engine import SettingValueType
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationParticipant,
    InboxMessage,
)
from app.schemas.lead_intake import (
    AiLeadIntakeClassification,
    LeadIntakeSubmission,
    LeadIntakeTemplateDraft,
    ResolvedLeadIntakeAddress,
)
from app.services import lead_intake_ai
from app.services.owner_commands import CommandContext
from app.services.sales import lead_intake
from app.services.settings_cache import SettingsCache


def _context(key: str) -> CommandContext:
    return CommandContext.system(
        actor="pytest:lead-intake",
        scope="sales.lead_intake:test",
        reason="focused Lead intake behavior test",
        idempotency_key=key,
    )


def _staff_and_team(db_session) -> tuple[SystemUser, ServiceTeam]:
    staff = SystemUser(
        first_name="Sales",
        last_name="Agent",
        email=f"lead-intake-{uuid4().hex}@example.com",
        is_active=True,
    )
    team = ServiceTeam(name=f"Sales Intake {uuid4().hex[:8]}", is_active=True)
    db_session.add_all([staff, team])
    db_session.commit()
    return staff, team


def _conversation(db_session) -> tuple[InboxConversation, InboxMessage]:
    endpoint = f"23480{uuid4().int % 10**8:08d}"
    conversation = InboxConversation(
        channel_type="whatsapp",
        contact_address=endpoint,
        external_thread_id=f"wa-{uuid4().hex}",
        metadata_={"contact_resolution": {"status": "unmatched"}},
        is_active=True,
    )
    db_session.add(conversation)
    db_session.flush()
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type="whatsapp",
        direction="inbound",
        body="I need a new internet connection in Abuja",
        from_address=endpoint,
        external_message_id=f"wamid.{uuid4().hex}",
        metadata_={"provider": "whatsapp", "phone_number_id": "phone-1"},
    )
    db_session.add(message)
    db_session.flush()
    db_session.add(
        InboxConversationParticipant(
            conversation_id=conversation.id,
            channel_type="whatsapp",
            normalized_endpoint=endpoint,
            provider_account_scope="phone-1",
            admission_source="inbound_from",
            admission_message_id=message.id,
        )
    )
    db_session.commit()
    return conversation, message


def _published_template(
    db_session,
    *,
    staff: SystemUser,
    team: ServiceTeam,
    party_type: LeadIntakePartyType = LeadIntakePartyType.individual,
):
    staff_id = staff.id
    team_id = team.id
    db_session.commit()
    template_id = uuid4()
    draft = LeadIntakeTemplateDraft(
        party_type=party_type.value,
        name=f"{party_type.value.title()} intake",
        heading="Tell us about your connection request",
        introduction="We need a few details to assess your request.",
        privacy_notice="We use these details only to process this enquiry.",
        invitation_message="Please complete this secure form: {link}",
        confirmation_message="Your details have been saved for our Sales team.",
        thank_you_message="Thank you. Our Sales team will contact you.",
        target_service_team_id=team_id,
    )
    lead_intake.mutate_template(
        db_session,
        lead_intake.TemplateCommand(
            context=_context(f"template-create:{template_id}"),
            action=lead_intake.TemplateAction.create,
            actor_system_user_id=staff_id,
            template_id=template_id,
            draft=draft,
        ),
    )
    outcome = lead_intake.mutate_template(
        db_session,
        lead_intake.TemplateCommand(
            context=_context(f"template-publish:{template_id}"),
            action=lead_intake.TemplateAction.publish,
            actor_system_user_id=staff_id,
            template_id=template_id,
        ),
    )
    return db_session.get(lead_intake.LeadIntakeTemplate, outcome.template_id)


def test_ai_classification_accepts_closed_json_vocabulary():
    item = AiLeadIntakeClassification.model_validate(
        {
            "intent": "coverage_request",
            "intent_confidence": 0.93,
            "party_type": "organization",
            "party_type_confidence": 0.88,
            "clarification_question": None,
        }
    )
    assert item.intent.value == "coverage_request"
    with pytest.raises(ValueError):
        AiLeadIntakeClassification.model_validate(
            {
                "intent": "upgrade",
                "intent_confidence": 0.93,
                "party_type": "organization",
                "party_type_confidence": 0.88,
                "clarification_question": None,
            }
        )


def test_published_template_is_immutable(db_session):
    staff, team = _staff_and_team(db_session)
    template = _published_template(db_session, staff=staff, team=team)
    template_id = template.id
    staff_id = staff.id
    team_id = team.id
    db_session.commit()
    with pytest.raises(lead_intake.LeadIntakeError) as exc:
        lead_intake.mutate_template(
            db_session,
            lead_intake.TemplateCommand(
                context=_context(f"template-update:{template_id}"),
                action=lead_intake.TemplateAction.update,
                actor_system_user_id=staff_id,
                template_id=template_id,
                draft=LeadIntakeTemplateDraft(
                    party_type="individual",
                    name="Changed",
                    heading="Changed",
                    privacy_notice="Privacy",
                    invitation_message="Complete {link}",
                    confirmation_message="Saved",
                    thank_you_message="Thanks",
                    target_service_team_id=team_id,
                ),
            ),
        )
    assert exc.value.code == "sales.lead_intake.published_template_immutable"


def test_high_confidence_unknown_meta_prospect_receives_one_auto_invitation(
    db_session,
):
    staff, team = _staff_and_team(db_session)
    _published_template(db_session, staff=staff, team=team)
    _published_template(
        db_session,
        staff=staff,
        team=team,
        party_type=LeadIntakePartyType.organization,
    )
    db_session.add_all(
        [
            DomainSetting(
                domain=SettingDomain.integration,
                key="lead_intake_auto_send_enabled",
                value_type=SettingValueType.boolean,
                value_text="true",
                is_active=True,
            ),
            AiIntakeConfig(
                scope_key=f"lead-intake-{uuid4().hex}",
                channel_type="whatsapp",
                is_enabled=True,
                confidence_threshold=0.8,
                allow_followup_questions=True,
                max_clarification_turns=1,
            ),
        ]
    )
    db_session.commit()
    SettingsCache.invalidate(
        SettingDomain.integration.value, "lead_intake_auto_send_enabled"
    )
    conversation, message = _conversation(db_session)
    conversation_id = conversation.id
    message_id = message.id
    db_session.commit()

    outcome = lead_intake.assess_inbound(
        db_session,
        lead_intake.AssessInboundCommand(
            context=_context(f"auto-assess:{message_id}"),
            conversation_id=conversation_id,
            message_id=message_id,
            classification=AiLeadIntakeClassification(
                intent="new_connection",
                intent_confidence=0.96,
                party_type="individual",
                party_type_confidence=0.94,
                clarification_question=None,
            ),
            provider_label="pytest",
            model_label="classifier",
        ),
    )

    assert outcome.action == "invite_issued"
    assert outcome.token
    assert outcome.invitation_id
    invitations = db_session.scalars(
        select(LeadIntakeInvitation).where(
            LeadIntakeInvitation.conversation_id == conversation_id,
            LeadIntakeInvitation.auto_issued.is_(True),
        )
    ).all()
    assert len(invitations) == 1


def test_shared_metadata_handoff_runs_only_for_classified_sales(
    db_session, monkeypatch
):
    conversation, message = _conversation(db_session)
    conversation_id = conversation.id
    message_id = message.id
    message.metadata_ = {
        **dict(message.metadata_ or {}),
        "ai_intake_status": "classified",
        "ai_intake_requires_follow_up": False,
        "ai_intent": "new_connection",
        "ai_confidence": 0.96,
        "ai_party_type": "individual",
        "ai_party_type_confidence": 0.94,
        "ai_intake_provider": "pytest",
        "ai_intake_model": "classifier",
    }
    db_session.commit()
    calls = []

    def _apply(_db, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(lead_intake_ai, "apply_shared_classification", _apply)

    lead_intake_ai.apply_inbox_intake_handoff(
        db_session, conversation_id=conversation_id, message_id=message_id
    )
    assert len(calls) == 1
    assert calls[0]["classification"].intent.value == "new_connection"

    message = db_session.get(InboxMessage, message_id)
    assert message is not None
    message.metadata_ = {
        **dict(message.metadata_ or {}),
        "ai_intent": "technical_support",
    }
    db_session.commit()
    lead_intake_ai.apply_inbox_intake_handoff(
        db_session, conversation_id=conversation_id, message_id=message_id
    )
    assert len(calls) == 1


def test_manual_form_completion_creates_party_first_lead_and_binds_inbox(db_session):
    staff, team = _staff_and_team(db_session)
    _published_template(db_session, staff=staff, team=team)
    conversation, message = _conversation(db_session)
    conversation_id = conversation.id
    message_id = message.id
    staff_id = staff.id
    team_id = team.id
    contact_address = conversation.contact_address
    db_session.commit()

    issued = lead_intake.issue_manual_invitation(
        db_session,
        lead_intake.ManualInvitationCommand(
            context=_context(f"invite:{conversation_id}"),
            conversation_id=conversation_id,
            trigger_message_id=message_id,
            party_type=LeadIntakePartyType.individual,
            actor_system_user_id=staff_id,
        ),
    )
    assert issued.token and issued.invitation_id
    invitation = db_session.get(LeadIntakeInvitation, issued.invitation_id)
    assert invitation is not None
    assert invitation.token_hash == lead_intake.token_hash(issued.token)
    assert issued.token not in invitation.token_hash
    invitation_id = invitation.id
    db_session.commit()

    outcome = lead_intake.submit_form(
        db_session,
        lead_intake.SubmitLeadIntakeCommand(
            context=_context(f"submit:{invitation_id}"),
            token=issued.token,
            submission=LeadIntakeSubmission(
                full_name="Amina Bello",
                gender="female",
                date_of_birth=date(1994, 5, 12),
                latitude=9.0765,
                longitude=7.3986,
                address_confirmation=True,
                privacy_acknowledged=True,
            ),
            resolved_address=ResolvedLeadIntakeAddress(
                display_name="Wuse 2, Abuja, Nigeria",
                latitude=9.0765,
                longitude=7.3986,
                state="FCT",
                country_code="ng",
            ),
        ),
    )

    lead = db_session.get(Lead, outcome.lead_id)
    party = db_session.get(Party, outcome.party_id)
    invitation = db_session.get(LeadIntakeInvitation, invitation_id)
    origin = db_session.scalar(
        select(LeadOriginCapture).where(LeadOriginCapture.lead_id == outcome.lead_id)
    )
    participant = db_session.scalar(
        select(InboxConversationParticipant).where(
            InboxConversationParticipant.conversation_id == conversation_id,
            InboxConversationParticipant.normalized_endpoint == contact_address,
        )
    )
    contact = db_session.get(PartyContactPoint, invitation.party_contact_point_id)
    role = db_session.scalar(
        select(PartyRole).where(
            PartyRole.party_id == party.id,
            PartyRole.role_type == "prospect",
        )
    )

    assert lead is not None and lead.subscriber_id is None
    assert party is not None and party.display_name == "Amina Bello"
    assert party.metadata_["state"] == "Federal Capital Territory"
    assert origin is not None
    assert origin.capture_method == "inbox_form"
    assert origin.source_platform == "team_inbox"
    assert invitation.status == "completed"
    assert invitation.lead_id == lead.id
    assert contact is not None and contact.consent_status == "unknown"
    assert participant.party_contact_point_id == contact.id
    assert participant.provider_account_scope == "phone-1"
    assert role is not None and role.status == "active"
    assert (
        db_session.get(InboxConversation, conversation_id).primary_service_team_id
        == team_id
    )
