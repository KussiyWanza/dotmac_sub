"""Service helpers for billing tax-rate web routes."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.billing import TaxRateCreate
from app.schemas.settings import DomainSettingUpdate
from app.services import billing as billing_service
from app.services import domain_settings as settings_service
from app.services import settings_spec
from app.validators.forms import parse_decimal

logger = logging.getLogger(__name__)
WITHHOLDING_TAX_RATE_KEY = "withholding_tax_rate_percent"


def list_data(db) -> dict[str, object]:
    """List all tax rates (active first, then inactive) for the admin UI."""
    active = billing_service.tax_rates.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    inactive = billing_service.tax_rates.list(
        db=db,
        is_active=False,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    return {
        "rates": active + inactive,
        "withholding_tax_rate_percent": get_withholding_tax_rate_percent(db),
    }


def create_tax_rate_from_form(
    db,
    *,
    name: str,
    rate: str,
    code: str | None,
    description: str | None,
):
    payload = TaxRateCreate(
        name=name.strip(),
        rate=parse_decimal(rate, "rate"),
        code=code.strip() if code else None,
        description=description.strip() if description else None,
    )
    return billing_service.tax_rates.create(db, payload)


def toggle_tax_rate(db, *, rate_id: str):
    return billing_service.tax_rates.toggle_active(db, rate_id)


def get_withholding_tax_rate_percent(db) -> str:
    raw = settings_spec.resolve_value(
        db, SettingDomain.billing, WITHHOLDING_TAX_RATE_KEY
    )
    return str(raw if raw not in (None, "") else "5.00")


def save_withholding_tax_rate_percent(db, *, rate_percent: str):
    normalized = str(rate_percent or "").strip()
    if not normalized:
        raise ValueError("WHT percentage is required")
    try:
        value = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("WHT percentage must be a valid decimal value") from exc
    if value <= Decimal("0") or value >= Decimal("100"):
        raise ValueError("WHT percentage must be greater than 0 and less than 100")
    stored = f"{value.quantize(Decimal('0.01'))}"
    return settings_service.billing_settings.upsert_by_key(
        db,
        WITHHOLDING_TAX_RATE_KEY,
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=stored,
            is_active=True,
        ),
    )
