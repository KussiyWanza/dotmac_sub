"""Normalize catalog_offers.aggregation to a uniform 1:5 across the unlimited family.

`aggregation` is the contention (oversubscription) ratio: dedicated offers carry
1, unlimited offers should carry a single shared value. In production the
unlimited family had drifted to six different values with no correlation to
speed or price — 40 Mbps carried 1:3 while 80 Mbps carried 1:5, four offers were
NULL, and two sat at 1:1 (a dedicated-grade promise inside a shared family).

Nothing currently enforces this field: the only read outside catalog CRUD is the
network-intent tuple in ``app/services/subscription_lifecycle.py``, which uses it
to decide whether a plan change needs remote reprovisioning. Normalizing it
therefore changes no device configuration. It makes the declared promise uniform
so a capacity-planning and enforcement path can be built against one number.

Prints rollback SQL for every row it touches before applying anything.

Usage:
    python -m scripts.one_off.normalize_unlimited_aggregation --dry-run
    python -m scripts.one_off.normalize_unlimited_aggregation --live
"""

from __future__ import annotations

import argparse

from app.db import SessionLocal
from app.models.catalog import CatalogOffer

TARGET_FAMILY = "unlimited"
TARGET_AGGREGATION = 5


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    changed = 0
    try:
        offers = (
            db.query(CatalogOffer)
            .filter(
                CatalogOffer.plan_family == TARGET_FAMILY,
                CatalogOffer.is_active.is_(True),
            )
            .order_by(CatalogOffer.speed_download_mbps)
            .all()
        )

        print(f"-- rollback for {TARGET_FAMILY} aggregation normalization")
        for offer in offers:
            if offer.aggregation == TARGET_AGGREGATION:
                continue
            previous = "NULL" if offer.aggregation is None else str(offer.aggregation)
            print(
                f"UPDATE catalog_offers SET aggregation = {previous} "
                f"WHERE id = '{offer.id}';  -- {offer.name} "
                f"({offer.speed_download_mbps} Mbps)"
            )
            offer.aggregation = TARGET_AGGREGATION
            changed += 1

        active = len(offers)
        if args.live:
            db.commit()
            print(
                f"\napplied: {changed} of {active} active {TARGET_FAMILY} offers "
                f"set to 1:{TARGET_AGGREGATION}"
            )
        else:
            db.rollback()
            print(
                f"\ndry-run: {changed} of {active} active {TARGET_FAMILY} offers "
                f"would be set to 1:{TARGET_AGGREGATION}"
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
