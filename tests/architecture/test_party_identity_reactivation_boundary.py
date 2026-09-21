from pathlib import Path

from app.services.sot_registry import registry

ROOT = Path(__file__).resolve().parents[2]


def test_party_identity_reactivation_has_complete_registered_contract():
    service = registry.service_relationship("party.identity_reactivation")
    assert service.module == "app.services.party_identity_reactivation"
    assert service.is_contracted is True
    assert service.contract is not None
    assert service.contract.transaction.mode.value == "owner_managed"
    assert service.contract.concerns[0].canonical_writer == service.name


def test_operator_adapter_delegates_without_direct_transaction_or_orm_mutation():
    source = (
        ROOT / "scripts" / "migration" / "reactivate_party_identity.py"
    ).read_text(encoding="utf-8")
    assert "reactivate_quarantined_party(" in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert ".status =" not in source
    assert "session.query(" not in source


def test_legacy_registry_has_no_party_reactivation_writer():
    source = (ROOT / "app" / "services" / "party.py").read_text(encoding="utf-8")
    assert "def reactivate_party" not in source
    assert "def restore_party" not in source


def test_party_active_status_transition_has_one_service_writer():
    needle = "party.status = PartyIdentityStatus.active.value"
    writers = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "app" / "services").rglob("*.py")
        if needle in path.read_text(encoding="utf-8")
    }
    assert writers == {"app/services/party_identity_reactivation.py"}
