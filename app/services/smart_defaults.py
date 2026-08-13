"""Smart Defaults Service - Provides intelligent default values for forms."""

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.billing_settings import resolve_payment_due_days

logger = logging.getLogger(__name__)


class SmartDefaultsService:
    """Service for providing smart default values based on domain settings.

    Uses Redis-based caching for thread safety in multi-worker environments.
    """

    def __init__(self, db: Session):
        self.db = db

    def _get_setting(self, domain: SettingDomain, key: str, default: Any = None) -> Any:
        """One resolved setting, through the one resolver.

        This used to query `DomainSetting` directly, pick a column, and cache
        the result under `settings:{domain}:{key}` — a fourth copy of the
        resolution rules with none of the coercion, bounds or degrade-to-default
        the spec declares, plus an unscoped cache key.

        `resolve_value` is the seam. Its cache is the kernel's, scoped by
        construction, so nothing is lost by not keeping one here.

        `default` still applies: it covers a key with no registered spec, where
        `resolve_value` returns None by contract.
        """

        from app.services import settings_spec

        value = settings_spec.resolve_value(self.db, domain, key)
        return default if value is None else value

    def get_invoice_defaults(self, subscriber: object | None = None) -> dict[str, Any]:
        """
        Get default values for creating a new invoice.

        Args:
            subscriber: Optional subscriber for per-customer payment terms.

        Returns:
            Dictionary containing default values for invoice fields:
            - currency: Default currency code (e.g., 'NGN')
            - payment_terms_days: Number of days until due
            - issued_at: Today's date
            - due_at: Calculated from issued_at + payment_terms_days
            - status: Default invoice status
        """
        # Get settings from billing domain
        currency = self._get_setting(SettingDomain.billing, "default_currency", "NGN")
        payment_terms_days = resolve_payment_due_days(
            self.db, default=30, subscriber=subscriber
        )

        today = date.today()
        due_date = today + timedelta(days=payment_terms_days)

        return {
            "currency": currency,
            "payment_terms_days": payment_terms_days,
            "issued_at": today.isoformat(),
            "due_at": due_date.isoformat(),
            "status": "draft",
        }

    def get_customer_defaults(self, customer_type: str = "person") -> dict[str, Any]:
        """
        Get default values for creating a new customer.

        Args:
            customer_type: Either 'person' or 'business'

        Returns:
            Dictionary containing default values for customer fields.
        """
        # Get default country from settings
        default_country = self._get_setting(
            SettingDomain.billing, "default_country_code", "NG"
        )
        default_locale = self._get_setting(
            SettingDomain.billing, "default_locale", "en-NG"
        )

        defaults = {
            "status": "active",
            "is_active": True,
            "country_code": default_country,
            "locale": default_locale,
        }

        if customer_type == "person":
            defaults.update(
                {
                    "gender": "unknown",
                    "email_verified": False,
                    "marketing_opt_in": False,
                }
            )
        elif customer_type == "business":
            # Business-specific defaults
            pass

        return defaults

    def get_subscription_defaults(self) -> dict[str, Any]:
        """
        Get default values for creating a new subscription.

        Returns:
            Dictionary containing default values for subscription fields.
        """
        billing_cycle = self._get_setting(
            SettingDomain.catalog, "default_billing_cycle", "monthly"
        )
        currency = self._get_setting(SettingDomain.billing, "default_currency", "NGN")

        today = date.today()

        return {
            "billing_cycle": billing_cycle,
            "currency": currency,
            "status": "pending",
            "start_date": today.isoformat(),
            "auto_renew": True,
        }

    def calculate_due_date(
        self,
        issued_at: date | None = None,
        payment_terms_days: int | None = None,
        subscriber: object | None = None,
    ) -> date:
        """
        Calculate the due date based on issue date and payment terms.

        Args:
            issued_at: The invoice issue date. Defaults to today.
            payment_terms_days: Days until due. If not provided, uses subscriber
                or global default setting.
            subscriber: Optional subscriber for per-customer payment terms.

        Returns:
            The calculated due date.
        """
        if issued_at is None:
            issued_at = date.today()

        if payment_terms_days is None:
            payment_terms_days = resolve_payment_due_days(
                self.db, default=30, subscriber=subscriber
            )

        return issued_at + timedelta(days=payment_terms_days)

    def calculate_due_date_detail(
        self,
        issued_at: date | None = None,
        payment_terms_days: int | None = None,
        subscriber: object | None = None,
    ) -> dict[str, Any]:
        """Calculate due date and return all resolved values.

        Returns dict with issued_at, payment_terms_days, and due_at as ISO strings.
        """
        if issued_at is None:
            issued_at = date.today()

        if payment_terms_days is None:
            payment_terms_days = resolve_payment_due_days(
                self.db, default=30, subscriber=subscriber
            )

        due_at = issued_at + timedelta(days=payment_terms_days)
        return {
            "issued_at": issued_at.isoformat(),
            "payment_terms_days": payment_terms_days,
            "due_at": due_at.isoformat(),
        }

    def get_currency_settings(self) -> dict[str, Any]:
        """
        Get currency-related settings.

        Returns:
            Dictionary containing currency settings:
            - default_currency: Default currency code
            - supported_currencies: List of supported currency codes
            - decimal_places: Number of decimal places for amounts
        """
        default_currency = self._get_setting(
            SettingDomain.billing, "default_currency", "NGN"
        )
        supported = self._get_setting(
            SettingDomain.billing, "supported_currencies", ["NGN", "USD", "EUR", "GBP"]
        )

        if isinstance(supported, str):
            supported = [c.strip() for c in supported.split(",")]

        return {
            "default_currency": default_currency,
            "supported_currencies": supported,
            "decimal_places": 2,
        }


def get_smart_defaults_service(db: Session) -> SmartDefaultsService:
    """Factory function to create a SmartDefaultsService instance."""
    return SmartDefaultsService(db)
