from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_application_failure_alerts_are_bounded_and_actionable() -> None:
    path = ROOT / "deploy/observability/application_failures.rules.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = document["groups"][0]["rules"]

    assert {rule["alert"] for rule in rules} == {
        "SubUnhandledApplicationExceptionsSustained",
        "SubPaymentVerificationUnexpectedFailures",
    }
    assert all(rule.get("for") for rule in rules)
    assert all(
        rule["annotations"]["runbook"]
        == "docs/designs/OPERATIONAL_EVIDENCE_AND_RETRY.md"
        for rule in rules
    )
    expressions = " ".join(str(rule["expr"]) for rule in rules)
    assert "application_exceptions_total" in expressions
    assert "payment_verification_outcomes_total" in expressions
