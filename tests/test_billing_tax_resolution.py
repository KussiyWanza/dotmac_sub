"""One compatibility owner selects VAT until dotmac-tax cutover."""

from __future__ import annotations

from decimal import Decimal

from app.models.billing import TaxApplication, TaxRate
from app.models.customer_tax_policy import CustomerTaxPolicy
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.billing_tax_resolution import (
    BillingTaxSource,
    resolve_active_tax_rate_id_for_percent,
    resolve_subscription_tax,
    resolve_subscription_taxes,
)


def test_percent_lookup_requires_one_unambiguous_active_rate(db_session):
    active = TaxRate(name="Installation VAT", rate=Decimal("7.5000"))
    inactive = TaxRate(
        name="Retired installation VAT",
        rate=Decimal("7.5000"),
        is_active=False,
    )
    db_session.add_all([active, inactive])
    db_session.commit()

    assert (
        resolve_active_tax_rate_id_for_percent(db_session, Decimal("7.5")) == active.id
    )

    db_session.add(TaxRate(name="Duplicate VAT", rate=Decimal("7.5000")))
    db_session.commit()

    assert resolve_active_tax_rate_id_for_percent(db_session, Decimal("7.5")) is None


def test_customer_exemption_is_the_highest_precedence_tax_fact(
    db_session, subscription, subscriber
):
    rate = TaxRate(
        name="Account VAT",
        code="ACCOUNT-VAT-RESOLUTION",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(rate)
    db_session.flush()
    subscriber.tax_rate_id = rate.id
    subscription.offer.with_vat = True
    db_session.add(
        CustomerTaxPolicy(
            account_id=subscriber.id,
            withholding_tax_enabled=False,
            vat_exempt=True,
            version=7,
            updated_by="pytest",
        )
    )
    db_session.commit()

    resolved = resolve_subscription_tax(db_session, subscription)

    assert resolved.tax_rate_id is None
    assert resolved.tax_application == TaxApplication.exempt
    assert resolved.source == BillingTaxSource.customer_vat_exemption
    assert resolved.customer_tax_policy_version == 7


def test_batch_resolution_returns_one_typed_result_per_subscription(
    db_session, subscription, subscriber
):
    rate = TaxRate(
        name="Account VAT",
        code="ACCOUNT-VAT-BATCH",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(rate)
    db_session.flush()
    subscriber.tax_rate_id = rate.id
    db_session.commit()

    resolved = resolve_subscription_taxes(db_session, [subscription])

    assert tuple(resolved) == (subscription.id,)
    assert resolved[subscription.id].tax_rate_id == rate.id
    assert resolved[subscription.id].source == BillingTaxSource.account_tax_rate


def test_configured_rate_identity_and_application_are_not_built_in(
    db_session, subscription
):
    rate = TaxRate(
        name="Configured tenant levy",
        code="TENANT-LEVY",
        rate=Decimal("3.1250"),
        is_active=True,
    )
    db_session.add(rate)
    db_session.flush()
    db_session.add_all(
        [
            DomainSetting(
                domain=SettingDomain.billing,
                key="default_tax_rate_id",
                value_type=SettingValueType.string,
                value_text=str(rate.id),
                is_active=True,
            ),
            DomainSetting(
                domain=SettingDomain.billing,
                key="default_tax_application",
                value_type=SettingValueType.string,
                value_text=TaxApplication.inclusive.value,
                is_active=True,
            ),
        ]
    )
    subscription.offer.with_vat = True
    subscription.offer.vat_percent = Decimal("0.0000")
    db_session.commit()

    resolved = resolve_subscription_tax(db_session, subscription)

    assert resolved.tax_rate_id == rate.id
    assert resolved.tax_rate_percent == Decimal("3.1250")
    assert resolved.tax_application == TaxApplication.inclusive
    assert resolved.source == BillingTaxSource.catalog_taxable_default
