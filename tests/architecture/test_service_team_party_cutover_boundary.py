from pathlib import Path

from app.services.sot_manifest import OwnerRole, TransactionMode
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_cutover_coordinator_has_complete_typed_contract() -> None:
    service = service_relationship("operations.service_team_party_cutover")
    assert service.module == "app.services.service_team_party_cutover"
    assert service.contract is not None
    concerns = {item.name: item.role for item in service.contract.concerns}
    assert concerns == {
        "service-team Party cutover readiness": OwnerRole.RESOLVER,
        "approved service-team Party cutover adoption": (
            OwnerRole.APPLICATION_COORDINATOR
        ),
    }
    assert service.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    inputs = {item.name: item.owner for item in service.contract.authoritative_inputs}
    assert inputs["CRM service-team identity snapshot"] == "external:dotmac_crm"
    assert inputs["canonical Person Party identity"] == "party.registry"
    assert (
        inputs["current native service-team cutover state"]
        == "operations.service_team_lifecycle"
    )


def test_execution_adapter_is_thin_and_private_artifacts_are_required() -> None:
    source = _source("scripts/migration/execute_service_team_party_cutover.py")
    assert "adopt_service_team_party_cutover(" in source
    assert "owner_command_session()" in source
    assert "--execute" in source
    assert "stat.S_IMODE" in source
    assert "db.add(" not in source
    assert "db.execute(" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "SystemUser" not in source
    assert "ServiceTeamMember" not in source


def test_planner_is_read_only_and_does_not_promote_email_matching() -> None:
    source = _source("scripts/migration/plan_service_team_party_cutover.py")
    assert "SET TRANSACTION READ ONLY" in source
    assert "CRM_DATABASE_URL" in source
    assert "SUB_DATABASE_URL" in source
    assert "crm_person_id,decision,system_user_id,decision_id,reason" in source
    assert "INSERT " not in source
    assert "UPDATE " not in source
    assert "DELETE " not in source
    assert "normalize_email" not in source


def test_deploy_checks_cutover_readiness_before_alembic() -> None:
    source = _source("scripts/deploy.sh")
    readiness = "python -m scripts.migration.audit_service_team_party_cutover --check"
    migration = 'log "Applying migrations (alembic upgrade heads)"'
    assert readiness in source
    assert source.index(readiness) < source.index(migration)


def test_cutover_owner_cannot_change_credentials_rbac_or_team_manager() -> None:
    source = _source("app/services/service_team_party_cutover.py")
    for forbidden in (
        "UserCredential(",
        "SystemUserRole(",
        ".manager_person_id =",
        ".is_active =",
        ".commit(",
        ".rollback(",
    ):
        assert forbidden not in source
    assert "party_registry.create_party(" in source
    assert "party_registry.bind_system_user_principal(" in source
    assert "party_registry.add_external_reference(" in source
    assert "execute_owner_command(" in source


def test_new_staff_principals_receive_party_binding_in_owner_transaction() -> None:
    source = _source("app/services/staff_provisioning.py")
    assert "party_registry.create_party(" in source
    assert "party_registry.bind_system_user_principal(" in source
    assert "auth.staff_provisioning:erp_hr" in source
    assert "auth.staff_provisioning:local" in source
    assert "user.person_party_id =" not in source
