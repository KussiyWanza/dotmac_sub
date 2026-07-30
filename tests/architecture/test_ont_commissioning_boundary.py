"""Architecture guards for the assignment-free commissioning boundary."""

from __future__ import annotations

import inspect
from pathlib import Path

from app.services import sot_relationships
from app.services.network import ont_commissioning, ont_provisioning_commands
from app.services.network_operation_dispatch import NetworkOperationCommand
from app.services.scheduler import PERMANENT_LIFECYCLE_TASKS
from app.services.task_reliability import TASK_RELIABILITY_CONTRACTS
from scripts.seed.seed_rbac import DEFAULT_PERMISSIONS, ROLE_PERMISSIONS

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_commissioning_owner_has_complete_typed_contract() -> None:
    service = sot_relationships.service_relationship("network.ont_commissioning")

    assert service.contract is not None
    assert service.contract.migration.new_owner == "network.ont_commissioning"
    assert {
        "temporary ONT commissioning intent lifecycle",
        "assignment-free management-only commissioning coordination",
        "commissioning expiry and assignment reconciliation",
    } == set(service.owns)


def test_commissioning_uses_a_separate_seeded_permission() -> None:
    permission_keys = {key for key, _description in DEFAULT_PERMISSIONS}

    assert "network:ont:commission" in permission_keys
    assert "network:ont:commission" in ROLE_PERMISSIONS["operator"]
    assert "network:ont:commission" in ROLE_PERMISSIONS["admin"]
    route_source = (PROJECT_ROOT / "app/web/admin/network_olts_inventory.py").read_text(
        encoding="utf-8"
    )
    assert 'require_permission("network:ont:commission")' in route_source


def test_commissioning_cannot_manufacture_a_customer_assignment() -> None:
    source = inspect.getsource(ont_commissioning)

    assert "OntAssignment(" not in source
    assert ".assign(" not in source
    assert "internet_config_ip_index=None" in source
    assert "wan_config_profile_id=None" in source
    assert "allow_registration_move=False" in source


def test_raw_authorization_requires_exact_assignment_admission() -> None:
    source = inspect.getsource(ont_provisioning_commands.request_ont_authorization)

    assert "OntAssignment" in source
    assert "PonPort" in source
    assert "Commission ONT" in source
    assert "assignment.pon_port_id" in source


def test_commission_verify_cleanup_are_versioned_durable_commands() -> None:
    assert NetworkOperationCommand.ont_commission_v1.value == "ont_commission.v1"
    assert (
        NetworkOperationCommand.ont_commission_verify_v1.value
        == "ont_commission_verify.v1"
    )
    assert (
        NetworkOperationCommand.ont_commission_cleanup_v1.value
        == "ont_commission_cleanup.v1"
    )


def test_commissioning_reconciler_is_permanent_and_contracted() -> None:
    task_name = "app.tasks.ont_commissioning.reconcile_intents"

    assert task_name in PERMANENT_LIFECYCLE_TASKS
    assert task_name in TASK_RELIABILITY_CONTRACTS
    assert TASK_RELIABILITY_CONTRACTS[task_name].idempotency.value == "idempotent"
