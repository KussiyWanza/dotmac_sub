"""Static ownership guard for the Inbox Lead intake slice."""

from pathlib import Path

from app.services.sot_relationships import all_services


def test_lead_intake_owner_has_complete_manifest_contract():
    service = next(item for item in all_services() if item.name == "sales.lead_intake")
    assert service.module == "app.services.sales.lead_intake"
    assert service.contract is not None
    assert service.contract.transaction.mode.value == "coordinator_managed"
    assert {item.name for item in service.contract.concerns} == set(service.owns)


def test_lead_intake_adapters_do_not_construct_owned_records():
    for path in (
        Path("app/web/public/lead_intake.py"),
        Path("app/web/admin/lead_intake.py"),
        Path("app/services/lead_intake_ai.py"),
    ):
        source = path.read_text(encoding="utf-8")
        assert "LeadIntakeInvitation(" not in source
        assert "LeadIntakeTemplate(" not in source
        assert "Lead(" not in source
        assert "Party(" not in source
