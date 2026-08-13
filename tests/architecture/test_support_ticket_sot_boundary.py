from pathlib import Path

from app.services.sot_relationships import all_services

ROOT = Path(__file__).resolve().parents[2]

MIGRATED_MODULES = (
    "app/services/support.py",
    "app/services/support_automation.py",
    "app/services/support_automation_rules.py",
    "app/services/support_ticket_settings.py",
    "app/services/support_ticket_region_projection.py",
    "app/services/ticket_assignment/admin.py",
    "app/services/ticket_assignment/engine.py",
    "app/services/ticket_assignment/selectors.py",
    "app/services/ticket_work_order_handoff.py",
    "app/services/ticket_validation.py",
    "app/services/web_support_ticket_bulk.py",
    "app/services/web_support_tickets.py",
)

CONTRACTED_OWNERS = {
    "support.ticket_lifecycle",
    "support.ticket_vocabulary",
    "support.ticket_configuration",
    "support.ticket_region_projection",
    "support.ticket_sla_clock",
    "support.ticket_work_order_handoff",
    "support.ticket_bulk_commands",
    "support.ticket_assignment_rule_configuration",
    "support.ticket_assignment_evaluation",
    "support.ticket_automation_rule_configuration",
    "support.ticket_automation_evaluation",
    "ui.support_ticket_list_projection",
    "ui.support_ticket_bulk_action_projection",
}
SERVICES_BY_NAME = {service.name: service for service in all_services()}


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_support_services_have_complete_registered_contracts() -> None:
    assert CONTRACTED_OWNERS <= SERVICES_BY_NAME.keys()
    for name in CONTRACTED_OWNERS:
        assert SERVICES_BY_NAME[name].contract is not None, name


def test_customer_publication_is_owned_and_legacy_visibility_is_reconciled() -> None:
    contract = SERVICES_BY_NAME["support.ticket_lifecycle"].contract
    assert contract is not None
    assert any(
        concern.name == "ticket customer publication visibility"
        for concern in contract.concerns
    )

    migration = _source("alembic/versions/503_reconcile_ticket_portal_visibility.py")
    assert '"description_is_internal"' in migration
    assert "UPDATE support_ticket_comments" in migration
    assert "SET is_internal = true" in migration
    assert "server_default=sa.true()" in migration
    lifecycle = _source("app/services/support.py")
    assert (
        lifecycle.count('"description_is_internal": ticket.description_is_internal')
        >= 3
    )
    assert "is_internal=True" in _source("app/services/crm_ticket_pull.py")
    assert (
        ROOT / "docs/runbooks/SUPPORT_TICKET_PORTAL_VISIBILITY_RECONCILIATION.md"
    ).exists()


def test_migrated_support_services_do_not_own_transport_or_transactions() -> None:
    forbidden = ("HTTPException", ".commit(", ".rollback(", ".begin_nested(")
    for relative_path in MIGRATED_MODULES:
        source = _source(relative_path)
        for token in forbidden:
            assert token not in source, f"{relative_path} contains {token}"


def test_shared_audit_helper_is_flush_only_inside_owner_commands() -> None:
    source = _source("app/services/audit_helpers.py")
    assert "if owner_command_active(db):" in source
    assert "stage_audit_event(" in source


def test_support_legacy_contract_and_writer_baselines_shrank() -> None:
    manifest = _source("tests/architecture/sot_manifest_legacy_baseline.txt")
    writers = _source("tests/architecture/sot_writer_baseline.txt")
    for name in CONTRACTED_OWNERS:
        assert name not in manifest
    for module in (
        "app.services.support_automation",
        "app.services.ticket_assignment.admin",
        "app.services.ticket_assignment.engine",
        "app.services.ticket_assignment.selectors",
    ):
        assert module not in writers


def test_assignment_and_automation_policies_do_not_write_ticket_lifecycle() -> None:
    assignment = _source("app/services/ticket_assignment/engine.py")
    automation = _source("app/services/support_automation.py")
    assert "ticket.assigned_to_person_id =" not in assignment
    assert "ticket.status =" not in assignment
    assert "ticket.service_team_id =" not in assignment
    assert "ticket.status =" not in automation
    assert "ticket.priority =" not in automation
    assert "ticket.service_team_id =" not in automation
    lifecycle = _source("app/services/support.py")
    assert "evaluate_rules(db, ticket, trigger)" in lifecycle
    assert "auto_assign_ticket(" in assignment


def test_portal_ticket_routing_stays_in_configuration_and_lifecycle_owners() -> None:
    configuration = _source("app/services/support_ticket_settings.py")
    lifecycle = _source("app/services/support.py")
    portal = _source("app/web/customer/routes.py")

    assert "class SupportTeamRoutingResolution" in configuration
    assert 'CUSTOMER_EXPERIENCE_TEAM_NAME = "Customer Experience"' in configuration
    assert 'SYSTEM_ADMIN_TEAM_NAME = "System Admin"' in configuration
    assert "func.lower(ServiceTeam.name) == team_name.lower()" in configuration
    assert "class TicketCreationRoutingMode" in lifecycle
    assert "preserve_requested_team" in lifecycle
    assert "resolve_portal_ticket_team_routing(db)" in portal
    assert "TicketCreationRoutingMode.preserve_requested_team" in _source(
        "app/services/crm_portal.py"
    )


def test_operator_status_subset_is_owned_by_ticket_configuration() -> None:
    configuration = _source("app/services/support_ticket_settings.py")
    web_projection = _source("app/services/web_support_tickets.py")
    admin_adapter = _source("app/web/admin/support_tickets.py")
    list_template = _source("templates/admin/support/tickets/_list.html")
    form_template = _source("templates/admin/support/tickets/new.html")

    assert "DEFAULT_STATUS_OPTIONS = [status.value for status in TicketStatus]" in (
        configuration
    )
    assert "parse_ticket_status(normalized).value" in configuration
    assert "class OperatorTicketStatusSelection" in configuration
    assert "class OperatorTicketStatusSelectionOutcome" in configuration
    assert "def resolve_operator_ticket_status_selection(" in configuration
    assert "list_status_options(db)" in web_projection
    assert "support_ticket_status_not_selectable" in web_projection
    assert "WebSupportTicketInputError" in admin_adapter
    assert "s != 'resolved'" not in list_template
    assert "s != 'resolved'" not in form_template


def test_ticket_region_projection_has_one_typed_owner() -> None:
    configuration = _source("app/services/support_ticket_settings.py")
    lifecycle = _source("app/services/support.py")
    projection = _source("app/services/support_ticket_region_projection.py")

    assert (
        "support_ticket_region_projection.list_canonical_region_options"
        in configuration
    )
    assert "configured_regions: tuple[str, ...]" in projection
    assert "func.lower(func.trim(Ticket.region))" in projection
    assert "normalize_region_value" in projection
    assert ".order_by(region_sources.c.region.asc())" in projection
    assert "func.lower(func.trim(Ticket.region)) == normalized_region" in lifecycle
    assert "db.query(Ticket.region)" not in configuration


def test_customer_reply_staff_email_stays_in_ticket_lifecycle_owner() -> None:
    lifecycle = _source("app/services/support.py")
    assert "class CustomerReplyStaffNotificationOutcome" in lifecycle
    assert "def _notify_staff_of_customer_comment" in lifecycle
    assert "CustomerReplyStaffNotificationSource.helpdesk_fallback" in lifecycle
    assert "Tickets._notify_staff_of_customer_comment(db, ticket, comment)" in lifecycle
    for adapter in (
        "app/api/me.py",
        "app/api/support.py",
        "app/services/crm_portal.py",
        "app/services/web_support_tickets.py",
    ):
        assert "queue_staff_email" not in _source(adapter)


def test_admin_ticket_creation_customer_email_stays_in_lifecycle_owner() -> None:
    lifecycle = _source("app/services/support.py")
    admin_adapter = _source("app/services/web_support_tickets.py")
    contract = SERVICES_BY_NAME["support.ticket_lifecycle"].contract

    assert contract is not None
    acknowledgement = next(
        concern
        for concern in contract.concerns
        if concern.name == "admin-created ticket customer email acknowledgement"
    )
    assert acknowledgement.input_names == (
        "typed ticket command",
        "canonical ticket state",
        "customer identity evidence",
        "customer communication delivery intent",
    )
    assert "class TicketCreationAcknowledgementMode" in lifecycle
    assert "_stage_admin_creation_customer_email(" in lifecycle
    assert 'event_type="support_ticket_created_admin"' in lifecycle
    assert "TicketCreationAcknowledgementMode.customer_email" in admin_adapter
    assert "default_channels=(NotificationChannel.email,)" in lifecycle


def test_unmatched_radio_creation_delegates_to_silent_ticket_participant() -> None:
    lifecycle = _source("app/services/support.py")
    queue = _source("app/services/unmatched_radio_queue.py")

    assert "class TicketCreationConsequenceMode" in lifecycle
    assert "stage_internal_creation_participant" in lifecycle
    assert "stage_internal_observation_participant" in lifecycle
    assert "Ticket(" not in queue
    assert "stage_internal_creation_participant(" in queue
    assert "stage_internal_observation_participant(" in queue

    allowed = {
        "app/services/support.py",
        "app/services/unmatched_radio_queue.py",
    }
    for path in (ROOT / "app" / "services").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        assert "stage_internal_creation_participant(" not in source, relative
        assert "stage_internal_observation_participant(" not in source, relative


def test_ticket_work_order_field_results_cannot_close_ticket() -> None:
    source = _source("app/services/ticket_work_order_handoff.py")
    assert "execute_owner_command(" in source
    assert "support:ticket:update" in source
    assert "operations:dispatch:write" in source
    assert "ticket.status =" not in source
    assert "transition_ticket_status" not in source


def test_historical_crm_provenance_has_gated_backfill_and_runbook() -> None:
    migration = _source("alembic/versions/406_support_ticket_work_order_provenance.py")
    assert "crm_ticket_id" in migration
    assert "origin_ticket_id" in migration
    assert "raise RuntimeError" in migration
    assert "UPDATE work_order" in migration
    assert "crm_ticket_id = NULL" not in migration
    assert (ROOT / "docs/runbooks/TICKET_WORK_ORDER_PROVENANCE_CUTOVER.md").exists()


def test_retired_ticket_owner_is_not_registered() -> None:
    assert "support.tickets" not in SERVICES_BY_NAME
    assert "support.ticket_lifecycle" in SERVICES_BY_NAME
