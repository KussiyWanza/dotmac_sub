"""Hide offers that must not be sold, without touching anyone's live service.

Two groups are portal-visible in production that should not be:

* ``custom-*`` codes — bespoke one-off deals priced at roughly twice retail.
  ``custom-3`` is 10 Mbps at N75,250 while retail Compact is 10 Mbps at
  N35,000, and both are on the portal.
* Four unlimited tiers (155/200/300/400 Mbps) that Michael confirmed on
  2026-08-05 are not real products.

This clears ``show_on_customer_portal`` and ``available_for_services`` so no NEW
customer can land on them. It deliberately does NOT deactivate the offer or
touch any subscription: existing customers keep their service and their price,
and the offer stays intact for billing and history. Freezing is reversible;
deactivating a subscribed offer is not.

Prints rollback SQL for every row before applying anything.

Usage:
    python -m scripts.one_off.freeze_unpublishable_offers --dry-run
    python -m scripts.one_off.freeze_unpublishable_offers --live
"""

from __future__ import annotations

import argparse

from app.db import SessionLocal
from app.models.catalog import CatalogOffer

BESPOKE_CODE_PREFIX = "custom-"

#: Confirmed by Michael 2026-08-05 as products that do not exist.
NON_PRODUCTS = (
    "Unlimited Plus Plan",
    "Unlimited Pro",
    "Unlimited Advanced",
    "Unlimited Ultimate",
)

#: The bespoke rule is scoped to the unlimited family ON PURPOSE.
#:
#: ``custom-N`` is an import artefact, not a semantic marker. Within the
#: unlimited family it reliably indicates a one-off deal priced at roughly
#: twice retail, which is what makes it unpublishable. Outside that family the
#: same prefix carries ordinary catalogue items — ``Fiber Last Mile``,
#: ``45mbps Leased Line``, ``Device Replacement`` — and freezing those would
#: withdraw real products from sale. Matching on the prefix alone hid 9 such
#: rows inside a 17-row change.
BESPOKE_FAMILY = "unlimited"


def _reason(offer: CatalogOffer) -> str | None:
    code = (offer.code or "").strip()
    if code.startswith(BESPOKE_CODE_PREFIX) and offer.plan_family == BESPOKE_FAMILY:
        return f"bespoke unlimited deal ({code})"
    if (offer.name or "").strip() in NON_PRODUCTS:
        return "not a real product"
    return None


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
            .filter(CatalogOffer.is_active.is_(True))
            .order_by(CatalogOffer.name)
            .all()
        )

        print("-- rollback for unpublishable-offer freeze")
        for offer in offers:
            reason = _reason(offer)
            if reason is None:
                continue
            if not offer.show_on_customer_portal and not offer.available_for_services:
                continue
            print(
                "UPDATE catalog_offers SET "
                f"show_on_customer_portal = {offer.show_on_customer_portal}, "
                f"available_for_services = {offer.available_for_services} "
                f"WHERE id = '{offer.id}';  -- {offer.name} [{reason}]"
            )
            offer.show_on_customer_portal = False
            offer.available_for_services = False
            changed += 1

        if args.live:
            db.commit()
            print(f"\napplied: {changed} offer(s) frozen")
        else:
            db.rollback()
            print(f"\ndry-run: {changed} offer(s) would be frozen")
    finally:
        db.close()


if __name__ == "__main__":
    main()
