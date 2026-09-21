"""Keep SLA writes behind the public owner while routes and tasks stay thin."""

from pathlib import Path

from app.services.sot_manifest import TransactionMode
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def test_sla_adapters_use_public_commands_without_database_writers() -> None:
    route = (ROOT / "app/web/admin/inbox_sla.py").read_text(encoding="utf-8")
    task = (ROOT / "app/tasks/inbox_sla.py").read_text(encoding="utf-8")
    assert "inbox_sla.save_policy(" in route
    assert "inbox_sla.activate_policy(" in route
    assert "inbox_sla.evaluate_due_clocks(" in task
    for source in (route, task):
        assert "owner_command_session()" in source
        for forbidden in (
            "db.query(",
            "db.get(",
            "db.flush(",
            "db.commit(",
            ".session()",
        ):
            assert forbidden not in source
    owner = service_relationship("communications.inbox_sla")
    assert owner.contract is not None
    assert owner.contract.transaction.mode is TransactionMode.OWNER_MANAGED
