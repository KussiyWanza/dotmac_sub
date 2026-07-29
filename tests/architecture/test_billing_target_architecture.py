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
        "expected_difference_count",
        "gap_count",
        "overlap_count",
        "operator_approved_at",
        "finance_approved_at",
    ):
        assert field in model
    assert "verification_blockers_present" in owner
    assert "operator_approval_required" in owner
    assert "execute_owner_command" in owner
    assert "record_phase2_run" in owner
    assert '"repair_requested": False' in owner
    assert "BillingRecordAuthority.authoritative" not in owner
    assert "AuthorityMigrationState.CUT_OVER" in contracts
    assert "AuthorityMigrationState.CUT_OVER" in obligations


def test_phase2_shadow_obligations_take_money_only_from_rating() -> None:
    root = Path(__file__).resolve().parents[2]
    contracts = (root / "app/services/billing/contracts.py").read_text(encoding="utf-8")
    obligations = (root / "app/services/billing/obligations.py").read_text(
        encoding="utf-8"
    )
    handler = (
        root / "app/services/events/handlers/billing_lifecycle_projection.py"
    ).read_text(encoding="utf-8")

    assert "schema_version=2" in contracts
    output_body = contracts.split(
        '"output": "billing.contracts.shadow_recorded"', maxsplit=1
    )[1]
    assert '"net_amount"' not in output_body
    assert '"tax_amount"' not in output_body
    assert "rate_line_period(" in obligations
    assert (
        "net_amount:"
        not in obligations.split("class ScheduleObligationCommand", maxsplit=1)[
            1
        ].split("class ObligationResult", maxsplit=1)[0]
    )
    assert "schema_versions=(1, 2)" in handler


def test_phase2_rating_replay_uses_immutable_provenance() -> None:
    root = Path(__file__).resolve().parents[2]
    model = (root / "app/models/billing_contract.py").read_text(encoding="utf-8")
    rating = (root / "app/services/billing/rating.py").read_text(encoding="utf-8")
    obligations = (root / "app/services/billing/obligations.py").read_text(
        encoding="utf-8"
    )
    migration = (
        root / "alembic/versions/439_billing_obligation_rating_provenance.py"
    ).read_text(encoding="utf-8")

    for field in (
        "rating_provenance_complete",
        "rating_policy_version",
        "rating_coverage_start",
        "rating_coverage_end",
        "rating_rate_basis",
        "rating_rate_unit",
        "rating_timezone_name",
        "rating_proration_policy",
        "rating_tax_rate_id",
        "rating_tax_rate_percent",
        "rating_input_fingerprint",
    ):
        assert field in model
    assert 'RATING_POLICY_VERSION = "billing-rating-v1"' in rating
    assert '_SUPPORTED_POLICY_VERSIONS = frozenset({"billing-rating-v1"})' in rating
    assert "class RatingProvenance" in rating
    assert "def rate_from_provenance" in rating
    assert "def _rate_v1" in rating
    assert "rating_provenance_fingerprint_mismatch" in rating
    assert "rate_from_provenance(_recorded_provenance(obligation))" in obligations
    assert "incomplete_rating_provenance" in obligations
    assert "rating_provenance_conflict" in obligations
    assert "server_default=sa.false()" in migration
    assert "UPDATE billing_obligations" not in migration


def test_phase2_current_owner_previews_do_not_hide_recurring_addons() -> None:
    root = Path(__file__).resolve().parents[2]
    postpaid = (root / "app/services/billing_automation.py").read_text(encoding="utf-8")
    prepaid = (root / "app/services/prepaid_service_renewals.py").read_text(
        encoding="utf-8"
    )
    verifier = (root / "app/services/billing/shadow_verification.py").read_text(
        encoding="utf-8"
    )

    assert "PostpaidChargeComponentPreview" in postpaid
    assert "_resolve_recurring_addon_charges(" in postpaid
    assert "components=tuple(components)" in postpaid
    assert "excluded_recurring_addon_ids" in prepaid
    assert "missing_target_recurring_addon" in verifier
    assert "current_prepaid_owner_excludes_recurring_addon" in verifier
    assert "uncontracted_recurring_addon" not in verifier


def test_recurring_addon_backfill_uses_owner_outputs_not_parallel_writes() -> None:
    root = Path(__file__).resolve().parents[2]
    producer = (root / "app/services/billing/addon_contract_backfill.py").read_text(
        encoding="utf-8"
    )
    contracts = (root / "app/services/billing/contracts.py").read_text(encoding="utf-8")
    handler = (
        root / "app/services/events/handlers/billing_lifecycle_projection.py"
    ).read_text(encoding="utf-8")
    operator = (root / "scripts/billing/billing_target_shadow.py").read_text(
        encoding="utf-8"
    )

    assert "execute_owner_command(" in producer
    assert "stage_owner_output(" in producer
    assert '"billing.addon_contract_backfill.captured"' in producer
    assert "BillingContractLine(" not in producer
    assert "repair_requested" not in producer
    assert "consume_recurring_addon_backfill" in contracts
    assert 'event_type="billing.addon_contract_backfill.captured"' in contracts
    assert 'producer_owner="billing.addon_contract_backfill"' in contracts
    assert "_ADDON_BACKFILL_OUTPUT" in handler
    assert "BillingAddonContractBackfill.preview(" in operator
    assert "BillingAddonContractBackfill.capture(" in operator


def test_live_addon_purchase_drives_the_receipted_timed_shadow_chain() -> None:
    root = Path(__file__).resolve().parents[2]
    producer = (root / "app/services/customer_portal_flow_addons.py").read_text(
        encoding="utf-8"
    )
    purchase_command = producer.split("def confirm_addon_purchase(", maxsplit=1)[
        1
    ].split("def cancel_addon(", maxsplit=1)[0]
    api = (root / "app/api/me.py").read_text(encoding="utf-8")
    contracts = (root / "app/services/billing/contracts.py").read_text(encoding="utf-8")
    obligations = (root / "app/services/billing/obligations.py").read_text(
        encoding="utf-8"
    )
    handler = (
        root / "app/services/events/handlers/billing_lifecycle_projection.py"
    ).read_text(encoding="utf-8")

    assert "PurchaseAddonCommand" in producer
    assert "AddonPurchaseOutcome" in producer
    assert "execute_owner_command(" in purchase_command
    assert "stage_owner_output(" in purchase_command
    assert "RECURRING_TERMS_ADDED_OUTPUT" in purchase_command
    assert "billing.contract_terms.recurring_addon_added" in producer
    assert "db.commit(" not in purchase_command
    assert "db.rollback(" not in purchase_command
    assert "with owner_session(db) as owner_db:" in api
    assert "confirm_addon_purchase(" in api

    assert "consume_recurring_addon_purchase" in contracts
    assert "BillingContractVersionStatus.draft" in contracts
    assert "schedule_timer(" in contracts
    assert "consume_pending_terms_effective_due" in contracts
    assert "billing.contracts.pending_terms_effective_due" in contracts
    assert '"recurring_addon_purchase"' in contracts
    assert "_LIVE_ADDON_PURCHASE_OUTPUT" in handler
    assert "_PENDING_TERMS_EFFECTIVE_TRIGGER" in handler
    assert "contract_change_kind=change_kind" in handler
    assert "source_kind=envelope_source_kind" in obligations
