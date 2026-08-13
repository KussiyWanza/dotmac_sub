"""Every detected billing anomaly must reach a log line.

`BillingHealthSnapshot.anomalies` is the list of things billing health knows
are wrong. Detecting one and not emitting it is the failure mode this guards:
nine of the seventeen were computed, published as gauges, and never written
anywhere a human or an alert would see, including 1,831 aged draft invoices
totalling over sixty million naira.

Read statically. Importing the app would make this guard depend on the
application importing cleanly, which is a different failure from the one it
is testing.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HEALTH = PROJECT_ROOT / "app" / "services" / "billing_health.py"
SCHEDULED = PROJECT_ROOT / "app" / "services" / "billing" / "scheduled.py"

#: Names appended to the anomaly list.
_DECLARED = re.compile(r'out\.append\("([a-z_]+)"\)')
#: Names the scheduled snapshot branches on to emit a line.
_SURFACED = re.compile(r'"([a-z_]+)" in anomalies')


def declared_anomalies() -> set[str]:
    return set(_DECLARED.findall(HEALTH.read_text(encoding="utf-8")))


def surfaced_anomalies() -> set[str]:
    return set(_SURFACED.findall(SCHEDULED.read_text(encoding="utf-8")))


def test_the_detectors_are_actually_found() -> None:
    """Guard the guard.

    If either regex stopped matching, the comparison below would pass while
    measuring nothing.
    """
    declared = declared_anomalies()
    assert len(declared) >= 15, (
        f"only {len(declared)} anomaly names found in billing_health; the "
        "declaration shape has probably changed and this guard is measuring "
        "the wrong thing"
    )
    assert "negative_prepaid_balances" in declared, (
        "a known anomaly name is missing; the declaration regex has drifted"
    )


def test_every_detected_anomaly_is_surfaced() -> None:
    missing = sorted(declared_anomalies() - surfaced_anomalies())

    assert not missing, (
        "billing health detects these anomalies but never emits them, so they "
        "are visible only to whoever opens the dashboard. Add a log line in "
        "app/services/billing/scheduled.py at a severity that matches what the "
        "signal means:\n  " + "\n  ".join(missing)
    )


def test_no_log_line_branches_on_an_undeclared_anomaly() -> None:
    """A branch on a name nothing raises is dead code that reads as coverage."""
    stray = sorted(surfaced_anomalies() - declared_anomalies())

    assert not stray, (
        "these anomaly names are branched on but never raised by "
        "BillingHealthSnapshot.anomalies, so the branch can never fire:\n  "
        + "\n  ".join(stray)
    )
