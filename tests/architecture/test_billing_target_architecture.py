"""Ratchet new work toward the ADR 0007 end-to-end billing target.

ADR 0007 is accepted but its migration phases have not cut over. These guards
do not claim the target is in place. They freeze the patterns the target
retires so a change cannot add another instance while the phases land:

- mutable money counters standing in for derived position (invariants 12, 13);
- metadata/JSON deciding financial identity or treatment (invariant 4);
- scheduled business-wide financial sweeps (invariant 18, section 7).

Each baseline is shrink-only. Removing debt means deleting the line, never
raising the count.
"""

from __future__ import annotations

from pathlib import Path

from scripts.architecture.billing_target_guards import (
    metadata_financial_authority_sites,
    mutable_money_counter_sites,
    read_count_baseline,
    scheduled_financial_sweeps,
)
from scripts.architecture.sot_debt import read_name_baseline

ADR = Path(__file__).resolve().parents[2] / (
    "docs/adr/0007-end-to-end-billing-target-architecture.md"
)
MUTABLE_COUNTER_BASELINE = Path(__file__).with_name(
    "billing_mutable_money_counter_baseline.txt"
)
METADATA_AUTHORITY_BASELINE = Path(__file__).with_name(
    "billing_metadata_authority_baseline.txt"
)
SCHEDULED_SWEEP_BASELINE = Path(__file__).with_name(
    "billing_scheduled_sweep_baseline.txt"
)


def _assert_count_baseline(
    current: dict[str, int],
    baseline: dict[str, int],
    *,
    subject: str,
    remedy: str,
) -> None:
    added = sorted(set(current) - set(baseline))
    assert not added, (
        f"new {subject} in files absent from the shrink-only baseline. "
        f"{remedy}\n  " + "\n  ".join(added)
    )

    grew = sorted(
        f"{path}: {baseline[path]} -> {current[path]}"
        for path in current
        if current[path] > baseline[path]
    )
    assert not grew, (
        f"{subject} increased in a file that is already migration debt. "
        f"{remedy}\n  " + "\n  ".join(grew)
    )

    resolved = sorted(
        f"{path}: {baseline[path]} -> {current.get(path, 0)}"
        for path in baseline
        if current.get(path, 0) < baseline[path]
    )
    assert not resolved, (
        f"{subject} decreased; lower or delete the baseline entry in the same "
        "change so the ratchet keeps holding:\n  " + "\n  ".join(resolved)
    )


def test_adr_0007_is_accepted_and_numbered_uniquely() -> None:
    """The guards below are only meaningful while ADR 0007 is the target."""

    text = ADR.read_text(encoding="utf-8")

    assert text.startswith("# ADR 0007: End-to-end billing target architecture")
    assert "\nStatus: accepted\n" in text

    numbers = [
        path.name.split("-", 1)[0]
        for path in ADR.parent.glob("[0-9][0-9][0-9][0-9]-*.md")
    ]
    assert len(numbers) == len(set(numbers)), (
        "two ADRs share a number; allocate the next free one:\n  "
        + "\n  ".join(sorted(numbers))
    )


def test_no_new_mutable_money_counter_writes() -> None:
    _assert_count_baseline(
        mutable_money_counter_sites(),
        read_count_baseline(MUTABLE_COUNTER_BASELINE),
        subject="mutable money counter writes",
        remedy=(
            "ADR 0007 derives customer position from immutable subledger "
            "postings; do not persist a running total."
        ),
    )


def test_no_new_metadata_financial_authority_reads() -> None:
    _assert_count_baseline(
        metadata_financial_authority_sites(),
        read_count_baseline(METADATA_AUTHORITY_BASELINE),
        subject="financial identity reads out of metadata",
        remedy=(
            "ADR 0007 makes metadata provenance only; join Sale to Money "
            "through a structural obligation, document, or application link."
        ),
    )


def test_no_new_scheduled_financial_sweep() -> None:
    current = scheduled_financial_sweeps()
    baseline = read_name_baseline(SCHEDULED_SWEEP_BASELINE)

    added = sorted(current - baseline)
    assert not added, (
        "new scheduled business-wide financial sweep. ADR 0007 section 7 "
        "requires the owning transition to create a durable per-entity timer "
        "instead of a cohort scan:\n  " + "\n  ".join(added)
    )

    retired = sorted(baseline - current)
    assert not retired, (
        "scheduled financial sweeps were removed; delete them from the "
        "shrink-only baseline in the same change:\n  " + "\n  ".join(retired)
    )


def test_sweep_baseline_is_sorted_and_unique() -> None:
    entries = [
        line.strip()
        for line in SCHEDULED_SWEEP_BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert entries == sorted(set(entries))


def test_phase1_shadow_pipeline_is_receipted_end_to_end() -> None:
    fulfillment = (
        Path(__file__).resolve().parents[2] / "app/services/sales_fulfillment.py"
    ).read_text(encoding="utf-8")
    contracts = (
        Path(__file__).resolve().parents[2] / "app/services/billing/contracts.py"
    ).read_text(encoding="utf-8")
    obligations = (
        Path(__file__).resolve().parents[2] / "app/services/billing/obligations.py"
    ).read_text(encoding="utf-8")
    handler = (
        Path(__file__).resolve().parents[2]
        / "app/services/events/handlers/billing_lifecycle_projection.py"
    ).read_text(encoding="utf-8")

    assert "consume_funding_satisfaction" in fulfillment
    assert '"sales.fulfillment.funding_applied"' in fulfillment
    assert "consume_owner_output" in contracts
    assert '"billing.contracts.shadow_recorded"' in contracts
    assert "consume_owner_output" in obligations
    assert '"billing.obligations.shadow_scheduled"' in obligations
    for output in (
        "_FULFILLMENT_OUTPUT",
        "_CONTRACT_OUTPUT",
        "_OBLIGATION_OUTPUT",
    ):
        assert output in handler


def test_cutover_evidence_is_durable_and_cannot_move_authority() -> None:
    root = Path(__file__).resolve().parents[2]
    model = (root / "app/models/billing_shadow_verification.py").read_text(
        encoding="utf-8"
    )
    owner = (root / "app/services/billing/shadow_verification.py").read_text(
        encoding="utf-8"
    )
    contracts = (root / "app/services/billing/contracts.py").read_text(encoding="utf-8")
    obligations = (root / "app/services/billing/obligations.py").read_text(
        encoding="utf-8"
    )

    for field in (
        "source_fingerprint",
        "result_fingerprint",
        "cohort_classification",
        "currency_totals",
        "event_outcomes",
        "operator_approved_at",
        "finance_approved_at",
    ):
        assert field in model
    assert "verification_blockers_present" in owner
    assert "operator_approval_required" in owner
    assert "execute_owner_command" in owner
    assert "BillingRecordAuthority.authoritative" not in owner
    assert "AuthorityMigrationState.CUT_OVER" in contracts
    assert "AuthorityMigrationState.CUT_OVER" in obligations
