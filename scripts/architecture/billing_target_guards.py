"""Structural detectors that ratchet work toward the ADR 0007 billing target.

ADR 0007 accepts an end-to-end billing target whose migration phases have not
cut over yet. Until they do, the existing money paths keep working. These
detectors do not delete that debt; they freeze it so a new change cannot add
another instance of a pattern the target explicitly retires:

- a mutable money counter standing in for a derived financial position
  (ADR 0007 invariants 12 and 13);
- a metadata/JSON read that decides financial identity or treatment
  (ADR 0007 invariant 4);
- a scheduled business-wide financial sweep that reconstructs work an owning
  transition should have scheduled as a durable timer
  (ADR 0007 invariant 18 and section 7).

Each detector returns evidence in a form the shrink-only baselines in
``tests/architecture`` can compare. Counts and names must only fall.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
SCHEDULER_CONFIG = APP_DIR / "services" / "scheduler_config.py"

# Attributes that persist a running money total on a business row. The target
# derives position from immutable postings instead of maintaining these.
MUTABLE_MONEY_COUNTER_ATTRS = frozenset(
    {
        "amount_paid",
        "balance",
        "collection_blocking_balance",
    }
)

# Receiver expressions that name a JSON/metadata bag rather than a typed column.
_METADATA_MARKERS = ("metadata", "meta_data", "extra_data")

# Keys that carry financial identity or accounting treatment. Reading one of
# these out of a metadata bag is an authoritative join, which the target
# replaces with a structural obligation/document/application link.
FINANCIAL_AUTHORITY_KEYS = frozenset(
    {
        "accounting_treatment",
        "billing_mode",
        "contract_id",
        "credit_note_id",
        "invoice_id",
        "invoice_number",
        "obligation_id",
        "order_id",
        "payment_flow",
        "payment_id",
        "sales_order_id",
        "subscription_id",
        "topup_intent_id",
    }
)

# Scheduled tasks are declared through this helper in ``scheduler_config``.
_SCHEDULED_TASK_HELPER = "_sync_scheduled_task"

# Task modules whose scheduled entry points act on customer money or access
# for a whole cohort rather than one named entity.
_FINANCIAL_SWEEP_TASK_PREFIXES = (
    "app.tasks.billing.",
    "app.tasks.collections.",
    "app.tasks.enforcement.",
)


def _python_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )


@cache
def _tree(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None


def _relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _is_metadata_receiver(node: ast.expr) -> bool:
    """Return whether ``node`` names a JSON/metadata bag."""

    try:
        rendered = ast.unparse(node).lower()
    except (AttributeError, ValueError):  # pragma: no cover - defensive
        return False
    return any(marker in rendered for marker in _METADATA_MARKERS)


def _literal_key(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


@cache
def mutable_money_counter_sites() -> dict[str, int]:
    """Return per-file counts of writes to a mutable money counter."""

    counts: dict[str, int] = {}
    for path in _python_files(APP_DIR):
        tree = _tree(path)
        if tree is None:
            continue
        hits = 0
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in MUTABLE_MONEY_COUNTER_ATTRS
                ):
                    hits += 1
        if hits:
            counts[_relative(path)] = hits
    return counts


@cache
def metadata_financial_authority_sites() -> dict[str, int]:
    """Return per-file counts of financial identity read out of metadata."""

    counts: dict[str, int] = {}
    for path in _python_files(APP_DIR):
        tree = _tree(path)
        if tree is None:
            continue
        hits = 0
        for node in ast.walk(tree):
            key: str | None = None
            receiver: ast.expr | None = None
            if isinstance(node, ast.Subscript):
                receiver = node.value
                key = _literal_key(node.slice)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
            ):
                receiver = node.func.value
                key = _literal_key(node.args[0])
            if key is None or receiver is None:
                continue
            if key in FINANCIAL_AUTHORITY_KEYS and _is_metadata_receiver(receiver):
                hits += 1
        if hits:
            counts[_relative(path)] = hits
    return counts


@cache
def scheduled_financial_sweeps() -> set[str]:
    """Return scheduled task names that sweep customer money or access."""

    tree = _tree(SCHEDULER_CONFIG)
    if tree is None:  # pragma: no cover - defensive
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = (
            func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        )
        if callee != _SCHEDULED_TASK_HELPER:
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        name = _literal_key(keywords.get("name", ast.Constant(value=None)))
        task_name = _literal_key(keywords.get("task_name", ast.Constant(value=None)))
        if not name or not task_name:
            continue
        if task_name.startswith(_FINANCIAL_SWEEP_TASK_PREFIXES):
            names.add(name)
    return names


def format_count_baseline(counts: dict[str, int]) -> list[str]:
    """Render ``count path`` lines in the shrink-only baseline format."""

    return [f"{count} {path}" for path, count in sorted(counts.items())]


def read_count_baseline(path: Path) -> dict[str, int]:
    """Read ``count path`` entries from a shrink-only baseline."""

    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        count, _, name = stripped.partition(" ")
        counts[name.strip()] = int(count)
    return counts
