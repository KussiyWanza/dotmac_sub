"""Pin payment-proof review alerts to the staff-notification owner."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services import sot_relationships
from app.services.sot_manifest import TransactionMode, contract_validation_errors

ROOT = Path(__file__).resolve().parents[2]


def _calls(path: Path, owner_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == owner_name
    }


def test_payment_proof_owner_requests_and_resolves_staff_review_notifications() -> None:
    path = ROOT / "app/services/payment_proofs.py"
    calls = _calls(path, "staff_notifications")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    referenced_names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }

    assert "queue_permission_review_request" in calls
    assert "resolve_permission_review_request" in calls
    assert "AdminAlert" not in referenced_names
    assert "AdminNotification" not in referenced_names


def test_payment_proof_owner_has_a_complete_owner_managed_contract() -> None:
    service = sot_relationships.service_relationship("financial.payment_proofs")
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert (
        contract_validation_errors(
            service,
            service_names={item.name for item in sot_relationships.all_services()},
        )
        == ()
    )


def test_payment_proof_commands_are_transport_neutral_and_complete_once() -> None:
    path = ROOT / "app/services/payment_proofs.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    public_commands = {
        node.name: {argument.arg for argument in node.args.kwonlyargs}
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name
        in {
            "submit_proof",
            "submit_direct_transfer_proof",
            "verify_proof",
            "reject_proof",
            "correct_duplicate_payment_proof",
        }
    }

    assert set(public_commands) == {
        "submit_proof",
        "submit_direct_transfer_proof",
        "verify_proof",
        "reject_proof",
        "correct_duplicate_payment_proof",
    }
    assert all("context" in arguments for arguments in public_commands.values())
    assert source.count("execute_owner_command(") == 5
    assert "commit" not in calls
    assert "rollback" not in calls
    assert "begin_nested" not in calls
    assert "HTTPException" not in source
    assert "fastapi" not in source


def test_payment_proof_owner_stages_named_transition_events() -> None:
    source = (ROOT / "app/services/payment_proofs.py").read_text(encoding="utf-8")
    event_types = (ROOT / "app/services/events/types.py").read_text(encoding="utf-8")

    for event_name in (
        "payment_proof.submitted",
        "payment_proof.verified",
        "payment_proof.rejected",
        "payment_proof.corrected",
        "withholding_tax.receivable_recorded",
    ):
        assert event_name in event_types
    assert "emit_event(" in source


def test_tax_owner_is_the_only_wht_source_record_writer() -> None:
    owner = sot_relationships.owning_service_for(
        "withholding-tax receivable source records"
    )
    payment_source = (ROOT / "app/services/payment_proofs.py").read_text(
        encoding="utf-8"
    )

    assert owner is not None
    assert owner.name == "financial.tax_accounting"
    assert "tax_accounting.stage_withholding_tax_receivable" in payment_source
    assert "WithholdingTaxRecord(" not in payment_source


def test_payment_proof_http_error_mapping_has_one_adapter_owner() -> None:
    api_source = (ROOT / "app/api/payment_proofs.py").read_text(encoding="utf-8")
    web_source = (ROOT / "app/web/admin/billing_payment_proofs.py").read_text(
        encoding="utf-8"
    )

    assert "from app.api.payment_proof_errors import" in api_source
    assert "from app.api.payment_proof_errors import" in web_source
    assert "def _payment_proof_http_error" not in api_source
    assert "def _payment_proof_error_status" not in web_source


def test_staff_notification_owner_is_registered_for_permission_targeting() -> None:
    owner = sot_relationships.owning_service_for(
        "permission-targeted staff notification audience resolution"
    )

    assert owner is not None
    assert owner.name == "communications.staff_notifications"
    assert owner.module == "app.services.staff_notifications"


def test_admin_inbox_links_use_the_user_scoped_open_boundary() -> None:
    template = (ROOT / "templates/admin/partials/notifications_menu.html").read_text(
        encoding="utf-8"
    )

    assert "/admin/notifications/inbox/{{ notification.id }}/open" in template
    assert "/admin/alerts/notifications/{{ notification.id }}/open" not in template


def test_payment_proof_escalation_is_keyed_to_the_generic_sla_owner() -> None:
    payment_source = (ROOT / "app/services/payment_proofs.py").read_text(
        encoding="utf-8"
    )
    staff_source = (ROOT / "app/services/staff_notifications.py").read_text(
        encoding="utf-8"
    )
    sla_ui_source = (ROOT / "app/services/web_notifications_sla_policies.py").read_text(
        encoding="utf-8"
    )

    assert '_REVIEW_SLA_TRIGGER = "payment_proof.review_requested"' in payment_source
    assert "sla_trigger=_REVIEW_SLA_TRIGGER" in payment_source
    assert "operational_escalation.matching_policies" in staff_source
    assert (
        "NotificationChannel.email"
        not in staff_source.split("def queue_permission_review_request", 1)[1].split(
            "def resolve_permission_review_request", 1
        )[0]
    )
    assert "OPERATIONAL_ENTITY_TYPES" in sla_ui_source
