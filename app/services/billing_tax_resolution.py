"""Single compatibility VAT resolver pending ``dotmac-tax`` cutover.

This owner consolidates the legacy Sub precedence without becoming a new
statutory-policy engine. Customer exemption wins, then service address,
customer account, catalog compatibility fields, and the configured default.
The result carries provenance so shadow comparison can retire every local path.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import TaxApplication, TaxRate
from app.models.catalog import CatalogOffer, Subscription
from app.models.customer_tax_policy import CustomerTaxPolicy
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Address, Subscriber
from app.services import settings_spec
from app.services.common import coerce_uuid


class BillingTaxSource(enum.StrEnum):
    """Legacy fact that selected the effective VAT treatment."""

    customer_vat_exemption = "customer_vat_exemption"
    service_address_tax_rate = "service_address_tax_rate"
    account_tax_rate = "account_tax_rate"
    catalog_vat_percent = "catalog_vat_percent"
    catalog_taxable_default = "catalog_taxable_default"
    catalog_offer_exempt = "catalog_offer_exempt"
    configured_default = "configured_default"
    unconfigured = "unconfigured"


@dataclass(frozen=True, slots=True)
class BillingTaxResolution:
    subscription_id: UUID
    tax_rate_id: UUID | None
    tax_rate_percent: Decimal | None
    tax_application: TaxApplication
    source: BillingTaxSource
    customer_tax_policy_version: int


def resolve_default_tax_rate_id(db: Session) -> UUID | None:
    """Return the active configured compatibility VAT rate, if any."""

    raw = settings_spec.resolve_value(db, SettingDomain.billing, "default_tax_rate_id")
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        rate = db.get(TaxRate, coerce_uuid(value))
    except (TypeError, ValueError):
        return None
    if rate is None or not bool(rate.is_active):
        return None
    return rate.id


def resolve_default_tax_application(db: Session) -> TaxApplication:
    """Return the configured legacy exclusive/inclusive/exempt treatment."""

    raw = settings_spec.resolve_value(
        db,
        SettingDomain.billing,
        "default_tax_application",
    )
    return TaxApplication(str(raw).strip().lower())


def resolve_active_tax_rate_id_for_percent(
    db: Session, rate_percent: Decimal
) -> UUID | None:
    """Resolve one unambiguous active TaxRate for a recorded percentage."""

    expected = Decimal(str(rate_percent)).quantize(Decimal("0.0001"))
    matches = [
        rate.id
        for rate in db.scalars(select(TaxRate).where(TaxRate.is_active.is_(True))).all()
        if Decimal(rate.rate or 0).quantize(Decimal("0.0001")) == expected
    ]
    return matches[0] if len(matches) == 1 else None


def _matching_catalog_tax_rate_id(
    rates: Sequence[TaxRate],
    vat_percent: Decimal | None,
) -> UUID | None:
    if vat_percent is None:
        return None
    percent = Decimal(str(vat_percent))
    if percent <= Decimal("0.00"):
        return None
    candidates = {percent}
    if percent > Decimal("1.00"):
        candidates.add(percent / Decimal("100"))
    else:
        candidates.add(percent * Decimal("100"))
    for rate in rates:
        if Decimal(str(rate.rate)) in candidates:
            return rate.id
    return None


def resolve_subscription_taxes(
    db: Session,
    subscriptions: Sequence[Subscription],
) -> dict[UUID, BillingTaxResolution]:
    """Resolve a bounded subscription cohort through one precedence policy."""

    rows = tuple(subscriptions)
    if not rows:
        return {}
    account_ids = {row.subscriber_id for row in rows}
    address_ids = {
        row.service_address_id for row in rows if row.service_address_id is not None
    }
    offer_ids = {row.offer_id for row in rows}

    policies = {
        account_id: (bool(vat_exempt), int(version))
        for account_id, vat_exempt, version in db.execute(
            select(
                CustomerTaxPolicy.account_id,
                CustomerTaxPolicy.vat_exempt,
                CustomerTaxPolicy.version,
            ).where(CustomerTaxPolicy.account_id.in_(account_ids))
        ).all()
    }
    account_tax_ids = {
        account_id: tax_rate_id
        for account_id, tax_rate_id in db.execute(
            select(Subscriber.id, Subscriber.tax_rate_id).where(
                Subscriber.id.in_(account_ids)
            )
        ).all()
    }
    address_tax_ids = (
        {
            address_id: tax_rate_id
            for address_id, tax_rate_id in db.execute(
                select(Address.id, Address.tax_rate_id).where(
                    Address.id.in_(address_ids)
                )
            ).all()
        }
        if address_ids
        else {}
    )
    offers = {
        offer.id: offer
        for offer in db.scalars(
            select(CatalogOffer).where(CatalogOffer.id.in_(offer_ids))
        ).all()
    }
    active_rates = tuple(
        db.scalars(select(TaxRate).where(TaxRate.is_active.is_(True))).all()
    )
    rates_by_id = {rate.id: rate for rate in active_rates}
    default_tax_rate_id = resolve_default_tax_rate_id(db)
    tax_application = resolve_default_tax_application(db)

    resolved: dict[UUID, BillingTaxResolution] = {}
    for subscription in rows:
        vat_exempt, policy_version = policies.get(
            subscription.subscriber_id,
            (False, 0),
        )
        tax_rate_id: UUID | None = None
        source = BillingTaxSource.unconfigured

        if vat_exempt:
            source = BillingTaxSource.customer_vat_exemption
        else:
            if subscription.service_address_id is not None:
                candidate = address_tax_ids.get(subscription.service_address_id)
                if candidate in rates_by_id:
                    tax_rate_id = candidate
                    source = BillingTaxSource.service_address_tax_rate
            if tax_rate_id is None:
                candidate = account_tax_ids.get(subscription.subscriber_id)
                if candidate in rates_by_id:
                    tax_rate_id = candidate
                    source = BillingTaxSource.account_tax_rate
            if tax_rate_id is None:
                offer = offers.get(subscription.offer_id)
                if offer is None:
                    tax_rate_id = default_tax_rate_id
                    source = (
                        BillingTaxSource.configured_default
                        if tax_rate_id is not None
                        else BillingTaxSource.unconfigured
                    )
                else:
                    tax_rate_id = _matching_catalog_tax_rate_id(
                        active_rates,
                        offer.vat_percent,
                    )
                    if tax_rate_id is not None:
                        source = BillingTaxSource.catalog_vat_percent
                    elif bool(offer.with_vat) or Decimal(
                        str(offer.vat_percent or "0")
                    ) > Decimal("0.00"):
                        tax_rate_id = default_tax_rate_id
                        source = (
                            BillingTaxSource.catalog_taxable_default
                            if tax_rate_id is not None
                            else BillingTaxSource.unconfigured
                        )
                    else:
                        source = BillingTaxSource.catalog_offer_exempt

        rate = rates_by_id.get(tax_rate_id) if tax_rate_id is not None else None
        resolved[subscription.id] = BillingTaxResolution(
            subscription_id=subscription.id,
            tax_rate_id=rate.id if rate is not None else None,
            tax_rate_percent=(Decimal(str(rate.rate)) if rate is not None else None),
            tax_application=(
                tax_application if rate is not None else TaxApplication.exempt
            ),
            source=source,
            customer_tax_policy_version=policy_version,
        )
    return resolved


def resolve_subscription_tax(
    db: Session,
    subscription: Subscription,
) -> BillingTaxResolution:
    """Thin scalar adapter over the bounded resolver."""

    return resolve_subscription_taxes(db, [subscription])[subscription.id]


__all__ = [
    "BillingTaxResolution",
    "BillingTaxSource",
    "resolve_active_tax_rate_id_for_percent",
    "resolve_default_tax_application",
    "resolve_default_tax_rate_id",
    "resolve_subscription_tax",
    "resolve_subscription_taxes",
]
