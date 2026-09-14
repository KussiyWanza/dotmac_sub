from __future__ import annotations

import ast
from pathlib import Path

from app.services.sot_manifest import contract_validation_errors
from app.services.sot_registry.registry import all_services, service_relationship

ROOT = Path(__file__).resolve().parents[2]
REPORT_SERVICE = ROOT / "app" / "services" / "ticket_sla_reports.py"
REPORT_ROUTE = ROOT / "app" / "web" / "admin" / "reports.py"


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )
    return ast.get_source_segment(source, function) or ""


def test_ticket_sla_report_has_one_complete_registered_projection_owner() -> None:
    owner = service_relationship("ui.ticket_sla_report")
    services = {service.name for service in all_services()}

    assert owner.module == "app.services.ticket_sla_reports"
    assert "current ticket SLA operational summary" in owner.owns
    assert not contract_validation_errors(owner, service_names=services)


def test_current_summary_cannot_regress_to_historical_breach_semantics() -> None:
    summary_source = _function_source(REPORT_SERVICE, "summary")

    assert "Ticket.status.notin_(excluded_statuses)" in summary_source
    assert "SlaClock.status == SlaClockStatus.breached.value" in summary_source
    assert "breached_at.is_not" not in summary_source
    assert "-> TicketSlaSummary" in summary_source


def test_ticket_sla_drilldown_keeps_the_not_closed_scope() -> None:
    drilldown_source = _function_source(REPORT_ROUTE, "_ticket_sla_drilldown_url")

    assert '"status": "not_closed"' in drilldown_source
