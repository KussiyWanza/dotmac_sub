"""Operator adapter contract for per-ONT reconciliation holds.

The owner already refuses to replay a reused idempotency key unless the WHOLE
command matches, review date included. That contract is only reachable if the
CLI can actually emit the same command twice -- and the original adapter could
not: it took a relative ``--review-in-days`` and resolved it against ``now()``
per invocation, so a byte-identical retry differed by microseconds and was
refused as a conflict. Mandatory idempotency that cannot replay through its own
supported entry point is incomplete, so these tests pin the adapter's side of
it.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta, timezone

import pytest

from scripts.one_off.ont_reconcile_hold import build_parser, parse_review_due_at

PLACE_ARGV = [
    "place",
    "--serial",
    "4857544328201B9A",
    "--reason-code",
    "wan_intent_adjudication",
    "--explanation",
    "Unverified WAN intent; management drift not adjudicated.",
    "--actor",
    "operator@dotmac",
    "--reviewer",
    "network-lead@dotmac",
    "--review-due-at",
    "2026-08-11T10:00:00+00:00",
    "--idempotency-key",
    "hold-cohort-001",
]


# ---------------------------------------------------------------------------
# The review date is an absolute instant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-11T10:00:00+00:00", datetime(2026, 8, 11, 10, 0, tzinfo=UTC)),
        ("2026-08-11T10:00:00Z", datetime(2026, 8, 11, 10, 0, tzinfo=UTC)),
        (
            "2026-08-11T11:00:00+01:00",
            datetime(2026, 8, 11, 11, 0, tzinfo=timezone(timedelta(hours=1))),
        ),
    ],
)
def test_a_timezone_aware_review_date_is_accepted(raw, expected):
    assert parse_review_due_at(raw) == expected


def test_the_review_date_does_not_depend_on_the_wall_clock():
    """The regression: the parsed instant is absolute, not ``now() + delta``.

    Asserting against a fixed instant is the point. A relative resolution
    cannot satisfy this, which is exactly the defect that made replay
    impossible.
    """
    assert parse_review_due_at("2026-08-11T10:00:00+00:00") == datetime(
        2026, 8, 11, 10, 0, tzinfo=UTC
    )


def test_parsing_the_same_argument_twice_yields_the_same_instant():
    first = parse_review_due_at("2026-08-11T10:00:00+00:00")
    second = parse_review_due_at("2026-08-11T10:00:00+00:00")

    assert first == second


def test_a_naive_review_date_is_refused():
    """Refused, not assumed to be UTC.

    ``_replayable`` coerces a naive stored value to UTC. Accepting naive input
    here would silently reinterpret an operator's local time, moving the review
    date by their offset without anyone being told.
    """
    with pytest.raises(argparse.ArgumentTypeError) as excinfo:
        parse_review_due_at("2026-08-11T10:00:00")

    assert "timezone" in str(excinfo.value)


def test_a_value_that_is_not_a_timestamp_is_refused():
    with pytest.raises(argparse.ArgumentTypeError) as excinfo:
        parse_review_due_at("next tuesday")

    assert "ISO-8601" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The argument contract
# ---------------------------------------------------------------------------


def test_place_parses_an_absolute_review_date():
    args = build_parser().parse_args(PLACE_ARGV)

    assert args.review_due_at == datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
    assert args.idempotency_key == "hold-cohort-001"
    assert args.reviewer != args.actor


def test_identical_argv_produces_an_identical_command():
    """Two independent invocations of the same command are the same command.

    This is what lets the owner recognise a retry as a replay rather than a
    conflicting second decision.
    """
    first = build_parser().parse_args(PLACE_ARGV)
    second = build_parser().parse_args(PLACE_ARGV)

    assert (
        first.serial,
        first.reason_code,
        first.explanation,
        first.actor,
        first.reviewer,
        first.review_due_at,
        first.idempotency_key,
    ) == (
        second.serial,
        second.reason_code,
        second.explanation,
        second.actor,
        second.reviewer,
        second.review_due_at,
        second.idempotency_key,
    )


def test_the_relative_review_flag_is_gone():
    """Guard against reintroducing the non-replayable path.

    Keeping ``--review-in-days`` as a convenience would preserve an entry point
    whose retries can never replay -- and it is the one an operator reaches for
    under pressure.
    """
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [*PLACE_ARGV[:-4], "--review-in-days", "7", *PLACE_ARGV[-2:]]
        )


def test_place_requires_an_explicit_review_date():
    argv = [
        a
        for a in PLACE_ARGV
        if a not in ("--review-due-at", "2026-08-11T10:00:00+00:00")
    ]

    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


def test_place_still_requires_an_idempotency_key():
    argv = [a for a in PLACE_ARGV if a not in ("--idempotency-key", "hold-cohort-001")]

    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)
