"""Pin customer notification policy to its typed read-only owner boundary."""

from pathlib import Path

from app.services.sot_manifest import (
    OwnerRole,
    TransactionMode,
    contract_validation_errors,
)
from app.services.sot_relationships import all_services, service_relationship

ROOT = Path(__file__).resolve().parents[2]


def test_customer_notification_policy_has_complete_read_only_contract() -> None:
    service = service_relationship("communications.customer_policy")

    assert service.module == "app.services.customer_notification_policy"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.READ_ONLY
    assert {concern.role for concern in service.contract.concerns} == {OwnerRole.POLICY}
    assert (
        contract_validation_errors(
            service,
            service_names={item.name for item in all_services()},
        )
        == ()
    )
    baseline = (ROOT / "tests/architecture/sot_manifest_legacy_baseline.txt").read_text(
        encoding="utf-8"
    )
    assert "communications.customer_policy" not in baseline.splitlines()


def test_bulk_preview_adapter_does_not_define_a_parallel_intent_writer() -> None:
    web_source = (ROOT / "app/services/web_customer_actions.py").read_text(
        encoding="utf-8"
    )
    intent_source = (ROOT / "app/services/communication_intents.py").read_text(
        encoding="utf-8"
    )

    assert "submit_bulk_customer_messages" not in web_source
    assert "BulkCustomerMessageIntent" not in web_source
    assert "submit_bulk_customer_messages" not in intent_source
    assert "queue_policy_checked_customer_delivery" not in intent_source
