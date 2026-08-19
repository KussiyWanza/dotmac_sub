"""Mailbox routing: the table that decides which team owns an inbound address.

`TeamInboxEmailRoute` had a model and a consumer
(`build_email_team_routing_plan`) but no writer outside direct SQL, which is
why production ran six live mailboxes against zero rows. This covers the CRUD
that closes that gap.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §3 and §5 slice 6.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.services import email as email_service
from app.services import team_inbox_commands, team_inbox_routing

ROUTES_TEMPLATE = Path("templates/admin/inbox/email_routes.html").read_text()
ROUTES_MODULE = Path("app/web/admin/inbox.py").read_text()


def test_ai_intake_meta_social_form_uses_connector_key():
    assert 'value="meta.social"' in ROUTES_TEMPLATE
    assert "ai_intake_policy.provider == 'meta.social'" in ROUTES_TEMPLATE
    assert 'value="meta_social"' not in ROUTES_TEMPLATE


def _team(db_session, name):
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    captured = team.id
    db_session.commit()
    return captured


def test_routing_a_mailbox_makes_it_resolvable(db_session):
    team_id = _team(db_session, "Support")

    team_inbox_commands.create_email_route(
        db_session,
        service_team_id=team_id,
        email_address="Support@Dotmac.NG",
        is_primary=True,
    )

    rows = team_inbox_routing.list_email_routes(db_session)
    assert len(rows) == 1
    # Addresses normalise, so casing in the form cannot create a second route.
    assert rows[0].email_address == "support@dotmac.ng"
    assert rows[0].service_team_name == "Support"
    assert rows[0].is_primary is True


def test_the_plan_builder_sees_the_new_route(db_session):
    """The point of the table: inbound mail resolves to a team."""
    team_id = _team(db_session, "Support")
    team_inbox_commands.create_email_route(
        db_session, service_team_id=team_id, email_address="support@dotmac.ng"
    )

    plan = team_inbox_routing.build_email_team_routing_plan(
        db_session, to_addresses=["support@dotmac.ng"], cc_addresses=[]
    )

    assert plan.primary_service_team_id == str(team_id)
    assert plan.unmatched_recipients == []


def test_channel_route_supplies_default_team_for_social_channel(db_session):
    team_id = _team(db_session, "Customer Experience")
    team_inbox_commands.create_channel_route(
        db_session,
        channel_type="whatsapp",
        provider="meta_cloud_api",
        account_scope="phone-1",
        service_team_id=team_id,
        display_name="Main WhatsApp",
    )

    decision = team_inbox_routing.resolve_channel_routing_decision(
        db_session,
        channel_type="whatsapp",
        provider="meta_cloud_api",
        account_scope="phone-1",
    )

    assert decision.primary_service_team_id == str(team_id)
    assert decision.channel_service_team_id == str(team_id)
    assert decision.reason == "channel_route"


def test_ai_route_overrides_channel_default_when_allowed_and_confident(db_session):
    default_team_id = _team(db_session, "Customer Experience")
    billing_team_id = _team(db_session, "Billing")
    team_inbox_commands.create_channel_route(
        db_session,
        channel_type="whatsapp",
        provider="meta_cloud_api",
        account_scope="phone-1",
        service_team_id=default_team_id,
        allow_ai_routing=True,
    )
    team_inbox_commands.create_ai_route(
        db_session,
        channel_type="any",
        intent_key="billing_issue",
        service_team_id=billing_team_id,
        confidence_threshold=0.8,
    )

    decision = team_inbox_routing.resolve_channel_routing_decision(
        db_session,
        channel_type="whatsapp",
        provider="meta_cloud_api",
        account_scope="phone-1",
        metadata={"ai_intent": "billing issue", "ai_confidence": 0.91},
    )

    assert decision.primary_service_team_id == str(billing_team_id)
    assert decision.channel_service_team_id == str(default_team_id)
    assert decision.ai_service_team_id == str(billing_team_id)
    assert decision.reason == "ai_intake_route"


def test_ai_route_does_not_override_channel_when_disabled(db_session):
    default_team_id = _team(db_session, "Customer Experience")
    billing_team_id = _team(db_session, "Billing")
    team_inbox_commands.create_channel_route(
        db_session,
        channel_type="whatsapp",
        provider="meta_cloud_api",
        account_scope="phone-1",
        service_team_id=default_team_id,
        allow_ai_routing=False,
    )
    team_inbox_commands.create_ai_route(
        db_session,
        channel_type="any",
        intent_key="billing_issue",
        service_team_id=billing_team_id,
        confidence_threshold=0.8,
    )

    decision = team_inbox_routing.resolve_channel_routing_decision(
        db_session,
        channel_type="whatsapp",
        provider="meta_cloud_api",
        account_scope="phone-1",
        metadata={"ai_intent": "billing_issue", "ai_confidence": 0.91},
    )

    assert decision.primary_service_team_id == str(default_team_id)
    assert decision.ai_service_team_id is None
    assert decision.reason == "channel_route"


def test_ai_route_confidence_threshold_is_not_bypassed_by_department(db_session):
    default_team_id = _team(db_session, "Customer Experience")
    technical_team_id = _team(db_session, "Technical Support")
    team_inbox_commands.create_channel_route(
        db_session,
        channel_type="whatsapp",
        provider="meta_cloud_api",
        account_scope="phone-1",
        service_team_id=default_team_id,
        allow_ai_routing=True,
    )
    team_inbox_commands.create_ai_route(
        db_session,
        channel_type="any",
        intent_key="technical_support",
        service_team_id=technical_team_id,
        confidence_threshold=0.95,
    )

    decision = team_inbox_routing.resolve_channel_routing_decision(
        db_session,
        channel_type="whatsapp",
        provider="meta_cloud_api",
        account_scope="phone-1",
        metadata={
            "ai_intake_status": "classified",
            "ai_intent": "technical_support",
            "ai_department": "technical_support",
            "ai_confidence": 0.9,
        },
    )

    assert decision.primary_service_team_id == str(default_team_id)
    assert decision.ai_service_team_id is None
    assert decision.reason == "channel_route"


def test_ai_classification_does_not_guess_team_by_department_name(db_session):
    default_team_id = _team(db_session, "Customer Experience")
    _team(db_session, "Technical Support")
    team_inbox_commands.create_channel_route(
        db_session,
        channel_type="whatsapp",
        provider="meta_cloud_api",
        account_scope="phone-1",
        service_team_id=default_team_id,
        allow_ai_routing=True,
    )

    decision = team_inbox_routing.resolve_channel_routing_decision(
        db_session,
        channel_type="whatsapp",
        provider="meta_cloud_api",
        account_scope="phone-1",
        metadata={
            "ai_intake_status": "classified",
            "ai_intent": "technical_support",
            "ai_department": "technical_support",
            "ai_confidence": 0.9,
        },
    )

    assert decision.primary_service_team_id == str(default_team_id)
    assert decision.ai_service_team_id is None
    assert decision.reason == "ai_intake_missing_team_id"


def test_ai_classification_rejects_invalid_team_id_and_uses_configured_fallback(
    db_session,
):
    fallback_team_id = _team(db_session, "Configured fallback")
    _team(db_session, "Technical Support")

    decision = team_inbox_routing.resolve_channel_routing_decision(
        db_session,
        channel_type="whatsapp",
        provider="meta_cloud_api",
        account_scope="phone-1",
        fallback_service_team_id=fallback_team_id,
        metadata={
            "ai_intake_status": "classified",
            "ai_intent": "technical_support",
            "ai_department": "Technical Support",
            "ai_department_team_id": "not-a-team-id",
            "ai_confidence": 0.9,
        },
    )

    assert decision.primary_service_team_id == str(fallback_team_id)
    assert decision.ai_service_team_id is None
    assert decision.reason == "ai_intake_invalid_team_id"


def test_ai_classification_routes_to_explicit_active_department_team_id(db_session):
    default_team_id = _team(db_session, "Customer Experience")
    technical_team_id = _team(db_session, "Technical Support")
    team_inbox_commands.create_channel_route(
        db_session,
        channel_type="whatsapp",
        provider="meta_cloud_api",
        account_scope="phone-1",
        service_team_id=default_team_id,
        allow_ai_routing=True,
    )

    decision = team_inbox_routing.resolve_channel_routing_decision(
        db_session,
        channel_type="whatsapp",
        provider="meta_cloud_api",
        account_scope="phone-1",
        metadata={
            "ai_intake_status": "classified",
            "ai_intent": "technical_support",
            "ai_department": "technical_support",
            "ai_department_team_id": str(technical_team_id),
            "ai_confidence": 0.9,
        },
    )

    assert decision.primary_service_team_id == str(technical_team_id)
    assert decision.ai_service_team_id == str(technical_team_id)
    assert decision.reason == "ai_intake_department"


def test_a_duplicate_address_for_the_same_team_is_refused(db_session):
    team_id = _team(db_session, "Support")
    team_inbox_commands.create_email_route(
        db_session, service_team_id=team_id, email_address="support@dotmac.ng"
    )

    with pytest.raises(team_inbox_routing.EmailRouteError) as exc:
        team_inbox_commands.create_email_route(
            db_session, service_team_id=team_id, email_address="support@dotmac.ng"
        )
    assert "already routed" in str(exc.value)


def test_only_one_primary_survives_per_team(db_session):
    """Two primaries would make team selection ambiguous."""
    team_id = _team(db_session, "Support")
    team_inbox_commands.create_email_route(
        db_session,
        service_team_id=team_id,
        email_address="first@dotmac.ng",
        is_primary=True,
    )
    team_inbox_commands.create_email_route(
        db_session,
        service_team_id=team_id,
        email_address="second@dotmac.ng",
        is_primary=True,
    )

    primaries = [
        r for r in team_inbox_routing.list_email_routes(db_session) if r.is_primary
    ]
    assert [r.email_address for r in primaries] == ["second@dotmac.ng"]


def test_deactivating_a_route_clears_primary_and_stops_resolution(db_session):
    team_id = _team(db_session, "Support")
    # The command returns the id so no read is needed between commands: a read
    # would leave the session in a transaction and the next command refuses it.
    route_id = team_inbox_commands.create_email_route(
        db_session,
        service_team_id=team_id,
        email_address="support@dotmac.ng",
        is_primary=True,
    )

    team_inbox_commands.delete_email_route(db_session, route_id=route_id)

    row = team_inbox_routing.list_email_routes(db_session)[0]
    assert row.is_active is False
    assert row.is_primary is False
    plan = team_inbox_routing.build_email_team_routing_plan(
        db_session, to_addresses=["support@dotmac.ng"], cc_addresses=[]
    )
    assert plan.primary_service_team_id is None
    assert plan.unmatched_recipients == ["support@dotmac.ng"]


def test_a_deactivated_route_can_be_reactivated(db_session):
    team_id = _team(db_session, "Support")
    route_id = team_inbox_commands.create_email_route(
        db_session, service_team_id=team_id, email_address="support@dotmac.ng"
    )
    team_inbox_commands.delete_email_route(db_session, route_id=route_id)

    team_inbox_commands.update_email_route(
        db_session, route_id=route_id, is_active=True
    )

    assert team_inbox_routing.list_email_routes(db_session)[0].is_active is True


def _smtp_profile(db_session, sender_key: str, *, is_active: bool = True) -> None:
    email_service.upsert_smtp_sender(
        db_session,
        sender_key=sender_key,
        host="mail.dotmac.ng",
        port=587,
        username=f"{sender_key}@dotmac.ng",
        password=None,
        from_email=f"{sender_key}@dotmac.ng",
        from_name=f"Dotmac {sender_key.title()}",
        use_tls=True,
        use_ssl=False,
        is_active=is_active,
    )
    db_session.commit()


def test_route_can_select_an_active_outbound_sender(db_session):
    team_id = _team(db_session, "Support")
    _smtp_profile(db_session, "support")
    route_id = team_inbox_commands.create_email_route(
        db_session, service_team_id=team_id, email_address="support@dotmac.ng"
    )

    team_inbox_commands.update_email_route(
        db_session,
        route_id=route_id,
        outbound_email_sender_key="SUPPORT",
        update_outbound_email_sender=True,
    )

    assert (
        team_inbox_routing.list_email_routes(db_session)[0].outbound_email_sender_key
        == "support"
    )


def test_route_rejects_an_unknown_or_inactive_outbound_sender(db_session):
    team_id = _team(db_session, "Support")
    _smtp_profile(db_session, "disabled", is_active=False)
    route_id = team_inbox_commands.create_email_route(
        db_session, service_team_id=team_id, email_address="support@dotmac.ng"
    )

    with pytest.raises(team_inbox_routing.EmailRouteError, match="active SMTP"):
        team_inbox_commands.update_email_route(
            db_session,
            route_id=route_id,
            outbound_email_sender_key="disabled",
            update_outbound_email_sender=True,
        )


def test_route_can_return_to_the_default_outbound_sender(db_session):
    team_id = _team(db_session, "Support")
    _smtp_profile(db_session, "support")
    route_id = team_inbox_commands.create_email_route(
        db_session, service_team_id=team_id, email_address="support@dotmac.ng"
    )
    team_inbox_commands.update_email_route(
        db_session,
        route_id=route_id,
        outbound_email_sender_key="support",
        update_outbound_email_sender=True,
    )

    team_inbox_commands.update_email_route(
        db_session,
        route_id=route_id,
        outbound_email_sender_key=None,
        update_outbound_email_sender=True,
    )

    assert (
        team_inbox_routing.list_email_routes(db_session)[0].outbound_email_sender_key
        is None
    )


def test_an_unknown_team_is_refused(db_session):
    import uuid

    with pytest.raises(team_inbox_routing.EmailRouteError):
        team_inbox_commands.create_email_route(
            db_session,
            service_team_id=uuid.uuid4(),
            email_address="support@dotmac.ng",
        )


def test_a_blank_address_is_refused(db_session):
    team_id = _team(db_session, "Support")
    with pytest.raises(team_inbox_routing.EmailRouteError):
        team_inbox_commands.create_email_route(
            db_session, service_team_id=team_id, email_address="   "
        )


def test_the_admin_surface_is_a_thin_adapter():
    assert "team_inbox_commands.create_email_route" in ROUTES_MODULE
    assert "team_inbox_commands.update_email_route" in ROUTES_MODULE
    assert "team_inbox_commands.delete_email_route" in ROUTES_MODULE
    assert "team_inbox_commands.create_channel_route" in ROUTES_MODULE
    assert "team_inbox_commands.create_ai_route" in ROUTES_MODULE
    # The route must not touch the ORM directly.
    assert "TeamInboxEmailRoute(" not in ROUTES_MODULE
    assert "TeamInboxChannelRoute(" not in ROUTES_MODULE
    assert "TeamInboxAiRoute(" not in ROUTES_MODULE


def test_the_page_states_what_it_does_not_control():
    """Routing decides where forwarded mail lands; it does not forward it."""
    assert "forwards to it" in ROUTES_TEMPLATE
    assert "Channel defaults" in ROUTES_TEMPLATE
    assert "AI intake routes" in ROUTES_TEMPLATE
    assert "/admin/crm/inbox/channel-routes" in ROUTES_TEMPLATE
    assert "/admin/crm/inbox/ai-routes" in ROUTES_TEMPLATE
    assert "components/forms/csrf_input.html" in ROUTES_TEMPLATE
    assert 'name="outbound_email_sender_key"' in ROUTES_TEMPLATE
    assert "Default SMTP profile" in ROUTES_TEMPLATE


def test_ai_intake_admin_lifecycle_uses_canonical_separate_actions():
    assert "/admin/crm/inbox/ai-intake-policy/draft" in ROUTES_TEMPLATE
    assert "/validate" in ROUTES_TEMPLATE
    assert "/activate" in ROUTES_TEMPLATE
    assert "/disable" in ROUTES_TEMPLATE
    assert 'name="is_enabled"' not in ROUTES_TEMPLATE
    assert "ai_conversation_intake.create_draft_policy" in ROUTES_MODULE
    assert "ai_conversation_intake.validate_policy_version" in ROUTES_MODULE
    assert "ai_conversation_intake.activate_policy_version" in ROUTES_MODULE
    assert "ai_conversation_intake.disable_policy" in ROUTES_MODULE
    assert "ai_intake.upsert_config" not in ROUTES_MODULE
    assert '"production_collection_enabled": False' in ROUTES_MODULE
    assert 'name="generic_clarification_question"' in ROUTES_TEMPLATE
    assert 'name="customer_type_clarification_question"' in ROUTES_TEMPLATE
    assert (
        "Sent first when a new eligible conversation starts AI intake"
        in ROUTES_TEMPLATE
    )


def test_ai_intake_admin_history_is_read_only_and_exposes_all_version_states():
    assert "Policy version history" in ROUTES_TEMPLATE
    assert "Current active" in ROUTES_TEMPLATE
    assert "'Active' if version.status == 'activated'" in ROUTES_TEMPLATE
    assert "ai_intake_policy_version_history" in ROUTES_TEMPLATE
    assert "Policy version history" not in ROUTES_MODULE


def test_ai_polish_settings_are_editable_on_inbox_settings_page():
    assert "/admin/crm/inbox/ai-polish-settings" in ROUTES_TEMPLATE
    assert 'name="inbox_ai_polish_business_voice"' in ROUTES_TEMPLATE
    assert 'name="inbox_ai_polish_channel_guidance"' in ROUTES_TEMPLATE
    assert "settings_api.upsert_integration_setting" in ROUTES_MODULE
