"""Prevent static HTML form fields and actions with no canonical consumer.

Global coverage rejects names absent from Python's request/schema vocabulary.
Route-scoped coverage then matches statically resolvable parent actions,
inline-Jinja branches, and submit-button ``formaction`` destinations to the
actual application boundary and its direct service/schema contracts. Explicit
client-side consumers remain local to the named element.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from tests.architecture.source_index import files, python_ast, python_files, source_text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
TEMPLATE_DIR = PROJECT_ROOT / "templates"

_FORM_TAG = re.compile(r"<(?:input|select|textarea|button)\b[^>]*>", re.I | re.S)
_FORM_BLOCK = re.compile(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>", re.I | re.S)
_STATIC_NAME = re.compile(
    r"(?<![:\w-])name\s*=\s*[\"']([A-Za-z_][A-Za-z0-9_.-]*)[\"']", re.I
)
_CLIENT_CONSUMER = re.compile(r"\bdata-form-consumer\s*=\s*[\"']client[\"']", re.I)
_ACTION = re.compile(
    r"\baction\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)')", re.I
)
_FORMACTION = re.compile(
    r"\bformaction\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)')", re.I
)
_METHOD = re.compile(r"\bmethod\s*=\s*[\"'](get|post|put|patch|delete)[\"']", re.I)
_FORMMETHOD = re.compile(
    r"\bformmethod\s*=\s*[\"'](get|post|put|patch|delete)[\"']", re.I
)
_PATH_VALUE = re.compile(r"\{\{.*?\}\}|\{[^{}]+\}")
_JINJA_IF = re.compile(
    r"{%\s*if\b.*?%}(?P<yes>.*?)(?:{%\s*else\s*%}(?P<no>.*?))?{%\s*endif\s*%}",
    re.I | re.S,
)
_DYNAMIC_ACTION = re.compile(r"^\s*\{\{\s*(?P<variable>action_url|form_action)\b", re.I)
_TEMPLATE_SET = re.compile(
    r"{%\s*set\s+(?P<variable>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<expression>.*?)%}",
    re.I | re.S,
)


@dataclass(frozen=True)
class _FormContract:
    template: str
    action: str
    method: str
    fields: frozenset[str]


@dataclass(frozen=True)
class _DynamicFormContract:
    template: str
    variable: str
    method: str
    fields: frozenset[str]


def _server_consumer_symbols() -> set[str]:
    symbols: set[str] = set()
    for path in python_files(APP_DIR):
        try:
            tree = python_ast(path)
        except SyntaxError:  # pragma: no cover - syntax checks fail elsewhere
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                symbols.add(node.value)
            elif isinstance(node, ast.arg):
                symbols.add(node.arg)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                symbols.add(node.target.id)
    return symbols


@cache
def _python_symbols(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        tree = python_ast(path)
    except SyntaxError:  # pragma: no cover - syntax checks fail elsewhere
        return set()
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            symbols.add(node.value)
        elif isinstance(node, ast.arg):
            symbols.add(node.arg)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            symbols.add(node.target.id)
    return symbols


@cache
def _module_path(module: str) -> Path | None:
    relative = Path(*module.split("."))
    module_file = PROJECT_ROOT / relative.with_suffix(".py")
    if module_file.is_file():
        return module_file
    package_file = PROJECT_ROOT / relative / "__init__.py"
    return package_file if package_file.is_file() else None


def _module_name(path: Path) -> str:
    relative = path.relative_to(PROJECT_ROOT).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join(parts)


@cache
def _direct_contract_imports(path: Path) -> set[str]:
    try:
        tree = python_ast(path)
    except SyntaxError:  # pragma: no cover - syntax checks fail elsewhere
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                alias.name
                for alias in node.names
                if alias.name.startswith(("app.services", "app.schemas"))
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(("app.services", "app.schemas")):
                modules.add(node.module)
                for alias in node.names:
                    candidate = f"{node.module}.{alias.name}"
                    if _module_path(candidate) is not None:
                        modules.add(candidate)
    return modules


@cache
def _route_consumer_symbols(module: str) -> set[str]:
    path = _module_path(module)
    if path is None:
        return set()
    symbols = _python_symbols(path)
    for imported in _direct_contract_imports(path):
        imported_path = _module_path(imported)
        if imported_path is not None:
            symbols.update(_python_symbols(imported_path))
            for nested in _direct_contract_imports(imported_path):
                nested_path = _module_path(nested)
                if nested_path is not None:
                    symbols.update(_python_symbols(nested_path))
    # Cross-cutting request consumers apply to every form action.
    symbols.update(_python_symbols(PROJECT_ROOT / "app/main.py"))
    symbols.update(_python_symbols(PROJECT_ROOT / "app/csrf.py"))
    return symbols


def _normalized_path(value: str) -> str:
    path = value.split("?", 1)[0].rstrip("/") or "/"
    return _PATH_VALUE.sub("{}", path)


def _attribute_value(match: re.Match[str]) -> str:
    return match.group("double") or match.group("single") or ""


def _jinja_concat_value(expression: str) -> str:
    fragments: list[str] = []
    for token in expression.split("~"):
        token = token.strip()
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
            fragments.append(token[1:-1])
        else:
            fragments.append("{}")
    return "".join(fragments)


def _jinja_expression_variants(raw_action: str) -> set[str]:
    stripped = raw_action.strip()
    if not (stripped.startswith("{{") and stripped.endswith("}}")):
        return set()
    expression = stripped[2:-2].strip()
    branches: list[str]
    if " if " in expression and " else " in expression:
        yes, conditional = expression.split(" if ", 1)
        _, no = conditional.rsplit(" else ", 1)
        branches = [yes, no]
    elif " or " in expression:
        branches = expression.split(" or ")
    else:
        branches = [expression]
    return {
        value
        for branch in branches
        if (value := _jinja_concat_value(branch)).startswith("/")
    }


def _static_action_variants(raw_action: str) -> set[str]:
    """Expand simple inline Jinja branches into statically checkable paths."""
    expression_variants = _jinja_expression_variants(raw_action)
    if expression_variants:
        return expression_variants
    variants = {raw_action.split("?", 1)[0]}
    while any(_JINJA_IF.search(value) for value in variants):
        expanded: set[str] = set()
        for value in variants:
            match = _JINJA_IF.search(value)
            if match is None:
                expanded.add(value)
                continue
            prefix = value[: match.start()]
            suffix = value[match.end() :]
            expanded.add(f"{prefix}{match.group('yes')}{suffix}")
            expanded.add(f"{prefix}{match.group('no') or ''}{suffix}")
        variants = expanded
    return {
        value
        for value in variants
        if value.startswith("/")
        and "{%" not in value
        and value.count("{{") == value.count("}}")
    }


def _paths_compatible(action: str, route: str) -> bool:
    """Match a rendered form value to a parameterized application route."""
    action_parts = action.split("/")
    route_parts = route.split("/")
    return len(action_parts) == len(route_parts) and all(
        action_part == route_part or "{}" in {action_part, route_part}
        for action_part, route_part in zip(action_parts, route_parts, strict=True)
    )


def _form_fields(body: str) -> frozenset[str]:
    fields: set[str] = set()
    for tag in _FORM_TAG.findall(body):
        name_match = _STATIC_NAME.search(tag)
        if name_match is not None and not _CLIENT_CONSUMER.search(tag):
            fields.add(name_match.group(1))
    return frozenset(fields)


def _static_form_contracts() -> list[_FormContract]:
    contracts: list[_FormContract] = []
    for path in files(TEMPLATE_DIR, "*.html"):
        text = source_text(path)
        for form in _FORM_BLOCK.finditer(text):
            method_match = _METHOD.search(form.group("attrs"))
            form_method = method_match.group(1).upper() if method_match else "GET"
            fields = _form_fields(form.group("body"))
            destinations: set[tuple[str, str]] = set()
            action_match = _ACTION.search(form.group("attrs"))
            if action_match is not None:
                destinations.update(
                    (action, form_method)
                    for action in _static_action_variants(
                        _attribute_value(action_match)
                    )
                )
            for tag in _FORM_TAG.findall(form.group("body")):
                formaction_match = _FORMACTION.search(tag)
                if formaction_match is not None:
                    formmethod_match = _FORMMETHOD.search(tag)
                    destinations.update(
                        (
                            action,
                            (
                                formmethod_match.group(1).upper()
                                if formmethod_match
                                else form_method
                            ),
                        )
                        for action in _static_action_variants(
                            _attribute_value(formaction_match)
                        )
                    )
            for raw_action, method in destinations:
                contracts.append(
                    _FormContract(
                        template=str(path.relative_to(PROJECT_ROOT)),
                        action=_normalized_path(raw_action),
                        method=method,
                        fields=fields,
                    )
                )
    return contracts


def _dynamic_form_contracts() -> list[_DynamicFormContract]:
    contracts: list[_DynamicFormContract] = []
    for path in files(TEMPLATE_DIR, "*.html"):
        text = source_text(path)
        for form in _FORM_BLOCK.finditer(text):
            action_match = _ACTION.search(form.group("attrs"))
            if action_match is None:
                continue
            dynamic = _DYNAMIC_ACTION.search(_attribute_value(action_match))
            if dynamic is None:
                continue
            method_match = _METHOD.search(form.group("attrs"))
            contracts.append(
                _DynamicFormContract(
                    template=str(path.relative_to(PROJECT_ROOT)),
                    variable=dynamic.group("variable"),
                    method=(method_match.group(1).upper() if method_match else "GET"),
                    fields=_form_fields(form.group("body")),
                )
            )
    return contracts


def _path_expression_values(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value} if node.value.startswith("/") else set()
    if isinstance(node, ast.JoinedStr):
        values = {""}
        for part in node.values:
            fragments = (
                {part.value}
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
                else {"{}"}
            )
            values = {left + right for left in values for right in fragments}
        return {value for value in values if value.startswith("/")}
    if isinstance(node, ast.IfExp):
        return _path_expression_values(node.body) | _path_expression_values(node.orelse)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _path_expression_values(node.left)
        right = _path_expression_values(node.right)
        return {a + b for a in left for b in right}
    return set()


def _target_context_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):
        slice_node = node.slice
        if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
            return slice_node.value
    return None


@cache
def _context_action_paths(path: Path, variable: str) -> set[str]:
    try:
        tree = python_ast(path)
    except SyntaxError:  # pragma: no cover - syntax checks fail elsewhere
        return set()
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == variable
                    and value is not None
                ):
                    values.update(_path_expression_values(value))
        elif isinstance(node, ast.keyword) and node.arg == variable:
            values.update(_path_expression_values(node.value))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_target_context_name(target) == variable for target in targets):
                values.update(_path_expression_values(node.value))
    return {_normalized_path(value) for value in values}


@cache
def _template_context_producer_modules(path: Path, template_name: str) -> set[str]:
    try:
        tree = python_ast(path)
    except SyntaxError:  # pragma: no cover - syntax checks fail elsewhere
        return set()

    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app.services"):
                    aliases[alias.asname or alias.name.split(".")[-1]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "app.services":
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"app.services.{alias.name}"
            elif node.module.startswith("app.services"):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = node.module

    producers: set[str] = set()
    for function in (
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        if not any(
            isinstance(node, ast.Constant) and node.value == template_name
            for node in ast.walk(function)
        ):
            continue
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            if isinstance(call.func, ast.Name) and call.func.id in aliases:
                producers.add(aliases[call.func.id])
            elif (
                isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id in aliases
            ):
                producers.add(aliases[call.func.value.id])
    return producers


@cache
def _reverse_contract_imports() -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = {}
    for path in python_files(APP_DIR):
        importer = _module_name(path)
        for imported in _direct_contract_imports(path):
            if _module_path(imported) is not None:
                reverse.setdefault(imported, set()).add(importer)
    return reverse


@cache
def _template_action_paths(template: str, variable: str) -> set[str]:
    template_path = PROJECT_ROOT / template
    local_values = {
        _normalized_path(value)
        for match in _TEMPLATE_SET.finditer(source_text(template_path))
        if match.group("variable") == variable
        for value in _jinja_expression_variants(
            "{{ " + match.group("expression") + " }}"
        )
    }
    template_name = template.removeprefix("templates/")
    module_paths = [
        path for path in python_files(APP_DIR) if template_name in _python_symbols(path)
    ]
    seed_modules = {_module_name(path) for path in module_paths}
    candidates = set(seed_modules)
    for path in module_paths:
        candidates.update(_template_context_producer_modules(path, template_name))

    values: set[str] = set()
    for module in candidates:
        path = _module_path(module)
        if path is not None:
            values.update(_context_action_paths(path, variable))
    if local_values or values:
        return local_values | values

    reverse = _reverse_contract_imports()
    frontier = set(seed_modules)
    for _ in range(2):
        frontier = {
            importer for module in frontier for importer in reverse.get(module, set())
        }
        for module in frontier:
            path = _module_path(module)
            if path is not None:
                values.update(_context_action_paths(path, variable))
        if values:
            break
    return local_values | values


@cache
def _application_route_modules() -> dict[tuple[str, str], set[str]]:
    from fastapi import FastAPI
    from fastapi.routing import APIRoute

    from app.main import (
        _CORE_ROUTER_SPECS,
        _DEFERRED_API_ROUTER_SPECS,
        _apply_router_spec,
    )

    contract_app = FastAPI()
    for spec in (*_CORE_ROUTER_SPECS, *_DEFERRED_API_ROUTER_SPECS):
        if spec[2] in {"web", "admin"}:
            _apply_router_spec(contract_app, spec)

    route_modules: dict[tuple[str, str], set[str]] = {}
    for route in contract_app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = _normalized_path(route.path)
        for method in route.methods or set():
            route_modules.setdefault((path, method.upper()), set()).add(
                route.endpoint.__module__
            )
    return route_modules


def _contract_failures(
    contracts: list[_FormContract],
    route_modules: dict[tuple[str, str], set[str]],
) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    unmatched: list[str] = []
    symbol_cache: dict[str, set[str]] = {}
    for contract in contracts:
        modules = {
            module
            for (route_path, route_method), candidates in route_modules.items()
            if route_method == contract.method
            and _paths_compatible(contract.action, route_path)
            for module in candidates
        }
        if not modules:
            unmatched.append(
                f"{contract.template}: {contract.method} {contract.action}"
            )
            continue
        consumers: set[str] = set()
        for module in modules:
            consumers.update(
                symbol_cache.setdefault(module, _route_consumer_symbols(module))
            )
        for field in sorted(contract.fields - consumers):
            missing.append(
                f"{contract.template}: {contract.method} {contract.action} -> {field}"
            )
    return missing, unmatched


def _template_fields() -> tuple[dict[str, set[str]], set[str]]:
    locations: dict[str, set[str]] = {}
    client_consumed: set[str] = set()
    for path in files(TEMPLATE_DIR, "*.html"):
        text = source_text(path)
        for tag in _FORM_TAG.findall(text):
            match = _STATIC_NAME.search(tag)
            if match is None:
                continue
            name = match.group(1)
            locations.setdefault(name, set()).add(str(path.relative_to(PROJECT_ROOT)))
            if _CLIENT_CONSUMER.search(tag):
                client_consumed.add(name)
    return locations, client_consumed


def test_every_static_form_field_has_a_consumer() -> None:
    fields, client_consumed = _template_fields()
    server_consumed = _server_consumer_symbols()
    missing = {
        name: paths
        for name, paths in fields.items()
        if name not in server_consumed and name not in client_consumed
    }

    details = [
        f"{name}: {', '.join(sorted(paths))}" for name, paths in sorted(missing.items())
    ]
    assert not missing, (
        "static form fields with no server consumer or explicit client-side "
        "consumer. Remove inert names, wire the canonical consumer, or annotate "
        'a real browser/JavaScript consumer with data-form-consumer="client":\n  '
        + "\n  ".join(details)
    )


def test_client_consumer_annotations_are_attached_to_named_fields() -> None:
    fields, client_consumed = _template_fields()
    stale = sorted(client_consumed - fields.keys())
    assert not stale, (
        "client form-consumer annotations without named fields: " + ", ".join(stale)
    )


def test_typeahead_display_inputs_do_not_submit_shadow_values() -> None:
    violations: list[str] = []
    for path in files(TEMPLATE_DIR, "*.html"):
        text = source_text(path)
        for tag in _FORM_TAG.findall(text):
            match = _STATIC_NAME.search(tag)
            if "data-typeahead-input" in tag and match is not None:
                violations.append(f"{path.relative_to(PROJECT_ROOT)}: {match.group(1)}")
    assert not violations, (
        "typeahead display inputs must be unnamed; submit only their canonical "
        "data-typeahead-hidden identifier:\n  " + "\n  ".join(sorted(violations))
    )


def test_static_form_actions_use_fields_consumed_by_their_route_boundary() -> None:
    """Tighten global name coverage to the actual statically matched action."""
    missing, unmatched = _contract_failures(
        _static_form_contracts(), _application_route_modules()
    )

    assert not unmatched, (
        "static form actions with no matching application route:\n  "
        + "\n  ".join(sorted(unmatched))
    )
    assert not missing, (
        "form fields absent from their matched route module and its direct "
        "service/schema dependencies:\n  " + "\n  ".join(sorted(missing))
    )


def test_runtime_supplied_form_actions_have_route_and_field_provenance() -> None:
    """Trace action_url/form_action producers into real route contracts."""
    contracts: list[_FormContract] = []
    unresolved: list[str] = []
    for dynamic in _dynamic_form_contracts():
        actions = _template_action_paths(dynamic.template, dynamic.variable)
        if not actions:
            unresolved.append(f"{dynamic.template}: {dynamic.variable}")
            continue
        contracts.extend(
            _FormContract(
                template=dynamic.template,
                action=action,
                method=dynamic.method,
                fields=dynamic.fields,
            )
            for action in actions
        )

    assert not unresolved, (
        "runtime-supplied form actions with no statically traceable producer:\n  "
        + "\n  ".join(sorted(unresolved))
    )

    missing, unmatched = _contract_failures(contracts, _application_route_modules())
    assert not unmatched, (
        "runtime-supplied form action values with no matching application route:\n  "
        + "\n  ".join(sorted(unmatched))
    )
    assert not missing, (
        "runtime-supplied form fields absent from their matched route module "
        "and its direct service/schema dependencies:\n  " + "\n  ".join(sorted(missing))
    )
