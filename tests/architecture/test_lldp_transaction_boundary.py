"""Prevent database lifetimes from spanning fleet LLDP network I/O."""

import ast
from pathlib import Path


def test_collection_has_no_session_input_or_database_calls() -> None:
    tree = ast.parse(Path("app/services/topology/lldp_poller.py").read_text())
    collector = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "poll_all"
    )
    assert not collector.args.args
    assert collector.args.kwonlyargs[0].arg == "snapshot"
    forbidden = {
        "query",
        "execute",
        "get",
        "commit",
        "rollback",
        "flush",
        "create_session",
    }
    for node in ast.walk(collector):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # Mapping lookups are permitted; session-bearing receivers are not.
            assert not (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"db", "session"}
                and node.func.attr in forbidden
            )


def test_task_has_separate_session_scopes_and_no_commit() -> None:
    tree = ast.parse(Path("app/tasks/topology_lldp.py").read_text())
    task = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_lldp_topology_poll"
    )
    scopes = [node for node in ast.walk(task) if isinstance(node, ast.With)]
    assert len(scopes) == 2
    for scope in scopes:
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "poll_all"
            for node in ast.walk(scope)
        )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit"
        for node in ast.walk(task)
    )
    handlers = [node for node in ast.walk(task) if isinstance(node, ast.ExceptHandler)]
    assert all(
        any(isinstance(node, ast.Raise) for node in ast.walk(handler))
        for handler in handlers
    )


def test_rest_cannot_resolve_settings_and_write_owner_is_registered() -> None:
    tree = ast.parse(Path("app/services/topology/lldp_poller.py").read_text())
    rest = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_read_neighbors_via_rest"
    )
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "execute"
        for node in ast.walk(rest)
    )
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "reconcile_poll"
    )
    assert (
        sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "execute_owner_command"
            for node in ast.walk(owner)
        )
        == 1
    )
    from app.services.sot_registry.registry import service_relationship

    assert service_relationship("network.lldp_observations").is_contracted
