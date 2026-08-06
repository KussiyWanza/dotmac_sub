"""One name, one sellable offer.

A name is not an identity, but every plan picker presents it as one.
Production carried two active offers both named "25 Mbps Fiber" — one at
N537,500 and one at N0.00 — and two subscriptions were created against the free
one on the same day, giving two customers unbilled dedicated fibre. A separate
pair both named "Unlimited Pro" caused a live 50 Mbps plan with three active
customers to be withdrawn from sale by a maintenance script matching on name.

Scoped to *sellable* offers rather than all of them. A retired offer keeping
its name is harmless and preserves history; the ambiguity only matters where
somebody is choosing. Withdrawing one of a pair from sale is therefore a valid
resolution, and does not require renaming or deleting anything.

Enforced here as well as in ``web_catalog_offers.assert_sellable_name_is_unique``
because the Splynx importer and any direct SQL do not pass through the service.
The service check exists to give an operator a sentence rather than a
constraint violation; this exists so the rule cannot be sidestepped.

If regional pricing ever needs the same product name at two prices, the region
belongs on ``OfferPrice``, not on a duplicated offer — see
docs/PLAN_FAMILY_ARCHITECTURE.md section 7.

Revision ID: 489_unique_sellable_offer_name
Revises: 488_pon_port_capacity
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "489_unique_sellable_offer_name"
down_revision: str | None = "488_pon_port_capacity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_catalog_offers_sellable_name"


def upgrade() -> None:
    bind = op.get_bind()
    # Fail loudly rather than let the index creation error out mid-migration
    # with a message that does not say which offers collide.
    collisions = bind.execute(
        sa.text(
            "SELECT name, count(*) FROM catalog_offers "
            "WHERE is_active AND available_for_services "
            "GROUP BY name HAVING count(*) > 1"
        )
    ).fetchall()
    if collisions:
        listed = ", ".join(f"{name!r} x{count}" for name, count in collisions)
        raise RuntimeError(
            "Cannot enforce unique sellable offer names while these collide: "
            f"{listed}. Withdraw one of each pair from sale "
            "(available_for_services = false) or rename it, then re-run."
        )

    op.create_index(
        _INDEX,
        "catalog_offers",
        ["name"],
        unique=True,
        postgresql_where=sa.text("is_active AND available_for_services"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, "catalog_offers")
