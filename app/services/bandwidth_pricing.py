"""Rule-driven bandwidth pricing: quote any speed from a band set.

Owner of "what does N Mbps cost". Dedicated circuits are sold at arbitrary
speeds, so pricing them from one ``CatalogOffer`` row per speed is what left
production with duplicate speeds at incompatible prices and a 500 Mbps circuit
priced below a 300 Mbps one. Sales quotes from the rule here instead.

Bands accumulate **progressively**, like tax brackets: a band's rate applies
only to the Mbps that fall inside it, not to the whole circuit.

    1-10 Mbps @ N10,000   ->  10 Mbps  = N100,000
    10-50 Mbps @ N8,000   ->  11 Mbps  = N100,000 + 1 x N8,000 = N108,000

The alternative — a band's rate applied to the whole speed — reintroduces the
defect this exists to prevent: at the boundary, 11 Mbps would cost 11 x N8,000
= N88,000, i.e. **less than 10 Mbps**. Progressive accumulation is monotonic by
construction, so no band set can ever price more bandwidth cheaper. That
property is asserted in the tests and must not be traded away for a simpler
sales explanation.

The quote is advisory. Nothing here writes a price: the contracted figure is
captured on ``QuoteLineItem.unit_price`` when the quote is raised, so re-rating
a band never rewrites an issued quote.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.catalog import PLAN_FAMILY_VALUES, BandwidthPriceBand
from app.services.domain_errors import DomainError


class BandwidthPricingError(DomainError):
    """Unquotable speed or an incoherent band set (adapter: HTTP 400)."""


@dataclass(frozen=True, slots=True)
class BandSegment:
    """One band's contribution to a quote — the arithmetic, shown."""

    speed_from_mbps: int
    speed_to_mbps: int | None
    mbps_charged: int
    rate_per_mbps: Decimal
    amount: Decimal


@dataclass(frozen=True, slots=True)
class BandwidthQuote:
    """Immutable quote for one speed. Carries its own derivation.

    ``segments`` is a tuple, not a list: a quote is evidence, and evidence a
    caller can mutate is not evidence.
    """

    plan_family: str
    speed_mbps: int
    currency: str
    amount: Decimal
    segments: tuple[BandSegment, ...]

    @property
    def effective_rate_per_mbps(self) -> Decimal:
        """Blended rate — what the customer is actually paying per Mbps."""
        if not self.speed_mbps:
            return Decimal("0.00")
        return (self.amount / Decimal(self.speed_mbps)).quantize(Decimal("0.01"))


def active_bands(db: Session, plan_family: str) -> tuple[BandwidthPriceBand, ...]:
    """Active bands for a family, ordered by where they start."""
    rows = (
        db.query(BandwidthPriceBand)
        .filter(
            BandwidthPriceBand.plan_family == plan_family,
            BandwidthPriceBand.is_active.is_(True),
        )
        .order_by(BandwidthPriceBand.speed_from_mbps)
        .all()
    )
    return tuple(rows)


def validate_band_set(bands: tuple[BandwidthPriceBand, ...]) -> tuple[str, ...]:
    """Structural problems that would make a quote ambiguous or impossible.

    Returned rather than raised so an admin screen can show every problem at
    once instead of the first one.
    """
    problems: list[str] = []
    if not bands:
        return ("no active bands",)

    if bands[0].speed_from_mbps != 0:
        problems.append(
            f"first band starts at {bands[0].speed_from_mbps} Mbps, "
            "leaving speeds below it unquotable; it must start at 0"
        )

    for current, following in zip(bands, bands[1:]):
        if current.speed_to_mbps is None:
            problems.append(
                f"open-ended band from {current.speed_from_mbps} Mbps is followed "
                f"by another band at {following.speed_from_mbps} Mbps; only the "
                "last band may be open-ended"
            )
            continue
        if current.speed_to_mbps > following.speed_from_mbps:
            problems.append(
                f"bands overlap between {following.speed_from_mbps} and "
                f"{current.speed_to_mbps} Mbps"
            )
        elif current.speed_to_mbps < following.speed_from_mbps:
            problems.append(
                f"gap between {current.speed_to_mbps} and "
                f"{following.speed_from_mbps} Mbps"
            )

    if bands[-1].speed_to_mbps is not None:
        problems.append(
            f"last band ends at {bands[-1].speed_to_mbps} Mbps; speeds above it "
            "are unquotable. Leave the top band open-ended instead"
        )

    currencies = {band.currency for band in bands}
    if len(currencies) > 1:
        problems.append(f"mixed currencies in one band set: {sorted(currencies)}")

    return tuple(problems)


def quote_bandwidth(
    db: Session, *, plan_family: str, speed_mbps: int
) -> BandwidthQuote:
    """What ``speed_mbps`` costs under the family's active bands.

    Raises rather than guessing: an unquotable speed is a band-set gap the
    admin must close, and inventing a number would put a figure in front of a
    customer that no rule produced.
    """
    if plan_family not in PLAN_FAMILY_VALUES:
        raise BandwidthPricingError(
            code="catalog.bandwidth_pricing.unknown_plan_family",
            message=(
                f"{plan_family!r} is not a known plan family "
                f"({', '.join(PLAN_FAMILY_VALUES)})."
            ),
        )
    if speed_mbps <= 0:
        raise BandwidthPricingError(
            code="catalog.bandwidth_pricing.invalid_speed",
            message="A quote needs a positive speed.",
            details={"speed_mbps": speed_mbps},
        )

    bands = active_bands(db, plan_family)
    problems = validate_band_set(bands)
    if problems:
        raise BandwidthPricingError(
            code="catalog.bandwidth_pricing.incoherent_band_set",
            message=(
                f"The {plan_family} band set cannot price anything: "
                f"{'; '.join(problems)}."
            ),
            details={"problems": list(problems)},
        )

    segments: list[BandSegment] = []
    total = Decimal("0.00")
    for band in bands:
        if speed_mbps <= band.speed_from_mbps:
            break
        upper = (
            speed_mbps
            if band.speed_to_mbps is None
            else min(speed_mbps, band.speed_to_mbps)
        )
        charged = upper - band.speed_from_mbps
        if charged <= 0:
            continue
        rate = Decimal(str(band.rate_per_mbps))
        amount = (rate * Decimal(charged)).quantize(Decimal("0.01"))
        total += amount
        segments.append(
            BandSegment(
                speed_from_mbps=band.speed_from_mbps,
                speed_to_mbps=band.speed_to_mbps,
                mbps_charged=charged,
                rate_per_mbps=rate,
                amount=amount,
            )
        )

    return BandwidthQuote(
        plan_family=plan_family,
        speed_mbps=speed_mbps,
        currency=bands[0].currency,
        amount=total.quantize(Decimal("0.01")),
        segments=tuple(segments),
    )
