"""Argument contract for reviewed ONT sweep cohort admission."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import pytest

from scripts.one_off.ont_reconcile_admission import build_parser, parse_expires_at

ADMIT_ARGV = [
    "admit",
    "--serial",
    "4857544328201B9A",
    "--cohort-key",
    "cohort-1-verified",
    "--reason-code",
    "initial_verified_rollout",
    "--explanation",
    "Canonical PON identity and sentinel review passed.",
    "--actor",
    "operator@dotmac",
    "--reviewer",
    "network-lead@dotmac",
    "--expires-at",
    "2026-08-12T10:00:00+00:00",
    "--idempotency-key",
    "admit-cohort-1-4857544328201B9A",
]


def test_timezone_aware_expiry_is_absolute_and_replayable():
    expected = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    assert parse_expires_at("2026-08-12T10:00:00Z") == expected
    assert parse_expires_at("2026-08-12T10:00:00+00:00") == expected


def test_naive_or_invalid_expiry_is_refused():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_expires_at("2026-08-12T10:00:00")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_expires_at("next week")


def test_admit_requires_named_cohort_review_expiry_and_idempotency():
    args = build_parser().parse_args(ADMIT_ARGV)

    assert args.cohort_key == "cohort-1-verified"
    assert args.reviewer != args.actor
    assert args.expires_at == datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    assert args.idempotency_key == "admit-cohort-1-4857544328201B9A"


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--cohort-key", "cohort-1-verified"),
        ("--reviewer", "network-lead@dotmac"),
        ("--expires-at", "2026-08-12T10:00:00+00:00"),
        ("--idempotency-key", "admit-cohort-1-4857544328201B9A"),
    ],
)
def test_admit_refuses_missing_authority_evidence(flag, value):
    argv = [item for item in ADMIT_ARGV if item not in (flag, value)]
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


def test_identical_argv_builds_identical_admission_commands():
    first = build_parser().parse_args(ADMIT_ARGV)
    second = build_parser().parse_args(ADMIT_ARGV)

    assert vars(first) == vars(second)
