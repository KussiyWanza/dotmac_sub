"""Classify the offers ``backfill_plan_families`` could not.

That script matched names against Unlimited / Homeflex / Dedicated, so anything
named for what it sells rather than which family it belongs to stayed NULL —
"100GB Plan", "/32 IP", and so on. Fifteen offers carrying 60 active
subscriptions were left unclassified, which means every family-scoped rule
silently skipped them: the SLA scope, the contention normalisation, the price
bands and the offer freeze all passed them by.

Two families are added for them:

``high_speed_data``
    A burst speed with a volume allowance that throttles on exhaustion. Sold
    by the gigabyte, not the megabit — which is why dividing its price by
    ``speed_download_mbps`` produces a meaningless rate.

``ip_block``
    A routed block sold as a service in its own right.

Classification prefers **structure over name** wherever a structural signal
exists, because name-matching is what left these unclassified in the first
place. A volume allowance is a fact about the product; a name is a label
someone typed.

    has usage_allowance_id      -> high_speed_data
    name begins with a CIDR     -> ip_block
    name says unlimited         -> unlimited

Anything else is reported and left alone. Guessing is what produces a catalogue
nobody trusts.

Usage:
    python -m scripts.one_off.classify_remaining_plan_families --dry-run
    python -m scripts.one_off.classify_remaining_plan_families --live
"""

from __future__ import annotations

import argparse
import re

from app.db import SessionLocal
from app.models.catalog import CatalogOffer

_CIDR_PREFIX = re.compile(r"^\s*/\d{1,2}\b")
_UNLIMITED = re.compile(r"\bunlimited\b", re.I)


def classify(offer: CatalogOffer) -> tuple[str | None, str]:
    """Return (family, why). ``None`` means leave it alone and report it."""
    # Structural first: an allowance is a fact about the product, and these are
    # currently the only offers in the catalogue that carry one.
    if offer.usage_allowance_id is not None:
        return "high_speed_data", "carries a usage allowance"
    if _CIDR_PREFIX.search(offer.name or ""):
        return "ip_block", "name begins with a CIDR prefix"
    if _UNLIMITED.search(offer.name or ""):
        return "unlimited", "name states the family"
    return None, "no structural signal and no family in the name"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    changed = 0
    skipped: list[tuple[str, str]] = []
    try:
        offers = (
            db.query(CatalogOffer)
            .filter(
                CatalogOffer.plan_family.is_(None),
                CatalogOffer.is_active.is_(True),
            )
            .order_by(CatalogOffer.name)
            .all()
        )

        print("-- rollback for plan-family classification")
        for offer in offers:
            family, why = classify(offer)
            if family is None:
                skipped.append((offer.name or "?", why))
                continue
            print(
                f"UPDATE catalog_offers SET plan_family = NULL "
                f"WHERE id = '{offer.id}';  -- {offer.name} -> {family} ({why})"
            )
            offer.plan_family = family
            changed += 1

        if skipped:
            print(f"\nleft unclassified ({len(skipped)}) — decide these by hand:")
            for name, why in skipped:
                print(f"  {name:32} {why}")

        if args.live:
            db.commit()
            print(f"\napplied: {changed} offer(s) classified")
        else:
            db.rollback()
            print(f"\ndry-run: {changed} offer(s) would be classified")
    finally:
        db.close()


if __name__ == "__main__":
    main()
