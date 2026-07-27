"""`billing.rating` — deterministic obligation rating (ADR 0007 Phase 2).

A read-only policy/resolver. Given one effective contract version, one of its
lines, and one exact service period, it returns the typed rated result the
obligation owner records: net, tax, and gross per currency, with the inputs
that produced them.

Rules straight from ADR 0007:

- rating is deterministic: the same version, line, period, coverage, and tax
  inputs always produce the same typed result;
- the rate unit is independent of the invoice interval, so a per-day rate can
  aggregate into one calendar-month obligation;
- proration uses the version's declared policy, never an implicit choice;
- tax comes from ``financial.tax_configuration`` records named by the line or
  version treatment code — a missing named code fails closed rather than
  silently rating tax-free;
- no session writes, no transaction completion, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import TaxRate
from app.models.billing_contract import (
    BillingContractLine,
    BillingContractVersion,
    RateBasis,
)
from app.services.billing.cadence import (
    BillingCadence,
    Interval,
    proration_factor,
    rate_units_in,
)
from app.services.billing.contracts import BillingContracts
from app.services.domain_errors import DomainError

OWNER = "billing.rating"

_CENT = Decimal("0.01")


class BillingRatingError(DomainError):
    """Fail-closed rating error."""


def _error(suffix: str, message: str, **details: object) -> BillingRatingError:
    return BillingRatingError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RatedObligation:
    """Typed, deterministic rating result for one line and period."""

    contract_version_id: UUID
    contract_line_key: UUID
    period: Interval
    currency: str
    net_amount: Decimal
    tax_amount: Decimal
    gross_amount: Decimal
    # Evidence: what produced the numbers.
    rate_basis: RateBasis
    rate_units: Decimal
    proration: Decimal
    tax_treatment_code: str | None
    tax_rate: Decimal
    tax_inclusive: bool


def _effective_tax_rate(
    db: Session,
    *,
    version: BillingContractVersion,
    line: BillingContractLine,
) -> tuple[str | None, Decimal]:
    """Resolve the tax rate named by the line or version treatment code.

    The line's code wins over the version's. No code means no tax — an
    explicit contracted fact, not a fallback. A named code that resolves to no
    active TaxRate fails closed (ADR 0007: missing mappings are never rated
    tax-free by accident).
    """

    code = line.tax_treatment_code or version.tax_treatment_code
    if code is None:
        return None, Decimal("0")

    rate = db.execute(
        select(TaxRate).where(
            TaxRate.code == code,
            TaxRate.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if rate is None:
        raise _error(
            "unknown_tax_treatment",
            "Contracted tax treatment code has no active tax rate.",
            tax_treatment_code=code,
        )
    return code, Decimal(rate.rate)


def _net_for_period(
    *,
    cadence: BillingCadence,
    line: BillingContractLine,
    period: Interval,
    covered: Interval | None,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (net, rate_units, proration) before tax and rounding."""

    base = Decimal(line.unit_price) * Decimal(line.quantity)

    if cadence.rate_basis in (
        RateBasis.fixed_per_service_period,
        RateBasis.per_quantity,
    ):
        units = Decimal("1")
    elif cadence.rate_basis is RateBasis.per_rate_unit:
        units = rate_units_in(cadence=cadence, period=period) / cadence.rate_quantity
    else:
        # usage_metered needs an observed quantity, which is a Phase 2+ input
        # from the usage owner; rating without it must not guess zero or one.
        raise _error(
            "usage_rating_requires_observation",
            "Usage-metered rating requires an observed usage quantity.",
            rate_basis=cadence.rate_basis.value,
        )

    factor = Decimal("1")
    if covered is not None:
        factor = proration_factor(cadence=cadence, period=period, covered=covered)

    return base * units * factor, units, factor


def rate_line_period(
    db: Session,
    *,
    contract_version_id: UUID,
    contract_line_key: UUID,
    period: Interval,
    covered: Interval | None = None,
) -> RatedObligation:
    """Rate one contract line for one exact period.

    ``covered`` narrows the billable interval inside ``period`` (activation or
    cancellation mid-period); the version's declared proration policy decides
    what that narrowing is worth.
    """

    version = db.get(BillingContractVersion, contract_version_id)
    if version is None:
        raise _error(
            "contract_version_not_found",
            "Rating requires an existing contract version.",
            contract_version_id=str(contract_version_id),
        )
    line = db.execute(
        select(BillingContractLine).where(
            BillingContractLine.contract_version_id == version.id,
            BillingContractLine.contract_line_key == contract_line_key,
        )
    ).scalar_one_or_none()
    if line is None:
        raise _error(
            "contract_line_not_found",
            "Rating requires a line on the named contract version.",
            contract_line_key=str(contract_line_key),
        )

    cadence = BillingContracts.cadence_of(version)
    raw_net, units, factor = _net_for_period(
        cadence=cadence, line=line, period=period, covered=covered
    )
    code, tax_rate = _effective_tax_rate(db, version=version, line=line)

    if version.tax_inclusive and tax_rate > 0:
        # The contracted price already contains tax: back the net out of it
        # rather than adding tax on top.
        gross = _money(raw_net)
        net = _money(gross / (Decimal("1") + tax_rate))
        tax = gross - net
    else:
        net = _money(raw_net)
        tax = _money(net * tax_rate)
        gross = net + tax

    return RatedObligation(
        contract_version_id=version.id,
        contract_line_key=contract_line_key,
        period=period,
        currency=line.currency,
        net_amount=net,
        tax_amount=tax,
        gross_amount=gross,
        rate_basis=cadence.rate_basis,
        rate_units=units,
        proration=factor,
        tax_treatment_code=code,
        tax_rate=tax_rate,
        tax_inclusive=version.tax_inclusive,
    )


__all__ = ["BillingRatingError", "RatedObligation", "rate_line_period"]
