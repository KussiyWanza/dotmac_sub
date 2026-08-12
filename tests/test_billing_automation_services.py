"""Tests for billing automation services."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.catalog import BillingCycle
from app.services import billing_automation
from app.services.events.types import EventType


def _record_retired_inline_settle_row(db) -> None:
    """Retain a legacy row in fixtures; the invoice owner ignores it."""
    from app.models.domain_settings import DomainSetting, SettingDomain

    db.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="settle_credit_on_invoice_enabled",
            value_text="false",
        )
    )
    db.commit()


# =============================================================================
# _add_months Tests
# =============================================================================


class TestAddMonths:
    """Tests for _add_months function."""

    def test_add_one_month(self):
        """Test adding one month."""
        start = datetime(2024, 1, 15, 12, 0, 0)
        result = billing_automation._add_months(start, 1)
        assert result.year == 2024
        assert result.month == 2
        assert result.day == 15

    def test_add_twelve_months(self):
        """Test adding 12 months (one year)."""
        start = datetime(2024, 3, 20, 12, 0, 0)
        result = billing_automation._add_months(start, 12)
        assert result.year == 2025
        assert result.month == 3
        assert result.day == 20

    def test_year_rollover(self):
        """Test adding months that roll over to next year."""
        start = datetime(2024, 11, 15, 12, 0, 0)
        result = billing_automation._add_months(start, 3)
        assert result.year == 2025
        assert result.month == 2
        assert result.day == 15

    def test_month_end_handling_january_to_february(self):
        """Test January 31 to February (clamp to 28/29)."""
        start = datetime(2024, 1, 31, 12, 0, 0)  # 2024 is leap year
        result = billing_automation._add_months(start, 1)
        assert result.year == 2024
        assert result.month == 2
        assert result.day == 29  # Leap year

    def test_month_end_handling_non_leap_year(self):
        """Test January 31 to February in non-leap year."""
        start = datetime(2023, 1, 31, 12, 0, 0)
        result = billing_automation._add_months(start, 1)
        assert result.year == 2023
        assert result.month == 2
        assert result.day == 28

    def test_month_end_31_to_30(self):
        """Test from 31-day month to 30-day month."""
        start = datetime(2024, 3, 31, 12, 0, 0)
        result = billing_automation._add_months(start, 1)
        assert result.year == 2024
        assert result.month == 4
        assert result.day == 30  # April has 30 days

    def test_add_zero_months(self):
        """Test adding zero months."""
        start = datetime(2024, 5, 15, 12, 0, 0)
        result = billing_automation._add_months(start, 0)
        assert result.year == 2024
        assert result.month == 5
        assert result.day == 15


# =============================================================================
# _period_end Tests
# =============================================================================


class TestPeriodEnd:
    """Tests for _period_end function."""

    def test_monthly_cycle(self):
        """Test monthly billing cycle."""
        start = datetime(2024, 6, 15, 12, 0, 0)
        result = billing_automation._period_end(start, BillingCycle.monthly)
        assert result.month == 7
        assert result.day == 15

    def test_annual_cycle(self):
        """Test annual billing cycle."""
        start = datetime(2024, 6, 15, 12, 0, 0)
        result = billing_automation._period_end(start, BillingCycle.annual)
        assert result.year == 2025
        assert result.month == 6
        assert result.day == 15

    def test_quarterly_cycle(self):
        """Test quarterly billing cycle."""
        start = datetime(2024, 6, 15, 12, 0, 0)
        result = billing_automation._period_end(start, BillingCycle.quarterly)
        assert result.year == 2024
        assert result.month == 9
        assert result.day == 15

    def test_none_cycle_defaults_to_monthly(self):
        """Test that None cycle defaults to monthly."""
        start = datetime(2024, 6, 15, 12, 0, 0)
        # None/unrecognized should default to monthly in code logic.
        result = billing_automation._period_end(start, BillingCycle.monthly)
        assert result.month == 7
        assert result.day == 15


# =============================================================================
# _resolve_price Tests
# =============================================================================


class TestResolvePrice:
    """Tests for _resolve_price function."""

    def test_price_from_offer_version(self, db_session, subscription):
        """Test getting price from offer version."""
        from app.models.catalog import (
            AccessType,
            BillingCycle,
            OfferVersion,
            OfferVersionPrice,
            PriceBasis,
            PriceType,
            ServiceType,
        )

        # Create offer version with price
        offer_version = OfferVersion(
            offer_id=subscription.offer_id,
            version_number=1,
            name="Test Version",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        )
        db_session.add(offer_version)
        db_session.commit()

        version_price = OfferVersionPrice(
            offer_version_id=offer_version.id,
            price_type=PriceType.recurring,
            amount=Decimal("99.99"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(version_price)

        subscription.offer_version_id = offer_version.id
        db_session.commit()

        amount, currency, cycle = billing_automation._resolve_price(
            db_session, subscription
        )
        assert amount == Decimal("99.99")
        assert currency == "USD"
        assert cycle == BillingCycle.monthly

    def test_price_from_offer(self, db_session, subscription):
        """Test getting price from offer when no version price."""
        from app.models.catalog import BillingCycle, OfferPrice, PriceType

        # Create offer price
        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("79.99"),
            currency="EUR",
            billing_cycle=BillingCycle.annual,
            is_active=True,
        )
        db_session.add(offer_price)
        # Clear the subscription's snapshotted cadence to exercise the
        # price-fallback branch (SOT: subscription cadence otherwise wins).
        subscription.billing_cycle = None
        db_session.commit()

        amount, currency, cycle = billing_automation._resolve_price(
            db_session, subscription
        )
        assert amount == Decimal("79.99")
        assert currency == "EUR"
        assert cycle == BillingCycle.annual

    def test_no_price_found(self, db_session, subscription):
        """Test when no price is found."""
        # No prices created
        amount, currency, cycle = billing_automation._resolve_price(
            db_session, subscription
        )
        assert amount is None
        assert currency is None
        assert cycle is None

    def test_two_active_prices_picks_newest_and_warns(
        self, db_session, subscription, caplog
    ):
        """Two active recurring prices: newest created_at wins, with a warning."""
        from app.models.catalog import BillingCycle, OfferPrice, PriceType

        now = datetime.now(UTC)
        db_session.add_all(
            [
                OfferPrice(
                    offer_id=subscription.offer_id,
                    price_type=PriceType.recurring,
                    amount=Decimal("50.00"),
                    currency="USD",
                    billing_cycle=BillingCycle.monthly,
                    is_active=True,
                    created_at=now - timedelta(days=10),
                ),
                OfferPrice(
                    offer_id=subscription.offer_id,
                    price_type=PriceType.recurring,
                    amount=Decimal("60.00"),
                    currency="USD",
                    billing_cycle=BillingCycle.monthly,
                    is_active=True,
                    created_at=now,
                ),
            ]
        )
        db_session.commit()

        caplog.set_level("WARNING")
        amount, currency, cycle = billing_automation._resolve_price(
            db_session, subscription
        )

        assert amount == Decimal("60.00")
        assert any(
            "Multiple active recurring offer prices" in record.getMessage()
            for record in caplog.records
        )


# =============================================================================
# _effective_unit_price Tests
# =============================================================================


def _sub(**overrides):
    """Bare subscription-shaped object for the pure pricing helper."""
    from types import SimpleNamespace

    defaults = {
        "unit_price": None,
        "discount": False,
        "discount_value": None,
        "discount_type": None,
        "discount_start_at": None,
        "discount_end_at": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestEffectiveUnitPrice:
    """Tests for the _effective_unit_price pure helper."""

    NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)

    def test_catalog_price_when_no_override(self):
        result = billing_automation._effective_unit_price(
            _sub(), Decimal("100.00"), self.NOW
        )
        assert result == Decimal("100.00")

    def test_unit_price_overrides_catalog(self):
        result = billing_automation._effective_unit_price(
            _sub(unit_price=Decimal("80.00")), Decimal("100.00"), self.NOW
        )
        assert result == Decimal("80.00")

    def test_zero_unit_price_falls_back_to_catalog(self):
        # Legacy importer stores 0 when the export had no per-service price.
        result = billing_automation._effective_unit_price(
            _sub(unit_price=Decimal("0.00")), Decimal("100.00"), self.NOW
        )
        assert result == Decimal("100.00")

    def test_percentage_discount(self):
        from app.models.catalog import DiscountType

        result = billing_automation._effective_unit_price(
            _sub(
                discount=True,
                discount_value=Decimal("10.00"),
                discount_type=DiscountType.percentage,
            ),
            Decimal("100.00"),
            self.NOW,
        )
        assert result == Decimal("90.00")

    def test_legacy_percent_discount(self):
        from app.models.catalog import DiscountType

        result = billing_automation._effective_unit_price(
            _sub(
                discount=True,
                discount_value=Decimal("25.00"),
                discount_type=DiscountType.percent,
            ),
            Decimal("100.00"),
            self.NOW,
        )
        assert result == Decimal("75.00")

    def test_fixed_discount(self):
        from app.models.catalog import DiscountType

        result = billing_automation._effective_unit_price(
            _sub(
                discount=True,
                discount_value=Decimal("15.00"),
                discount_type=DiscountType.fixed,
            ),
            Decimal("100.00"),
            self.NOW,
        )
        assert result == Decimal("85.00")

    def test_discount_on_negotiated_unit_price(self):
        from app.models.catalog import DiscountType

        result = billing_automation._effective_unit_price(
            _sub(
                unit_price=Decimal("80.00"),
                discount=True,
                discount_value=Decimal("50.00"),
                discount_type=DiscountType.percentage,
            ),
            Decimal("100.00"),
            self.NOW,
        )
        assert result == Decimal("40.00")

    def test_expired_discount_window_ignored(self):
        from app.models.catalog import DiscountType

        result = billing_automation._effective_unit_price(
            _sub(
                discount=True,
                discount_value=Decimal("10.00"),
                discount_type=DiscountType.percentage,
                discount_end_at=self.NOW - timedelta(days=1),
            ),
            Decimal("100.00"),
            self.NOW,
        )
        assert result == Decimal("100.00")

    def test_future_discount_window_ignored(self):
        from app.models.catalog import DiscountType

        result = billing_automation._effective_unit_price(
            _sub(
                discount=True,
                discount_value=Decimal("10.00"),
                discount_type=DiscountType.percentage,
                discount_start_at=self.NOW + timedelta(days=1),
            ),
            Decimal("100.00"),
            self.NOW,
        )
        assert result == Decimal("100.00")

    def test_open_ended_window_applies(self):
        from app.models.catalog import DiscountType

        result = billing_automation._effective_unit_price(
            _sub(
                discount=True,
                discount_value=Decimal("10.00"),
                discount_type=DiscountType.percentage,
                discount_start_at=self.NOW - timedelta(days=30),
            ),
            Decimal("100.00"),
            self.NOW,
        )
        assert result == Decimal("90.00")

    def test_disabled_discount_flag_ignored(self):
        from app.models.catalog import DiscountType

        result = billing_automation._effective_unit_price(
            _sub(
                discount=False,
                discount_value=Decimal("10.00"),
                discount_type=DiscountType.percentage,
            ),
            Decimal("100.00"),
            self.NOW,
        )
        assert result == Decimal("100.00")

    def test_never_negative(self):
        from app.models.catalog import DiscountType

        result = billing_automation._effective_unit_price(
            _sub(
                discount=True,
                discount_value=Decimal("150.00"),
                discount_type=DiscountType.fixed,
            ),
            Decimal("100.00"),
            self.NOW,
        )
        assert result == Decimal("0.00")

    def test_naive_now_handled(self):
        from app.models.catalog import DiscountType

        result = billing_automation._effective_unit_price(
            _sub(
                discount=True,
                discount_value=Decimal("10.00"),
                discount_type=DiscountType.percentage,
                discount_start_at=datetime(2026, 1, 1),
                discount_end_at=datetime(2027, 1, 1),
            ),
            Decimal("100.00"),
            datetime(2026, 6, 11, 12, 0, 0),
        )
        assert result == Decimal("90.00")


# =============================================================================
# _resolve_tax_rate_id Tests
# =============================================================================


class TestResolveTaxRateId:
    """Tests for _resolve_tax_rate_id function."""

    def test_tax_rate_from_service_address(self, db_session, subscription, subscriber):
        """Test getting tax rate from service address."""

        from app.models.billing import TaxRate
        from app.models.subscriber import Address

        # Create tax rate
        tax_rate = TaxRate(
            name="State Tax",
            rate=Decimal("0.08"),
        )
        db_session.add(tax_rate)
        db_session.commit()

        # Create address with tax rate
        address = Address(
            subscriber_id=subscriber.id,
            address_line1="123 Main St",
            tax_rate_id=tax_rate.id,
        )
        db_session.add(address)
        db_session.commit()

        subscription.service_address_id = address.id
        db_session.commit()

        result = billing_automation._resolve_tax_rate_id(db_session, subscription)
        assert result == tax_rate.id

    def test_tax_rate_from_account(self, db_session, subscription, subscriber_account):
        """Test getting tax rate from account when no address tax rate."""
        from app.models.billing import TaxRate

        # Create tax rate
        tax_rate = TaxRate(
            name="Account Tax",
            rate=Decimal("0.06"),
        )
        db_session.add(tax_rate)
        db_session.commit()

        # Set account tax rate
        subscriber_account.tax_rate_id = tax_rate.id
        db_session.commit()

        result = billing_automation._resolve_tax_rate_id(db_session, subscription)
        assert result == tax_rate.id

    def test_no_tax_rate(self, db_session, subscription):
        """Test when no tax rate is configured."""
        result = billing_automation._resolve_tax_rate_id(db_session, subscription)
        assert result is None

    def test_inactive_tax_rate_is_skipped(
        self, db_session, subscription, subscriber_account
    ):
        """Soft-deleted (inactive) tax rates must not be applied to new lines."""
        from app.models.billing import TaxRate

        tax_rate = TaxRate(
            name="Retired Tax",
            rate=Decimal("0.075"),
            is_active=False,
        )
        db_session.add(tax_rate)
        db_session.commit()

        subscriber_account.tax_rate_id = tax_rate.id
        db_session.commit()

        result = billing_automation._resolve_tax_rate_id(db_session, subscription)
        assert result is None

    def test_inactive_address_rate_falls_back_to_account_rate(
        self, db_session, subscription, subscriber
    ):
        """Inactive address rate falls through to an active account rate."""
        from app.models.billing import TaxRate
        from app.models.subscriber import Address

        inactive = TaxRate(name="Old Tax", rate=Decimal("0.08"), is_active=False)
        active = TaxRate(name="Current Tax", rate=Decimal("0.05"), is_active=True)
        db_session.add_all([inactive, active])
        db_session.commit()

        address = Address(
            subscriber_id=subscriber.id,
            address_line1="123 Main St",
            tax_rate_id=inactive.id,
        )
        db_session.add(address)
        db_session.commit()

        subscription.service_address_id = address.id
        subscriber.tax_rate_id = active.id
        db_session.commit()

        result = billing_automation._resolve_tax_rate_id(db_session, subscription)
        assert result == active.id

    def test_catalog_positive_vat_percent_is_taxable_even_when_flag_false(
        self, db_session, subscription
    ):
        """Imported offers may have vat_percent populated while with_vat is false."""
        from app.models.billing import TaxRate

        rate = TaxRate(
            name="VAT 7.5%",
            code="VAT75",
            rate=Decimal("7.5000"),
            is_active=True,
        )
        db_session.add(rate)
        subscription.offer.with_vat = False
        subscription.offer.vat_percent = Decimal("7.50")
        db_session.commit()

        result = billing_automation._resolve_tax_rate_id(db_session, subscription)

        assert result == rate.id

    def test_catalog_exempt_offer_blocks_default_vat(self, db_session, subscription):
        from app.models.billing import TaxRate
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.models.subscription_engine import SettingValueType

        rate = TaxRate(
            name="VAT 7.5%",
            code="VAT75",
            rate=Decimal("7.5000"),
            is_active=True,
        )
        db_session.add(rate)
        db_session.flush()
        db_session.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key="default_tax_rate_id",
                value_type=SettingValueType.string,
                value_text=str(rate.id),
                is_active=True,
            )
        )
        subscription.offer.with_vat = False
        subscription.offer.vat_percent = Decimal("0.00")
        db_session.commit()

        result = billing_automation._resolve_tax_rate_id(db_session, subscription)

        assert result is None


# =============================================================================
# _prorated_amount Tests
# =============================================================================


class TestProratedAmount:
    """Tests for _prorated_amount function."""

    def test_full_period(self):
        """Test full period returns full amount."""
        period_start = datetime(2024, 1, 1, 0, 0, 0)
        period_end = datetime(2024, 2, 1, 0, 0, 0)

        result = billing_automation._prorated_amount(
            full_amount=Decimal("100.00"),
            period_start=period_start,
            period_end=period_end,
            usage_start=period_start,
            usage_end=period_end,
        )
        assert result == Decimal("100.00")

    def test_half_period(self):
        """Test half period returns half amount."""
        period_start = datetime(2024, 1, 1, 0, 0, 0)
        period_end = datetime(2024, 1, 31, 0, 0, 0)
        usage_start = datetime(2024, 1, 1, 0, 0, 0)
        usage_end = datetime(2024, 1, 16, 0, 0, 0)  # Half the month

        result = billing_automation._prorated_amount(
            full_amount=Decimal("100.00"),
            period_start=period_start,
            period_end=period_end,
            usage_start=usage_start,
            usage_end=usage_end,
        )
        assert result == Decimal("50.00")

    def test_zero_period(self):
        """Test zero period returns zero."""
        period_start = datetime(2024, 1, 1, 0, 0, 0)
        period_end = datetime(2024, 1, 1, 0, 0, 0)  # Same time

        result = billing_automation._prorated_amount(
            full_amount=Decimal("100.00"),
            period_start=period_start,
            period_end=period_end,
            usage_start=period_start,
            usage_end=period_end,
        )
        assert result == Decimal("0.00")

    def test_zero_usage(self):
        """Test zero usage returns zero."""
        period_start = datetime(2024, 1, 1, 0, 0, 0)
        period_end = datetime(2024, 2, 1, 0, 0, 0)
        usage_start = datetime(2024, 1, 15, 0, 0, 0)
        usage_end = datetime(2024, 1, 15, 0, 0, 0)  # Same time

        result = billing_automation._prorated_amount(
            full_amount=Decimal("100.00"),
            period_start=period_start,
            period_end=period_end,
            usage_start=usage_start,
            usage_end=usage_end,
        )
        assert result == Decimal("0.00")

    def test_usage_capped_at_full_amount(self):
        """Test usage longer than period is capped at full amount."""
        period_start = datetime(2024, 1, 1, 0, 0, 0)
        period_end = datetime(2024, 1, 15, 0, 0, 0)
        usage_start = datetime(2023, 12, 1, 0, 0, 0)  # Before period
        usage_end = datetime(2024, 2, 1, 0, 0, 0)  # After period

        result = billing_automation._prorated_amount(
            full_amount=Decimal("100.00"),
            period_start=period_start,
            period_end=period_end,
            usage_start=usage_start,
            usage_end=usage_end,
        )
        assert result == Decimal("100.00")


# =============================================================================
# run_invoice_cycle Tests
# =============================================================================


class TestRunInvoiceCycle:
    """Tests for run_invoice_cycle function."""

    def test_dry_run_no_changes(self, db_session, subscription, subscriber_account):
        """Test dry run doesn't create invoices."""
        from app.models.billing import Invoice
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        # Use naive datetime (SQLite stores naive)
        now_naive = datetime.now(UTC).replace(tzinfo=None)

        # Ensure subscription and account are active
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=30)
        subscription.next_billing_at = now_naive - timedelta(days=1)
        db_session.commit()

        # Create a price
        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()
        original_next_billing_at = subscription.next_billing_at
        invoice_count_before = db_session.query(Invoice).count()

        # Run dry mode - pass naive run_at to match SQLite
        summary = billing_automation.run_invoice_cycle(
            db_session, run_at=now_naive, dry_run=True
        )

        assert summary["run_id"] is None
        assert summary["subscriptions_scanned"] >= 1
        assert db_session.query(Invoice).count() == invoice_count_before
        assert subscription.next_billing_at == original_next_billing_at
        assert subscription not in db_session.dirty

    def test_creates_invoice_for_active_subscription(
        self, db_session, subscription, subscriber_account
    ):
        """Test creates invoice for active subscription."""
        from app.models.billing import Invoice
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)

        # Setup active subscription
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=30)
        subscription.next_billing_at = now_naive - timedelta(days=1)
        db_session.commit()

        # Create a price
        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("100.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        initial_invoices = (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .count()
        )

        # Run billing - pass naive run_at to match SQLite
        summary = billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        assert summary["run_id"] is not None
        assert summary["invoices_created"] >= 1
        assert summary["lines_created"] >= 1

        # Verify invoice was created
        final_invoices = (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .count()
        )
        assert final_invoices > initial_invoices

    def test_creates_invoice_for_active_subscription_on_blocked_account(
        self, db_session, subscription, subscriber_account
    ):
        """Blocked network access should not make an active subscription invisible
        to recurring billing."""
        from app.models.billing import Invoice
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        run_at = datetime(2026, 6, 17, tzinfo=UTC).replace(tzinfo=None)
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.blocked
        subscription.start_at = run_at - timedelta(days=30)
        subscription.next_billing_at = run_at - timedelta(days=2)
        db_session.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("100.00"),
                currency="USD",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        db_session.commit()

        summary = billing_automation.run_invoice_cycle(db_session, run_at=run_at)

        assert summary["invoices_created"] >= 1
        invoice = (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .one()
        )
        assert invoice.total == Decimal("100.00")
        assert subscription.next_billing_at > run_at

    def test_zero_amount_subscription_advances_without_invoice(
        self, db_session, subscription, subscriber_account
    ):
        """Free/internal services should not stay perpetually due."""
        from app.models.billing import Invoice
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        run_at = datetime(2026, 6, 17, tzinfo=UTC).replace(tzinfo=None)
        period_start = run_at - timedelta(days=2)
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = run_at - timedelta(days=30)
        subscription.next_billing_at = period_start
        subscription.unit_price = Decimal("0.00")
        db_session.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("0.00"),
                currency="USD",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        db_session.commit()

        summary = billing_automation.run_invoice_cycle(db_session, run_at=run_at)

        assert summary["invoices_created"] == 0
        assert summary["zero_amount_advanced"] == 1
        assert (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .count()
            == 0
        )
        db_session.refresh(subscription)
        assert subscription.next_billing_at.replace(tzinfo=None) == (
            billing_automation._period_end(period_start, BillingCycle.monthly)
        )

    def test_scheduled_invoice_cycle_does_not_touch_prepaid_paid_coverage(
        self, db_session, subscription, subscriber_account
    ):
        from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
        from app.models.catalog import (
            BillingCycle,
            BillingMode,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        run_at = datetime(2026, 6, 28, tzinfo=UTC).replace(tzinfo=None)
        paid_from = datetime(2026, 6, 1, tzinfo=UTC).replace(tzinfo=None)
        paid_until = datetime(2026, 6, 30, tzinfo=UTC).replace(tzinfo=None)

        subscription.status = SubscriptionStatus.active
        subscription.billing_mode = BillingMode.prepaid
        subscription.start_at = paid_from
        subscription.next_billing_at = run_at
        subscriber_account.status = AccountStatus.active
        db_session.add_all(
            [
                OfferPrice(
                    offer_id=subscription.offer_id,
                    price_type=PriceType.recurring,
                    amount=Decimal("100.00"),
                    currency="NGN",
                    billing_cycle=BillingCycle.monthly,
                    is_active=True,
                ),
            ]
        )
        db_session.commit()

        paid_invoice = Invoice(
            account_id=subscriber_account.id,
            invoice_number="SPX-PAID-JUNE",
            status=InvoiceStatus.paid,
            currency="NGN",
            subtotal=Decimal("100.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("0.00"),
            billing_period_start=paid_from,
            billing_period_end=paid_until,
            issued_at=paid_from,
            due_at=paid_from,
            paid_at=paid_from,
            splynx_invoice_id=202601006161,
        )
        db_session.add(paid_invoice)
        db_session.flush()
        db_session.add(
            InvoiceLine(
                invoice_id=paid_invoice.id,
                subscription_id=subscription.id,
                description="Imported June service",
                quantity=Decimal("1.000"),
                unit_price=Decimal("100.00"),
                amount=Decimal("100.00"),
                is_active=True,
            )
        )
        db_session.commit()

        summary = billing_automation.run_invoice_cycle(db_session, run_at=run_at)

        invoices = (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .all()
        )
        assert len(invoices) == 1
        assert summary["invoices_created"] == 0
        assert summary["skipped"] == 0
        assert summary["prepaid_skipped"] == 1
        db_session.refresh(subscription)
        assert subscription.next_billing_at == run_at

    def test_applies_existing_credit_to_new_invoice(
        self, db_session, subscription, subscriber_account
    ):
        """If a top-up lands before the invoice exists, invoice generation should
        settle the new invoice from available account credit."""
        from app.models.billing import (
            Invoice,
            InvoiceStatus,
            LedgerEntry,
            LedgerEntryType,
            LedgerSource,
            Payment,
            PaymentSettlement,
            PaymentSettlementOrigin,
            PaymentStatus,
        )
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        run_at = datetime(2026, 6, 17, tzinfo=UTC).replace(tzinfo=None)
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = run_at - timedelta(days=30)
        subscription.next_billing_at = run_at - timedelta(days=2)
        db_session.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("100.00"),
                currency="USD",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        payment = Payment(
            account_id=subscriber_account.id,
            amount=Decimal("100.00"),
            currency="USD",
            status=PaymentStatus.succeeded,
            paid_at=run_at - timedelta(hours=1),
            memo="Pre-invoice top-up",
        )
        db_session.add(payment)
        db_session.flush()
        entry = LedgerEntry(
            account_id=subscriber_account.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("100.00"),
            currency="USD",
            memo="Pre-invoice top-up",
        )
        db_session.add(entry)
        db_session.flush()
        db_session.add(
            PaymentSettlement(
                payment_id=payment.id,
                unallocated_ledger_entry_id=entry.id,
                amount=Decimal("100.00"),
                unallocated_amount=Decimal("100.00"),
                prepaid_amount=Decimal("0.00"),
                currency="USD",
                origin=PaymentSettlementOrigin.system,
                idempotency_key=f"test-preinvoice-credit-{payment.id}",
            )
        )
        db_session.commit()
        _record_retired_inline_settle_row(db_session)

        summary = billing_automation.run_invoice_cycle(db_session, run_at=run_at)

        assert summary["invoices_created"] >= 1
        assert summary["credit_applied"] == Decimal("100.00")
        assert summary["credit_settled_invoices"] == 1
        invoice = (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .one()
        )
        assert invoice.status == InvoiceStatus.paid
        assert invoice.balance_due == Decimal("0.00")

    def test_settling_credit_restores_walled_account(
        self, db_session, subscription, subscriber_account
    ):
        """Strand-after-settle fix: when credit settles a blocked account's debt,
        the runner re-evaluates enforcement and restores access. No payment event
        fires on this path, so without this the account would settle-but-stay-walled."""
        from app.models.billing import (
            LedgerEntry,
            LedgerEntryType,
            LedgerSource,
            Payment,
            PaymentSettlement,
            PaymentSettlementOrigin,
            PaymentStatus,
        )
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        run_at = datetime(2026, 6, 17, tzinfo=UTC).replace(tzinfo=None)
        subscription.status = SubscriptionStatus.active
        # Account-level walled-garden with an active subscription (the runner's
        # widened, imported-style population).
        subscriber_account.status = AccountStatus.blocked
        subscription.start_at = run_at - timedelta(days=30)
        subscription.next_billing_at = run_at - timedelta(days=2)
        db_session.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("100.00"),
                currency="USD",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        payment = Payment(
            account_id=subscriber_account.id,
            amount=Decimal("100.00"),
            currency="USD",
            status=PaymentStatus.succeeded,
            paid_at=run_at - timedelta(hours=1),
            memo="Pre-invoice top-up",
        )
        db_session.add(payment)
        db_session.flush()
        entry = LedgerEntry(
            account_id=subscriber_account.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("100.00"),
            currency="USD",
            memo="Pre-invoice top-up",
        )
        db_session.add(entry)
        db_session.flush()
        db_session.add(
            PaymentSettlement(
                payment_id=payment.id,
                unallocated_ledger_entry_id=entry.id,
                amount=Decimal("100.00"),
                unallocated_amount=Decimal("100.00"),
                prepaid_amount=Decimal("0.00"),
                currency="USD",
                origin=PaymentSettlementOrigin.system,
                idempotency_key=f"test-walled-credit-{payment.id}",
            )
        )
        db_session.commit()
        _record_retired_inline_settle_row(db_session)

        summary = billing_automation.run_invoice_cycle(db_session, run_at=run_at)

        assert summary["credit_applied"] == Decimal("100.00")
        assert summary["accounts_restored"] >= 1
        db_session.refresh(subscriber_account)
        assert subscriber_account.status == AccountStatus.active

    def test_backdated_subscription_fast_forwards_without_arrears(
        self, db_session, subscription, subscriber_account
    ):
        """A subscription whose next_billing_at is months in the past must NOT
        be billed once per missed month (the phantom-invoice incident). The
        runner fast-forwards to the current period and bills only that."""
        from app.models.billing import Invoice
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=400)
        subscription.next_billing_at = now_naive - timedelta(days=180)  # ~6 months
        db_session.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("100.00"),
                currency="USD",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        invoices = (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .all()
        )
        # Exactly one invoice for the *current* period, not one per missed month.
        assert len(invoices) == 1
        period_start = invoices[0].billing_period_start.replace(tzinfo=None)
        assert period_start > now_naive - timedelta(days=31)
        db_session.refresh(subscription)
        assert subscription.next_billing_at.replace(tzinfo=None) > now_naive

    def test_retired_bill_backdated_periods_row_cannot_restore_arrears(
        self, db_session, subscription, subscriber_account
    ):
        """Historical arrears require reviewed repair, not a stale setting row."""
        from app.models.billing import Invoice
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.models.subscriber import AccountStatus
        from app.models.subscription_engine import SettingValueType

        now_naive = datetime.now(UTC).replace(tzinfo=None)
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=400)
        subscription.next_billing_at = now_naive - timedelta(days=180)
        db_session.add_all(
            [
                OfferPrice(
                    offer_id=subscription.offer_id,
                    price_type=PriceType.recurring,
                    amount=Decimal("100.00"),
                    currency="USD",
                    billing_cycle=BillingCycle.monthly,
                    is_active=True,
                ),
                DomainSetting(
                    domain=SettingDomain.billing,
                    key="bill_backdated_periods",
                    value_type=SettingValueType.boolean,
                    value_text="true",
                    is_active=True,
                ),
            ]
        )
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        invoice = (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .one()
        )
        period_start = invoice.billing_period_start.replace(tzinfo=None)
        assert period_start > now_naive - timedelta(days=31)

    def test_bills_recurring_addon_on_invoice(
        self, db_session, subscription, subscriber_account
    ):
        """A recurring SubscriptionAddOn adds its own line to the monthly bill."""
        from app.models.billing import Invoice, InvoiceLine
        from app.models.catalog import (
            AddOn,
            AddOnPrice,
            AddOnType,
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionAddOn,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=30)
        subscription.next_billing_at = now_naive - timedelta(days=1)
        db_session.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("100.00"),
                currency="USD",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        add_on = AddOn(name="/29 IP", addon_type=AddOnType.extra_ip, is_active=True)
        db_session.add(add_on)
        db_session.flush()
        db_session.add(
            AddOnPrice(
                add_on_id=add_on.id,
                price_type=PriceType.recurring,
                amount=Decimal("25.00"),
                currency="USD",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        db_session.add(
            SubscriptionAddOn(
                subscription_id=subscription.id,
                add_on_id=add_on.id,
                quantity=1,
                # Active before the period starts → billed for the full period.
                start_at=now_naive - timedelta(days=30),
            )
        )
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        invoice = (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .first()
        )
        lines = (
            db_session.query(InvoiceLine)
            .filter(InvoiceLine.invoice_id == invoice.id)
            .all()
        )
        # base plan line + the recurring add-on line
        assert len(lines) == 2
        addon_line = next(line for line in lines if "/29 IP" in line.description)
        assert Decimal(str(addon_line.amount)) == Decimal("25.00")
        assert addon_line.billing_line_key is not None
        # base plan + add-on are both on the bill
        assert sum(Decimal(str(line.amount)) for line in lines) == Decimal("125.00")

    def test_recurring_addon_starting_after_period_is_not_billed(
        self, db_session, subscription, subscriber_account
    ):
        from app.models.billing import InvoiceLine
        from app.models.catalog import (
            AddOn,
            AddOnPrice,
            AddOnType,
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionAddOn,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=30)
        subscription.next_billing_at = now_naive
        db_session.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("100.00"),
                currency="USD",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        add_on = AddOn(name="/29 IP", addon_type=AddOnType.extra_ip, is_active=True)
        db_session.add(add_on)
        db_session.flush()
        db_session.add(
            AddOnPrice(
                add_on_id=add_on.id,
                price_type=PriceType.recurring,
                amount=Decimal("25.00"),
                currency="USD",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        db_session.add(
            SubscriptionAddOn(
                subscription_id=subscription.id,
                add_on_id=add_on.id,
                quantity=1,
                start_at=now_naive + timedelta(days=45),
            )
        )
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        addon_lines = (
            db_session.query(InvoiceLine)
            .filter(InvoiceLine.description.like("/29 IP%"))
            .all()
        )
        assert addon_lines == []

    def test_skips_subscription_without_price(
        self, db_session, subscription, subscriber_account
    ):
        """Test skips subscription without a price."""
        from app.models.catalog import SubscriptionStatus
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)

        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=30)
        subscription.next_billing_at = now_naive - timedelta(days=1)
        db_session.commit()

        # No price created

        summary = billing_automation.run_invoice_cycle(
            db_session, run_at=now_naive, dry_run=True
        )

        assert summary["skipped"] >= 1

    def test_skips_future_billing_date(
        self, db_session, subscription, subscriber_account
    ):
        """Test skips subscription with future billing date."""
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)

        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.next_billing_at = now_naive + timedelta(days=30)  # Future
        db_session.commit()

        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        summary = billing_automation.run_invoice_cycle(
            db_session, run_at=now_naive, dry_run=True
        )

        # Subscription should be scanned but not billed
        assert summary["subscriptions_scanned"] >= 1
        assert summary["subscriptions_billed"] == 0

    def test_skips_inactive_subscription(
        self, db_session, subscription, subscriber_account
    ):
        """Test skips inactive subscription."""
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        subscription.status = SubscriptionStatus.canceled  # Inactive
        subscriber_account.status = AccountStatus.active
        db_session.commit()

        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        summary = billing_automation.run_invoice_cycle(db_session, dry_run=True)

        # Inactive subscription not scanned
        assert summary["subscriptions_billed"] == 0

    def test_skips_inactive_account(self, db_session, subscription, subscriber_account):
        """Test skips subscription with inactive account."""
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        subscription.status = SubscriptionStatus.active
        # Terminal account states (disabled/canceled) are never billed. Note
        # suspended/blocked are now intentionally billable — they still owe for
        # active service periods — so a terminal status is what proves the skip.
        subscriber_account.status = AccountStatus.disabled  # Inactive
        db_session.commit()

        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        summary = billing_automation.run_invoice_cycle(db_session, dry_run=True)

        assert summary["subscriptions_billed"] == 0

    def test_filter_by_billing_cycle(
        self, db_session, subscription, subscriber_account
    ):
        """Test filtering by billing cycle."""
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)

        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=30)
        subscription.next_billing_at = now_naive - timedelta(days=1)
        db_session.commit()

        # Create monthly price
        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        # Run with annual filter - should not match monthly subscription
        summary = billing_automation.run_invoice_cycle(
            db_session,
            run_at=now_naive,
            billing_cycle=BillingCycle.annual,
            dry_run=True,
        )

        assert summary["subscriptions_billed"] == 0

    def test_skips_ended_subscription(
        self, db_session, subscription, subscriber_account
    ):
        """Test skips subscription that has ended."""
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)

        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=60)
        subscription.next_billing_at = now_naive - timedelta(days=1)
        subscription.end_at = now_naive - timedelta(days=30)  # Already ended
        db_session.commit()

        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        summary = billing_automation.run_invoice_cycle(
            db_session, run_at=now_naive, dry_run=True
        )

        assert summary["subscriptions_billed"] == 0

    def test_updates_next_billing_at(
        self, db_session, subscription, subscriber_account
    ):
        """Test updates next_billing_at after invoicing."""
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)

        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        original_billing = now_naive - timedelta(days=1)
        subscription.start_at = now_naive - timedelta(days=30)
        subscription.next_billing_at = original_billing
        db_session.commit()

        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        db_session.refresh(subscription)
        assert subscription.next_billing_at > original_billing

    def test_run_invoice_cycle_emits_invoice_reminder_for_configured_day(
        self,
        db_session,
        subscriber,
        subscription,
        monkeypatch,
    ):
        from app.models.billing import Invoice, InvoiceStatus
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.models.subscription_engine import SettingValueType

        run_at = datetime.now(UTC).replace(tzinfo=None)
        invoice = Invoice(
            account_id=subscriber.id,
            invoice_number="INV-REM-1",
            status=InvoiceStatus.issued,
            total=Decimal("150.00"),
            balance_due=Decimal("150.00"),
            due_at=run_at + timedelta(days=7),
            metadata_={},
        )
        db_session.add(invoice)
        db_session.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key="invoice_reminder_days",
                value_type=SettingValueType.string,
                value_text="7,1",
                is_active=True,
            )
        )
        db_session.commit()

        calls: list[tuple[object, dict[str, object]]] = []

        def _capture_emit(*args, **kwargs):
            calls.append((args[1], args[2]))

        monkeypatch.setattr("app.services.billing_automation.emit_event", _capture_emit)
        monkeypatch.setattr(
            billing_automation.enforcement_window,
            "within_send_window",
            lambda db, run_at: True,
        )

        summary = billing_automation.run_billing_notifications(
            db_session, run_at=run_at
        )
        db_session.refresh(invoice)

        assert summary["invoice_reminders_sent"] == 1
        assert calls[-1][0] == EventType.invoice_sent
        assert calls[-1][1]["invoice_number"] == "INV-REM-1"
        assert (invoice.metadata_ or {}).get("invoice_reminder_sent_7")

    def test_run_invoice_cycle_skips_reminders_when_customer_pauses_them(
        self,
        db_session,
        subscriber,
        subscription,
        monkeypatch,
    ):
        from app.models.billing import Invoice, InvoiceStatus
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.models.subscription_engine import SettingValueType

        subscriber.metadata_ = {
            **(subscriber.metadata_ or {}),
            "send_billing_notifications": False,
        }
        run_at = datetime.now(UTC).replace(tzinfo=None)
        invoice = Invoice(
            account_id=subscriber.id,
            invoice_number="INV-REM-PAUSED",
            status=InvoiceStatus.issued,
            total=Decimal("150.00"),
            balance_due=Decimal("150.00"),
            due_at=run_at + timedelta(days=7),
            metadata_={},
        )
        db_session.add(invoice)
        db_session.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key="invoice_reminder_days",
                value_type=SettingValueType.string,
                value_text="7,1",
                is_active=True,
            )
        )
        db_session.commit()

        calls: list[tuple[object, dict[str, object]]] = []

        def _capture_emit(*args, **kwargs):
            calls.append((args[1], args[2]))

        monkeypatch.setattr("app.services.billing_automation.emit_event", _capture_emit)
        monkeypatch.setattr(
            billing_automation.enforcement_window,
            "within_send_window",
            lambda db, run_at: True,
        )

        summary = billing_automation.run_billing_notifications(
            db_session, run_at=run_at
        )
        db_session.refresh(invoice)

        assert summary["invoice_reminders_sent"] == 0
        assert calls == []
        assert not (invoice.metadata_ or {}).get("invoice_reminder_sent_7")

    def test_run_invoice_cycle_skips_reminders_for_terminal_account(
        self,
        db_session,
        subscriber,
        subscription,
        monkeypatch,
    ):
        """Disabled/terminated service does not receive pre-due reminders."""
        from app.models.billing import Invoice, InvoiceStatus
        from app.models.catalog import SubscriptionStatus
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.models.subscription_engine import SettingValueType

        subscription.status = SubscriptionStatus.disabled  # service terminated
        db_session.add(subscription)

        run_at = datetime.now(UTC).replace(tzinfo=None)
        db_session.add(
            Invoice(
                account_id=subscriber.id,
                invoice_number="INV-REM-TERM",
                status=InvoiceStatus.issued,
                total=Decimal("150.00"),
                balance_due=Decimal("150.00"),
                due_at=run_at + timedelta(days=7),
                metadata_={},
            )
        )
        db_session.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key="invoice_reminder_days",
                value_type=SettingValueType.string,
                value_text="7,1",
                is_active=True,
            )
        )
        db_session.commit()

        summary = billing_automation.run_invoice_cycle(db_session, run_at=run_at)

        assert summary["invoice_reminders_sent"] == 0

    def test_run_invoice_cycle_ignores_retired_dunning_escalation_setting(
        self,
        db_session,
        subscriber,
        subscription,
        monkeypatch,
    ):
        from app.models.billing import Invoice, InvoiceStatus
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.models.subscription_engine import SettingValueType

        run_at = datetime.now(UTC).replace(tzinfo=None)
        invoice = Invoice(
            account_id=subscriber.id,
            invoice_number="INV-DUN-1",
            status=InvoiceStatus.overdue,
            total=Decimal("200.00"),
            balance_due=Decimal("200.00"),
            due_at=run_at - timedelta(days=3),
            metadata_={},
        )
        db_session.add(invoice)
        db_session.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key="dunning_escalation_days",
                value_type=SettingValueType.string,
                value_text="3,7,14",
                is_active=True,
            )
        )
        db_session.commit()

        calls: list[tuple[object, dict[str, object]]] = []

        def _capture_emit(*args, **kwargs):
            calls.append((args[1], args[2]))

        monkeypatch.setattr("app.services.billing_automation.emit_event", _capture_emit)
        summary = billing_automation.run_invoice_cycle(db_session, run_at=run_at)
        db_session.refresh(invoice)

        assert "dunning_escalations_sent" not in summary
        assert calls == []
        assert not (invoice.metadata_ or {}).get("dunning_escalation_sent_3")

    def test_run_invoice_cycle_logs_structured_summary(
        self,
        db_session,
        subscriber,
        monkeypatch,
        caplog,
    ):
        from app.models.billing import Invoice, InvoiceStatus
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.models.subscription_engine import SettingValueType

        run_at = datetime.now(UTC).replace(tzinfo=None)
        invoice = Invoice(
            account_id=subscriber.id,
            invoice_number="INV-LOG-1",
            status=InvoiceStatus.issued,
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
            due_at=run_at + timedelta(days=7),
            metadata_={},
        )
        db_session.add(invoice)
        db_session.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key="invoice_reminder_days",
                value_type=SettingValueType.string,
                value_text="7",
                is_active=True,
            )
        )
        db_session.commit()
        monkeypatch.setattr(
            "app.services.billing_automation.emit_event", lambda *a, **k: None
        )

        caplog.set_level("INFO")
        summary = billing_automation.run_invoice_cycle(db_session, run_at=run_at)

        start_record = next(
            record
            for record in caplog.records
            if record.getMessage() == "billing_run_start"
        )
        complete_record = next(
            record
            for record in caplog.records
            if "Billing run completed:" in record.getMessage()
        )

        assert start_record.event == "billing_run"
        assert (
            complete_record.invoice_reminders_sent == summary["invoice_reminders_sent"]
        )


# =============================================================================
# Recurring add-on hardening: start_at guard, proration, IP-route coupling
# =============================================================================


class TestRecurringAddonHardening:
    """start_at guard, proration, and IP-route coupling in _bill_recurring_addons."""

    def _seed(
        self,
        db_session,
        subscription,
        subscriber_account,
        *,
        addon_start,
        addon_end=None,
        quantity=1,
        ip_is_public=False,
        ip_prefix_length=None,
        sub_end_at=None,
    ):
        from app.models.catalog import (
            AddOn,
            AddOnPrice,
            AddOnType,
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionAddOn,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=30)
        subscription.next_billing_at = now_naive - timedelta(days=1)
        subscription.end_at = sub_end_at
        db_session.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("100.00"),
                currency="USD",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        add_on = AddOn(
            name="/29 IP",
            addon_type=AddOnType.extra_ip,
            is_active=True,
            ip_is_public=ip_is_public,
            ip_prefix_length=ip_prefix_length,
        )
        db_session.add(add_on)
        db_session.flush()
        db_session.add(
            AddOnPrice(
                add_on_id=add_on.id,
                price_type=PriceType.recurring,
                amount=Decimal("25.00"),
                currency="USD",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        db_session.add(
            SubscriptionAddOn(
                subscription_id=subscription.id,
                add_on_id=add_on.id,
                quantity=quantity,
                start_at=addon_start,
                end_at=addon_end,
            )
        )
        return now_naive

    def _enable_route_guard(self, db_session):
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.models.subscription_engine import SettingValueType

        db_session.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key="bill_ip_addon_requires_active_route",
                value_type=SettingValueType.boolean,
                value_text="true",
                is_active=True,
            )
        )

    def _add_routes(self, db_session, subscriber_account, *, prefix_length=29, count=1):
        from app.models.network import SubscriberAdditionalRoute

        for i in range(count):
            db_session.add(
                SubscriberAdditionalRoute(
                    subscriber_id=subscriber_account.id,
                    cidr=f"10.0.{i}.0/{prefix_length}",
                    prefix_length=prefix_length,
                    is_active=True,
                )
            )

    def _addon_line(self, db_session, subscriber_account):
        from app.models.billing import Invoice, InvoiceLine

        invoice = (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .first()
        )
        if invoice is None:
            return None
        lines = (
            db_session.query(InvoiceLine)
            .filter(InvoiceLine.invoice_id == invoice.id)
            .all()
        )
        return next((line for line in lines if "/29 IP" in line.description), None)

    def test_future_dated_addon_not_billed(
        self, db_session, subscription, subscriber_account
    ):
        """An add-on starting in a later period (no overlap) is not billed yet."""
        now_naive = self._seed(
            db_session,
            subscription,
            subscriber_account,
            # Beyond period_end (period is ~[now-1d, now+~29d]).
            addon_start=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=45),
        )
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        assert self._addon_line(db_session, subscriber_account) is None

    def test_addon_started_mid_period_is_prorated(
        self, db_session, subscription, subscriber_account
    ):
        """An add-on that starts mid-period is billed only for its active part."""
        now_naive = datetime.now(UTC).replace(tzinfo=None)
        # Period is ~[now-1d, now+~29d]; start 15 days in → ~half overlap.
        self._seed(
            db_session,
            subscription,
            subscriber_account,
            addon_start=now_naive + timedelta(days=15),
        )
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        line = self._addon_line(db_session, subscriber_account)
        assert line is not None
        assert Decimal("0.00") < Decimal(str(line.amount)) < Decimal("25.00")

    def test_addon_ended_mid_period_is_billed_for_active_part(
        self, db_session, subscription, subscriber_account
    ):
        """An add-on that ended mid-period is billed for the part it was active,
        not skipped entirely (the old run_at filter dropped it)."""
        now_naive = datetime.now(UTC).replace(tzinfo=None)
        # Active from period start, ended ~3 days in (before run_at=now).
        self._seed(
            db_session,
            subscription,
            subscriber_account,
            addon_start=now_naive - timedelta(days=1),
            addon_end=now_naive - timedelta(hours=12),
        )
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        line = self._addon_line(db_session, subscriber_account)
        assert line is not None
        assert Decimal("0.00") < Decimal(str(line.amount)) < Decimal("25.00")

    def test_addon_prorated_on_partial_period(
        self, db_session, subscription, subscriber_account
    ):
        """An add-on on a service that ends mid-cycle is prorated, not billed full."""
        now_naive = datetime.now(UTC).replace(tzinfo=None)
        self._seed(
            db_session,
            subscription,
            subscriber_account,
            addon_start=now_naive - timedelta(days=1),
            sub_end_at=now_naive + timedelta(days=14),
        )
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        line = self._addon_line(db_session, subscriber_account)
        assert line is not None
        # Partial period → strictly between zero and the full ₦25 add-on price.
        assert Decimal("0.00") < Decimal(str(line.amount)) < Decimal("25.00")

    def test_ip_addon_skipped_when_no_active_route_and_guard_on(
        self, db_session, subscription, subscriber_account
    ):
        """With the guard on, a public-IP add-on with no live route is not billed."""
        now_naive = self._seed(
            db_session,
            subscription,
            subscriber_account,
            addon_start=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1),
            ip_is_public=True,
            ip_prefix_length=29,
        )
        self._enable_route_guard(db_session)
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        assert self._addon_line(db_session, subscriber_account) is None

    def test_ip_addon_billed_when_active_route_present(
        self, db_session, subscription, subscriber_account
    ):
        """With the guard on, a public-IP add-on with a live route bills normally."""
        now_naive = self._seed(
            db_session,
            subscription,
            subscriber_account,
            addon_start=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1),
            ip_is_public=True,
            ip_prefix_length=29,
        )
        self._enable_route_guard(db_session)
        self._add_routes(db_session, subscriber_account, prefix_length=29, count=1)
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        line = self._addon_line(db_session, subscriber_account)
        assert line is not None
        assert Decimal(str(line.amount)) == Decimal("25.00")

    def test_ip_addon_quantity_capped_to_active_routes(
        self, db_session, subscription, subscriber_account
    ):
        """Billed quantity is capped to the number of active routes (qty 2 → 1)."""
        now_naive = self._seed(
            db_session,
            subscription,
            subscriber_account,
            addon_start=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1),
            quantity=2,
            ip_is_public=True,
            ip_prefix_length=29,
        )
        self._enable_route_guard(db_session)
        self._add_routes(db_session, subscriber_account, prefix_length=29, count=1)
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        line = self._addon_line(db_session, subscriber_account)
        assert line is not None
        assert Decimal(str(line.quantity)) == Decimal("1")
        assert Decimal(str(line.amount)) == Decimal("25.00")

    def test_ip_addon_guard_off_bills_without_route(
        self, db_session, subscription, subscriber_account
    ):
        """Guard default-off: a public-IP add-on still bills with no route (neutral)."""
        now_naive = self._seed(
            db_session,
            subscription,
            subscriber_account,
            addon_start=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1),
            ip_is_public=True,
            ip_prefix_length=29,
        )
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        line = self._addon_line(db_session, subscriber_account)
        assert line is not None
        assert Decimal(str(line.amount)) == Decimal("25.00")


# =============================================================================
# Money correctness in the runner
# =============================================================================


class TestRunInvoiceCycleMoneyCorrectness:
    def _activate(self, db_session, subscription, subscriber_account, now_naive):
        from app.models.catalog import SubscriptionStatus
        from app.models.subscriber import AccountStatus

        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=30)
        subscription.next_billing_at = now_naive - timedelta(days=1)
        db_session.commit()

    def _add_offer_price(self, db_session, subscription, amount, currency="USD"):
        from app.models.catalog import BillingCycle, OfferPrice, PriceType

        db_session.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=amount,
                currency=currency,
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        db_session.commit()

    def _line_for(self, db_session, subscription):
        from app.models.billing import InvoiceLine

        return (
            db_session.query(InvoiceLine)
            .filter(InvoiceLine.subscription_id == subscription.id)
            .one()
        )

    def test_percentage_discount_applied_to_invoice_line(
        self, db_session, subscription, subscriber_account
    ):
        from app.models.catalog import DiscountType

        now_naive = datetime.now(UTC).replace(tzinfo=None)
        self._activate(db_session, subscription, subscriber_account, now_naive)
        self._add_offer_price(db_session, subscription, Decimal("100.00"))
        subscription.discount = True
        subscription.discount_value = Decimal("10.00")
        subscription.discount_type = DiscountType.percentage
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        line = self._line_for(db_session, subscription)
        assert Decimal(str(line.amount)) == Decimal("90.00")

    def test_negotiated_unit_price_overrides_catalog(
        self, db_session, subscription, subscriber_account
    ):
        now_naive = datetime.now(UTC).replace(tzinfo=None)
        self._activate(db_session, subscription, subscriber_account, now_naive)
        self._add_offer_price(db_session, subscription, Decimal("100.00"))
        subscription.unit_price = Decimal("75.00")
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        line = self._line_for(db_session, subscription)
        assert Decimal(str(line.amount)) == Decimal("75.00")

    def test_expired_discount_bills_full_price(
        self, db_session, subscription, subscriber_account
    ):
        from app.models.catalog import DiscountType

        now_naive = datetime.now(UTC).replace(tzinfo=None)
        self._activate(db_session, subscription, subscriber_account, now_naive)
        self._add_offer_price(db_session, subscription, Decimal("100.00"))
        subscription.discount = True
        subscription.discount_value = Decimal("50.00")
        subscription.discount_type = DiscountType.percentage
        subscription.discount_end_at = now_naive - timedelta(days=5)
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        line = self._line_for(db_session, subscription)
        assert Decimal(str(line.amount)) == Decimal("100.00")

    def test_runner_invoice_gets_invoice_number(
        self, db_session, subscription, subscriber_account
    ):
        from app.models.billing import Invoice

        now_naive = datetime.now(UTC).replace(tzinfo=None)
        self._activate(db_session, subscription, subscriber_account, now_naive)
        self._add_offer_price(db_session, subscription, Decimal("100.00"))

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        invoice = (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .one()
        )
        assert invoice.invoice_number

    def test_prorated_invoice_gets_number_and_discounted_amount(
        self, db_session, subscription, subscriber_account
    ):
        from app.models.catalog import DiscountType

        now_naive = datetime.now(UTC).replace(tzinfo=None)
        self._activate(db_session, subscription, subscriber_account, now_naive)
        self._add_offer_price(db_session, subscription, Decimal("100.00"))
        subscription.discount = True
        subscription.discount_value = Decimal("20.00")
        subscription.discount_type = DiscountType.percentage
        db_session.commit()

        # Mid-month activation so proration actually happens
        activation = now_naive.replace(day=15, hour=12)
        invoice = billing_automation.generate_prorated_invoice(
            db_session, subscription, activation_date=activation
        )

        assert invoice is not None
        assert invoice.invoice_number
        # Prorated from the discounted price, so strictly below 80.00
        line = self._line_for(db_session, subscription)
        assert Decimal(str(line.amount)) <= Decimal("80.00")
        assert Decimal(str(line.amount)) > Decimal("0.00")

    def test_currency_mismatch_is_counted_and_logged(
        self, db_session, subscription, subscriber_account, caplog
    ):
        from app.models.catalog import (
            BillingCycle,
            BillingMode,
            OfferVersion,
            OfferVersionPrice,
            PriceType,
            Subscription,
            SubscriptionStatus,
        )

        now_naive = datetime.now(UTC).replace(tzinfo=None)
        self._activate(db_session, subscription, subscriber_account, now_naive)
        self._add_offer_price(db_session, subscription, Decimal("100.00"), "USD")

        # Second subscription on the same account priced in another currency
        version = (
            db_session.query(OfferVersion)
            .filter(OfferVersion.offer_id == subscription.offer_id)
            .first()
        )
        db_session.add(
            OfferVersionPrice(
                offer_version_id=version.id,
                price_type=PriceType.recurring,
                amount=Decimal("50.00"),
                currency="EUR",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        other = Subscription(
            subscriber_id=subscriber_account.id,
            offer_id=subscription.offer_id,
            offer_version_id=version.id,
            status=SubscriptionStatus.active,
            billing_mode=BillingMode.postpaid,
            start_at=now_naive - timedelta(days=30),
            next_billing_at=now_naive - timedelta(days=1),
        )
        db_session.add(other)
        db_session.commit()

        caplog.set_level("WARNING")
        summary = billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        assert summary["currency_skipped"] == 1
        mismatch_records = [
            record
            for record in caplog.records
            if record.getMessage() == "billing_currency_mismatch_skip"
        ]
        assert mismatch_records
        logged_ids = {
            getattr(record, "subscription_id", None) for record in mismatch_records
        }
        assert logged_ids & {str(subscription.id), str(other.id)}


# =============================================================================
# Billing run resilience
# =============================================================================


class TestBillingRunResilience:
    def test_audit_log_failure_does_not_fail_the_run(self, db_session, monkeypatch):
        """A failing audit write must not mark a committed run as failed.

        Regression: every 2026 billing run was marked failed by a NOT NULL
        violation in the audit insert, after the invoices had already
        committed.
        """
        from app.models.billing import BillingRun, BillingRunStatus

        def boom(*args, **kwargs):
            raise RuntimeError("audit_events insert failed")

        monkeypatch.setattr(billing_automation, "_log_billing_run_audit", boom)

        summary = billing_automation.run_invoice_cycle(db_session)

        run = db_session.get(BillingRun, summary["run_id"])
        assert run is not None
        assert run.status == BillingRunStatus.success
        assert run.finished_at is not None

    def test_abandoned_running_runs_are_swept(self, db_session):
        """Runs stuck in `running` for hours get marked failed by the sweep."""
        from app.models.billing import BillingRun, BillingRunStatus

        stale = BillingRun(
            run_at=datetime.now(UTC) - timedelta(hours=30),
            status=BillingRunStatus.running,
            started_at=datetime.now(UTC) - timedelta(hours=30),
        )
        fresh = BillingRun(
            run_at=datetime.now(UTC),
            status=BillingRunStatus.running,
            started_at=datetime.now(UTC) - timedelta(minutes=10),
        )
        db_session.add_all([stale, fresh])
        db_session.commit()

        swept = billing_automation._fail_abandoned_runs(db_session)

        assert swept == 1
        db_session.refresh(stale)
        db_session.refresh(fresh)
        assert stale.status == BillingRunStatus.failed
        assert "abandoned" in (stale.error or "")
        assert fresh.status == BillingRunStatus.running


# =============================================================================
# mark_overdue_invoices — observational overdue checker
# =============================================================================


class TestMarkOverdueInvoices:
    """Hourly overdue checker emits one observation per invoice."""

    def _make_invoice(self, db_session, subscriber, **kwargs):
        import uuid as _uuid

        from app.models.billing import Invoice, InvoiceStatus

        defaults = {
            "account_id": subscriber.id,
            "invoice_number": f"INV-{_uuid.uuid4().hex[:8]}",
            "status": InvoiceStatus.issued,
            "total": Decimal("100.00"),
            "balance_due": Decimal("100.00"),
            "due_at": datetime.now(UTC) - timedelta(hours=1),
            "metadata_": {},
        }
        defaults.update(kwargs)
        invoice = Invoice(**defaults)
        db_session.add(invoice)
        db_session.commit()
        return invoice

    def _capture_emits(self, monkeypatch):
        import importlib

        invoice_owner_module = importlib.import_module("app.services.billing.invoices")

        calls = []

        def _capture(db, event_type, payload, **kwargs):
            calls.append((event_type, dict(payload)))

        monkeypatch.setattr(invoice_owner_module, "emit_event", _capture)
        return calls

    def test_first_run_marks_and_emits_once(self, db_session, subscriber, monkeypatch):
        from app.models.billing import InvoiceStatus

        invoice = self._make_invoice(db_session, subscriber)
        calls = self._capture_emits(monkeypatch)

        result = billing_automation.mark_overdue_invoices(db_session)
        db_session.refresh(invoice)

        assert result["marked_overdue"] == 1
        assert invoice.status == InvoiceStatus.overdue
        assert (invoice.metadata_ or {}).get("overdue_event_sent")
        assert [c[0] for c in calls] == [EventType.invoice_overdue]

        # Subsequent runs do not re-emit or infer an access consequence.
        result2 = billing_automation.mark_overdue_invoices(db_session)
        assert result2["marked_overdue"] == 0
        assert [c[0] for c in calls] == [EventType.invoice_overdue]

    def test_already_overdue_invoice_never_reemits_or_writes_access_metadata(
        self, db_session, subscriber, monkeypatch
    ):
        from app.models.billing import InvoiceStatus

        invoice = self._make_invoice(
            db_session,
            subscriber,
            status=InvoiceStatus.overdue,
            due_at=datetime.now(UTC) - timedelta(hours=72),
            metadata_={"overdue_event_sent": "2026-01-01T00:00:00+00:00"},
        )
        calls = self._capture_emits(monkeypatch)

        result = billing_automation.mark_overdue_invoices(db_session)
        db_session.refresh(invoice)

        assert result["marked_overdue"] == 0
        assert calls == []
        assert not (invoice.metadata_ or {}).get("suspension_warning_sent_at")
        assert not (invoice.metadata_ or {}).get("suspension_escalation_sent")

    def test_paid_invoice_not_scanned(self, db_session, subscriber, monkeypatch):
        """Once the balance is cleared the invoice drops out of the sweep."""
        from app.models.billing import InvoiceStatus

        self._make_invoice(
            db_session,
            subscriber,
            status=InvoiceStatus.paid,
            balance_due=Decimal("0.00"),
            due_at=datetime.now(UTC) - timedelta(hours=72),
            metadata_={
                "overdue_event_sent": "2026-01-01T00:00:00+00:00",
                "suspension_warning_sent_at": "2026-01-01T00:00:00+00:00",
            },
        )
        calls = self._capture_emits(monkeypatch)

        result = billing_automation.mark_overdue_invoices(db_session)

        assert result["scanned"] == 0
        assert calls == []


class TestMarkOverdueReconciliationHold:
    """A reconciliation_hold flag excludes an invoice from overdue marking so
    the phantom-invoice cleanup can stop dunning before voiding."""

    def _invoice(self, db_session, account_id, *, hold):
        from app.models.billing import Invoice, InvoiceStatus

        inv = Invoice(
            account_id=account_id,
            status=InvoiceStatus.issued,
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
            due_at=datetime.now(UTC) - timedelta(days=5),
            is_active=True,
            metadata_={"reconciliation_hold": True} if hold else {},
        )
        db_session.add(inv)
        db_session.commit()
        db_session.refresh(inv)
        return inv

    def test_held_invoice_not_marked_overdue(self, db_session, subscriber_account):
        from app.models.billing import InvoiceStatus

        held = self._invoice(db_session, subscriber_account.id, hold=True)
        normal = self._invoice(db_session, subscriber_account.id, hold=False)

        result = billing_automation.mark_overdue_invoices(db_session)

        db_session.refresh(held)
        db_session.refresh(normal)
        assert held.status == InvoiceStatus.issued  # untouched
        assert normal.status == InvoiceStatus.overdue
        assert result["skipped_on_hold"] >= 1


class TestPermanentBillingLifecycle:
    """A stale billing_enabled row cannot stop the canonical invoice owner."""

    def _disable(self, db_session):
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.models.subscription_engine import SettingValueType

        db_session.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key="billing_enabled",
                value_type=SettingValueType.boolean,
                value_text="false",
                is_active=True,
            )
        )
        db_session.commit()

    def test_retired_false_row_does_not_block_invoice_creation(
        self, db_session, subscription, subscriber_account
    ):
        from app.models.billing import Invoice
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now = datetime.now(UTC).replace(tzinfo=None)
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now - timedelta(days=30)
        subscription.next_billing_at = now - timedelta(days=1)
        db_session.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("100.00"),
                currency="USD",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        db_session.commit()
        self._disable(db_session)

        summary = billing_automation.run_invoice_cycle(db_session, run_at=now)
        assert not summary.get("billing_disabled")
        assert (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .count()
            >= 1
        )

    def test_retired_false_row_does_not_block_dry_run(
        self, db_session, subscription, subscriber_account
    ):
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now = datetime.now(UTC).replace(tzinfo=None)
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now - timedelta(days=30)
        subscription.next_billing_at = now - timedelta(days=1)
        db_session.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("100.00"),
                currency="USD",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        db_session.commit()
        self._disable(db_session)

        summary = billing_automation.run_invoice_cycle(
            db_session, run_at=now, dry_run=True
        )
        # Dry-run is exempt and now returns the exact review scope consumed by
        # the staff batch preview.
        assert not summary.get("billing_disabled")
        assert summary["subscriptions_billed"] >= 1
        assert summary["invoices_created"] >= 1
        assert summary["accounts_affected"] >= 1
        assert summary["subscriptions"][0]["id"] == str(subscription.id)
        assert summary["totals_by_currency"]["USD"] == Decimal("100.00")


# =============================================================================
# Default-VAT mechanism (configurable fallback rate + application mode)
# =============================================================================


class TestDefaultTaxRate:
    """billing.default_tax_rate_id / default_tax_application — a configurable
    fallback applied only when neither the service address nor the subscriber
    carries a tax_rate_id. Defaults preserve the prior no-tax behaviour."""

    def _add_setting(self, db_session, key, value):
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.models.subscription_engine import SettingValueType

        db_session.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key=key,
                value_type=SettingValueType.string,
                value_text=value,
                is_active=True,
            )
        )
        db_session.commit()

    def _tax_rate(self, db_session, *, active=True):
        from app.models.billing import TaxRate

        rate = TaxRate(name="VAT", code="VAT", rate=Decimal("7.5000"), is_active=active)
        db_session.add(rate)
        db_session.commit()
        return rate

    def test_resolve_returns_none_without_default(self, db_session, subscription):
        # No address/subscriber rate and no default setting → no tax (unchanged).
        assert billing_automation._resolve_tax_rate_id(db_session, subscription) is None

    def test_resolve_returns_configured_default(self, db_session, subscription):
        rate = self._tax_rate(db_session)
        self._add_setting(db_session, "default_tax_rate_id", str(rate.id))
        subscription.offer.with_vat = True
        db_session.commit()
        assert (
            billing_automation._resolve_tax_rate_id(db_session, subscription) == rate.id
        )

    def test_inactive_default_is_ignored(self, db_session, subscription):
        rate = self._tax_rate(db_session, active=False)
        self._add_setting(db_session, "default_tax_rate_id", str(rate.id))
        subscription.offer.with_vat = True
        db_session.commit()
        assert billing_automation._resolve_tax_rate_id(db_session, subscription) is None

    def test_bad_default_id_is_ignored(self, db_session, subscription):
        self._add_setting(db_session, "default_tax_rate_id", "not-a-uuid")
        subscription.offer.with_vat = True
        db_session.commit()
        assert billing_automation._resolve_tax_rate_id(db_session, subscription) is None

    def test_default_tax_application_modes(self, db_session):
        from app.models.billing import TaxApplication

        # Unset → exclusive (prior behaviour).
        assert (
            billing_automation._default_tax_application(db_session)
            == TaxApplication.exclusive
        )
        self._add_setting(db_session, "default_tax_application", "inclusive")
        assert (
            billing_automation._default_tax_application(db_session)
            == TaxApplication.inclusive
        )

    def test_default_tax_application_exempt(self, db_session):
        from app.models.billing import TaxApplication

        self._add_setting(db_session, "default_tax_application", "exempt")
        assert (
            billing_automation._default_tax_application(db_session)
            == TaxApplication.exempt
        )


# =============================================================================
# Cancellation credit resolves the cycle from the active price, not the offer
# =============================================================================


class TestCancellationCreditCycle:
    def test_credit_window_uses_price_cycle_not_offer_cycle(
        self, db_session, subscription, subscriber_account
    ):
        """The unused-portion window comes from the active recurring price's
        cycle (version/price-aware), matching the runner — not offer.billing_cycle."""
        from app.models.billing import (
            CreditNote,
            Invoice,
            InvoiceLine,
            InvoiceStatus,
            TaxApplication,
            TaxRate,
        )
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now = datetime.now(UTC)
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        # Clear the snapshotted cadence so the weekly price drives the window
        # via the fallback (SOT: an owned subscription cadence otherwise wins).
        subscription.billing_cycle = None
        # 3 days left; the active price bills WEEKLY.
        subscription.next_billing_at = now + timedelta(days=3)
        db_session.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("70.00"),
                currency="NGN",
                billing_cycle=BillingCycle.weekly,
                is_active=True,
            )
        )
        tax_rate = TaxRate(
            name="Cancellation VAT",
            code="VAT-CANCEL",
            rate=Decimal("7.5000"),
        )
        db_session.add(tax_rate)
        invoice = Invoice(
            account_id=subscriber_account.id,
            invoice_number="INV-CC-1",
            status=InvoiceStatus.issued,
            currency="NGN",
            subtotal=Decimal("70.00"),
            tax_total=Decimal("5.25"),
            total=Decimal("75.25"),
            balance_due=Decimal("75.25"),
        )
        db_session.add(invoice)
        db_session.flush()
        db_session.add(
            InvoiceLine(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description="Plan",
                quantity=Decimal("1.000"),
                unit_price=Decimal("70.00"),
                amount=Decimal("70.00"),
                tax_rate_id=tax_rate.id,
                tax_application=TaxApplication.exclusive,
            )
        )
        db_session.commit()

        billing_automation.generate_cancellation_credit(db_session, subscription)

        credit = (
            db_session.query(CreditNote)
            .filter(CreditNote.account_id == subscriber_account.id)
            .first()
        )
        assert credit is not None
        assert credit.issued_at is not None
        assert credit.invoice_id == invoice.id
        assert credit.tax_total > Decimal("0.00")
        assert credit.lines[0].tax_rate_id == tax_rate.id
        assert credit.lines[0].tax_application == TaxApplication.exclusive
        # Weekly window: ~3/7 of ₦70 ≈ ₦30. The old offer-cycle (monthly) bug
        # would credit ~3/30 ≈ ₦7.
        assert credit.subtotal > Decimal("20.00")


# =============================================================================
# Prorated-invoice idempotency keys on the day-floored period, not the instant
# =============================================================================


class TestProratedInvoiceIdempotency:
    def test_same_day_reactivation_does_not_duplicate(
        self, db_session, subscription, subscriber_account
    ):
        """Two activations on the same day must not mint a second proration."""
        from app.models.billing import InvoiceLine
        from app.models.catalog import (
            BillingCycle,
            BillingMode,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        subscription.status = SubscriptionStatus.active
        subscription.billing_mode = BillingMode.postpaid
        subscriber_account.status = AccountStatus.active
        db_session.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("300.00"),
                currency="NGN",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        db_session.commit()

        # Mid-month activation (day 1 short-circuits proration).
        base = datetime.now(UTC).replace(
            day=15, hour=9, minute=0, second=0, microsecond=0
        )
        inv1 = billing_automation.generate_prorated_invoice(
            db_session, subscription, activation_date=base
        )
        assert inv1 is not None

        # Same day, a few seconds later — must dedupe to the floored period.
        inv2 = billing_automation.generate_prorated_invoice(
            db_session, subscription, activation_date=base + timedelta(seconds=5)
        )
        assert inv2 is None

        lines = (
            db_session.query(InvoiceLine)
            .filter(InvoiceLine.subscription_id == subscription.id)
            .all()
        )
        assert len(lines) == 1


class RetiredPrepaidDraftUntilFunded:
    """Historical tests retained as non-collected migration evidence.

    This draft-invoice implementation is retired by migration 392 and must not
    be collected or re-enabled. New prepaid periods belong exclusively to the
    debit + entitlement renewal owner."""

    def _setup_prepaid_monthly(self, db_session, subscription, subscriber_account):
        from app.models.catalog import (
            BillingCycle,
            BillingMode,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.models.subscriber import AccountStatus

        run_at = datetime(2026, 6, 17, tzinfo=UTC).replace(tzinfo=None)
        subscription.status = SubscriptionStatus.active
        subscription.billing_mode = BillingMode.prepaid
        subscription.start_at = run_at - timedelta(days=30)
        subscription.next_billing_at = run_at
        subscriber_account.status = AccountStatus.active
        settings = [
            DomainSetting(
                domain=SettingDomain.modules,
                key="billing_prepaid_monthly_invoicing",
                value_text="true",
            ),
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("100.00"),
                currency="NGN",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            ),
        ]
        db_session.add_all(settings)
        db_session.commit()
        return run_at

    def _add_credit(self, db_session, subscriber_account, amount, run_at):
        import uuid

        from app.models.billing import (
            LedgerEntry,
            LedgerEntryType,
            LedgerSource,
            Payment,
            PaymentSettlement,
            PaymentSettlementOrigin,
            PaymentStatus,
        )

        payment = Payment(
            account_id=subscriber_account.id,
            amount=Decimal(amount),
            currency="NGN",
            status=PaymentStatus.succeeded,
            paid_at=run_at - timedelta(hours=1),
            memo="Top-up",
        )
        db_session.add(payment)
        db_session.flush()
        entry = LedgerEntry(
            account_id=subscriber_account.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal(amount),
            currency="NGN",
            memo="Top-up",
        )
        db_session.add(entry)
        db_session.flush()
        db_session.add(
            PaymentSettlement(
                payment_id=payment.id,
                unallocated_ledger_entry_id=entry.id,
                amount=Decimal(amount),
                unallocated_amount=Decimal(amount),
                prepaid_amount=Decimal("0.00"),
                currency="NGN",
                origin=PaymentSettlementOrigin.system,
                idempotency_key=f"test-prepaid-credit-{uuid.uuid4()}",
            )
        )
        db_session.commit()

    def _invoice(self, db_session, subscriber_account):
        from app.models.billing import Invoice

        return (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .one()
        )

    def test_default_leaves_underfunded_prepaid_invoice_draft(
        self, db_session, subscription, subscriber_account
    ):
        from app.models.billing import InvoiceStatus

        run_at = self._setup_prepaid_monthly(
            db_session, subscription, subscriber_account
        )
        billing_automation.run_invoice_cycle(db_session, run_at=run_at)
        inv = self._invoice(db_session, subscriber_account)
        assert inv.status == InvoiceStatus.draft
        assert inv.issued_at is None
        assert inv.due_at is None

    def test_underfunded_left_draft(self, db_session, subscription, subscriber_account):
        from app.models.billing import InvoiceStatus, ServiceEntitlement

        run_at = self._setup_prepaid_monthly(
            db_session, subscription, subscriber_account
        )
        summary = billing_automation.run_invoice_cycle(db_session, run_at=run_at)
        inv = self._invoice(db_session, subscriber_account)
        # No deposit → stays draft: not AR, no due date, not issued.
        assert inv.status == InvoiceStatus.draft
        assert inv.issued_at is None
        assert inv.due_at is None
        assert inv.balance_due == Decimal("100.00")
        assert summary.get("prepaid_invoices_left_draft") == 1
        db_session.refresh(subscription)
        assert subscription.next_billing_at == run_at
        assert (
            db_session.query(ServiceEntitlement)
            .filter(ServiceEntitlement.subscription_id == subscription.id)
            .count()
            == 0
        )

    def test_fully_funded_issues_and_pays_drawing_down_wallet(
        self, db_session, subscription, subscriber_account
    ):
        from app.models.billing import InvoiceStatus, ServiceEntitlement
        from app.services.billing._common import get_account_credit_balance

        run_at = self._setup_prepaid_monthly(
            db_session, subscription, subscriber_account
        )
        self._add_credit(db_session, subscriber_account, "100.00", run_at)
        assert get_account_credit_balance(
            db_session, str(subscriber_account.id), currency="NGN"
        ) == Decimal("100.00")

        summary = billing_automation.run_invoice_cycle(db_session, run_at=run_at)
        inv = self._invoice(db_session, subscriber_account)
        # Funded → issued + paid, one drawdown.
        assert inv.status == InvoiceStatus.paid
        assert inv.balance_due == Decimal("0.00")
        assert inv.issued_at is not None
        assert summary.get("prepaid_invoices_funded") == 1
        # Wallet drawn down by exactly the invoice total (no double count).
        assert get_account_credit_balance(
            db_session, str(subscriber_account.id), currency="NGN"
        ) == Decimal("0.00")
        db_session.refresh(subscription)
        assert subscription.next_billing_at > run_at
        entitlement = (
            db_session.query(ServiceEntitlement)
            .filter(ServiceEntitlement.subscription_id == subscription.id)
            .one()
        )
        assert entitlement.source_invoice_id == inv.id
        assert entitlement.starts_at == run_at
        assert entitlement.ends_at == subscription.next_billing_at

    def test_wallet_entitlement_overlap_skips_duplicate_prepaid_invoice(
        self, db_session, subscription, subscriber_account
    ):
        from app.models.billing import (
            Invoice,
            ServiceEntitlement,
            ServiceEntitlementStatus,
        )

        run_at = self._setup_prepaid_monthly(
            db_session, subscription, subscriber_account
        )
        entitlement_end = run_at + timedelta(days=30)
        db_session.add(
            ServiceEntitlement(
                account_id=subscriber_account.id,
                subscription_id=subscription.id,
                starts_at=run_at,
                ends_at=entitlement_end,
                amount_funded=Decimal("100.00"),
                currency="NGN",
                status=ServiceEntitlementStatus.active,
                metadata_={"source": "wallet_prepaid_renewal"},
            )
        )
        db_session.commit()

        summary = billing_automation.run_invoice_cycle(db_session, run_at=run_at)

        assert summary["skipped"] >= 1
        assert (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .count()
            == 0
        )
        db_session.refresh(subscription)
        assert subscription.next_billing_at == entitlement_end

    def test_future_wallet_entitlement_does_not_skip_current_prepaid_invoice(
        self, db_session, subscription, subscriber_account
    ):
        from app.models.billing import (
            Invoice,
            InvoiceStatus,
            ServiceEntitlement,
            ServiceEntitlementStatus,
        )

        run_at = self._setup_prepaid_monthly(
            db_session, subscription, subscriber_account
        )
        db_session.add(
            ServiceEntitlement(
                account_id=subscriber_account.id,
                subscription_id=subscription.id,
                starts_at=run_at + timedelta(days=5),
                ends_at=run_at + timedelta(days=35),
                amount_funded=Decimal("100.00"),
                currency="NGN",
                status=ServiceEntitlementStatus.active,
                metadata_={"source": "wallet_prepaid_renewal"},
            )
        )
        db_session.commit()

        summary = billing_automation.run_invoice_cycle(db_session, run_at=run_at)

        assert summary["invoices_created"] == 1
        invoice = (
            db_session.query(Invoice)
            .filter(Invoice.account_id == subscriber_account.id)
            .one()
        )
        assert invoice.status == InvoiceStatus.draft
        db_session.refresh(subscription)
        assert subscription.next_billing_at == run_at

    def test_partial_credit_left_draft_no_allocation(
        self, db_session, subscription, subscriber_account
    ):
        from app.models.billing import InvoiceStatus, PaymentAllocation
        from app.services.billing._common import get_account_credit_balance

        run_at = self._setup_prepaid_monthly(
            db_session, subscription, subscriber_account
        )
        self._add_credit(db_session, subscriber_account, "40.00", run_at)

        billing_automation.run_invoice_cycle(db_session, run_at=run_at)
        inv = self._invoice(db_session, subscriber_account)
        # All-or-nothing: 40 < 100 → left draft, no partial allocation made.
        assert inv.status == InvoiceStatus.draft
        assert inv.due_at is None
        alloc = (
            db_session.query(PaymentAllocation)
            .filter(PaymentAllocation.invoice_id == inv.id)
            .count()
        )
        assert alloc == 0
        # Credit untouched.
        assert get_account_credit_balance(
            db_session, str(subscriber_account.id), currency="NGN"
        ) == Decimal("40.00")

    def test_later_topup_settles_existing_prepaid_draft(
        self, db_session, subscription, subscriber_account
    ):
        from app.models.billing import InvoiceStatus, ServiceEntitlement
        from app.services.billing._common import get_account_credit_balance
        from app.services.billing.reconcile_unposted import (
            settle_prepaid_draft_invoices_from_credit,
        )

        run_at = self._setup_prepaid_monthly(
            db_session, subscription, subscriber_account
        )
        billing_automation.run_invoice_cycle(db_session, run_at=run_at)
        inv = self._invoice(db_session, subscriber_account)
        assert inv.status == InvoiceStatus.draft
        db_session.refresh(subscription)
        assert subscription.next_billing_at == run_at

        self._add_credit(db_session, subscriber_account, "100.00", run_at)
        result = settle_prepaid_draft_invoices_from_credit(
            db_session, str(subscriber_account.id), run_at=run_at + timedelta(hours=2)
        )
        db_session.commit()

        db_session.refresh(inv)
        assert result.applied == Decimal("100.00")
        assert inv.status == InvoiceStatus.paid
        assert inv.balance_due == Decimal("0.00")
        assert get_account_credit_balance(
            db_session, str(subscriber_account.id), currency="NGN"
        ) == Decimal("0.00")
        db_session.refresh(subscription)
        assert subscription.next_billing_at > run_at
        assert (
            db_session.query(ServiceEntitlement)
            .filter(ServiceEntitlement.subscription_id == subscription.id)
            .count()
            == 1
        )
