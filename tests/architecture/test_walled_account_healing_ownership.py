"""Walled-account healing is an account-bound durable-timer owner command.

Before this it was reachable only from a one-off script while the scheduled
detector hard-coded ``apply=False``. The repair must not add another cohort
scan: committed funding schedules one exact account timer under ADR 0007 §7.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

from app.services.sot_relationships import all_services

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE = PROJECT_ROOT / "tests" / "architecture" / "sot_writer_baseline.txt"
OWNER_MODULE = "app.services.billing.unwall_paid_accounts"
OWNER_NAME = "financial.walled_account_healing"
TIMER_TRIGGER = "financial.walled_account_healing_due"


def _source(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def test_the_healing_owner_is_declared_in_the_registry() -> None:
    services = {service.name: service for service in all_services()}
    owner = services.get(OWNER_NAME)
    assert owner is not None, "walled-account healing has no declared owner"
    assert owner.module == OWNER_MODULE
    assert owner.contract is not None, "new healing owner has no typed contract"


def test_the_healing_owner_left_the_undeclared_writer_baseline() -> None:
    """The baseline is a shrink-only ratchet; a resolved owner must leave it."""
    entries = {
        line.strip()
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert OWNER_MODULE not in entries


def test_funding_events_schedule_an_exact_durable_timer_and_consume_its_fire() -> None:
    handler = _source("app/services/events/handlers/billing_lifecycle_projection.py")
    owner = _source("app/services/billing/unwall_paid_accounts.py")

    assert "EventType.payment_received" in handler
    assert "EventType.account_credit_deposited" in handler
    assert "schedule_walled_account_healing(" in handler
    assert "consume_walled_account_healing_due(" in handler
    assert f'"{TIMER_TRIGGER}"' in owner
    assert "ScheduleTimerCommand(" in owner
    assert "consume_owner_output(" in owner
    assert 'entity_kind="subscriber"' in owner


def test_healing_adds_no_business_wide_scheduler_or_celery_task() -> None:
    tasks = _source("app/tasks/enforcement.py")
    scheduled = _source("app/services/enforcement_scheduled.py")
    beat = _source("app/services/scheduler_config.py")
    owner = _source("app/services/billing/unwall_paid_accounts.py")

    assert "heal_walled_paid_accounts" not in tasks
    assert "heal_walled_paid_accounts" not in scheduled
    assert 'name="walled_account_healing"' not in beat
    assert "run_scheduled_walled_account_healing" not in owner
    assert 'name="durable_timer_dispatch_runner"' in beat


def test_automated_application_requires_proven_zero_overdue_receivable() -> None:
    owner = _source("app/services/billing/unwall_paid_accounts.py")

    assert "def decide_unwall(" in owner
    assert "lock_account(" in owner, "the recomputation must hold an account lock"
    assert "overdue_receivable_snapshot(" in owner
    assert "require_zero_overdue_receivable" in owner
    assert "_stage_unwall_exception(" in owner


_TOLERANCE_TOKENS = ("epsilon", "tolerance", "de_minimis", "deminimis", "fudge")


def _identifiers(tree: ast.Module) -> set[str]:
    """Every name this module actually binds or reads, excluding prose."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            names.add(node.attr.lower())
        elif isinstance(node, ast.arg):
            names.add(node.arg.lower())
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name.lower())
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg.lower())
    return names


def _decimal_literals(tree: ast.Module) -> set[str]:
    """String literals handed straight to ``Decimal(...)`` — money constants."""
    literals: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "Decimal" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            literals.add(first.value)
    return literals


def test_healing_introduces_no_money_tolerance() -> None:
    """Exact arithmetic: no epsilon, tolerance, or de-minimis threshold.

    Checked against the module's real identifiers and money constants rather
    than a substring sweep of the source, so a comment or docstring *stating*
    that no tolerance exists cannot trip the guard — and, more importantly,
    rewording prose cannot smuggle one past it either.
    """
    tree = ast.parse(
        (PROJECT_ROOT / "app/services/billing/unwall_paid_accounts.py").read_text(
            encoding="utf-8"
        )
    )

    offending = sorted(
        name
        for name in _identifiers(tree)
        for token in _TOLERANCE_TOKENS
        if token in name
    )
    assert not offending, (
        "identifiers suggest a money tolerance was introduced: " + ", ".join(offending)
    )

    non_zero = sorted(
        value for value in _decimal_literals(tree) if Decimal(value) != Decimal("0")
    )
    assert not non_zero, (
        "a non-zero Decimal constant in the healing owner would act as a "
        "de-minimis threshold: " + ", ".join(non_zero)
    )
