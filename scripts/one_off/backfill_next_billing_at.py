"""Retired: the former billing-anchor backfill guessed and forgave periods.

This compatibility entry point intentionally performs no database work. The
old implementation derived ``next_billing_at`` from mutable catalog cadence,
subscription creation time, and an operator-selected forgiveness date, then
wrote the ORM field directly. Those inputs cannot prove paid-through state and
bypass the canonical billing-anchor writer.

Use ``scripts/one_off/repair_stale_prepaid_billing_anchors.py``. It can repair
an absent or stale active-prepaid anchor only when active entitlement evidence
proves the exact target. Every other row remains review stock.
"""

from __future__ import annotations


def main() -> None:
    raise SystemExit(
        "backfill_next_billing_at.py is retired: use "
        "repair_stale_prepaid_billing_anchors.py and review its exact "
        "entitlement-backed fingerprint"
    )


if __name__ == "__main__":
    main()
