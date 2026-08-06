"""Shrink-only guard on writers of the served IPv4 projection.

``network.ip_assignment_lifecycle`` owns the desired IPv4 at exact service
grain. ``Subscription.ipv4_address`` is a compatibility projection of the
active ``IPAssignment`` — see ``app/models/catalog.py``,
``app/services/connectivity_reconciler.py``, and
``docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md``.

The cutover in that design is gated on six drift cohorts reaching zero and
staying there across two audit cycles. Repair sweeps drain those cohorts; only
a writer boundary stops them refilling. Bulk provisioning activation was the
concrete case: it calls ``activate_subscription(emit=False)``, so the
provisioning handler's allocator never ran, and it compensated by writing the
column directly — manufacturing an ``assignment_missing`` row per static
activation.

This guard does not claim the boundary is in place. It freezes the remaining
direct writers so a change cannot add another one while the cutover lands.
"""

from __future__ import annotations

from pathlib import Path

from scripts.architecture.sot_debt import served_ipv4_projection_writes

BASELINE = Path(__file__).with_name("served_ipv4_writer_baseline.txt")


def _read_baseline(path: Path) -> dict[str, int]:
    """Read ``count path`` entries from the shrink-only baseline."""

    counts: dict[str, int] = {}
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        count, _, name = stripped.partition(" ")
        try:
            parsed = int(count)
        except ValueError as exc:  # pragma: no cover - malformed baseline
            raise ValueError(
                f"invalid baseline entry {path}:{line_number}: {raw!r}"
            ) from exc
        if parsed < 1:  # pragma: no cover - malformed baseline
            raise ValueError(f"baseline count must be positive at {path}:{line_number}")
        counts[name.strip()] = parsed
    return counts


REMEDY = (
    "Route the write through network.ip_assignment_lifecycle so the served "
    "column stays a projection of an exact-service IPAssignment. Provisioning "
    "callers can use provisioning_helpers.ensure_ipv4_assignment_for_"
    "subscription."
)


def test_no_new_served_ipv4_projection_writers() -> None:
    current = served_ipv4_projection_writes()
    baseline = _read_baseline(BASELINE)

    added = sorted(set(current) - set(baseline))
    assert not added, (
        "new direct writes to Subscription.ipv4_address in files absent from "
        f"the shrink-only baseline. {REMEDY}\n  " + "\n  ".join(added)
    )

    grew = sorted(
        f"{name}: {baseline[name]} -> {current[name]}"
        for name in set(current) & set(baseline)
        if current[name] > baseline[name]
    )
    assert not grew, (
        f"existing served-IPv4 writers gained new write sites. {REMEDY}\n  "
        + "\n  ".join(grew)
    )


def test_served_ipv4_writer_baseline_only_shrinks() -> None:
    current = served_ipv4_projection_writes()
    baseline = _read_baseline(BASELINE)

    retired = sorted(set(baseline) - set(current))
    assert not retired, (
        "served-IPv4 writers were removed; delete them from the shrink-only "
        "baseline so it keeps describing real debt:\n  " + "\n  ".join(retired)
    )

    shrunk = sorted(
        f"{name}: baseline {baseline[name]}, now {current[name]}"
        for name in set(current) & set(baseline)
        if current[name] < baseline[name]
    )
    assert not shrunk, (
        "served-IPv4 write sites were removed; lower these baseline counts so "
        "the ratchet cannot be spent twice:\n  " + "\n  ".join(shrunk)
    )
