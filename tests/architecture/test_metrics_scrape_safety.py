"""Architecture guard for the cross-Dotmac metrics scrape contract."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SAFE_COLLECTOR_SERVICES = {
    "app.services.billing_health",
    "app.services.connectivity_reconciler",
    "app.services.ip_consistency_audit",
    "app.services.observability",
    "app.services.poller_health",
    "app.services.radius_reconciliation",
}
FORBIDDEN_NAMES = {
    "SessionLocal",
    "db_session_adapter",
    "get_db",
}
FORBIDDEN_METHODS = {
    "commit",
    "connect",
    "execute",
    "query",
    "read_session",
    "rollback",
    "scalar",
    "session",
}


def _metrics_route(tree: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "get"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and decorator.args[0].value == "/metrics"
            ):
                return node
    raise AssertionError("No /metrics route found")


def _violations(node: ast.AST) -> list[str]:
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in FORBIDDEN_NAMES:
            found.append(child.id)
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in FORBIDDEN_METHODS
        ):
            found.append(child.func.attr)
        if isinstance(child, ast.ImportFrom) and (
            child.module == "sqlalchemy"
            or str(child.module).startswith("sqlalchemy.")
            or str(child.module).startswith("app.models")
        ):
            found.append(str(child.module))
    return sorted(set(found))


def test_metrics_route_has_no_database_or_business_work() -> None:
    tree = ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8"))
    route = _metrics_route(tree)
    assert _violations(route) == []
    assert not any(
        isinstance(node, ast.ImportFrom) and str(node.module).startswith("app.services")
        for node in ast.walk(route)
    )


def test_custom_collectors_use_only_approved_snapshot_services() -> None:
    tree = ast.parse((ROOT / "app/metrics.py").read_text(encoding="utf-8"))
    collector_methods: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    imported_services: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not node.name.endswith("Collector"):
            continue
        for child in node.body:
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == "collect"
            ):
                collector_methods.append(child)
                for descendant in ast.walk(child):
                    if isinstance(descendant, ast.ImportFrom) and str(
                        descendant.module
                    ).startswith("app.services."):
                        imported_services.add(str(descendant.module))

    violations = [
        violation for method in collector_methods for violation in _violations(method)
    ]
    assert sorted(set(violations)) == []
    assert imported_services <= SAFE_COLLECTOR_SERVICES


def test_metrics_scrape_policy_is_checked_in() -> None:
    assert (ROOT / "docs/METRICS_SCRAPE_SAFETY.md").is_file()


def test_billing_health_producer_does_not_call_forensic_invariant_scan() -> None:
    source = (ROOT / "app/services/billing_health.py").read_text(encoding="utf-8")
    assert "AccountCreditApplications.summarize_invariants(" in source
    assert "AccountCreditApplications.inspect_invariants(" not in source


def test_invariant_summary_covers_every_forensic_violation_code() -> None:
    """The aggregate and the record scan must define the same invariant set.

    `summarize_invariants` re-implements `inspect_invariants` in SQL so the
    frequent snapshot stays bounded. Two implementations of one definition
    drift: adding an eighth violation code to the forensic scan without a
    matching summary field makes the gauge silently under-count, and no
    scenario test fails, because a scenario test only covers the codes someone
    remembered to write one for.

    Pin the correspondence itself. A new code must land with its field.
    """
    from app.services.billing.account_credit import AccountCreditInvariantSummary

    source = (ROOT / "app/services/billing/account_credit.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    inspector = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "inspect_invariants"
    )
    emitted_codes = {
        keyword.value.value
        for call in ast.walk(inspector)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "AccountCreditInvariantViolation"
        for keyword in call.keywords
        if keyword.arg == "code" and isinstance(keyword.value, ast.Constant)
    }

    summary_fields = set(AccountCreditInvariantSummary.__dataclass_fields__)

    assert emitted_codes, "found no violation codes -- the AST scan stopped matching"
    assert emitted_codes == summary_fields


def test_prometheus_callbacks_stay_in_reviewed_exporter_module() -> None:
    violations = []
    for path in (ROOT / "app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if (
            "REGISTRY.register(" in source or ".set_function(" in source
        ) and path != ROOT / "app/metrics.py":
            violations.append(str(path.relative_to(ROOT)))
    assert violations == []
