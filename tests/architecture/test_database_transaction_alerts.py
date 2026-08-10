from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_database_transaction_alert_rules_are_bounded_and_actionable() -> None:
    path = ROOT / "deploy/observability/database_transactions.rules.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = document["groups"][0]["rules"]

    assert {rule["alert"] for rule in rules} == {
        "DatabaseSlowTransactionsSustained",
        "DatabaseTransactionDurationCritical",
    }
    assert all(rule.get("for") for rule in rules)
    assert all(
        rule["annotations"]["runbook"]
        == "docs/runbooks/DATABASE_TRANSACTION_PRESSURE.md"
        for rule in rules
    )
    expressions = " ".join(str(rule["expr"]) for rule in rules)
    assert "database_transaction_spans_slow_total" in expressions
    assert "database_transaction_span_seconds_bucket" in expressions


def test_transaction_metrics_have_no_high_cardinality_labels() -> None:
    source = (ROOT / "app/metrics.py").read_text(encoding="utf-8")
    block = source.split("DATABASE_TRANSACTION_SPANS =", maxsplit=1)[1].split(
        "JOB_DURATION =", maxsplit=1
    )[0]

    assert "request_id" not in block
    assert "session_id" not in block
    assert "customer" not in block
