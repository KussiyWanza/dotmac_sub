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

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import TaxRate
from app.models.billing_contract import (
    BillingContractLine,
    BillingContractVersion,
    IntervalUnit,
    ProrationPolicy,
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
RATING_POLICY_VERSION = "billing-rating-v1"
_SUPPORTED_POLICY_VERSIONS = frozenset({"billing-rating-v1"})

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
class RatingProvenance:
    """Immutable inputs that reproduce one obligation's rated result."""

    contract_version_id: UUID
    contract_line_key: UUID
    policy_version: str
    period: Interval
    currency: str
    covered: Interval
    unit_price: Decimal
    quantity: Decimal
    rate_basis: RateBasis
    rate_unit: IntervalUnit
    rate_quantity: Decimal
    timezone_name: str
    proration_policy: ProrationPolicy
    rate_units: Decimal
    proration: Decimal
    tax_treatment_code: str | None
    tax_rate_id: UUID | None
    tax_rate_percent: Decimal
    tax_inclusive: bool
    input_fingerprint: str


@dataclass(frozen=True)
class RatedObligation:
    """Typed, deterministic result plus its complete replay provenance."""

    contract_version_id: UUID
    contract_line_key: UUID
    period: Interval
    currency: str
    net_amount: Decimal
    tax_amount: Decimal
    gross_amount: Decimal
    provenance: RatingProvenance

    @property
    def rate_basis(self) -> RateBasis:
        return self.provenance.rate_basis

    @property
    def rate_units(self) -> Decimal:
        return self.provenance.rate_units

    @property
    def proration(self) -> Decimal:
        return self.provenance.proration

    @property
    def tax_treatment_code(self) -> str | None:
        return self.provenance.tax_treatment_code

    @property
    def tax_rate(self) -> Decimal:
        return self.provenance.tax_rate_percent / Decimal("100")

    @property
    def tax_inclusive(self) -> bool:
        return self.provenance.tax_inclusive


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _instant_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise _error(
            "invalid_rating_provenance",
            "Rating provenance instants must be timezone-aware.",
        )
    return value.astimezone(UTC).isoformat()


def _fingerprint_payload(provenance: RatingProvenance) -> dict[str, object]:
    return {
        "contract_version_id": str(provenance.contract_version_id),
        "contract_line_key": str(provenance.contract_line_key),
        "policy_version": provenance.policy_version,
        "period_start": _instant_text(provenance.period.starts_at),
        "period_end": _instant_text(provenance.period.ends_at),
        "currency": provenance.currency,
        "coverage_start": _instant_text(provenance.covered.starts_at),
        "coverage_end": _instant_text(provenance.covered.ends_at),
        "unit_price": _decimal_text(provenance.unit_price),
        "quantity": _decimal_text(provenance.quantity),
        "rate_basis": provenance.rate_basis.value,
        "rate_unit": provenance.rate_unit.value,
        "rate_quantity": _decimal_text(provenance.rate_quantity),
        "timezone_name": provenance.timezone_name,
        "proration_policy": provenance.proration_policy.value,
        "rate_units": _decimal_text(provenance.rate_units),
        "proration": _decimal_text(provenance.proration),
        "tax_treatment_code": provenance.tax_treatment_code,
        "tax_rate_id": (
            str(provenance.tax_rate_id) if provenance.tax_rate_id is not None else None
        ),
        "tax_rate_percent": _decimal_text(provenance.tax_rate_percent),
        "tax_inclusive": provenance.tax_inclusive,
    }


def rating_input_fingerprint(provenance: RatingProvenance) -> str:
    """Content-address one exact set of rating replay inputs."""

    payload = json.dumps(
        _fingerprint_payload(provenance),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _with_fingerprint(provenance: RatingProvenance) -> RatingProvenance:
    return replace(
        provenance,
        input_fingerprint=rating_input_fingerprint(provenance),
    )


def _effective_tax_rate(
    db: Session,
    *,
    version: BillingContractVersion,
    line: BillingContractLine,
) -> tuple[str | None, UUID | None, Decimal]:
    """Resolve the tax rate named by the line or version treatment code.

    The line's code wins over the version's. No code means no tax — an
    explicit contracted fact, not a fallback. A named code that resolves to no
    active TaxRate fails closed (ADR 0007: missing mappings are never rated
    tax-free by accident).
    """

    code = line.tax_treatment_code or version.tax_treatment_code
    if code is None:
        return None, None, Decimal("0")

    rates = list(
        db.execute(
            select(TaxRate)
            .where(
                TaxRate.code == code,
                TaxRate.is_active.is_(True),
            )
            .order_by(TaxRate.id)
            .limit(2)
        ).scalars()
    )
    if not rates:
        raise _error(
            "unknown_tax_treatment",
            "Contracted tax treatment code has no active tax rate.",
            tax_treatment_code=code,
        )
    if len(rates) > 1:
        raise _error(
            "ambiguous_tax_treatment",
            "Contracted tax treatment code resolves to multiple active tax rates.",
            tax_treatment_code=code,
        )
    rate = rates[0]
    # Preserve the financial tax owner's source convention in provenance:
    # 7.5 means 7.5%. Arithmetic normalizes it only inside the versioned
    # rating policy.
    return code, rate.id, Decimal(rate.rate)


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
    _, units, factor = _net_for_period(
        cadence=cadence, line=line, period=period, covered=covered
    )
    code, tax_rate_id, tax_rate_percent = _effective_tax_rate(
        db,
        version=version,
        line=line,
    )
    provenance = _with_fingerprint(
        RatingProvenance(
            contract_version_id=version.id,
            contract_line_key=contract_line_key,
            policy_version=RATING_POLICY_VERSION,
            period=period,
            currency=line.currency,
            covered=covered or period,
            unit_price=Decimal(line.unit_price),
            quantity=Decimal(line.quantity),
            rate_basis=cadence.rate_basis,
            rate_unit=cadence.rate_unit,
            rate_quantity=Decimal(cadence.rate_quantity),
            timezone_name=cadence.timezone_name,
            proration_policy=cadence.proration_policy,
            rate_units=units,
            proration=factor,
            tax_treatment_code=code,
            tax_rate_id=tax_rate_id,
            tax_rate_percent=tax_rate_percent,
            tax_inclusive=version.tax_inclusive,
            input_fingerprint="",
        )
    )
    return rate_from_provenance(provenance)


def rate_from_provenance(provenance: RatingProvenance) -> RatedObligation:
    """Reproduce a rated result without reading mutable current configuration."""

    if provenance.policy_version not in _SUPPORTED_POLICY_VERSIONS:
        raise _error(
            "unsupported_policy_version",
            "Rating provenance names an unsupported policy version.",
            policy_version=provenance.policy_version,
        )
    if rating_input_fingerprint(provenance) != provenance.input_fingerprint:
        raise _error(
            "rating_provenance_fingerprint_mismatch",
            "Stored rating provenance does not match its immutable fingerprint.",
        )
    if (
        provenance.covered.starts_at < provenance.period.starts_at
        or provenance.covered.ends_at > provenance.period.ends_at
    ):
        raise _error(
            "invalid_rating_provenance",
            "Rating coverage must fall inside the obligation period.",
        )
    if (
        provenance.unit_price < 0
        or provenance.quantity <= 0
        or provenance.rate_quantity <= 0
        or provenance.rate_units < 0
        or not Decimal("0") <= provenance.proration <= Decimal("1")
        or provenance.tax_rate_percent < 0
    ):
        raise _error(
            "invalid_rating_provenance",
            "Rating provenance contains an invalid numeric input.",
        )
    if provenance.tax_treatment_code is None:
        if provenance.tax_rate_id is not None or provenance.tax_rate_percent != Decimal(
            "0"
        ):
            raise _error(
                "invalid_rating_provenance",
                "Tax-free provenance cannot carry a tax source or rate.",
            )
    elif provenance.tax_rate_id is None:
        raise _error(
            "invalid_rating_provenance",
            "A named tax treatment must retain its exact tax-rate identity.",
        )
    if provenance.policy_version == "billing-rating-v1":
        return _rate_v1(provenance)
    # The supported-version guard keeps this branch unreachable today. It
    # remains explicit so a future policy adds a branch without editing v1.
    raise _error(
        "unsupported_policy_version",
        "Rating provenance names an unsupported policy version.",
        policy_version=provenance.policy_version,
    )


def _rate_v1(provenance: RatingProvenance) -> RatedObligation:
    """Frozen arithmetic for persisted ``billing-rating-v1`` snapshots."""

    raw_net = (
        provenance.unit_price
        * provenance.quantity
        * provenance.rate_units
        * provenance.proration
    )
    tax_rate = provenance.tax_rate_percent / Decimal("100")
    if provenance.tax_inclusive and tax_rate > 0:
        gross = _money(raw_net)
        net = _money(gross / (Decimal("1") + tax_rate))
        tax = gross - net
    else:
        net = _money(raw_net)
        tax = _money(net * tax_rate)
        gross = net + tax

    return RatedObligation(
        contract_version_id=provenance.contract_version_id,
        contract_line_key=provenance.contract_line_key,
        period=provenance.period,
        currency=provenance.currency,
        net_amount=net,
        tax_amount=tax,
        gross_amount=gross,
        provenance=provenance,
    )


__all__ = [
    "RATING_POLICY_VERSION",
    "BillingRatingError",
    "RatedObligation",
    "RatingProvenance",
    "rate_from_provenance",
    "rate_line_period",
    "rating_input_fingerprint",
]
