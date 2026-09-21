"""Report/display modules never assign to a persistent display attribute.

Finding 1b of the platform adoption ledger: ``web_reports`` and
``subscriber_growth`` used to write a *derived* display status back onto live
ORM ``Subscriber`` rows (``sub.status = _derive_subscriber_status(sub)``).
The request path never commits, but mutating persistent objects for
presentation is an autoflush hazard — any later query in the same session can
flush the derived default into the database as real account state — and it
makes the report layer a parallel projection of account status.

``web_billing_accounts`` had the same shape for a ``balance`` attribute:
``account.balance = Decimal(open_balance)`` stamped a computed open-invoice
total onto a live ``Subscriber`` row purely to carry it into a template, even
though ``Subscriber`` has no persistent ``balance`` column at all (2026-09
billing-account balance display hygiene finding).

The fix carries derived values in explicit immutable view models or local
mappings; this test pins the persistent-attribute boundary per module, using
``GUARDED_MODULE_ATTRIBUTES`` to say which attribute name is guarded in which
module. AST-based: any ``Assign``/``AnnAssign``/``AugAssign`` whose target is
an attribute named exactly the guarded name fails, regardless of the value
expression. Extend ``GUARDED_MODULE_ATTRIBUTES`` when a new report/display
module writes a derived value onto a persistent model for presentation.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# module path -> attribute name guarded against assignment in that module.
GUARDED_MODULE_ATTRIBUTES = {
    "app/services/web_reports.py": "status",
    "app/services/subscriber_growth.py": "status",
    "app/services/web_billing_accounts.py": "balance",
}


def test_report_modules_never_assign_guarded_attribute() -> None:
    offenders: list[str] = []
    for rel, guarded_attr in GUARDED_MODULE_ATTRIBUTES.items():
        tree = ast.parse((PROJECT_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == guarded_attr:
                    offenders.append(f"{rel}:{node.lineno} (.{guarded_attr})")
    assert not offenders, (
        "Report/display modules must not mutate a persistent attribute for "
        "presentation — derive into a local variable or immutable view model: "
        + ", ".join(sorted(offenders))
    )
