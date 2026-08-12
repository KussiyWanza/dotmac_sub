"""The R1 parity report is aggregate, deterministic, and fail-closed."""

from __future__ import annotations

from app.services.audit import _AUDIT_R1_PARITY_QUERY, AuditR1ParityReport


def _report(**overrides: int) -> AuditR1ParityReport:
    values = {
        "total_rows": 12,
        "historical_rows_without_created_at": 10,
        "r1_rows": 2,
        "missing_details": 0,
        "metadata_mismatches": 0,
        "ip_address_mismatches": 0,
        "user_agent_mismatches": 0,
        "unknown_actor_types": 0,
        "missing_required_actor_ids": 0,
    }
    values.update(overrides)
    return AuditR1ParityReport(**values)


def test_zero_drift_with_observed_r1_rows_is_parity() -> None:
    report = _report()

    assert report.status == "parity"
    assert report.blocking_mismatches == 0


def test_any_mismatch_blocks_parity() -> None:
    report = _report(metadata_mismatches=2, missing_required_actor_ids=1)

    assert report.status == "drift"
    assert report.blocking_mismatches == 3


def test_zero_new_rows_is_not_misreported_as_proven_parity() -> None:
    assert _report(r1_rows=0).status == "no_r1_rows"


def test_query_reads_aggregates_without_selecting_forensic_values() -> None:
    sql = str(_AUDIT_R1_PARITY_QUERY).lower()

    assert "count(*) filter" in sql
    assert "from audit_events" in sql
    assert "select *" not in sql
    assert "group by" not in sql
