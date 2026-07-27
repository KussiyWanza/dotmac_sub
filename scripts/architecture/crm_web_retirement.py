"""Inventory and govern the Dotmac CRM web-layer retirement.

The checked-in ledger is a migration control, not proof of parity by itself.
This module statically inventories the external ``dotmac_crm/app/web`` tree,
resolves mounted FastAPI router prefixes, and preserves manually reviewed
retirement fields when the source inventory is refreshed.

CI validates the checked-in ledger without requiring a sibling CRM checkout.
Operators can additionally compare it with a specific CRM revision:

.. code-block:: bash

   poetry run python scripts/architecture/crm_web_retirement.py refresh \
     --crm-root ../dotmac_crm \
     --source-revision "$(git -C ../dotmac_crm rev-parse origin/main)"

   poetry run python scripts/architecture/crm_web_retirement.py validate \
     --crm-root ../dotmac_crm \
     --source-revision "$(git -C ../dotmac_crm rev-parse origin/main)"
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = PROJECT_ROOT / "docs" / "audits" / "crm_web_retirement_ledger.json"
CRM_WEB_ROOT = Path("app/web")
SCHEMA_VERSION = 2

ZERO_TRAFFIC_MINIMUM_DAYS = 30
ZERO_TRAFFIC_EVIDENCE_CONTRACT = {
    "corroborating_metric": "http_requests_total",
    "corroborating_query_template": (
        "sum(increase(http_requests_total{<crm-target-labels>,"
        'method="<method>",path="<effective-path>"}[<window>]))'
    ),
    "minimum_observation_days": ZERO_TRAFFIC_MINIMUM_DAYS,
    "primary_log_source": "Dotmac Observability Loki",
    "primary_query_template": (
        'sum(count_over_time({app="dotmac-crm",environment="production"} '
        '|= "request_completed" | json | method="<method>" '
        '| path="<effective-path>" [<window>]))'
    ),
    "required_record_fields": [
        "window_started_at",
        "window_ended_at",
        "loki_query",
        "loki_request_count",
        "victoriametrics_query",
        "victoriametrics_request_count",
        "telemetry_health_evidence",
        "operator_record",
    ],
    "window_rule": (
        "The window starts only after route cutover and fallback disablement. "
        "Both sources must be healthy for the complete window; absent or stale "
        "telemetry is not zero-traffic evidence."
    ),
}

EXPECTED_MODULE_COUNT = 73
EXPECTED_ROUTE_COUNT = 813
EXPECTED_METHOD_COUNTS = {"DELETE": 3, "GET": 430, "POST": 380}
DEFAULT_SUB_REVISION = "0fbff5c1c2e52b495e50e9c081f31692c02112b3"
REVIEWED_SUB_PULL_REQUESTS = (
    *range(1601, 1618),
    1619,
    1621,
    1623,
    1624,
    1625,
    1626,
    1629,
    1631,
    1632,
    1633,
)

HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put"}
MODULE_CLASSIFICATIONS = {
    "covered_candidate": (
        "app/web/__init__.py",
        "app/web/admin/admin_hub.py",
        "app/web/admin/dashboard.py",
        "app/web/admin/gis.py",
        "app/web/admin/legal.py",
        "app/web/admin/notifications.py",
        "app/web/admin/projects.py",
        "app/web/admin/crm_referrals.py",
        "app/web/auth/__init__.py",
        "app/web/auth/dependencies.py",
        "app/web/public/__init__.py",
        "app/web/public/legal.py",
        "app/web/reseller/__init__.py",
        "app/web/reseller/dependencies.py",
        "app/web/templates.py",
        "app/web/vendor/__init__.py",
        "app/web/vendor/auth.py",
    ),
    "usable_surface_gap": (
        "app/web/admin/campaigns.py",
        "app/web/admin/crm_widget.py",
        "app/web/admin/expense_requests.py",
        "app/web/admin/intelligence.py",
        "app/web/admin/material_requests.py",
        "app/web/admin/surveys.py",
        "app/web/admin/user_guide.py",
        "app/web/agent/reports.py",
        "app/web/agent/workqueue.py",
        "app/web/public/surveys.py",
        "app/web/public/track.py",
    ),
    "partial_capability": (
        "app/web/admin/ai.py",
        "app/web/admin/billing_risk.py",
        "app/web/admin/crm_contacts.py",
        "app/web/admin/crm_inbox_actions_core.py",
        "app/web/admin/crm_inbox_catalog.py",
        "app/web/admin/crm_inbox_comment_reply.py",
        "app/web/admin/crm_inbox_comments.py",
        "app/web/admin/crm_inbox_connectors_actions.py",
        "app/web/admin/crm_inbox_conversations.py",
        "app/web/admin/crm_inbox_message.py",
        "app/web/admin/crm_inbox_private_notes.py",
        "app/web/admin/crm_inbox_settings.py",
        "app/web/admin/crm_inbox_start.py",
        "app/web/admin/crm_inbox_status.py",
        "app/web/admin/crm_leads.py",
        "app/web/admin/crm_presence.py",
        "app/web/admin/crm_quotes.py",
        "app/web/admin/crm_sales.py",
        "app/web/admin/integrations.py",
        "app/web/admin/network.py",
        "app/web/admin/operations.py",
        "app/web/admin/reports.py",
        "app/web/admin/subscribers.py",
        "app/web/admin/system.py",
        "app/web/admin/tickets.py",
        "app/web/admin/vendors.py",
        "app/web/auth/routes.py",
        "app/web/reseller/routes.py",
        "app/web/vendor/routes.py",
    ),
    "owner_policy_gap": (
        "app/web/admin/automations.py",
        "app/web/admin/data_quality.py",
        "app/web/admin/inventory.py",
        "app/web/admin/meta_oauth.py",
        "app/web/admin/performance.py",
        "app/web/admin/service_teams.py",
        "app/web/agent/performance.py",
    ),
    "replacement_retirement": (
        "app/web/admin/__init__.py",
        "app/web/admin/_auth_helpers.py",
        "app/web/admin/crm.py",
        "app/web/admin/crm_support.py",
        "app/web/admin/storage.py",
        "app/web/agent/__init__.py",
        "app/web/auth/rbac.py",
        "app/web/public/crm_webhooks.py",
        "app/web/public/media.py",
    ),
}

CLASSIFICATION_DESCRIPTIONS = {
    "covered_candidate": (
        "Sub appears to have the capability, but parity and retirement still "
        "require route-level evidence."
    ),
    "usable_surface_gap": (
        "Sub has relevant backend capability but lacks a verified usable "
        "operator or public surface."
    ),
    "partial_capability": (
        "Sub has a related implementation, but behavior, surface, data, or "
        "operational closure is incomplete."
    ),
    "owner_policy_gap": (
        "The authoritative owner or policy boundary must be decided and built "
        "before migration."
    ),
    "replacement_retirement": (
        "The CRM path should not be copied literally; its callers must be "
        "replaced or redirected and the old surface proved unused before deletion."
    ),
}

ASSESSMENT_STATES = {
    "inventory_only",
    "assessed",
    "implementation_in_progress",
    "shadow_verification",
    "cutover_ready",
    "cut_over",
    "retired",
}
EVIDENCE_STATES = {"unassessed", "not_applicable", "in_progress", "verified"}
REPLACEMENT_KINDS = {
    "unassigned",
    "native_web_route",
    "native_api_surface",
    "native_workflow",
    "redirect",
    "explicit_removal",
}
USAGE_STATES = {"unknown", "active", "inactive", "unreachable"}

TEAM_INBOX_MODULES = frozenset(
    {
        "app/web/admin/crm_inbox_actions_core.py",
        "app/web/admin/crm_inbox_catalog.py",
        "app/web/admin/crm_inbox_comment_reply.py",
        "app/web/admin/crm_inbox_comments.py",
        "app/web/admin/crm_inbox_connectors_actions.py",
        "app/web/admin/crm_inbox_conversations.py",
        "app/web/admin/crm_inbox_message.py",
        "app/web/admin/crm_inbox_private_notes.py",
        "app/web/admin/crm_inbox_settings.py",
        "app/web/admin/crm_inbox_start.py",
        "app/web/admin/crm_inbox_status.py",
        "app/web/admin/crm_presence.py",
    }
)

MODULE_REVIEW_OVERRIDES: dict[str, dict[str, Any]] = {
    "app/web/admin/projects.py": {
        "assessment_state": "implementation_in_progress",
        "decision": {
            "notes": (
                "PR #1610 completed the native project-task to work-order operator "
                "workflow and PR #1617 added the vendor-delivery projection on the "
                "reviewed Sub target revision. "
                "operations.project_lifecycle owns project and task decisions and "
                "ui.project_list_projection owns the admin projection. Data/caller "
                "cutover, production parity, zero traffic, and CRM deletion remain."
            ),
            "owner_service": "operations.project_lifecycle",
            "state": "verified",
        },
        "target_slice": "project-crm-data-caller-and-traffic-cutover",
    },
    "app/web/admin/service_teams.py": {
        "assessment_state": "cutover_ready",
        "decision": {
            "notes": (
                "This slice registers operations.service_team_lifecycle as the native "
                "owner, adds its admin surface, moves ServiceTeam person references to "
                "reviewed Party identity, removes the ticket-settings mirror writer, "
                "cuts reviewed application callers over to the owner contract, and "
                "adds authenticated browser lifecycle, CSRF, and permission coverage. "
                "Production migration apply evidence, traffic cutover, fallback "
                "removal, zero traffic, and CRM source deletion remain."
            ),
            "owner_service": "operations.service_team_lifecycle",
            "state": "verified",
        },
        "target_slice": "service-team-production-cutover-and-crm-retirement",
    },
    "app/web/agent/workqueue.py": {
        "assessment_state": "cutover_ready",
        "decision": {
            "notes": (
                "This slice registers operations.agent_workqueue, cuts scope reads "
                "over to the service-team owner, adds the native admin page and "
                "partials, moves API and web snooze writes through one typed command, "
                "and coordinates ticket and Inbox claim/complete through their "
                "canonical lifecycle owners. Work Orders remain open/snooze-only. "
                "Production snooze reconciliation, shadow comparison, traffic "
                "cutover, fallback removal, zero traffic, and CRM source deletion "
                "remain."
            ),
            "owner_service": "operations.agent_workqueue",
            "state": "verified",
        },
        "target_slice": "agent-workqueue-production-cutover-and-crm-retirement",
    },
}

SERVICE_TEAM_ROUTE_REPLACEMENTS: dict[str, tuple[str, str, str]] = {
    "service_team_list": (
        "Service-team list and active role/region projection",
        "native_web_route",
        "GET /admin/system/service-teams",
    ),
    "service_team_new": (
        "Service-team creation form",
        "native_web_route",
        "GET /admin/system/service-teams/new",
    ),
    "service_team_create": (
        "Service-team creation",
        "native_web_route",
        "POST /admin/system/service-teams",
    ),
    "service_team_detail": (
        "Service-team detail and membership projection",
        "native_web_route",
        "GET /admin/system/service-teams/{team_id}",
    ),
    "service_team_edit": (
        "Service-team edit form",
        "native_web_route",
        "GET /admin/system/service-teams/{team_id}/edit",
    ),
    "service_team_update": (
        "Service-team metadata and manager update",
        "native_web_route",
        "POST /admin/system/service-teams/{team_id}",
    ),
    "service_team_activate": (
        "Service-team activation",
        "native_web_route",
        "POST /admin/system/service-teams/{team_id}/active",
    ),
    "service_team_deactivate": (
        "Service-team deactivation",
        "native_web_route",
        "POST /admin/system/service-teams/{team_id}/active",
    ),
    "service_team_delete": (
        "Hard delete removed; audited deactivation preserves operational history",
        "explicit_removal",
        "POST /admin/system/service-teams/{team_id}/active",
    ),
    "service_team_add_member": (
        "Service-team membership add",
        "native_web_route",
        "POST /admin/system/service-teams/{team_id}/members",
    ),
    "service_team_remove_member": (
        "Service-team membership removal",
        "native_web_route",
        "POST /admin/system/service-teams/{team_id}/members/{member_id}/remove",
    ),
}

SERVICE_TEAM_READ_HANDLERS = frozenset(
    {
        "service_team_list",
        "service_team_new",
        "service_team_detail",
        "service_team_edit",
    }
)

WORKQUEUE_ROUTE_REPLACEMENTS: dict[str, tuple[str, str]] = {
    "page": (
        "Ranked native agent workqueue page",
        "GET /admin/workqueue",
    ),
    "partial_right_now": (
        "Cross-source right-now ranking partial",
        "GET /admin/workqueue/_right-now",
    ),
    "partial_section": (
        "Native source section partial",
        "GET /admin/workqueue/_section/{kind}",
    ),
    "post_snooze": (
        "Personal workqueue snooze command",
        "POST /admin/workqueue/snooze",
    ),
    "post_clear_snooze": (
        "Personal workqueue snooze restore command",
        "POST /admin/workqueue/snooze/clear",
    ),
    "post_claim": (
        "Scope-checked source-owner claim coordination",
        "POST /admin/workqueue/claim",
    ),
    "post_complete": (
        "Scope-checked source-owner completion coordination",
        "POST /admin/workqueue/complete",
    ),
}

WORKQUEUE_READ_HANDLERS = frozenset(
    {
        "page",
        "partial_right_now",
        "partial_section",
    }
)


@dataclass(frozen=True)
class Symbol:
    """A symbol defined by or imported into one Python module."""

    module: str
    name: str


@dataclass(frozen=True)
class RouterNode:
    """A statically identified ``APIRouter`` instance."""

    module: str
    name: str


@dataclass
class ModuleScan:
    """Static facts needed to assemble the mounted route graph."""

    file: str
    module: str
    tree: ast.Module
    routers: dict[str, str]
    imports: dict[str, Symbol]
    function_returns: dict[str, ast.expr]
    service_dependencies: set[str]
    model_dependencies: set[str]
    template_paths: set[str]


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{dynamic}")
        return "".join(parts)
    return None


def _call_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _keyword_string(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return _literal_string(keyword.value)
    return None


def _module_name(web_root: Path, path: Path) -> str:
    relative = path.relative_to(web_root.parent).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return "app." + ".".join(parts)


def _dependency_name(module: str, imported_name: str | None = None) -> str:
    if imported_name and module in {"app.services", "app.models", "app.logic"}:
        return f"{module}.{imported_name}"
    return module


def _template_paths(node: ast.AST) -> set[str]:
    paths: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = _call_name(child.func)
        if not name or not name.endswith(("TemplateResponse", "get_template")):
            continue
        values = [
            *(_literal_string(argument) for argument in child.args[:2]),
            *(_literal_string(keyword.value) for keyword in child.keywords),
        ]
        paths.update(
            value
            for value in values
            if value and value.endswith((".html", ".j2", ".jinja"))
        )
    return paths


def _scan_module(web_root: Path, path: Path) -> ModuleScan:
    file = path.relative_to(web_root.parent.parent).as_posix()
    module = _module_name(web_root, path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=file)
    routers: dict[str, str] = {}
    imports: dict[str, Symbol] = {}
    function_returns: dict[str, ast.expr] = {}
    service_dependencies: set[str] = set()
    model_dependencies: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imports[alias.asname or alias.name] = Symbol(node.module, alias.name)
                dependency = _dependency_name(node.module, alias.name)
                if node.module.startswith("app.services"):
                    service_dependencies.add(dependency)
                elif node.module.startswith(("app.models", "app.logic")):
                    model_dependencies.add(dependency)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name] = Symbol(alias.name, "")
                if alias.name.startswith("app.services"):
                    service_dependencies.add(alias.name)
                elif alias.name.startswith(("app.models", "app.logic")):
                    model_dependencies.add(alias.name)

        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if isinstance(value, ast.Call) and (_call_name(value.func) or "").endswith(
                "APIRouter"
            ):
                prefix = _keyword_string(value, "prefix") or ""
                for target in targets:
                    if isinstance(target, ast.Name):
                        routers[target.id] = prefix

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            returns = [
                statement.value
                for statement in node.body
                if isinstance(statement, ast.Return) and statement.value is not None
            ]
            if len(returns) == 1:
                function_returns[node.name] = returns[0]

    return ModuleScan(
        file=file,
        module=module,
        tree=tree,
        routers=routers,
        imports=imports,
        function_returns=function_returns,
        service_dependencies=service_dependencies,
        model_dependencies=model_dependencies,
        template_paths=_template_paths(tree),
    )


def _resolve_symbol(
    symbol: Symbol,
    scans: dict[str, ModuleScan],
    *,
    seen: frozenset[Symbol],
) -> RouterNode | None:
    if symbol in seen:
        return None
    scan = scans.get(symbol.module)
    if scan is None:
        return None
    if symbol.name in scan.routers:
        return RouterNode(symbol.module, symbol.name)
    expression = scan.function_returns.get(symbol.name)
    if expression is None:
        return None
    return _resolve_router_expression(
        expression,
        scan,
        scans,
        seen=seen | {symbol},
    )


def _resolve_router_expression(
    expression: ast.AST,
    scan: ModuleScan,
    scans: dict[str, ModuleScan],
    *,
    seen: frozenset[Symbol] = frozenset(),
) -> RouterNode | None:
    if isinstance(expression, ast.Name):
        if expression.id in scan.routers:
            return RouterNode(scan.module, expression.id)
        imported = scan.imports.get(expression.id)
        if imported is not None:
            return _resolve_symbol(imported, scans, seen=seen)
        return _resolve_symbol(Symbol(scan.module, expression.id), scans, seen=seen)
    if isinstance(expression, ast.Call):
        return _resolve_router_expression(
            expression.func,
            scan,
            scans,
            seen=seen,
        )
    return None


def _join_path(*parts: str) -> str:
    populated = [part.strip("/") for part in parts if part and part != "/"]
    if not populated:
        return "/"
    return "/" + "/".join(populated)


def _source_route_id(
    *,
    module: str,
    handler: str,
    method: str,
    effective_path: str,
) -> str:
    identity = "|".join((module, handler, method, effective_path))
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"{module}:{handler}:{method}:{effective_path}:{suffix}"


def _router_graph(
    scans: dict[str, ModuleScan],
) -> tuple[
    dict[RouterNode, str],
    dict[RouterNode, list[tuple[RouterNode, str]]],
]:
    prefixes: dict[RouterNode, str] = {}
    edges: dict[RouterNode, list[tuple[RouterNode, str]]] = defaultdict(list)
    for scan in scans.values():
        for name, prefix in scan.routers.items():
            prefixes[RouterNode(scan.module, name)] = prefix
        for call in (
            node for node in ast.walk(scan.tree) if isinstance(node, ast.Call)
        ):
            if not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr != "include_router" or not call.args:
                continue
            parent = _resolve_router_expression(call.func.value, scan, scans)
            child = _resolve_router_expression(call.args[0], scan, scans)
            if parent is None or child is None:
                continue
            edges[parent].append((child, _keyword_string(call, "prefix") or ""))
    return prefixes, edges


def _mounted_prefixes(
    scans: dict[str, ModuleScan],
    prefixes: dict[RouterNode, str],
    edges: dict[RouterNode, list[tuple[RouterNode, str]]],
) -> dict[RouterNode, set[str]]:
    root_scan = scans["app.web"]
    root_expression = root_scan.function_returns["build_router"]
    root = _resolve_router_expression(root_expression, root_scan, scans)
    if root is None:
        raise ValueError("could not resolve app.web.build_router root")

    mounted: dict[RouterNode, set[str]] = defaultdict(set)

    def visit(
        node: RouterNode, parent_prefix: str, stack: tuple[RouterNode, ...]
    ) -> None:
        if node in stack:
            cycle = " -> ".join(f"{item.module}.{item.name}" for item in (*stack, node))
            raise ValueError(f"router include cycle: {cycle}")
        effective = _join_path(parent_prefix, prefixes.get(node, ""))
        if effective in mounted[node]:
            return
        mounted[node].add(effective)
        for child, include_prefix in edges.get(node, ()):
            visit(child, _join_path(effective, include_prefix), (*stack, node))

    visit(root, "", ())
    return mounted


def _route_records(
    scans: dict[str, ModuleScan],
    mounted: dict[RouterNode, set[str]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for scan in scans.values():
        for function in (
            node
            for node in ast.walk(scan.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            function_templates = sorted(_template_paths(function))
            for decorator in function.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                name = _call_name(decorator.func)
                if not name or "." not in name:
                    continue
                router_name, method_name = name.rsplit(".", maxsplit=1)
                if method_name not in HTTP_METHODS:
                    continue
                router = _resolve_router_expression(
                    ast.Name(id=router_name),
                    scan,
                    scans,
                )
                if router is None:
                    raise ValueError(
                        f"{scan.file}:{decorator.lineno}: unknown router {router_name}"
                    )
                local_path = (
                    _literal_string(decorator.args[0]) if decorator.args else ""
                )
                if local_path is None:
                    raise ValueError(
                        f"{scan.file}:{decorator.lineno}: dynamic route path"
                    )
                bases = sorted(mounted.get(router, ()))
                if not bases:
                    bases = [_join_path(scan.routers.get(router.name, ""))]
                if len(bases) != 1:
                    raise ValueError(
                        f"{scan.file}:{decorator.lineno}: route is mounted "
                        f"{len(bases)} times: {bases}"
                    )
                method = method_name.upper()
                effective_path = _join_path(bases[0], local_path)
                records.append(
                    {
                        "id": _source_route_id(
                            module=scan.module,
                            handler=function.name,
                            method=method,
                            effective_path=effective_path,
                        ),
                        "source": {
                            "effective_path": effective_path,
                            "file": scan.file,
                            "handler": function.name,
                            "line": function.lineno,
                            "local_path": local_path or "/",
                            "method": method,
                            "mounted": router in mounted,
                            "router": router.name,
                            "templates": function_templates,
                        },
                    }
                )
    return sorted(
        records,
        key=lambda item: (
            item["source"]["file"],
            item["source"]["line"],
            item["source"]["method"],
            item["source"]["effective_path"],
        ),
    )


def scan_crm(crm_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return deterministic module and route source facts from a CRM checkout."""

    web_root = crm_root.resolve() / CRM_WEB_ROOT
    paths = sorted(
        path
        for path in web_root.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )
    scans = {
        scan.module: scan for scan in (_scan_module(web_root, path) for path in paths)
    }
    prefixes, edges = _router_graph(scans)
    mounted = _mounted_prefixes(scans, prefixes, edges)
    classification_by_file = {
        file: classification
        for classification, files in MODULE_CLASSIFICATIONS.items()
        for file in files
    }
    modules = [
        {
            "classification": classification_by_file.get(scan.file),
            "file": scan.file,
            "model_dependencies": sorted(scan.model_dependencies),
            "module": scan.module,
            "route_count": 0,
            "service_dependencies": sorted(scan.service_dependencies),
            "template_paths": sorted(scan.template_paths),
        }
        for scan in sorted(scans.values(), key=lambda item: item.file)
    ]
    routes = _route_records(scans, mounted)
    route_counts = Counter(route["source"]["file"] for route in routes)
    for module in modules:
        module["route_count"] = route_counts[module["file"]]
    return modules, routes


def _evidence_gate() -> dict[str, Any]:
    return {"evidence": [], "state": "unassessed"}


def _service_team_route_tracking(route: dict[str, Any]) -> dict[str, Any]:
    source = route.get("source", {})
    if source.get("file") != "app/web/admin/service_teams.py":
        return {}
    handler = source.get("handler")
    replacement = SERVICE_TEAM_ROUTE_REPLACEMENTS.get(handler)
    if replacement is None:
        return {}
    capability, kind, surface = replacement
    write_route = handler not in SERVICE_TEAM_READ_HANDLERS
    no_write_evidence = [
        (
            "CRM GET capability is replaced by a query-only native adapter; "
            "audit, event, and idempotency write semantics do not apply."
        )
    ]
    command_evidence = [
        "tests/test_service_team_lifecycle.py",
        "docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md",
    ]
    write_gate = (
        {"evidence": command_evidence, "state": "verified"}
        if write_route
        else {"evidence": no_write_evidence, "state": "not_applicable"}
    )
    notes = (
        "Hard delete is intentionally removed by the native retention policy; "
        "audited deactivation is the replacement. Authenticated browser coverage "
        "verifies that no delete action is exposed. Production migration, traffic "
        "cutover, and retirement evidence remain."
        if handler == "service_team_delete"
        else (
            "The native owner and thin admin surface are implemented in this slice. "
            "Authenticated browser lifecycle, CSRF, and permission parity is covered. "
            "Production migration, traffic cutover, and retirement evidence remain."
        )
    )
    return {
        "assessment_state": "cutover_ready",
        "replacement": {
            "capability": capability,
            "kind": kind,
            "notes": notes,
            "owner_service": "operations.service_team_lifecycle",
            "surfaces": [surface],
        },
        "parity": {
            "audit": write_gate,
            "behavior": {
                "evidence": [
                    "tests/test_service_team_lifecycle.py",
                    "tests/test_service_team_web.py",
                    "tests/playwright/e2e/test_service_teams.py",
                    "docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md",
                ],
                "state": "verified",
            },
            "errors": {
                "evidence": [
                    "tests/test_service_team_lifecycle.py",
                    "app/web/admin/service_teams.py",
                ],
                "state": "verified",
            },
            "events": write_gate,
            "idempotency": write_gate,
            "permissions": {
                "evidence": [
                    "tests/architecture/test_service_team_lifecycle_boundary.py",
                    "tests/test_service_team_web.py",
                    "tests/playwright/e2e/test_service_teams.py",
                    "scripts/seed/seed_rbac.py",
                ],
                "state": "verified",
            },
        },
        "migration": {
            "callers": {
                "evidence": [
                    "tests/architecture/test_service_team_lifecycle_boundary.py",
                    "tests/test_support_ticket_settings.py",
                    "tests/test_field_job_chat.py",
                ],
                "state": "verified",
            },
            "cutover": {
                "evidence": [
                    (
                        "Native code cutover and authenticated browser parity are "
                        "complete in this slice; production migration and CRM traffic "
                        "cutover remain pending."
                    )
                ],
                "state": "in_progress",
            },
            "data": {
                "evidence": [
                    "alembic/versions/426_service_team_lifecycle.py",
                    "tests/test_service_team_lifecycle_migration.py",
                ],
                "state": "in_progress",
            },
            "rollback": {
                "evidence": [
                    (
                        "Migration 425 is forward-only; the design requires a reviewed "
                        "pre-cutover backup before production apply."
                    )
                ],
                "state": "in_progress",
            },
        },
    }


def _workqueue_route_tracking(route: dict[str, Any]) -> dict[str, Any]:
    source = route.get("source", {})
    if source.get("file") != "app/web/agent/workqueue.py":
        return {}
    handler = source.get("handler")
    replacement = WORKQUEUE_ROUTE_REPLACEMENTS.get(handler)
    if replacement is None:
        return {}
    capability, surface = replacement
    write_route = handler not in WORKQUEUE_READ_HANDLERS
    no_write_evidence = [
        (
            "CRM GET behavior is replaced by the owner-built query projection; "
            "audit, event, and idempotency write semantics do not apply."
        )
    ]
    command_evidence = [
        "tests/test_workqueue_commands.py",
        "docs/designs/AGENT_WORKQUEUE_SOT.md",
    ]
    write_gate = (
        {"evidence": command_evidence, "state": "verified"}
        if write_route
        else {"evidence": no_write_evidence, "state": "not_applicable"}
    )
    return {
        "assessment_state": "cutover_ready",
        "replacement": {
            "capability": capability,
            "kind": "native_web_route",
            "notes": (
                "The native owner and operator surface implement this behavior. "
                "Ticket and Inbox lifecycle changes delegate to their canonical "
                "owners; Work Orders intentionally expose no inline lifecycle "
                "transition. Production data reconciliation, traffic cutover, and "
                "retirement evidence remain."
            ),
            "owner_service": "operations.agent_workqueue",
            "surfaces": [surface],
        },
        "parity": {
            "audit": write_gate,
            "behavior": {
                "evidence": [
                    "tests/test_workqueue_parity.py",
                    "tests/test_workqueue_commands.py",
                    "tests/test_workqueue_web.py",
                    "tests/playwright/e2e/test_workqueue.py",
                    "docs/designs/AGENT_WORKQUEUE_SOT.md",
                ],
                "state": "verified",
            },
            "errors": {
                "evidence": [
                    "tests/test_workqueue_commands.py",
                    "app/web/admin/workqueue.py",
                    "app/api/workqueue.py",
                ],
                "state": "verified",
            },
            "events": write_gate,
            "idempotency": write_gate,
            "permissions": {
                "evidence": [
                    "tests/test_workqueue_commands.py",
                    "tests/test_workqueue_web.py",
                    "tests/playwright/e2e/test_workqueue.py",
                    "tests/architecture/test_agent_workqueue_boundary.py",
                ],
                "state": "verified",
            },
        },
        "migration": {
            "callers": {
                "evidence": [
                    "app/api/workqueue.py",
                    "app/web/admin/workqueue.py",
                    "templates/components/navigation/admin_sidebar.html",
                    "tests/architecture/test_agent_workqueue_boundary.py",
                ],
                "state": "verified",
            },
            "cutover": {
                "evidence": [
                    (
                        "Native web, API, and navigation cutover is implemented; "
                        "production CRM traffic cutover remains pending."
                    )
                ],
                "state": "in_progress",
            },
            "data": {
                "evidence": [
                    (
                        "Native source rows are authoritative. Production CRM "
                        "WorkqueueSnooze reconciliation or a reviewed zero-data "
                        "disposition remains required."
                    ),
                    "docs/designs/AGENT_WORKQUEUE_SOT.md",
                ],
                "state": "in_progress",
            },
            "rollback": {
                "evidence": [
                    (
                        "The code rollback retains native source authority; the "
                        "production traffic rollback and snooze-data procedure must "
                        "be rehearsed before cutover."
                    )
                ],
                "state": "in_progress",
            },
            "shadow_verification": {
                "evidence": [
                    (
                        "Provider parity tests are green; production scope, membership, "
                        "ordering-band, and action comparison remains pending."
                    )
                ],
                "state": "in_progress",
            },
        },
        "retirement": {
            "crm_route_deleted": {
                "evidence": ["CRM route remains at the pinned source revision."],
                "state": "in_progress",
            },
            "fallback_removed": {
                "evidence": [
                    "CRM route, templates, action dispatcher, and snooze writer remain."
                ],
                "state": "in_progress",
            },
            "zero_traffic": {
                "evidence": [
                    (
                        "The 30-day Loki and VictoriaMetrics observation window has "
                        "not started."
                    )
                ],
                "state": "in_progress",
            },
        },
    }


def _new_module_tracking(module: dict[str, Any]) -> dict[str, Any]:
    tracking = {
        "assessment_state": "inventory_only",
        "decision": {
            "notes": None,
            "owner_service": None,
            "state": "unassessed",
        },
        "retirement": {
            "crm_module_deleted": _evidence_gate(),
            "fallback_removed": _evidence_gate(),
            "zero_traffic": _evidence_gate(),
        },
        "target_slice": None,
    }
    file = module["file"]
    if file in TEAM_INBOX_MODULES:
        tracking = _deep_merge(
            tracking,
            {
                "assessment_state": "implementation_in_progress",
                "decision": {
                    "notes": (
                        "PRs #1602 through #1611 materially completed native Inbox "
                        "operator capabilities and PR #1615 added the field-job "
                        "conversation lifecycle on the reviewed Sub target revision. "
                        "The CRM module spans route-specific registered "
                        "communications.team_inbox_* owners. History migration, "
                        "channel configuration and traffic cutover, authenticated "
                        "browser parity, fallback removal, zero traffic, and CRM "
                        "source deletion remain."
                    ),
                    "owner_service": None,
                    "state": "in_progress",
                },
                "target_slice": "team-inbox-history-channel-and-crm-cutover",
            },
        )
    return _deep_merge(tracking, MODULE_REVIEW_OVERRIDES.get(file, {}))


def _new_route_tracking(route: dict[str, Any] | None = None) -> dict[str, Any]:
    tracking = {
        "assessment_state": "inventory_only",
        "production_usage": {"evidence": [], "state": "unknown"},
        "replacement": {
            "capability": None,
            "kind": "unassigned",
            "notes": None,
            "owner_service": None,
            "surfaces": [],
        },
        "parity": {
            "audit": _evidence_gate(),
            "behavior": _evidence_gate(),
            "errors": _evidence_gate(),
            "events": _evidence_gate(),
            "idempotency": _evidence_gate(),
            "permissions": _evidence_gate(),
        },
        "migration": {
            "callers": _evidence_gate(),
            "cutover": _evidence_gate(),
            "data": _evidence_gate(),
            "rollback": _evidence_gate(),
            "shadow_verification": _evidence_gate(),
        },
        "retirement": {
            "crm_route_deleted": _evidence_gate(),
            "fallback_removed": _evidence_gate(),
            "zero_traffic": _evidence_gate(),
        },
    }
    if route is None:
        return tracking
    tracking = _deep_merge(tracking, _service_team_route_tracking(route))
    return _deep_merge(tracking, _workqueue_route_tracking(route))


def _merge_tracking(
    source_records: list[dict[str, Any]],
    existing_records: dict[str, dict[str, Any]],
    *,
    key: str,
    default_factory: Any,
    sparse_default: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for source_record in source_records:
        identity = source_record[key]
        existing = existing_records.get(identity, {})
        default = default_factory(source_record)
        existing_tracking = {
            name: value
            for name, value in existing.get("tracking", {}).items()
            if name != "triage_basis"
        }
        tracking = _deep_merge(default, existing_tracking)
        merged.append(
            {
                **source_record,
                "tracking": _tracking_overrides(
                    tracking,
                    sparse_default if sparse_default is not None else default,
                ),
            }
        )
    return merged


def _deep_merge(base: Any, override: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override
    merged = {key: _copy_json_value(value) for key, value in base.items()}
    for key, value in override.items():
        merged[key] = (
            _deep_merge(merged[key], value) if key in merged else _deep_merge({}, value)
        )
    return merged


def _copy_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _copy_json_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_copy_json_value(child) for child in value]
    return value


def _tracking_overrides(value: Any, default: Any) -> Any:
    if value == default:
        return {}
    if isinstance(value, dict) and isinstance(default, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if key not in default:
                result[key] = child
                continue
            if child != default[key]:
                result[key] = _tracking_overrides(child, default[key])
        return result
    return value


def build_ledger(
    crm_root: Path,
    *,
    source_revision: str,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ledger while preserving tracking data for stable source IDs."""

    modules, routes = scan_crm(crm_root)
    existing = existing or {}
    existing_modules = {
        item["file"]: item for item in existing.get("modules", []) if "file" in item
    }
    existing_routes = {
        item["id"]: item for item in existing.get("routes", []) if "id" in item
    }
    merged_modules = _merge_tracking(
        modules,
        existing_modules,
        key="file",
        default_factory=_new_module_tracking,
        sparse_default=_new_module_tracking({"file": "", "classification": ""}),
    )
    merged_routes = _merge_tracking(
        routes,
        existing_routes,
        key="id",
        default_factory=_new_route_tracking,
        sparse_default=_new_route_tracking(),
    )
    method_counts = Counter(route["source"]["method"] for route in merged_routes)
    category_counts = Counter(module["classification"] for module in merged_modules)
    return {
        "schema_version": SCHEMA_VERSION,
        "goal": (
            "Build every operational capability from every dotmac_crm web module "
            "in Sub, migrate callers/data/traffic/jobs, prove parity and cutover, "
            "and retire dotmac_crm."
        ),
        "source": {
            "method_counts": dict(sorted(method_counts.items())),
            "module_classification_counts": dict(sorted(category_counts.items())),
            "module_count": len(merged_modules),
            "repository": "dotmac_crm",
            "revision": source_revision,
            "route_count": len(merged_routes),
            "web_root": CRM_WEB_ROOT.as_posix(),
        },
        "target": {
            "merged_pull_requests_reviewed": list(REVIEWED_SUB_PULL_REQUESTS),
            "repository": "dotmac_sub",
            "revision": DEFAULT_SUB_REVISION,
        },
        "completion_rule": (
            "A route is retired only when its replacement owner and surface or "
            "explicit removal are reviewed; behavior and controls are verified; "
            "data, callers, shadow comparison, cutover, rollback, fallback removal, "
            "zero traffic, and CRM deletion have evidence."
        ),
        "classification_descriptions": CLASSIFICATION_DESCRIPTIONS,
        "zero_traffic_evidence_contract": ZERO_TRAFFIC_EVIDENCE_CONTRACT,
        "tracking_defaults": {
            "module": _new_module_tracking({"file": "", "classification": ""}),
            "route": _new_route_tracking(),
        },
        "modules": merged_modules,
        "routes": merged_routes,
    }


def _validate_evidence_gate(
    gate: Any,
    *,
    context: str,
    errors: list[str],
    require_verified: bool,
) -> None:
    if not isinstance(gate, dict):
        errors.append(f"{context} must be an object")
        return
    state = gate.get("state")
    evidence = gate.get("evidence")
    if state not in EVIDENCE_STATES:
        errors.append(f"{context}.state has invalid value {state!r}")
    if not isinstance(evidence, list):
        errors.append(f"{context}.evidence must be a list")
    if state in {"not_applicable", "verified"} and not evidence:
        errors.append(f"{context} needs evidence for state {state!r}")
    if require_verified and state not in {"not_applicable", "verified"}:
        errors.append(f"{context} must be verified before retirement")


def _parse_evidence_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _validate_zero_traffic_gate(
    gate: Any,
    *,
    context: str,
    errors: list[str],
    require_verified: bool,
) -> None:
    _validate_evidence_gate(
        gate,
        context=context,
        errors=errors,
        require_verified=require_verified,
    )
    if not isinstance(gate, dict) or gate.get("state") != "verified":
        return
    evidence = gate.get("evidence")
    if not isinstance(evidence, list):
        return
    compliant_records = 0
    required_fields = ZERO_TRAFFIC_EVIDENCE_CONTRACT["required_record_fields"]
    for index, record in enumerate(evidence):
        record_context = f"{context}.evidence[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{record_context} must be a structured traffic record")
            continue
        missing = [
            field
            for field in required_fields
            if field not in record or record[field] in (None, "", [])
        ]
        if missing:
            errors.append(f"{record_context} is missing fields {missing}")
            continue
        started_at = _parse_evidence_instant(record["window_started_at"])
        ended_at = _parse_evidence_instant(record["window_ended_at"])
        if started_at is None or ended_at is None:
            errors.append(f"{record_context} needs RFC 3339 observation timestamps")
            continue
        if started_at.tzinfo is None or ended_at.tzinfo is None:
            errors.append(f"{record_context} observation timestamps need timezones")
            continue
        if (ended_at - started_at).total_seconds() < (
            ZERO_TRAFFIC_MINIMUM_DAYS * 24 * 60 * 60
        ):
            errors.append(
                f"{record_context} must cover at least {ZERO_TRAFFIC_MINIMUM_DAYS} days"
            )
            continue
        if record["loki_request_count"] != 0:
            errors.append(f"{record_context}.loki_request_count must be zero")
            continue
        if record["victoriametrics_request_count"] != 0:
            errors.append(
                f"{record_context}.victoriametrics_request_count must be zero"
            )
            continue
        health = record["telemetry_health_evidence"]
        if not isinstance(health, list) or not all(
            isinstance(item, str) and item.strip() for item in health
        ):
            errors.append(
                f"{record_context}.telemetry_health_evidence "
                "must be a non-empty list of durable references"
            )
            continue
        compliant_records += 1
    if compliant_records == 0:
        errors.append(f"{context} needs a compliant zero-traffic observation record")


def ledger_validation_errors(ledger: dict[str, Any]) -> tuple[str, ...]:
    """Return all structural and completion-gate violations in a ledger."""

    errors: list[str] = []
    if ledger.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if ledger.get("zero_traffic_evidence_contract") != ZERO_TRAFFIC_EVIDENCE_CONTRACT:
        errors.append(
            "zero_traffic_evidence_contract must match the checked-in "
            "observability contract"
        )
    source = ledger.get("source", {})
    expected_source = {
        "module_count": EXPECTED_MODULE_COUNT,
        "route_count": EXPECTED_ROUTE_COUNT,
        "method_counts": EXPECTED_METHOD_COUNTS,
    }
    for key, expected in expected_source.items():
        if source.get(key) != expected:
            errors.append(f"source.{key} must be {expected!r}")
    target = ledger.get("target", {})
    if target.get("repository") != "dotmac_sub":
        errors.append("target.repository must be 'dotmac_sub'")
    if target.get("revision") != DEFAULT_SUB_REVISION:
        errors.append(f"target.revision must be {DEFAULT_SUB_REVISION!r}")
    if target.get("merged_pull_requests_reviewed") != list(REVIEWED_SUB_PULL_REQUESTS):
        errors.append(
            "target.merged_pull_requests_reviewed must match the reviewed baseline"
        )

    modules = ledger.get("modules")
    routes = ledger.get("routes")
    tracking_defaults = ledger.get("tracking_defaults", {})
    module_tracking_default = tracking_defaults.get("module")
    route_tracking_default = tracking_defaults.get("route")
    if not isinstance(module_tracking_default, dict):
        errors.append("tracking_defaults.module must be an object")
        module_tracking_default = {}
    if not isinstance(route_tracking_default, dict):
        errors.append("tracking_defaults.route must be an object")
        route_tracking_default = {}
    if not isinstance(modules, list):
        return (*errors, "modules must be a list")
    if not isinstance(routes, list):
        return (*errors, "routes must be a list")

    files = [module.get("file") for module in modules]
    if len(files) != len(set(files)):
        errors.append("module file identities must be unique")
    expected_files = {
        file
        for files_in_category in MODULE_CLASSIFICATIONS.values()
        for file in files_in_category
    }
    actual_files = {file for file in files if isinstance(file, str)}
    missing_classifications = sorted(expected_files - actual_files)
    unexpected_files = sorted(actual_files - expected_files)
    if missing_classifications:
        errors.append(f"classified modules missing: {missing_classifications}")
    if unexpected_files:
        errors.append(f"unclassified modules present: {unexpected_files}")

    category_counts = Counter(module.get("classification") for module in modules)
    expected_category_counts = {
        category: len(files) for category, files in MODULE_CLASSIFICATIONS.items()
    }
    if dict(sorted(category_counts.items())) != dict(
        sorted(expected_category_counts.items())
    ):
        errors.append(
            "module classification counts differ: "
            f"{dict(sorted(category_counts.items()))!r}"
        )

    ids = [route.get("id") for route in routes]
    if len(ids) != len(set(ids)):
        errors.append("route identities must be unique")
    route_count_by_file = Counter(
        route.get("source", {}).get("file") for route in routes
    )
    route_states_by_file: dict[str, list[str | None]] = defaultdict(list)
    for route in routes:
        route_tracking = _deep_merge(
            route_tracking_default,
            route.get("tracking", {}),
        )
        route_states_by_file[route.get("source", {}).get("file")].append(
            route_tracking.get("assessment_state")
        )
    for module in modules:
        context = f"module {module.get('file')}"
        classification = module.get("classification")
        if classification not in MODULE_CLASSIFICATIONS:
            errors.append(f"{context} has invalid classification {classification!r}")
        if module.get("route_count") != route_count_by_file[module.get("file")]:
            errors.append(f"{context} route_count differs from route inventory")
        tracking = _deep_merge(
            module_tracking_default,
            module.get("tracking", {}),
        )
        state = tracking.get("assessment_state")
        if state not in ASSESSMENT_STATES:
            errors.append(f"{context} has invalid assessment_state {state!r}")
        decision = tracking.get("decision", {})
        if state != "inventory_only":
            if not tracking.get("target_slice"):
                errors.append(f"{context} needs a target_slice after assessment")
            if decision.get("state") == "unassessed" or not decision.get("notes"):
                errors.append(f"{context} needs a reviewed decision after assessment")
        require_retired = state == "retired"
        if decision.get("state") not in EVIDENCE_STATES:
            errors.append(f"{context} has invalid owner decision state")
        owner_service = decision.get("owner_service")
        if isinstance(owner_service, str) and "*" in owner_service:
            errors.append(f"{context} owner_service must name one exact owner")
        if require_retired and (
            decision.get("state") != "verified"
            or not decision.get("owner_service")
            or not decision.get("notes")
        ):
            errors.append(
                f"{context} needs a verified owner decision before retirement"
            )
        if require_retired and any(
            route_state != "retired"
            for route_state in route_states_by_file[module.get("file")]
        ):
            errors.append(f"{context} still has routes that are not retired")
        for name, gate in tracking.get("retirement", {}).items():
            validator = (
                _validate_zero_traffic_gate
                if name == "zero_traffic"
                else _validate_evidence_gate
            )
            validator(
                gate,
                context=f"{context}.retirement.{name}",
                errors=errors,
                require_verified=require_retired,
            )

    for route in routes:
        route_id = route.get("id")
        context = f"route {route_id}"
        source_route = route.get("source", {})
        if source_route.get("file") not in actual_files:
            errors.append(f"{context} references an unknown module")
        if not source_route.get("mounted"):
            errors.append(f"{context} is not reachable from app.web.build_router")
        tracking = _deep_merge(
            route_tracking_default,
            route.get("tracking", {}),
        )
        state = tracking.get("assessment_state")
        if state not in ASSESSMENT_STATES:
            errors.append(f"{context} has invalid assessment_state {state!r}")
        usage = tracking.get("production_usage", {})
        if usage.get("state") not in USAGE_STATES:
            errors.append(f"{context} has invalid production usage state")
        if not isinstance(usage.get("evidence"), list):
            errors.append(f"{context}.production_usage.evidence must be a list")
        replacement = tracking.get("replacement", {})
        if replacement.get("kind") not in REPLACEMENT_KINDS:
            errors.append(f"{context} has invalid replacement kind")

        require_retired = state == "retired"
        if require_retired:
            if usage.get("state") == "unknown" or not usage.get("evidence"):
                errors.append(f"{context} needs production usage evidence")
            if replacement.get("kind") == "unassigned":
                errors.append(f"{context} needs a replacement disposition")
            if not replacement.get("owner_service"):
                errors.append(f"{context} needs a replacement owner")
            if not replacement.get("capability"):
                errors.append(f"{context} needs a replacement capability")
            if replacement.get("kind") not in {
                "explicit_removal",
                "redirect",
            } and not replacement.get("surfaces"):
                errors.append(f"{context} needs at least one replacement surface")

        for group in ("parity", "migration", "retirement"):
            gates = tracking.get(group)
            if not isinstance(gates, dict):
                errors.append(f"{context}.{group} must be an object")
                continue
            for name, gate in gates.items():
                validator = (
                    _validate_zero_traffic_gate
                    if group == "retirement" and name == "zero_traffic"
                    else _validate_evidence_gate
                )
                validator(
                    gate,
                    context=f"{context}.{group}.{name}",
                    errors=errors,
                    require_verified=require_retired,
                )
    return tuple(errors)


def load_ledger(path: Path = DEFAULT_LEDGER) -> dict[str, Any]:
    """Load one checked-in retirement ledger."""

    return json.loads(path.read_text(encoding="utf-8"))


def _compare_source(
    ledger: dict[str, Any],
    *,
    crm_root: Path,
    source_revision: str,
) -> tuple[str, ...]:
    regenerated = build_ledger(
        crm_root,
        source_revision=source_revision,
        existing=ledger,
    )
    errors: list[str] = []
    if ledger.get("source") != regenerated.get("source"):
        errors.append(
            "checked-in source summary differs from the requested CRM revision"
        )
    for key in ("modules", "routes"):
        current_source = [
            {name: value for name, value in item.items() if name != "tracking"}
            for item in ledger.get(key, [])
        ]
        regenerated_source = [
            {name: value for name, value in item.items() if name != "tracking"}
            for item in regenerated.get(key, [])
        ]
        if current_source != regenerated_source:
            errors.append(
                f"checked-in {key} inventory differs from the requested CRM revision"
            )
    return tuple(errors)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("refresh", "validate"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
        command_parser.add_argument("--crm-root", type=Path)
        command_parser.add_argument("--source-revision")
    return parser


def main() -> int:
    """Run the ledger refresh or validation command."""

    args = _parser().parse_args()
    if args.command == "refresh":
        if args.crm_root is None or not args.source_revision:
            raise SystemExit("refresh requires --crm-root and --source-revision")
        existing = load_ledger(args.ledger) if args.ledger.exists() else None
        ledger = build_ledger(
            args.crm_root,
            source_revision=args.source_revision,
            existing=existing,
        )
        errors = ledger_validation_errors(ledger)
        if errors:
            raise SystemExit("\n".join(errors))
        args.ledger.parent.mkdir(parents=True, exist_ok=True)
        args.ledger.write_text(
            json.dumps(ledger, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            f"wrote {args.ledger}: {len(ledger['modules'])} modules, "
            f"{len(ledger['routes'])} routes"
        )
        return 0

    ledger = load_ledger(args.ledger)
    errors = list(ledger_validation_errors(ledger))
    if args.crm_root is not None or args.source_revision:
        if args.crm_root is None or not args.source_revision:
            raise SystemExit(
                "source comparison requires --crm-root and --source-revision"
            )
        errors.extend(
            _compare_source(
                ledger,
                crm_root=args.crm_root,
                source_revision=args.source_revision,
            )
        )
    if errors:
        raise SystemExit("\n".join(errors))
    print(
        f"valid: {ledger['source']['module_count']} modules, "
        f"{ledger['source']['route_count']} routes at "
        f"{ledger['source']['revision']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
