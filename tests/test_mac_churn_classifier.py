"""The MAC-churn classifier: replacement is normal, interleaving is not.

The whole point of measuring before enforcing is that "a login was seen with
two MACs" is not evidence of anything. An ordinary CPE swap produces exactly
that. What distinguishes sharing is that the observation windows OVERLAP -- one
credential in use from two devices at once -- rather than succeed one another.

Getting this backwards in either direction is costly: flagging replacement
would bury the real signal in false positives, and missing interleaving would
make an enforced binding pointless.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "audit_mac_churn",
    Path(__file__).resolve().parents[1] / "scripts/one_off/audit_mac_churn.py",
)
churn = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(churn)

BASE = datetime(2026, 7, 1, tzinfo=UTC)


def _w(start_h: int, end_h: int) -> tuple[datetime, datetime]:
    return (BASE + timedelta(hours=start_h), BASE + timedelta(hours=end_h))


def test_one_device_is_stable():
    assert churn.classify([_w(0, 100)]) == "stable"


def test_a_clean_device_swap_is_replacement_not_sharing():
    """Old CPE stops, new CPE starts. This must never be flagged."""
    assert churn.classify([_w(0, 48), _w(50, 100)]) == "replaced"


def test_a_swap_with_touching_endpoints_is_still_replacement():
    """A stop and a start in the same second is a swap, not an overlap."""
    assert churn.classify([_w(0, 48), _w(48, 100)]) == "replaced"


def test_overlapping_windows_are_interleaved():
    """One credential live from two devices at once -- the sharing signal."""
    assert churn.classify([_w(0, 60), _w(30, 90)]) == "interleaved"


def test_alternating_use_is_interleaved():
    assert churn.classify([_w(0, 100), _w(20, 40)]) == "interleaved"


def test_many_devices_is_churning_regardless_of_overlap():
    windows = [_w(i * 10, i * 10 + 5) for i in range(6)]
    assert churn.classify(windows) == "churning"


def test_no_observations_is_not_a_verdict():
    assert churn.classify([]) == "unobserved"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF"),
        ("aa-bb-cc-dd-ee-ff", "AA:BB:CC:DD:EE:FF"),
        ("aabb.ccdd.eeff", "AA:BB:CC:DD:EE:FF"),
        ("AABBCCDDEEFF", "AA:BB:CC:DD:EE:FF"),
    ],
)
def test_known_mac_formats_normalize_to_one_form(raw, expected):
    assert churn._normalize_mac(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "not-a-mac", "AA:BB:CC", "AA:BB:CC:DD:EE"])
def test_unreadable_values_are_refused_rather_than_guessed(raw):
    """An unparseable Calling-Station-Id must not silently reduce the MAC count.

    Returning None routes the session to `unparseable`, where a human sees it,
    instead of making a shared credential look stable.
    """
    assert churn._normalize_mac(raw) is None
