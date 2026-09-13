"""Pins the completed Team Inbox owner family and retired parallel paths."""

from __future__ import annotations

from pathlib import Path

from app.services.sot_manifest import TransactionMode, contract_validation_errors
from app.services.sot_relationships import all_services, service_relationship

ROOT = Path(__file__).resolve().parents[2]

OWNERS = {
    "communications.team_inbox_observations": TransactionMode.OWNER_MANAGED,
    "communications.team_inbox_processing": TransactionMode.COORDINATOR_MANAGED,
    "communications.team_inbox_threads": TransactionMode.PARTICIPANT,
    "communications.team_inbox_contact_resolution": TransactionMode.OWNER_MANAGED,
    "communications.team_inbox_routing": TransactionMode.OWNER_MANAGED,
    "communications.team_inbox_operator_state": TransactionMode.OWNER_MANAGED,
    "communications.team_inbox_outbound_intents": TransactionMode.OWNER_MANAGED,
    "communications.team_inbox_delivery_receipts": TransactionMode.PARTICIPANT,
    "communications.team_inbox_commands": TransactionMode.COORDINATOR_MANAGED,
    "communications.team_inbox_widget": TransactionMode.OWNER_MANAGED,
    "communications.team_inbox_projection": TransactionMode.READ_ONLY,
    "communications.team_inbox_metrics": TransactionMode.READ_ONLY,
    "communications.team_inbox_maintenance": TransactionMode.OWNER_MANAGED,
    "communications.team_inbox_realtime": TransactionMode.NOT_APPLICABLE,
    "communications.team_inbox_smtp_transport": TransactionMode.NOT_APPLICABLE,
    "communications.team_inbox_health": TransactionMode.OWNER_MANAGED,
    "communications.team_inbox_campaigns": TransactionMode.PARTICIPANT,
    "communications.team_inbox_participants": TransactionMode.PARTICIPANT,
}


def test_team_inbox_owner_family_has_complete_typed_contracts() -> None:
    service_names = {service.name for service in all_services()}
    for name, transaction_mode in OWNERS.items():
        service = service_relationship(name)
        assert service.contract is not None
        assert not contract_validation_errors(service, service_names=service_names)
        assert service.contract.transaction.mode is transaction_mode
        assert service.contract.authoritative_inputs
        assert service.contract.errors.domain_codes
        assert service.contract.design_refs
        assert service.contract.test_refs


def test_observation_owner_contracts_collision_quarantine() -> None:
    service = service_relationship("communications.team_inbox_observations")

    assert "provider observation identity collision quarantine" in service.owns
    assert service.contract is not None
    assert (
        "communications.team_inbox_observations.provider_event_identity_collision"
        in service.contract.errors.domain_codes
    )

    model = (ROOT / "app/models/team_inbox.py").read_text(encoding="utf-8")
    owner = (ROOT / "app/services/team_inbox_observations.py").read_text(
        encoding="utf-8"
    )
    smtp = (ROOT / "app/services/team_inbox_smtp_inbound.py").read_text(
        encoding="utf-8"
    )
    assert "class InboxProviderObservationCollision" in model
    assert "SEMANTIC_FINGERPRINT_VERSION = 2" in owner
    assert "ObservationCollisionPolicy.quarantine" in smtp


def test_routing_owner_contracts_signed_in_agent_presence() -> None:
    service = service_relationship("communications.team_inbox_routing")
    assert service.contract is not None
    assert {
        "current agent presence state and freshness",
        "agent presence transitions",
        "immutable agent presence transition evidence",
    } <= set(service.owns)
    input_owners = {
        item.name: item.owner for item in service.contract.authoritative_inputs
    }
    assert input_owners["successful staff session issuance"] == "app_sessions.auth"
    assert input_owners["active staff principal"] == "auth.staff_provisioning"

    auth_flow = (ROOT / "app/services/auth_flow.py").read_text(encoding="utf-8")
    projection = (ROOT / "app/services/team_inbox_projection.py").read_text(
        encoding="utf-8"
    )
    assert "AgentSignedInPresenceCommand(" in auth_flow
    assert "record_agent_signed_in_presence(" in auth_flow
    assert "InboxAgentPresence(" not in auth_flow
    assert "agent_availability_snapshots(" in projection


def test_queue_correctness_contracts_are_explicit_and_provider_preflighted() -> None:
    routing = service_relationship("communications.team_inbox_routing")
    notifications = service_relationship(
        "communications.team_inbox_queue_notifications"
    )
    assert {
        "strict per-team FIFO head serialization",
        "current customer-visible queue position projection",
        "global per-agent active assignment capacity enforcement",
    } <= set(routing.owns)
    assert {
        "queue notification lifecycle deduplication and suppression",
        "queue notification provider-dispatch validity decision",
    } <= set(notifications.owns)

    assignment = (ROOT / "app/services/team_inbox_assignment.py").read_text(
        encoding="utf-8"
    )
    notification_owner = (
        ROOT / "app/services/team_inbox_queue_notifications.py"
    ).read_text(encoding="utf-8")
    delivery_adapter = (ROOT / "app/tasks/notifications.py").read_text(encoding="utf-8")
    assert "def _lock_agent_capacity(" in assignment
    assert "def _try_lock_team(" in assignment
    assert "queue-position:{entry.id}:{lifecycle}:{position}" in notification_owner
    assert "preflight_queue_notification_delivery(" in delivery_adapter

    assignment_constructors = []
    for path in (ROOT / "app").rglob("*.py"):
        if path == ROOT / "app/models/team_inbox.py":
            continue
        if "InboxConversationAssignment(" in path.read_text(encoding="utf-8"):
            assignment_constructors.append(path.relative_to(ROOT).as_posix())
    assert assignment_constructors == ["app/services/team_inbox_assignment.py"]


def test_legacy_catch_all_is_retired() -> None:
    baseline = (
        ROOT / "tests/architecture/sot_manifest_legacy_baseline.txt"
    ).read_text()
    assert "communications.team_inbox\n" not in baseline
    assert "communications.team_inbox_campaigns\n" not in baseline
    try:
        service_relationship("communications.team_inbox")
    except KeyError:
        pass
    else:
        raise AssertionError("legacy Team Inbox catch-all returned to the registry")


def test_admin_route_delegates_query_contract_and_transactions() -> None:
    route = (ROOT / "app/web/admin/inbox.py").read_text(encoding="utf-8")
    assert "INBOX_LIST_DEFINITION" not in route
    assert "build_queue_projection" in route
    assert "get_conversation_projection" in route
    assert ".commit(" not in route
    assert ".rollback(" not in route


def test_representative_customer_path_is_conversation_scoped() -> None:
    contact_owner = (ROOT / "app/services/team_inbox_contact_links.py").read_text(
        encoding="utf-8"
    )
    start = contact_owner.index("def associate_represented_customer(")
    end = contact_owner.index("\ndef bind_contact_link_party_contact_point(", start)
    representative_path = contact_owner[start:end]
    coordinator = (ROOT / "app/services/team_inbox_commands.py").read_text(
        encoding="utf-8"
    )
    model = (ROOT / "app/models/team_inbox.py").read_text(encoding="utf-8")
    template = (ROOT / "templates/admin/inbox/_authoritative_context.html").read_text(
        encoding="utf-8"
    )

    assert 'representative = "representative"' in model
    assert "AssociateRepresentedCustomerCommand" in representative_path
    assert "team_inbox_participants.mark_representative(" in representative_path
    assert "conversation.subscriber_id = subscriber.id" in representative_path
    assert "InboxContactLink(" not in representative_path
    assert "link_conversation_contact(" not in representative_path
    assert "historical_rows" not in representative_path
    assert "command: LinkRepresentedCustomerCommand" in coordinator
    assert "command: LinkRepresentedLeadCommand" in coordinator
    assert "conversation_lead_relationships.link_conversation_lead_participant(" in (
        coordinator
    )
    assert 'action="inbox_represented_customer_selected"' in coordinator
    assert (
        'action="/admin/inbox/{{ contact_context.conversation_id }}/represented-customer"'
        in template
    )
    assert (
        'action="/admin/inbox/{{ contact_context.conversation_id }}/represented-lead"'
        in template
    )
    assert 'data-typeahead-url="/api/v1/search/subscribers"' in template
    assert 'data-typeahead-url="/admin/inbox/search/leads"' in template


def test_projection_owns_response_cohorts_from_authoritative_inputs() -> None:
    service = service_relationship("communications.team_inbox_projection")
    assert service.contract is not None
    assert "Inbox email recipient envelope projection" in {
        concern.name for concern in service.contract.concerns
    }
    input_owners = {
        item.name: item.owner for item in service.contract.authoritative_inputs
    }
    assert input_owners["conversation records"] == ("communications.team_inbox_threads")
    assert input_owners["delivery projection"] == (
        "communications.team_inbox_delivery_receipts"
    )
    assert input_owners["ticket handoff provenance"] == (
        "communications.conversation_ticket_handoff"
    )
    assert input_owners["active service-team selector projection"] == (
        "operations.service_team_lifecycle"
    )
    assert (
        "communications.team_inbox_projection.invalid_filter"
        in service.contract.errors.domain_codes
    )

    model = (ROOT / "app/models/team_inbox.py").read_text(encoding="utf-8")
    projection = (ROOT / "app/services/team_inbox_read.py").read_text(encoding="utf-8")
    assert "needs_attention" not in model
    assert "class InboxResponseCohort" in projection
    assert "def response_cohort" in projection
    assert "bcc_addresses" in projection


def test_metrics_owner_is_bounded_set_based_and_registered() -> None:
    service = service_relationship("communications.team_inbox_metrics")
    assert service.contract is not None
    assert {
        "bounded Inbox team performance report projection",
        "bounded Inbox agent performance report projection",
        "current Inbox escalation candidate projection",
    } == set(service.owns)
    assert {item.owner for item in service.contract.authoritative_inputs} >= {
        "communications.team_inbox_threads",
        "communications.team_inbox_routing",
        "operations.service_team_lifecycle",
        "operations.service_team_composition",
        "auth.staff_provisioning",
    }

    owner = (ROOT / "app/services/team_inbox_metrics.py").read_text(encoding="utf-8")
    assert "DEFAULT_PERFORMANCE_WINDOW_DAYS = 30" in owner
    assert "MAX_PERFORMANCE_WINDOW_DAYS = 366" in owner
    assert "def team_performance_page(" in owner
    assert "def escalation_page(" in owner
    assert "team_capacity_snapshots(" in owner
    assert "db.query(InboxMessage)" not in owner


def test_operator_unread_projection_is_set_based_and_indexed() -> None:
    owner = (ROOT / "app/services/team_inbox_read_state.py").read_text(encoding="utf-8")
    model = (ROOT / "app/models/team_inbox.py").read_text(encoding="utf-8")
    migration = (
        ROOT / "alembic/versions/513_team_inbox_unread_query_indexes.py"
    ).read_text(encoding="utf-8")

    assert "def unread_conversation_clause" not in owner
    assert ".correlate(InboxConversation)" not in owner
    assert "def unread_conversation_ids_select" in owner
    assert "ix_inbox_messages_unread" in model
    assert "ix_inbox_read_states_person_conversation" in model
    assert "CREATE INDEX CONCURRENTLY" in migration
    assert (
        'down_revision: str | None = "512_open_setting_value_type_vocabulary"'
        in migration
    )


def test_inbox_services_have_no_transport_errors_or_direct_completion() -> None:
    offenders: list[str] = []
    for path in (ROOT / "app/services").glob("team_inbox_*.py"):
        source = path.read_text(encoding="utf-8")
        for token in ("HTTPException", ".commit(", ".rollback(", "begin_nested"):
            if token in source:
                offenders.append(f"{path.name}: {token}")
    assert not offenders


def test_webhook_adapters_do_not_copy_raw_provider_payloads() -> None:
    for relative in (
        "app/api/inbox_webhooks.py",
        "app/api/meta_inbox_webhooks.py",
        "app/api/fiber_inquiry_webhooks.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert '"raw":' not in source
        assert "record_provider_observation" not in source
    receiver = (ROOT / "app/services/team_inbox_channel_receive.py").read_text()
    assert "receive_whatsapp_webhook_batch_committed" in receiver
