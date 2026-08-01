"""Architecture guards for the assignment-free commissioning boundary."""

from __future__ import annotations

import ast
import inspect
from dataclasses import is_dataclass
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

from app.services import sot_relationships
from app.services.network import (
    ont_authorization,
    ont_authorization_contracts,
    ont_commissioning,
    ont_provisioning_commands,
    ont_provisioning_execution,
)
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
    assert "OltConnectionConfig.from_model" in source
    assert "get_protocol_adapter_from_config" in source
    assert 'f"0/{ont.board}/{ont.port}"' not in source
    assert ".assign(" not in source
    assert "internet_config_ip_index=None" in source
    assert "wan_config_profile_id=None" in source
    assert "register_ont_for_commissioning" in source


def test_assigned_authorization_requires_exact_assignment_admission() -> None:
    source = inspect.getsource(
        ont_provisioning_commands.evaluate_assigned_authorization
    )
    request_source = inspect.getsource(
        ont_provisioning_commands.request_ont_authorization
    )

    assert "OntAssignment" in source
    assert "PonPort" in source
    assert "Commission ONT" in source
    assert "assignment.pon_port_id" in source
    assert "evaluate_assigned_authorization(" in request_source


def test_authorization_capabilities_have_only_their_named_owner_callers() -> None:
    restricted_names = {
        "_authorize_registration",
        "_execute_authorization_workflow",
        "authorize_and_provision_ont",
        "authorize_autofind_ont",
        "authorize_ont",
        "register_ont_for_commissioning",
    }
    observed: set[tuple[str, str]] = set()
    module_import_bypasses: list[str] = []

    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        relative = str(path.relative_to(PROJECT_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "app.services.network.ont_authorization":
                    observed.update(
                        (relative, alias.name)
                        for alias in node.names
                        if alias.name in restricted_names
                    )
                if node.module == "app.services.network" and any(
                    alias.name == "ont_authorization" for alias in node.names
                ):
                    module_import_bypasses.append(relative)
            elif isinstance(node, ast.Import) and any(
                alias.name == "app.services.network.ont_authorization"
                for alias in node.names
            ):
                module_import_bypasses.append(relative)

    assert observed == {
        (
            "app/services/network/ont_commissioning.py",
            "register_ont_for_commissioning",
        ),
        (
            "app/services/network/ont_provisioning_execution.py",
            "authorize_and_provision_ont",
        ),
    }
    assert module_import_bypasses == []
    assert not hasattr(ont_authorization, "authorize_ont")
    assert not hasattr(ont_authorization, "authorize_autofind_ont")
    for capability in (
        ont_authorization.authorize_and_provision_ont,
        ont_authorization.register_ont_for_commissioning,
    ):
        parameters = inspect.signature(capability).parameters
        assert tuple(parameters) == ("db", "command")
        assert "provision" not in parameters
        assert "dependency_scope" not in parameters
        assert "allow_registration_move" not in parameters


def test_authorization_process_uses_typed_commands_and_outcomes() -> None:
    request_parameters = inspect.signature(
        ont_provisioning_commands.request_ont_authorization
    ).parameters

    assert tuple(request_parameters) == ("db", "command")
    assert request_parameters["command"].annotation == (
        "RequestAssignedOntAuthorization"
    )
    assert (
        inspect.signature(
            ont_provisioning_commands.request_ont_authorization
        ).return_annotation
        == "OntAuthorizationAdmission"
    )
    assert {
        "RequestAssignedOntAuthorization",
        "ExecuteAssignedOntAuthorization",
        "RegisterCommissioningOnt",
        "OntAuthorizationTarget",
        "OntFsp",
        "OntSerialNumber",
        "OntAuthorizationAdmission",
        "AssignedAuthorizationDecision",
    }.issubset(set(vars(ont_authorization_contracts)))
    immutable_contracts = (
        ont_authorization_contracts.OntFsp,
        ont_authorization_contracts.OntSerialNumber,
        ont_authorization_contracts.OntAuthorizationTarget,
        ont_authorization_contracts.RequestAssignedOntAuthorization,
        ont_authorization_contracts.ExecuteAssignedOntAuthorization,
        ont_authorization_contracts.RegisterCommissioningOnt,
        ont_authorization_contracts.OntAuthorizationAdmission,
        ont_authorization_contracts.AssignedAuthorizationDecision,
        ont_authorization.AuthorizationWorkflowResult,
        ont_provisioning_execution.OntAuthorizationExecutionOutcome,
        ont_commissioning.ExecuteOntCommissioning,
        ont_commissioning.OntCommissioningExecutionOutcome,
        ont_commissioning.RecordOntCommissioningExternalWriteFailure,
        ont_commissioning.ExternalWriteReconciliationOutcome,
        ont_commissioning._CommissioningPreflightOutcome,
        ont_commissioning._CommissioningManagementPlan,
        ont_commissioning._CommissioningExecutionPlan,
    )
    for contract in immutable_contracts:
        assert is_dataclass(contract)
        assert contract.__dataclass_params__.frozen is True

    request_hints = get_type_hints(
        ont_authorization_contracts.RequestAssignedOntAuthorization
    )
    assert request_hints["ont_id"] is UUID
    assert request_hints["target"] is ont_authorization_contracts.OntAuthorizationTarget
    execution_parameters = inspect.signature(
        ont_provisioning_execution.execute_ont_authorization
    ).parameters
    assert tuple(execution_parameters) == ("db", "command")
    assert (
        inspect.signature(
            ont_provisioning_execution.execute_ont_authorization
        ).return_annotation
        == "OntAuthorizationExecutionOutcome"
    )
    commissioning_parameters = inspect.signature(
        ont_commissioning.execute_ont_commissioning
    ).parameters
    assert tuple(commissioning_parameters) == ("db", "command")
    assert commissioning_parameters["command"].annotation == "ExecuteOntCommissioning"
    assert (
        inspect.signature(ont_commissioning.execute_ont_commissioning).return_annotation
        == "OntCommissioningExecutionOutcome"
    )
    commissioning_hints = get_type_hints(ont_commissioning.ExecuteOntCommissioning)
    assert commissioning_hints["intent_id"] is UUID
    assert commissioning_hints["operation_id"] is UUID


def test_commissioning_external_io_uses_detached_plan_and_typed_task_boundary() -> None:
    owner_source = inspect.getsource(ont_commissioning.execute_ont_commissioning)
    task_source = (PROJECT_ROOT / "app/tasks/ont_commissioning.py").read_text(
        encoding="utf-8"
    )

    assert "_CommissioningExecutionPlan(" in owner_source
    assert "OltConnectionConfig.from_model" in owner_source
    assert "get_protocol_adapter_from_config(plan.olt)" in owner_source
    assert "ExecuteOntCommissioning(" in task_source
    assert "outcome.to_transport()" in task_source
    assert "owner_command_session()" in task_source


def test_commissioning_recovery_never_reissues_landed_authorization() -> None:
    recovery_source = inspect.getsource(
        ont_commissioning._stage_interrupted_management_recovery
    )
    execution_source = inspect.getsource(ont_commissioning.execute_ont_commissioning)

    assert '"authorization_reissue_allowed": False' in recovery_source
    assert "authorization_already_recorded" in execution_source
    assert "_verify_recovery_registration" in execution_source


def test_reauthorization_delegates_to_assigned_command_owner() -> None:
    source = (
        PROJECT_ROOT / "app/services/web_network_ont_actions/device_actions.py"
    ).read_text(encoding="utf-8")
    adapter_source = (PROJECT_ROOT / "app/services/olt_action_adapter.py").read_text(
        encoding="utf-8"
    )

    assert "request_ont_authorization(" in source
    assert "from app.services.network.ont_authorization import" not in source
    assert "def authorize_ont(" not in adapter_source


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
    task_source = (PROJECT_ROOT / "app/tasks/ont_commissioning.py").read_text(
        encoding="utf-8"
    )
    assert "record_external_write_reconciliation_required" in task_source
    assert (
        "with db_session_adapter.owner_command_session() as recovery_db" in task_source
    )
    assert "RecordOntCommissioningExternalWriteFailure(" in task_source
    assert "outcome.to_transport()" in task_source


def test_commissioning_reconciler_is_permanent_and_contracted() -> None:
    task_name = "app.tasks.ont_commissioning.reconcile_intents"

    assert task_name in PERMANENT_LIFECYCLE_TASKS
    assert task_name in TASK_RELIABILITY_CONTRACTS
    assert TASK_RELIABILITY_CONTRACTS[task_name].idempotency.value == "idempotent"
