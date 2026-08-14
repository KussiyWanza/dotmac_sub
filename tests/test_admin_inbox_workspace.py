from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

from app.models.party import Party, PartyType
from app.models.sales import Lead, LeadOriginCapture
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceStatus,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationLabel,
    InboxConversationStatus,
    InboxLabel,
    InboxMessage,
    InboxMessageTemplate,
    InboxSavedFilter,
)
from app.services import (
    team_inbox_commands,
    team_inbox_operations,
    team_inbox_outbound,
    team_inbox_projection,
    team_inbox_read,
)
from app.services.list_query import PageMeta
from tests.staff_identity_fixtures import add_bound_staff_user


def _conversation(db_session) -> uuid.UUID:
    conversation = InboxConversation(
        channel_type="email",
        subject="Router offline",
        contact_address="customer@example.com",
        status=InboxConversationStatus.open.value,
    )
    db_session.add(conversation)
    db_session.flush()
    conversation_id = conversation.id
    db_session.commit()
    return conversation_id


def test_inbox_workspace_templates_compile():
    environment = Environment(loader=FileSystemLoader("templates"), autoescape=True)

    for template_name in (
        "admin/inbox/index.html",
        "admin/inbox/_sidebar.html",
        "admin/inbox/_conversation.html",
        "admin/inbox/_comment_thread.html",
        "admin/inbox/_contact_drawer.html",
        "admin/inbox/_authoritative_context.html",
        "admin/inbox/action_resolver.html",
        "admin/inbox/_empty_state.html",
        "admin/inbox/_floating_surfaces.html",
        "admin/inbox/_overlays.html",
        "admin/inbox/_ticket_panel.html",
        "admin/inbox/manager_ai.html",
    ):
        assert environment.get_template(template_name) is not None


@pytest.mark.parametrize(
    ("page", "total_items", "previous_page", "next_page"),
    (
        (1, 1, None, None),
        (2, 75, 1, 3),
        (3, 75, 2, None),
    ),
)
def test_inbox_pagination_builds_only_available_navigation_links(
    page,
    total_items,
    previous_page,
    next_page,
):
    environment = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    pagination = environment.get_template(
        "admin/inbox/_queue_macros.html"
    ).module.inbox_pagination
    list_query = team_inbox_projection.INBOX_LIST_DEFINITION.build_query(
        search=None,
        filters={},
        page=page,
        per_page=25,
    )
    page_meta = PageMeta.from_query(list_query, total_items)

    rendered = str(pagination(list_query, page_meta, "/admin/inbox"))

    assert f'aria-current="page" aria-label="Page {page}"' in rendered
    assert ("page=0" in rendered) is False
    assert (">Previous</a>" in rendered) is (previous_page is not None)
    assert (">Next</a>" in rendered) is (next_page is not None)
    if previous_page is not None:
        assert f"page={previous_page}" in rendered
    if next_page is not None:
        assert f"page={next_page}" in rendered


def test_inbox_pagination_renders_compact_page_numbers_and_preserves_selection():
    environment = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    pagination = environment.get_template(
        "admin/inbox/_queue_macros.html"
    ).module.inbox_pagination
    list_query = team_inbox_projection.INBOX_LIST_DEFINITION.build_query(
        search="router",
        filters={"status": "open"},
        page=7,
        per_page=25,
    )
    page_meta = PageMeta.from_query(list_query, total_items=300)
    active_id = str(uuid.uuid4())

    rendered = str(
        pagination(
            list_query,
            page_meta,
            "/admin/inbox",
            active_id=active_id,
        )
    )

    assert page_meta.navigation == (1, None, 6, 7, 8, None, 12)
    assert "151&ndash;175 of 300 conversations" in rendered
    assert 'aria-label="Page 1"' in rendered
    assert 'aria-label="Page 6"' in rendered
    assert 'aria-current="page" aria-label="Page 7"' in rendered
    assert 'aria-label="Page 8"' in rendered
    assert 'aria-label="Page 12"' in rendered
    assert rendered.count(f"conversation_id={active_id}") == 12


def test_workspace_exposes_responsive_realtime_and_accessible_controls():
    index = Path("templates/admin/inbox/index.html").read_text()
    sidebar = Path("templates/admin/inbox/_sidebar.html").read_text()
    conversation = Path("templates/admin/inbox/_conversation.html").read_text()
    javascript = Path("static/js/admin-inbox.js").read_text()

    assert "startSidebarResize" in index
    assert "inbox-sidebar-content" in index
    assert 'role="dialog"' in Path("templates/admin/inbox/_overlays.html").read_text()
    assert "@input.debounce.300ms" in sidebar
    assert "/admin/inbox/presence" in sidebar
    assert "Only online agents receive auto-assigned inbox conversations." in sidebar
    assert "conversation_id" in sidebar
    assert "Advanced team conditions" in sidebar
    assert "support:inbox:self_assign" in conversation
    assert "service_team_options | default(())" in conversation
    assert "/admin/inbox/{{ timeline.id }}/assign-to-me" in conversation
    assert 'action="/admin/inbox/bulk"' not in conversation
    assert 'aria-label="Team for assignment to me"' in conversation
    assert 'name="service_team_id" required' in conversation
    assert "inboxTeamFilterBuilder" in javascript
    assert 'filters: "filters"' in javascript
    assert 'name="priority_at_most"' in sidebar
    assert "data-reply-composer" in conversation
    assert "idempotency_key" in conversation
    assert "import message_bubble with context" in conversation
    triage = Path("templates/components/ui/triage.html").read_text()
    assert "att.mime_type.startswith('video/')" in triage
    assert "att.mime_type.startswith('audio/')" in triage
    assert "<video" in triage
    assert "<audio" in triage
    assert 'set priority_label = "Urgent"' in triage
    assert "assignee.initials" in triage
    assert "title=\"{{ assignee.name or 'Assigned agent' }}\"" in triage
    assert "message.sender" in triage
    assert "outbound_sender.display_name" in triage
    assert "outbound_sender.initials" in triage
    assert 'aria-label="Sent by {{ outbound_sender_name }}"' in triage
    assert ">AG</div>" not in triage
    assert "att.location.map_url" in triage
    assert "att.location.latitude" in triage
    assert "att.location.longitude" in triage
    assert "https://www.google.com/maps/search/?api=1&query=" in triage
    assert "Open in Google Maps" in triage
    assert 'rel="noopener noreferrer"' in triage
    assert "dotmac.inbox.draft." in javascript
    assert "newMessagesAvailable" in javascript
    assert "data.agent_name" in javascript
    assert 'name: agentName || "Another agent"' in javascript
    assert "${names[0]} is replying" in javascript
    assert "expiresAt: Date.now() + 3500" in javascript
    assert "clearTypingPresence()" in javascript
    assert "scheduleTypingPrune()" in javascript
    assert "setInterval" in javascript
    assert "5000" in javascript
    assert "handleShortcut" in javascript
    assert 'hx-sync="this:replace"' in sidebar
    assert ':aria-busy="filterLoading.toString()"' in sidebar
    assert "stale.xhr.abort()" in javascript
    assert "event.detail.shouldSwap = false" in javascript
    assert "if (this.filterLoading) return" in javascript
    assert 'document.body.addEventListener("htmx:sendAbort", release)' in javascript
    assert (
        "SAFE_INLINE_VIDEO_CONTENT_TYPES"
        in Path("app/services/team_inbox_projection.py").read_text()
    )
    assert "video/mp4" in Path("app/services/team_inbox_projection.py").read_text()


def test_projection_supplies_live_agent_and_assignment_options(db_session):
    user, person = add_bound_staff_user(
        db_session,
        email="ada-agent@example.test",
    )
    user.first_name = "Ada"
    user.last_name = "Agent"
    user.display_name = "Ada Agent"
    team = ServiceTeam(name="Support", team_type=ServiceTeamType.support.value)
    db_session.add_all([user, team])
    db_session.flush()
    db_session.add(ServiceTeamMember(team_id=team.id, person_id=person.id))
    conversation = InboxConversation(
        channel_type="email",
        subject="Help",
        contact_address="customer@example.test",
    )
    db_session.add(conversation)
    db_session.commit()

    projection = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(actor_person_id=user.id),
    )

    assert projection.agent_options[0].name == "Ada Agent"
    assert projection.agent_options[0].initials == "AA"
    assert projection.agent_options[0].presence_status == (
        InboxAgentPresenceStatus.offline.value
    )
    assert projection.service_team_options[0].name == "Support"
    assert projection.service_team_options[0].id == team.id
    assert (
        team_inbox_projection.list_actor_service_team_options(db_session, user.id)
        == projection.service_team_options
    )
    assert projection.agent_presence is not None
    assert projection.agent_presence.status == InboxAgentPresenceStatus.offline.value
    assert projection.assignment_counts.all == 1
    assert projection.assignment_counts.unassigned == 1


def test_projection_reads_current_agent_presence(db_session):
    user, person = add_bound_staff_user(
        db_session,
        email="ada-agent-presence@example.test",
    )
    user.first_name = "Ada"
    user.last_name = "Agent"
    user.display_name = "Ada Agent"
    team = ServiceTeam(name="Support", team_type=ServiceTeamType.support.value)
    db_session.add_all([user, team])
    db_session.flush()
    db_session.add(ServiceTeamMember(team_id=team.id, person_id=person.id))
    db_session.add(
        InboxAgentPresence(
            person_id=user.id,
            status=InboxAgentPresenceStatus.online.value,
            manual_override_status=InboxAgentPresenceStatus.away.value,
        )
    )
    db_session.commit()

    projection = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(actor_person_id=user.id),
    )

    assert projection.agent_presence is not None
    assert projection.agent_presence.status == InboxAgentPresenceStatus.away.value


def test_assignment_agent_options_show_team_and_presence_status(db_session):
    user, person = add_bound_staff_user(
        db_session,
        email="online-agent@example.test",
    )
    user.first_name = "Online"
    user.last_name = "Agent"
    user.display_name = "Online Agent"
    team = ServiceTeam(name="Support", team_type=ServiceTeamType.support.value)
    db_session.add_all([user, team])
    db_session.flush()
    db_session.add(ServiceTeamMember(team_id=team.id, person_id=person.id))
    db_session.add(
        InboxAgentPresence(
            person_id=user.id,
            status=InboxAgentPresenceStatus.online.value,
        )
    )
    db_session.commit()

    projection = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(actor_person_id=user.id),
    )

    conversation_template = Path("templates/admin/inbox/_conversation.html").read_text()

    assert projection.agent_options[0].presence_status == (
        InboxAgentPresenceStatus.online.value
    )
    assert 'name="service_team_id"' in conversation_template
    assert "service_team_options" in conversation_template
    assert "agent.presence_status" in conversation_template


def test_set_agent_presence_command_updates_current_operator(db_session):
    person_id = uuid.uuid4()

    outcome = team_inbox_commands.set_agent_presence(
        db_session,
        actor_person_id=person_id,
        status=InboxAgentPresenceStatus.online.value,
    )

    presence = (
        db_session.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id == person_id)
        .one()
    )
    assert outcome.status == InboxAgentPresenceStatus.online.value
    assert outcome.already_set is False
    assert presence.manual_override_status == InboxAgentPresenceStatus.online.value
    assert presence.last_seen_at is not None


def test_queue_row_projects_real_contact_name_and_unread_message_count(db_session):
    actor_id = uuid.uuid4()
    conversation = InboxConversation(
        channel_type="email",
        subject="Account help",
        contact_address="customer@example.test",
        metadata_={"contact_name": "Amina Customer"},
    )
    db_session.add(conversation)
    db_session.flush()
    for minute in (1, 2):
        db_session.add(
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction="inbound",
                body=f"Message {minute}",
                received_at=datetime(2026, 7, 27, 10, minute, tzinfo=UTC),
            )
        )
    db_session.commit()

    projection = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(actor_person_id=actor_id),
    )

    assert projection.rows[0].contact_name == "Amina Customer"
    assert projection.rows[0].is_unread is True
    assert projection.rows[0].unread_count == 2


def test_queue_row_prefers_canonical_party_name_over_provider_name(db_session):
    party = Party(
        party_type=PartyType.person.value,
        display_name="Canonical Customer",
    )
    db_session.add(party)
    db_session.flush()
    subscriber = Subscriber(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="test",
        party_binding_reason="verified test identity",
        first_name="Legacy",
        last_name="Customer",
        email="canonical-customer@example.test",
    )
    db_session.add(subscriber)
    db_session.flush()
    conversation = InboxConversation(
        subscriber_id=subscriber.id,
        channel_type="whatsapp",
        contact_address="+2348000000000",
        metadata_={"contact_name": "Provider Customer"},
    )
    db_session.add(conversation)
    db_session.commit()

    projection = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(actor_person_id=uuid.uuid4()),
    )

    row = next(item for item in projection.rows if item.id == str(conversation.id))
    assert row.contact_name == "Canonical Customer"
    assert row.contact_initials == "CC"
    assert row.contact_name_source == "party"


def test_queue_and_detail_use_email_from_name_when_contact_is_unlinked(db_session):
    conversation = InboxConversation(
        channel_type="email",
        contact_address="unlinked@example.test",
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type="email",
            direction="inbound",
            body="Hello",
            from_address="unlinked@example.test",
            received_at=datetime.now(UTC),
            metadata_={"from_name": "Email Sender"},
        )
    )
    db_session.commit()

    projection = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(actor_person_id=uuid.uuid4()),
    )
    row = next(item for item in projection.rows if item.id == str(conversation.id))
    timeline = team_inbox_read.get_conversation_timeline(
        db_session,
        conversation.id,
    )

    assert row.contact_name == "Email Sender"
    assert row.contact_initials == "ES"
    assert row.contact_name_source == "provider"
    assert timeline is not None
    assert timeline.contact_name == row.contact_name
    assert timeline.contact_initials == row.contact_initials


def test_sidebar_projection_preserves_selection_without_loading_detail(
    db_session, monkeypatch
):
    conversation_id = _conversation(db_session)

    def fail_if_detail_is_loaded(*_args, **_kwargs):
        raise AssertionError("sidebar projection must not load conversation detail")

    monkeypatch.setattr(
        team_inbox_projection,
        "get_conversation_projection",
        fail_if_detail_is_loaded,
    )

    projection = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(
            actor_person_id=uuid.uuid4(),
            selected_conversation_id=conversation_id,
            composition=team_inbox_projection.InboxQueueComposition.sidebar,
        ),
    )

    assert projection.selected is None
    assert projection.selected_id == str(conversation_id)


def test_manager_dashboard_projects_presence_load_status_and_channels(db_session):
    user, person = add_bound_staff_user(
        db_session,
        email="maya-manager@example.test",
    )
    user.first_name = "Maya"
    user.last_name = "Manager"
    user.display_name = "Maya Manager"
    team = ServiceTeam(name="Support", team_type=ServiceTeamType.support.value)
    db_session.add_all([user, team])
    db_session.flush()
    db_session.add(ServiceTeamMember(team_id=team.id, person_id=person.id))
    assigned = InboxConversation(
        channel_type="whatsapp",
        status=InboxConversationStatus.open.value,
        primary_service_team_id=team.id,
        subject="Connection help",
        contact_address="+2348000000000",
    )
    pending = InboxConversation(
        channel_type="email",
        status=InboxConversationStatus.pending.value,
        subject="Awaiting customer",
        contact_address="customer@example.test",
    )
    resolved = InboxConversation(
        channel_type="email",
        status=InboxConversationStatus.resolved.value,
        subject="Completed",
        contact_address="resolved@example.test",
        metadata_={
            "status_history": [
                {
                    "from": "open",
                    "to": "resolved",
                    "at": datetime.now(UTC).isoformat(),
                }
            ]
        },
    )
    db_session.add_all([assigned, pending, resolved])
    db_session.flush()
    db_session.add_all(
        [
            InboxAgentPresence(person_id=user.id, status="online"),
            InboxConversationAssignment(
                conversation_id=assigned.id,
                service_team_id=team.id,
                person_id=user.id,
            ),
        ]
    )
    db_session.commit()

    dashboard = team_inbox_projection.build_manager_dashboard_projection(
        db_session,
        queue_metrics=team_inbox_operations.queue_metrics(db_session),
        needs_attention=2,
    )

    assert dashboard.online_agents == 1
    assert dashboard.chats_with_online_agents == 1
    assert dashboard.open == 1
    assert dashboard.pending == 1
    assert dashboard.resolved_today == 1
    assert dashboard.needs_attention == 2
    assert dashboard.agents[0].active_chats == 1
    assert {row.key: row.count for row in dashboard.channel_split}["whatsapp"] == 1


def test_projection_keeps_direct_conversation_link_when_page_is_canonicalized(
    db_session,
):
    conversation_id = _conversation(db_session)

    projection = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(
            page=99,
            selected_conversation_id=conversation_id,
        ),
    )

    assert projection.canonical_url is not None
    assert f"conversation_id={conversation_id}" in projection.canonical_url


def test_conversation_row_projects_labels_and_send_failure_summary(db_session):
    conversation_id = _conversation(db_session)
    label = InboxLabel(name="VIP", slug=f"vip-{uuid.uuid4()}")
    db_session.add(label)
    db_session.flush()
    db_session.add(
        InboxConversationLabel(
            conversation_id=conversation_id,
            label_id=label.id,
        )
    )
    db_session.add(
        InboxMessage(
            conversation_id=conversation_id,
            channel_type="email",
            direction="outbound",
            body="Delivery attempt",
            metadata_={
                "delivery_status": "failed",
                "send_error": "Recipient server rejected the message",
            },
        )
    )
    db_session.commit()

    row = team_inbox_read.list_conversations(db_session).items[0]

    assert [item.name for item in row.labels] == ["VIP"]
    assert row.latest_delivery_status == "failed"
    assert row.latest_delivery_error == "Recipient server rejected the message"


def test_reply_idempotency_key_replays_without_duplicate_message(
    db_session,
    monkeypatch,
):
    conversation_id = _conversation(db_session)
    calls = 0

    def fake_send(db, *, conversation, payload, record_failure):
        nonlocal calls
        calls += 1
        message = InboxMessage(
            conversation_id=conversation.id,
            channel_type="email",
            direction="outbound",
            body=payload.body_text,
            from_address="support@example.test",
            to_addresses=[conversation.contact_address],
            cc_addresses=[],
            metadata_={
                **dict(payload.metadata or {}),
                "body_text": payload.body_text,
                "delivery_status": "queued",
            },
        )
        db.add(message)
        db.flush()
        return team_inbox_outbound.InboxReplyResult(
            kind="queued",
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            from_address=message.from_address,
        )

    monkeypatch.setattr(
        team_inbox_commands.team_inbox_outbound,
        "send_inbox_reply",
        fake_send,
    )

    first = team_inbox_commands.reply(
        db_session,
        command=team_inbox_commands.ReplyCommand(
            conversation_id=conversation_id,
            body_text="We are checking.",
            actor_person_id=uuid.uuid4(),
            idempotency_key="send-key-1",
        ),
    )
    second = team_inbox_commands.reply(
        db_session,
        command=team_inbox_commands.ReplyCommand(
            conversation_id=conversation_id,
            body_text="We are checking.",
            actor_person_id=uuid.uuid4(),
            idempotency_key="send-key-1",
        ),
    )

    assert first.replayed is False
    assert second.replayed is True
    assert first.message_id is not None
    assert second.message_id == first.message_id
    assert calls == 1
    assert db_session.query(InboxMessage).count() == 1


def test_reply_idempotency_key_rejects_changed_body(db_session, monkeypatch):
    conversation_id = _conversation(db_session)

    def fake_send(db, *, conversation, payload, record_failure):
        message = InboxMessage(
            conversation_id=conversation.id,
            channel_type="email",
            direction="outbound",
            body=payload.body_text,
            from_address="support@example.test",
            to_addresses=[conversation.contact_address],
            cc_addresses=[],
            metadata_={
                **dict(payload.metadata or {}),
                "body_text": payload.body_text,
                "delivery_status": "queued",
            },
        )
        db.add(message)
        db.flush()
        return team_inbox_outbound.InboxReplyResult(
            kind="queued",
            conversation_id=str(conversation.id),
            message_id=str(message.id),
        )

    monkeypatch.setattr(
        team_inbox_commands.team_inbox_outbound,
        "send_inbox_reply",
        fake_send,
    )
    team_inbox_commands.reply(
        db_session,
        command=team_inbox_commands.ReplyCommand(
            conversation_id=conversation_id,
            body_text="Original",
            actor_person_id=uuid.uuid4(),
            idempotency_key="send-key-2",
        ),
    )

    with pytest.raises(
        team_inbox_commands.InboxCommandRejected,
        match="different reply",
    ):
        team_inbox_commands.reply(
            db_session,
            command=team_inbox_commands.ReplyCommand(
                conversation_id=conversation_id,
                body_text="Changed",
                actor_person_id=uuid.uuid4(),
                idempotency_key="send-key-2",
            ),
        )


def test_start_conversation_keeps_whatsapp_template_values_and_uploads(
    db_session,
    monkeypatch,
):
    from app.services.integrations import whatsapp_capability

    template = InboxMessageTemplate(
        name="Welcome",
        channel_type="whatsapp",
        body_text="Hello {{1}}",
        metadata_={
            "provider_template_name": "welcome_customer",
            "provider_template_language": "en",
        },
    )
    db_session.add(template)
    db_session.commit()
    captured: dict[str, object] = {}
    asset_id = uuid.uuid4()

    def fake_stage(db, *, conversation, file_name, content_type, data, uploaded_by):
        captured["upload"] = (file_name, content_type, data, uploaded_by)
        return SimpleNamespace(id=asset_id)

    def fake_bind(db, *, message, asset_ids):
        captured["asset_ids"] = tuple(asset_ids)
        return []

    def fake_send(db, *, conversation, payload, record_failure):
        captured["payload"] = payload
        message = InboxMessage(
            conversation_id=conversation.id,
            channel_type="whatsapp",
            direction="outbound",
            body=payload.body_text,
            metadata_={"delivery_status": "queued"},
        )
        db.add(message)
        db.flush()
        return team_inbox_outbound.InboxReplyResult(
            kind="queued",
            conversation_id=str(conversation.id),
            message_id=str(message.id),
        )

    monkeypatch.setattr(
        team_inbox_commands.team_inbox_media,
        "stage_outbound_attachment",
        fake_stage,
    )
    monkeypatch.setattr(
        team_inbox_commands.team_inbox_media,
        "bind_assets_to_message",
        fake_bind,
    )
    monkeypatch.setattr(
        team_inbox_commands.team_inbox_outbound,
        "send_inbox_reply",
        fake_send,
    )
    monkeypatch.setattr(
        whatsapp_capability,
        "list_approved_templates",
        lambda _db: (
            {
                "name": "welcome_customer",
                "language": "en",
                "status": "APPROVED",
                "components": [],
            },
        ),
    )
    components = (
        {
            "type": "body",
            "parameters": [{"type": "text", "text": "Ada"}],
        },
    )

    team_inbox_commands.start_conversation(
        db_session,
        channel_type="whatsapp",
        contact_address="+2348000000000",
        body_text="",
        template_id=template.id,
        template_values=("Ada",),
        whatsapp_template_name="welcome_customer",
        whatsapp_template_language="en",
        whatsapp_template_components=components,
        uploads=(("proof.png", "image/png", b"png"),),
        actor_person_id=uuid.uuid4(),
    )

    payload = captured["payload"]
    assert payload.body_text == "Hello {{1}}"
    assert payload.metadata["whatsapp_template"] == {
        "name": "welcome_customer",
        "language": "en",
        "components": list(components),
        "variables": {},
        "inbox_template_id": str(template.id),
    }
    assert captured["asset_ids"] == (str(asset_id),)


def test_only_saved_view_owner_can_delete(db_session):
    owner_id = uuid.uuid4()
    saved_filter = InboxSavedFilter(
        name="My queue",
        owner_person_id=owner_id,
        filter_payload={"open_only": True},
    )
    db_session.add(saved_filter)
    db_session.flush()
    saved_filter_id = saved_filter.id
    db_session.commit()

    with pytest.raises(team_inbox_commands.InboxCommandRejected):
        team_inbox_commands.delete_filter(
            db_session,
            filter_id=saved_filter_id,
            actor_person_id=uuid.uuid4(),
        )

    team_inbox_commands.delete_filter(
        db_session,
        filter_id=saved_filter_id,
        actor_person_id=owner_id,
    )
    assert db_session.get(InboxSavedFilter, saved_filter_id).is_active is False


def test_bulk_priority_action_uses_existing_command_owner(db_session):
    conversation_id = _conversation(db_session)

    outcome = team_inbox_commands.bulk_action(
        db_session,
        conversation_ids=[conversation_id],
        action="priority",
        priority=25,
        actor_person_id=uuid.uuid4(),
    )

    conversation = db_session.get(InboxConversation, conversation_id)
    assert outcome.message == "Updated priority for 1 conversations."
    assert conversation.priority == 25
    assert conversation.metadata_["priority_history"][0]["to"] == 25


def test_create_lead_from_unmatched_conversation_is_idempotent(db_session):
    conversation_id = _conversation(db_session)

    first = team_inbox_commands.create_lead_from_conversation(
        db_session,
        conversation_id=conversation_id,
        actor_person_id=uuid.uuid4(),
    )
    second = team_inbox_commands.create_lead_from_conversation(
        db_session,
        conversation_id=conversation_id,
        actor_person_id=uuid.uuid4(),
    )

    conversation = db_session.get(InboxConversation, conversation_id)
    lead = db_session.get(Lead, first.lead_id)
    origin = db_session.query(LeadOriginCapture).one()
    assert second.replayed is True
    assert second.lead_id == first.lead_id
    assert lead is not None
    assert lead.party_id is not None
    assert lead.subscriber_id is None
    assert origin.source_interaction_id == f"team-inbox:{conversation_id}"
    assert conversation.metadata_["lead_capture"]["lead_id"] == first.lead_id
    assert db_session.query(Lead).count() == 1


def test_merge_contact_to_customer_captures_and_attaches_lead(db_session):
    conversation_id = _conversation(db_session)
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Customer",
        email="ada-merge@example.test",
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add(subscriber)
    db_session.flush()
    subscriber_id = subscriber.id
    db_session.commit()

    outcome = team_inbox_commands.merge_contact(
        db_session,
        conversation_id=conversation_id,
        target_type="subscriber",
        target_query="ada-merge@example.test",
        actor_person_id=uuid.uuid4(),
    )

    lead = db_session.query(Lead).one()
    conversation = db_session.get(InboxConversation, conversation_id)
    subscriber = db_session.get(Subscriber, subscriber_id)
    assert outcome.target_type == "subscriber"
    assert outcome.target_id == str(subscriber_id)
    assert lead.subscriber_id == subscriber_id
    assert subscriber.party_id == lead.party_id
    assert conversation.subscriber_id == subscriber_id
    assert (
        conversation.metadata_["lead_capture"]["merge"]["target_type"] == "subscriber"
    )
