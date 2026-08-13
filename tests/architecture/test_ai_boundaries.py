"""Architecture guard: AI is advisory — it never writes domain state.

``docs/designs/AI_SOT.md`` / SOT map §AI Control Plane. AI observes, derives,
and recommends; acting on a recommendation means calling the domain's declared
owner (``support.ticket_lifecycle``, ``operations.work_orders``,
``operations.project_lifecycle``, ``communications.team_inbox_commands``), which applies
its own guards, events, and audit.

The failure this prevents: an LLM suggestion silently becoming a domain
transition that bypassed its owner's rules — an unreviewable authority leak,
and precisely the parallel decision path the source-of-truth standard forbids.

Two invariants:
1. No ``app/services/ai*`` module constructs or session-writes a non-AI ORM
   row (it may write AIInsight/AiIntakeConfig — that is its own derived
   state).
2. ``ai_operations`` is the only writer of ``AIInsight`` and ``ai_intake`` is
   the only writer of ``AiIntakeConfig``.
3. Conversational intake is invoked once from the shared non-email channel
   receiver and contains no messaging or individual-assignment call.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.source_index import python_ast, python_files, source_text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICES = PROJECT_ROOT / "app" / "services"
APP = PROJECT_ROOT / "app"

# The AI family's own derived state — writing these is the point.
_AI_OWNED_MODELS = {"AIInsight", "AiIntakeConfig"}

_WRITE_CALLS = {"add", "add_all", "delete", "merge"}
_SESSION_TOKENS = {"db", "session", "db_session"}


def _ai_modules() -> list[Path]:
    """Every AI service module: the ``ai*``-named files AND the ``ai`` package.

    The package clause is load-bearing. Matching on filename alone covered
    only ``ai_operations.py`` — ``app/services/ai/engine.py`` and
    ``.../gateway.py`` are named ``engine``/``gateway``, so the generation
    slice would have imported the whole guard's blind spot.
    """
    return [
        p
        for p in python_files(SERVICES)
        if p.name.startswith("ai") or "ai" in p.relative_to(SERVICES).parts[:-1]
    ]


def _model_names_written(tree: ast.AST) -> set[str]:
    """Model classes passed to a session write in this module."""
    written: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _WRITE_CALLS:
            continue
        receiver = node.func.value
        if not (isinstance(receiver, ast.Name) and receiver.id in _SESSION_TOKENS):
            continue
        for arg in node.args:
            # db.add(Model(...)) or db.add(existing_row)
            if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
                written.add(arg.func.id)
    return written


def test_ai_services_never_write_domain_rows():
    offenders: list[str] = []
    for path in _ai_modules():
        tree = python_ast(path)
        for model in sorted(_model_names_written(tree) - _AI_OWNED_MODELS):
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}: writes {model}")
    assert not offenders, (
        "AI services must not write domain rows — request the outcome from "
        "the domain's declared owner instead (SOT map §AI Control Plane):\n  "
        + "\n  ".join(offenders)
    )


def test_ai_insight_has_a_single_writer():
    offenders: list[str] = []
    for path in python_files(APP):
        rel = str(path.relative_to(PROJECT_ROOT))
        if rel in {
            "app/services/ai_operations.py",
            "app/models/ai_insight.py",
        }:
            continue
        text = source_text(path)
        if "AIInsight" not in text:
            continue
        tree = python_ast(path)
        if "AIInsight" in _model_names_written(tree):
            offenders.append(rel)
    assert not offenders, (
        "AIInsight rows have one canonical writer (ai_operations.create_insight); "
        "these modules write them directly:\n  " + "\n  ".join(sorted(offenders))
    )


def test_ai_intake_config_has_a_single_writer():
    offenders: list[str] = []
    for path in python_files(APP):
        rel = str(path.relative_to(PROJECT_ROOT))
        if rel in {"app/services/ai_intake.py", "app/models/ai_intake.py"}:
            continue
        tree = python_ast(path)
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "AiIntakeConfig"
            for node in ast.walk(tree)
        ):
            offenders.append(rel)
    assert not offenders, (
        "AiIntakeConfig has one canonical writer (ai_intake.upsert_config):\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_customer_intake_has_one_non_email_inbound_adapter_and_no_side_effect_port():
    callers: list[str] = []
    for path in python_files(APP):
        rel = str(path.relative_to(PROJECT_ROOT))
        if rel == "app/services/ai_intake.py":
            continue
        if "classify_message(" in source_text(path):
            callers.append(rel)
    assert callers == ["app/services/ai_conversation_intake.py"]

    tree = python_ast(SERVICES / "ai_intake.py")
    forbidden_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"send", "reply", "assign", "enqueue", "dispatch"}
    }
    assert forbidden_calls == set()
