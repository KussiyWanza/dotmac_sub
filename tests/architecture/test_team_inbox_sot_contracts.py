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


def test_projection_owns_response_cohorts_from_authoritative_inputs() -> None:
    service = service_relationship("communications.team_inbox_projection")
    assert service.contract is not None
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


def test_inbox_services_have_no_transport_errors_or_direct_completion() -> None:
    offenders: list[str] = []
    for path in (ROOT / "app/services").glob("team_inbox_*.py"):
        source = path.read_text(encoding="utf-8")
        for token in ("HTTPException", ".commit(", ".rollback(", "begin_nested"):
            if token in source:
                offenders.append(f"{path.name}: {token}")
    assert not offenders


def test_webhook_adapters_do_not_copy_raw_provider_payloads() -> None:
    for relative in ("app/api/inbox_webhooks.py", "app/api/meta_inbox_webhooks.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert '"raw":' not in source
        assert "record_provider_observation" not in source
    receiver = (ROOT / "app/services/team_inbox_channel_receive.py").read_text()
    assert "receive_whatsapp_webhook_batch_committed" in receiver
