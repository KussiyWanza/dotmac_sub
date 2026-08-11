import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy.engine import Connection

import scripts.migration.check_crm_ticket_drift as drift_mod
from scripts.migration.check_crm_ticket_drift import (
    CRM_TITLE_MAX_LENGTH,
    CRM_TO_SUB_STATUS,
    DEFAULT_EXCLUDE_TITLE_REGEX,
    TERMINAL_STATUSES,
    FieldDiff,
    compare_children_counts,
    compare_ticket_fields,
    expected_sub_status,
    in_live_window,
    status_diff_terminal_precedence,
    title_matches_crm_truncation,
)

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)


def _crm_ticket(**overrides: Any) -> dict[str, Any]:
    ticket: dict[str, Any] = {
        "id": "crm-ticket-1",
        "subscriber_id": None,
        "created_by_person_id": None,
        "assigned_to_person_id": None,
        "ticket_manager_person_id": None,
        "assistant_manager_person_id": None,
        "service_team_id": None,
        "title": "Slow browsing",
        "status": "open",
        "priority": "normal",
        "ticket_type": "support",
        "number": "TCK-100",
        "region": "Abuja",
        "due_at": None,
        "resolved_at": None,
        "closed_at": None,
        "updated_at": NOW - timedelta(days=1),
    }
    ticket.update(overrides)
    return ticket


def _sub_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "support_ticket_id": "local-ticket-1",
        "crm_ticket_id": "crm-ticket-1",
        "unmapped_policy": None,
        "subscriber_id": None,
        "created_by_person_id": None,
        "assigned_to_person_id": None,
        "ticket_manager_person_id": None,
        "site_coordinator_person_id": None,
        "service_team_id": None,
        "title": "Slow browsing",
        "status": "open",
        "priority": "normal",
        "ticket_type": "support",
        "number": "TCK-100",
        "region": "Abuja",
        "due_at": None,
        "resolved_at": None,
        "closed_at": None,
        "updated_at": NOW - timedelta(days=1),
    }
    row.update(overrides)
    return row


# ---- status map ------------------------------------------------------------


def test_status_map_is_identity_except_for_legacy_resolved() -> None:
    for crm_status, sub_status in CRM_TO_SUB_STATUS.items():
        assert expected_sub_status(crm_status) == sub_status
        if crm_status != "resolved":
            assert sub_status == crm_status


def test_expected_status_defaults_to_open_for_missing() -> None:
    assert expected_sub_status(None) == "open"
    assert expected_sub_status("") == "open"
    assert expected_sub_status("  ") == "open"


def test_expected_status_passes_unknown_values_through() -> None:
    assert expected_sub_status("escalated_weird") == "escalated_weird"


def test_legacy_resolved_maps_to_native_closed() -> None:
    assert CRM_TO_SUB_STATUS["resolved"] == "closed"
    assert "resolved" not in TERMINAL_STATUSES


# ---- terminal precedence ---------------------------------------------------


def test_terminal_precedence_sub_closed_crm_older_non_terminal() -> None:
    assert status_diff_terminal_precedence(
        crm_status="open",
        sub_status="closed",
        crm_updated_at=NOW - timedelta(hours=2),
        sub_updated_at=NOW - timedelta(hours=1),
    )


def test_terminal_precedence_lost_when_crm_updated_later_non_terminally() -> None:
    assert not status_diff_terminal_precedence(
        crm_status="open",
        sub_status="closed",
        crm_updated_at=NOW - timedelta(hours=1),
        sub_updated_at=NOW - timedelta(hours=2),
    )


def test_terminal_precedence_holds_when_both_sides_terminal() -> None:
    # Sub keeps its own terminal status even if CRM chose a different one,
    # regardless of which side moved last.
    assert status_diff_terminal_precedence(
        crm_status="canceled",
        sub_status="closed",
        crm_updated_at=NOW,
        sub_updated_at=NOW - timedelta(days=1),
    )


def test_terminal_precedence_never_applies_to_non_terminal_sub_status() -> None:
    assert not status_diff_terminal_precedence(
        crm_status="closed",
        sub_status="open",
        crm_updated_at=NOW - timedelta(days=1),
        sub_updated_at=NOW,
    )
    assert not status_diff_terminal_precedence(
        crm_status="pending_confirmation",
        sub_status="pending_confirmation",
        crm_updated_at=None,
        sub_updated_at=None,
    )


def test_terminal_precedence_holds_without_timestamps() -> None:
    assert status_diff_terminal_precedence(
        crm_status="open",
        sub_status="merged",
        crm_updated_at=None,
        sub_updated_at=None,
    )


# ---- live-window classification ---------------------------------------------


def test_in_live_window_inside_window() -> None:
    assert in_live_window(NOW - timedelta(minutes=10), now=NOW, window_minutes=30)


def test_in_live_window_outside_window() -> None:
    assert not in_live_window(NOW - timedelta(minutes=31), now=NOW, window_minutes=30)


def test_in_live_window_boundary_is_inclusive() -> None:
    assert in_live_window(NOW - timedelta(minutes=30), now=NOW, window_minutes=30)


def test_in_live_window_none_or_disabled() -> None:
    assert not in_live_window(None, now=NOW, window_minutes=30)
    assert not in_live_window(NOW, now=NOW, window_minutes=0)


# ---- field comparison --------------------------------------------------------


def test_compare_identical_ticket_has_no_diffs() -> None:
    comparison = compare_ticket_fields(_crm_ticket(), _sub_row(), subscriber_map={})

    assert comparison.diffs == ()
    assert comparison.enrichments == ()
    assert comparison.unresolved_subscriber_reason is None


def test_compare_title_default_matches_importer_fallback() -> None:
    comparison = compare_ticket_fields(
        _crm_ticket(title=None),
        _sub_row(title="Untitled CRM ticket"),
        subscriber_map={},
    )

    assert comparison.diffs == ()


def test_title_truncation_tolerance_pure_rules() -> None:
    long_title = "x" * (CRM_TITLE_MAX_LENGTH + 55)

    # Exactly sub's title cut at CRM's String(200) cap.
    assert title_matches_crm_truncation(long_title[:CRM_TITLE_MAX_LENGTH], long_title)
    # Equal after strip.
    assert title_matches_crm_truncation("Slow browsing", "Slow browsing  ")
    # Genuine differences stay drift.
    assert not title_matches_crm_truncation("Different title", long_title)
    assert not title_matches_crm_truncation(
        long_title[: CRM_TITLE_MAX_LENGTH - 1], long_title
    )
    # Sub titles at or under the cap get no truncation allowance.
    assert not title_matches_crm_truncation("Slow", "Slow browsing")


def test_compare_title_crm_truncation_is_enrichment_not_drift() -> None:
    sub_title = "Customer reports intermittent drops " * 8  # > 200 chars
    crm_title = sub_title[:CRM_TITLE_MAX_LENGTH]

    comparison = compare_ticket_fields(
        _crm_ticket(title=crm_title),
        _sub_row(title=sub_title),
        subscriber_map={},
    )

    assert comparison.diffs == ()
    assert comparison.enrichments == (FieldDiff("title", crm_title, sub_title),)


def test_compare_genuine_title_difference_stays_gating() -> None:
    comparison = compare_ticket_fields(
        _crm_ticket(title="Slow browsing"),
        _sub_row(title="No connectivity"),
        subscriber_map={},
    )

    assert comparison.diffs == (FieldDiff("title", "Slow browsing", "No connectivity"),)
    assert comparison.enrichments == ()


def test_compare_priority_default_matches_importer_fallback() -> None:
    comparison = compare_ticket_fields(
        _crm_ticket(priority=None),
        _sub_row(priority="normal"),
        subscriber_map={},
    )

    assert comparison.diffs == ()


def test_compare_status_diff_carries_terminal_precedence_flag() -> None:
    comparison = compare_ticket_fields(
        _crm_ticket(status="open", updated_at=NOW - timedelta(hours=2)),
        _sub_row(status="closed", updated_at=NOW - timedelta(hours=1)),
        subscriber_map={},
    )

    assert comparison.diffs == (
        FieldDiff("status", "open", "closed", terminal_precedence=True),
    )


def test_compare_status_diff_without_terminal_allowance() -> None:
    comparison = compare_ticket_fields(
        _crm_ticket(status="pending", updated_at=NOW),
        _sub_row(status="open", updated_at=NOW),
        subscriber_map={},
    )

    assert comparison.diffs == (
        FieldDiff("status", "pending", "open", terminal_precedence=False),
    )


def test_compare_site_coordinator_maps_from_assistant_manager() -> None:
    comparison = compare_ticket_fields(
        _crm_ticket(assistant_manager_person_id="PERSON-9"),
        _sub_row(site_coordinator_person_id="person-9"),
        subscriber_map={},
    )

    assert comparison.diffs == ()

    drifted = compare_ticket_fields(
        _crm_ticket(assistant_manager_person_id="person-9"),
        _sub_row(site_coordinator_person_id=None),
        subscriber_map={},
    )

    assert drifted.diffs == (FieldDiff("site_coordinator_person_id", "person-9", None),)


def test_compare_subscriber_through_map_flags_mismatch() -> None:
    comparison = compare_ticket_fields(
        _crm_ticket(subscriber_id="crm-sub-1"),
        _sub_row(subscriber_id="local-sub-2"),
        subscriber_map={"crm-sub-1": "local-sub-1"},
    )

    assert comparison.diffs == (
        FieldDiff("subscriber_id", "local-sub-1", "local-sub-2"),
    )
    assert comparison.unresolved_subscriber_reason is None


def test_compare_subscriber_map_hit_including_alias_ids_is_clean() -> None:
    # _load_subscriber_map flattens crm_alias_ids into the same map; a hit via
    # an alias id compares exactly like a primary-link hit.
    comparison = compare_ticket_fields(
        _crm_ticket(subscriber_id="crm-alias-7"),
        _sub_row(subscriber_id="local-sub-1"),
        subscriber_map={"crm-sub-1": "local-sub-1", "crm-alias-7": "local-sub-1"},
    )

    assert comparison.diffs == ()


def test_compare_unmapped_subscriber_counts_unresolved_not_drift() -> None:
    comparison = compare_ticket_fields(
        _crm_ticket(subscriber_id="crm-sub-404", status="closed"),
        _sub_row(
            subscriber_id=None,
            status="closed",
            unmapped_policy="unmapped_closed_history",
        ),
        subscriber_map={},
    )

    assert comparison.diffs == ()
    assert comparison.unresolved_subscriber_reason == "unmapped_closed_history"


def test_compare_sub_only_subscriber_is_enrichment_not_drift() -> None:
    # CRM empty + sub linked = desired sub enrichment (94 prod rows), never
    # gating; it surfaces in the informational sub_enrichment class.
    comparison = compare_ticket_fields(
        _crm_ticket(subscriber_id=None),
        _sub_row(subscriber_id="local-sub-1"),
        subscriber_map={},
    )

    assert comparison.diffs == ()
    assert comparison.enrichments == (FieldDiff("subscriber_id", None, "local-sub-1"),)


def test_compare_crm_subscriber_set_sub_empty_stays_gating() -> None:
    comparison = compare_ticket_fields(
        _crm_ticket(subscriber_id="crm-sub-1"),
        _sub_row(subscriber_id=None),
        subscriber_map={"crm-sub-1": "local-sub-1"},
    )

    assert comparison.diffs == (FieldDiff("subscriber_id", "local-sub-1", None),)
    assert comparison.enrichments == ()


def test_compare_timestamps_normalize_zulu_and_offset() -> None:
    comparison = compare_ticket_fields(
        _crm_ticket(resolved_at="2026-07-08T12:00:00Z"),
        _sub_row(resolved_at=datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)),
        subscriber_map={},
    )

    assert comparison.diffs == ()

    drifted = compare_ticket_fields(
        _crm_ticket(closed_at="2026-07-08T12:00:00Z"),
        _sub_row(closed_at=None),
        subscriber_map={},
    )

    assert drifted.diffs == (FieldDiff("closed_at", "2026-07-08T12:00:00+00:00", None),)


def test_compare_verbatim_text_fields() -> None:
    comparison = compare_ticket_fields(
        _crm_ticket(ticket_type="installation", region="Lagos", number="TCK-7"),
        _sub_row(ticket_type="support", region="Lagos", number="TCK-7"),
        subscriber_map={},
    )

    assert comparison.diffs == (FieldDiff("ticket_type", "installation", "support"),)


# ---- children counts ---------------------------------------------------------


def test_compare_children_counts_reports_only_mismatches() -> None:
    mismatches = compare_children_counts(
        {"comments": 3, "assignees": 2, "links": 1, "merges": 0},
        {"comments": 3, "assignees": 1, "links": 1, "merges": 1},
    )

    assert mismatches == [("assignees", 2, 1), ("merges", 0, 1)]


def test_compare_children_counts_missing_keys_default_to_zero() -> None:
    assert compare_children_counts({}, {}) == []
    assert compare_children_counts({"comments": 2}, {}) == [("comments", 2, 0)]


# ---- run_drift_check classification and gating -------------------------------


def test_run_drift_check_classifies_window_probe_orphan_and_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crm_tickets = [
        _crm_ticket(id="t-ok"),
        _crm_ticket(id="t-missing", title="Real missing ticket"),
        _crm_ticket(
            id="t-inflight",
            status="pending",
            updated_at=NOW - timedelta(minutes=5),
        ),
        _crm_ticket(
            id="t-probe",
            title="Codex production Selfcare webhook probe b73fd71d",
        ),
        _crm_ticket(id="t-enriched", subscriber_id=None),
    ]
    sub_rows = [
        _sub_row(support_ticket_id="l-ok", crm_ticket_id="t-ok"),
        _sub_row(
            support_ticket_id="l-inflight",
            crm_ticket_id="t-inflight",
            status="open",
            updated_at=NOW - timedelta(minutes=5),
        ),
        _sub_row(support_ticket_id="l-orphan", crm_ticket_id="t-gone"),
        _sub_row(
            support_ticket_id="l-enriched",
            crm_ticket_id="t-enriched",
            subscriber_id="local-sub-1",
        ),
    ]

    monkeypatch.setattr(
        drift_mod,
        "_crm_tickets",
        lambda conn, limit=None, updated_since=None: crm_tickets,
    )
    monkeypatch.setattr(drift_mod, "_sub_marker_rows", lambda conn: sub_rows)
    monkeypatch.setattr(drift_mod, "_load_subscriber_map", lambda conn: {})
    monkeypatch.setattr(
        drift_mod,
        "_sub_child_counts",
        lambda conn: {"comments": {}, "assignees": {}, "links": {}, "merges": {}},
    )
    monkeypatch.setattr(
        drift_mod,
        "_crm_child_counts",
        lambda conn, importable: {
            "comments": {"t-ok": 1},
            "assignees": {},
            "links": {},
            "merges": {},
        },
    )
    monkeypatch.setattr(
        drift_mod, "_unmapped_staff_rows", lambda sub, crm, staff_map: []
    )

    summary, classes = drift_mod.run_drift_check(
        sub=cast(Connection, None),
        crm=cast(Connection, None),
        window_minutes=30,
        exclude_title_re=re.compile(DEFAULT_EXCLUDE_TITLE_REGEX),
        staff_map={},
        now=NOW,
    )

    assert summary["classes"]["crm_missing_in_sub"] == {"rows": 1, "drift": 1}
    assert classes["crm_missing_in_sub"][0]["crm_ticket_id"] == "t-missing"
    assert summary["classes"]["probe_skipped"] == {"rows": 1, "drift": 0}
    assert classes["probe_skipped"][0]["crm_ticket_id"] == "t-probe"
    assert summary["classes"]["sub_orphan_markers"] == {"rows": 1, "drift": 1}
    assert classes["sub_orphan_markers"][0]["crm_ticket_id"] == "t-gone"

    # t-inflight's status diff is inside the live window: reported, not gating.
    assert summary["classes"]["field_drift"] == {"rows": 1, "drift": 0}
    assert classes["field_drift"][0]["in_live_window"] is True
    assert classes["expected_in_flight"] == [
        {"crm_ticket_id": "t-inflight", "findings": "field:status"}
    ]

    # t-ok's CRM comment count (1) vs sub (0) gates.
    assert summary["classes"]["children_count_mismatch"] == {"rows": 1, "drift": 1}
    assert classes["children_count_mismatch"][0]["child"] == "comments"

    # t-enriched: sub linked a subscriber CRM lacks — informational only.
    assert summary["classes"]["sub_enrichment"] == {"rows": 1, "drift": 0}
    assert classes["sub_enrichment"][0] == {
        "crm_ticket_id": "t-enriched",
        "support_ticket_id": "l-enriched",
        "number": "TCK-100",
        "field": "subscriber_id",
        "crm_value": None,
        "sub_value": "local-sub-1",
    }

    assert summary["drift_total"] == 3
    assert summary["totals"] == {"crm_tickets": 5, "sub_marker_rows": 4, "joined": 3}
