import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.migration.import_crm_tickets_phase1 import (
    COMMENT_WATERMARK_KEY,
    DEFAULT_EXCLUDE_TITLE_REGEX,
    TICKET_WATERMARK_KEY,
    UnmappedDecision,
    _format_datetime,
    _load_staff_map,
    _parse_datetime,
    _state_since,
    _state_watermark,
    _write_state,
    canonical_ticket_status,
    decide_unmapped_ticket,
    derive_comment_author,
    plan_comment_sweep,
    resolve_sweep_since,
)


def test_legacy_resolved_import_status_is_canonical_closed() -> None:
    assert canonical_ticket_status("resolved") == "closed"
    assert canonical_ticket_status("open") == "open"


def test_unmapped_override_map_wins() -> None:
    decision = decide_unmapped_ticket(
        {"id": "ticket-1", "title": "Anything", "status": "open"},
        overrides={"ticket-1": UnmappedDecision("map", "manual_override", "sub-1")},
        exclude_title_re=re.compile(DEFAULT_EXCLUDE_TITLE_REGEX),
        allow_unmapped_closed=True,
    )

    assert decision == UnmappedDecision("map", "manual_override", "sub-1")


def test_unmapped_override_skip_wins() -> None:
    decision = decide_unmapped_ticket(
        {"id": "ticket-1", "title": "Real open ticket", "status": "open"},
        overrides={"ticket-1": UnmappedDecision("skip", "known_test")},
        exclude_title_re=None,
        allow_unmapped_closed=False,
    )

    assert decision == UnmappedDecision("skip", "known_test")


def test_unmapped_test_probe_title_is_skipped() -> None:
    decision = decide_unmapped_ticket(
        {
            "id": "ticket-1",
            "title": "Codex production Selfcare webhook probe b73fd71d",
            "status": "open",
        },
        overrides={},
        exclude_title_re=re.compile(DEFAULT_EXCLUDE_TITLE_REGEX),
        allow_unmapped_closed=True,
    )

    assert decision == UnmappedDecision("skip", "exclude_title_regex")


def test_unmapped_closed_history_imports_unlinked() -> None:
    decision = decide_unmapped_ticket(
        {"id": "ticket-1", "title": "Slow browsing", "status": "closed"},
        overrides={},
        exclude_title_re=re.compile(DEFAULT_EXCLUDE_TITLE_REGEX),
        allow_unmapped_closed=True,
    )

    assert decision == UnmappedDecision("unlink", "unmapped_closed_history")


def test_unmapped_open_real_ticket_blocks() -> None:
    decision = decide_unmapped_ticket(
        {"id": "ticket-1", "title": "Slow browsing", "status": "open"},
        overrides={},
        exclude_title_re=re.compile(DEFAULT_EXCLUDE_TITLE_REGEX),
        allow_unmapped_closed=True,
    )

    assert decision == UnmappedDecision("block", "unmapped_subscriber")


def test_parse_datetime_normalizes_zulu_to_utc() -> None:
    parsed = _parse_datetime("2026-07-08T12:34:56Z")

    assert parsed == datetime(2026, 7, 8, 12, 34, 56, tzinfo=UTC)
    assert _format_datetime(parsed) == "2026-07-08T12:34:56+00:00"


def _write_staff_map_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    fieldnames = [
        "crm_person_id",
        "crm_name",
        "crm_email",
        "system_user_id",
        "match_via",
    ]
    lines = [",".join(fieldnames)]
    for row in rows:
        lines.append(",".join(row.get(name, "") for name in fieldnames))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_staff_map_lowercases_ids_and_skips_blank_rows(tmp_path: Path) -> None:
    csv_path = _write_staff_map_csv(
        tmp_path / "staff_map.csv",
        [
            {
                "crm_person_id": "PERSON-1",
                "crm_name": "Ada",
                "crm_email": "ada@dotmac.io",
                "system_user_id": "USER-1",
                "match_via": "person_email",
            },
            {
                "crm_person_id": "person-2",
                "crm_name": "No sub account",
                "crm_email": "gone@dotmac.io",
                "system_user_id": "",
                "match_via": "person_email",
            },
        ],
    )

    assert _load_staff_map(str(csv_path)) == {"person-1": "user-1"}


def test_load_staff_map_rejects_conflicting_rows(tmp_path: Path) -> None:
    csv_path = _write_staff_map_csv(
        tmp_path / "staff_map.csv",
        [
            {"crm_person_id": "person-1", "system_user_id": "user-1"},
            {"crm_person_id": "person-1", "system_user_id": "user-2"},
        ],
    )

    with pytest.raises(SystemExit):
        _load_staff_map(str(csv_path))


def test_load_staff_map_without_path_is_empty() -> None:
    assert _load_staff_map(None) == {}


def test_derive_comment_author_null_author_is_system() -> None:
    derived = derive_comment_author(
        None, staff_map={"person-1": "user-1"}, person_subscriber_map={}
    )

    assert derived == ("system", None, None)


def test_derive_comment_author_staff_map_hit() -> None:
    derived = derive_comment_author(
        "PERSON-1",
        staff_map={"person-1": "user-1"},
        person_subscriber_map={},
    )

    assert derived == ("staff", None, "user-1")


def test_derive_comment_author_subscriber_linked_person_is_customer() -> None:
    derived = derive_comment_author(
        "person-9",
        staff_map={"person-1": "user-1"},
        person_subscriber_map={"person-9": "sub-9"},
    )

    assert derived == ("customer", "sub-9", None)


def test_derive_comment_author_staff_map_wins_over_subscriber_link() -> None:
    derived = derive_comment_author(
        "person-1",
        staff_map={"person-1": "user-1"},
        person_subscriber_map={"person-1": "sub-1"},
    )

    assert derived == ("staff", None, "user-1")


def test_derive_comment_author_unmapped_stays_staff_without_system_user() -> None:
    derived = derive_comment_author(
        "person-404", staff_map={}, person_subscriber_map={}
    )

    assert derived == ("staff", None, None)


def _comment(comment_id: str, ticket_id: str) -> dict[str, str]:
    return {"id": comment_id, "ticket_id": ticket_id, "body": "hello"}


def test_plan_comment_sweep_keeps_only_missing_mapped_comments() -> None:
    comments = [
        _comment("c-1", "crm-1"),
        _comment("c-2", "crm-1"),
        _comment("c-3", "crm-unmapped"),
    ]

    missing, skipped_unmapped = plan_comment_sweep(
        comments,
        {"crm-1": "sub-1"},
        existing_comment_ids={"c-1"},
    )

    assert [comment["id"] for comment in missing] == ["c-2"]
    assert skipped_unmapped == 1


def test_plan_comment_sweep_empty_inputs() -> None:
    assert plan_comment_sweep([], {}, set()) == ([], 0)


def test_plan_comment_sweep_preserves_created_at_order() -> None:
    comments = [_comment("c-2", "crm-1"), _comment("c-1", "crm-1")]

    missing, skipped_unmapped = plan_comment_sweep(comments, {"crm-1": "sub-1"}, set())

    assert [comment["id"] for comment in missing] == ["c-2", "c-1"]
    assert skipped_unmapped == 0


def test_resolve_sweep_since_since_hours_overrides_state(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({COMMENT_WATERMARK_KEY: "2026-07-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)

    since = resolve_sweep_since(
        state_file=str(state),
        overlap_seconds=600,
        since_hours=48,
        now=now,
    )

    assert since == now - timedelta(hours=48)


def test_resolve_sweep_since_uses_state_watermark_minus_overlap(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({COMMENT_WATERMARK_KEY: "2026-07-08T12:00:00+00:00"}),
        encoding="utf-8",
    )

    since = resolve_sweep_since(
        state_file=str(state), overlap_seconds=600, since_hours=None
    )

    assert since == datetime(2026, 7, 8, 11, 50, 0, tzinfo=UTC)


def test_resolve_sweep_since_without_state_or_hours_is_full_sweep() -> None:
    assert (
        resolve_sweep_since(state_file=None, overlap_seconds=600, since_hours=None)
        is None
    )


def test_write_state_merges_and_preserves_other_watermark(tmp_path: Path) -> None:
    state = tmp_path / "state.json"

    _write_state(str(state), ticket_updated_at="2026-07-08T00:00:00+00:00")
    _write_state(str(state), comment_created_at="2026-07-09T00:00:00+00:00")

    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload[TICKET_WATERMARK_KEY] == "2026-07-08T00:00:00+00:00"
    assert payload[COMMENT_WATERMARK_KEY] == "2026-07-09T00:00:00+00:00"
    assert _state_since(str(state), 0) == datetime(2026, 7, 8, tzinfo=UTC)
    assert _state_watermark(str(state), COMMENT_WATERMARK_KEY, 0) == datetime(
        2026, 7, 9, tzinfo=UTC
    )


def test_write_state_without_values_is_a_noop(tmp_path: Path) -> None:
    state = tmp_path / "state.json"

    _write_state(str(state))

    assert not state.exists()
