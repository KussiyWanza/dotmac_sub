import ast
from pathlib import Path

from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
)
from app.services.sot_relationships import service_relationship


def _calls(path: str) -> set[str]:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    return {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }


def test_agent_workqueue_has_complete_typed_owner_contract():
    service = service_relationship("operations.agent_workqueue")
    assert service.module == "app.services.workqueue.commands"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert service.contract.migration.state is AuthorityMigrationState.CUTOVER_READY
    concerns = {concern.name: concern for concern in service.contract.concerns}
    assert concerns["personal workqueue snooze state"].role is OwnerRole.COMMAND_WRITER
    assert (
        concerns["agent workqueue action coordination"].role
        is OwnerRole.APPLICATION_COORDINATOR
    )
    assert (
        concerns["agent workqueue prioritization projection"].role is OwnerRole.RESOLVER
    )
    assert "docs/designs/AGENT_WORKQUEUE_SOT.md" in service.contract.design_refs


def test_workqueue_scope_consumes_service_team_owner_queries():
    source = Path("app/services/workqueue/scope.py").read_text(encoding="utf-8")
    assert "service_team_composition.resolve_staff_capability_scope" in source
    assert "service_team_lifecycle.list_active_team_member_system_user_ids" in source
    assert "ServiceTeamResponsibilityKey.queue_lead" in source
    assert "ServiceTeamMember" not in source
    assert "SystemUser" not in source


def test_workqueue_adapters_delegate_commands_and_never_own_transactions():
    web_source = Path("app/web/admin/workqueue.py").read_text(encoding="utf-8")
    api_source = Path("app/api/workqueue.py").read_text(encoding="utf-8")
    command_calls = _calls("app/services/workqueue/commands.py")

    assert "execute_action" in web_source
    assert "execute_action" in api_source
    assert ".commit(" not in web_source
    assert ".commit(" not in api_source
    assert "execute_owner_command" in command_calls
    assert "update" in command_calls
    assert "assign_conversation" in command_calls
    assert "update_status" in command_calls


def test_lifecycle_actions_use_server_owned_review_contract():
    projection_source = Path("app/services/workqueue/web.py").read_text(
        encoding="utf-8"
    )
    command_source = Path("app/services/workqueue/commands.py").read_text(
        encoding="utf-8"
    )
    row_template = Path("templates/admin/workqueue/_row.html").read_text(
        encoding="utf-8"
    )

    assert "ActionForm(" in projection_source
    assert "ActionConfirmation(" in projection_source
    assert "action_state_fingerprint(item, action)" in projection_source
    assert "_validate_action_review(command, item)" in command_source
    assert "compare_digest(supplied, expected)" in command_source
    assert "action_form(row.claim_action)" in row_template
    assert "action_form(row.complete_action)" in row_template


def test_team_inbox_commands_can_participate_in_cross_domain_coordinator():
    source = Path("app/services/team_inbox_commands.py").read_text(encoding="utf-8")
    assert "owner_command_active(db)" in source
    assert "db_session_adapter.release_read_transaction(db)" in source
