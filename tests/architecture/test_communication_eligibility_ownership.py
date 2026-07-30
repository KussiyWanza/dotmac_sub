"""One owner decides who we may contact. Keep it that way.

The disease this cures: marketing eligibility was decided inside the campaign
segment filter, where opting in was an optional checkbox. So the answer to "may
we email this person" depended on which sender you asked -- and a customer who
unsubscribed from one path stayed reachable by every other.

A rule with two implementations has no owner. These tests fail if a second one
appears.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.source_index import python_ast, python_files, source_text

ROOT = Path(__file__).resolve().parents[2]

#: The ONE module allowed to decide contact eligibility.
OWNER = "app/services/communication_eligibility.py"

#: The ONE place transports are invoked, and therefore the only place the gate
#: is guaranteed to run. A sender that calls a transport from anywhere else can
#: skip the ledger -- which is the bug, not a style issue.
DELIVERY_POINT = "app/tasks/notifications.py"

TRANSPORT_CALLS = {
    "send_email",
    "send_sms",
    "send_push",
    "send_text_message",
}

#: The transports themselves. These ARE the send.
TRANSPORT_MODULES = {
    "app/services/email.py",
    "app/services/sms.py",
    "app/services/push.py",
}

#: Every path that currently calls a transport directly, skipping the delivery
#: point and therefore the consent ledger.
#:
#: This is NOT an allowlist of "these are fine". It is the migration backlog for
#: SOT finding 9 -- "many billing, project, import, export, mirror and inbox
#: paths call email/SMS/push transports directly [and] can bypass suppression,
#: category preferences, quiet hours, dedupe and auditing".
#:
#: Each of these can mail a customer who has unsubscribed. THIS SET MUST ONLY
#: EVER SHRINK. Converting one to submit a Notification through the queue -- and
#: deleting its line here -- is the unit of progress. Adding a line is a
#: regression, and CI will not tell you off for it, so reviewers must.
LEDGER_BYPASS_BACKLOG = {
    "app/api/crm_webhooks.py",
    "app/services/billing_payment_receipts.py",
    "app/services/crm_ticket_pull.py",
    "app/services/notification_adapter.py",
    "app/services/operational_escalation_delivery.py",
    "app/services/quotes_mirror.py",
    "app/services/web_catalog_subscriptions.py",
    "app/services/web_notifications.py",
    "app/services/web_system_export_tool.py",
    # The NCC report digest goes to internal compliance staff, not customers —
    # it is a regulatory ops email, not a marketing/customer send, so it is
    # not subject to the customer-consent ledger. Direct send is correct here.
    "app/services/ncc_report_email.py",
    "app/tasks/imports.py",
}

KNOWN_DIRECT_SENDERS = TRANSPORT_MODULES | LEDGER_BYPASS_BACKLOG


def _py_files() -> list[Path]:
    return list(python_files(ROOT / "app"))


def test_the_consent_rule_has_exactly_one_implementation() -> None:
    """`may_send` / `is_marketing` must be defined once, in the owner."""
    definitions: dict[str, list[str]] = {"may_send": [], "is_marketing": []}

    for path in _py_files():
        try:
            tree = python_ast(path)
        except SyntaxError:  # pragma: no cover
            continue
        rel = str(path.relative_to(ROOT))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in definitions:
                    definitions[node.name].append(rel)

    for name, found in definitions.items():
        assert found == [OWNER], (
            f"`{name}` must be defined only in {OWNER}, found in: {found}. "
            "A second implementation of the consent rule means the answer "
            "depends on who is asking -- which is the bug this ledger exists "
            "to remove."
        )


def test_no_new_module_calls_a_transport_directly() -> None:
    """Every send must pass the gate in the delivery point.

    A module that calls `send_email` itself can mail an unsubscribed customer
    without ever consulting the ledger.
    """
    offenders: list[str] = []

    for path in _py_files():
        rel = str(path.relative_to(ROOT))
        if rel in KNOWN_DIRECT_SENDERS or rel == DELIVERY_POINT:
            continue
        try:
            tree = python_ast(path)
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name in TRANSPORT_CALLS:
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "These call a transport directly, bypassing the consent ledger:\n  "
        + "\n  ".join(sorted(offenders))
        + f"\n\nSubmit through {DELIVERY_POINT}, which gates on "
        "communication_eligibility.may_send(). If a path genuinely must send "
        "directly, add it to KNOWN_DIRECT_SENDERS *and say why* -- that set is "
        "a to-do list, not an allowlist."
    )


def test_the_delivery_point_actually_consults_the_ledger() -> None:
    """A gate that is not wired in is worse than no gate: it looks safe."""
    source = source_text(ROOT / DELIVERY_POINT)
    assert "communication_eligibility" in source, (
        f"{DELIVERY_POINT} must import the eligibility owner"
    )
    assert "may_send(" in source, (
        f"{DELIVERY_POINT} must gate every send on may_send() -- it is the only "
        "place all four transports are called, so it is the only place the "
        "check is guaranteed to run."
    )


def test_the_bypass_backlog_only_shrinks() -> None:
    """Every module in the backlog must still exist and still bypass.

    If one no longer calls a transport, it has been migrated -- delete its line.
    Leaving a stale entry lets a genuinely new bypass hide behind an old name.
    """
    stale: list[str] = []

    for rel in sorted(LEDGER_BYPASS_BACKLOG):
        path = ROOT / rel
        if not path.exists():
            stale.append(f"{rel} (module deleted)")
            continue
        try:
            tree = python_ast(path)
        except SyntaxError:  # pragma: no cover
            continue
        calls_transport = any(
            isinstance(node, ast.Call)
            and (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else None
            )
            in TRANSPORT_CALLS
            for node in ast.walk(tree)
        )
        if not calls_transport:
            stale.append(f"{rel} (migrated -- remove it from the backlog)")

    assert not stale, (
        "These are listed as bypassing the consent ledger but no longer do:\n  "
        + "\n  ".join(stale)
        + "\n\nDelete them from LEDGER_BYPASS_BACKLOG. A stale entry is a place "
        "a new bypass can hide."
    )
