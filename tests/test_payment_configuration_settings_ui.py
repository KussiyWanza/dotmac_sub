from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.schemas.billing import (
    CollectionAccountCreate,
    CollectionAccountUpdate,
    PaymentChannelAccountCreate,
    PaymentChannelAccountUpdate,
    PaymentChannelCreate,
    PaymentChannelUpdate,
)

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def test_configuration_pages_are_settings_owned_and_have_no_legacy_routes():
    routes = _read("app/web/admin/billing_channels.py") + _read(
        "app/web/admin/billing_collection_accounts.py"
    )
    assert 'prefix="/settings/billing"' in routes
    assert 'prefix="/billing"' not in routes
    assert "/admin/billing/payment-channels" not in routes
    assert "/admin/billing/payment-channel-accounts" not in routes
    assert "/admin/billing/collection-accounts" not in routes


def test_generic_api_cannot_bypass_reviewed_lifecycle_owner():
    api = _read("app/api/billing.py")
    schemas = _read("app/schemas/billing.py")
    services = _read("app/services/billing/payments.py") + _read(
        "app/services/billing/collection_accounts.py"
    )
    for route in (
        '"/collection-accounts/{account_id}"',
        '"/payment-channels/{channel_id}"',
        '"/payment-channel-accounts/{mapping_id}"',
    ):
        assert f"@router.delete(\n    {route}" not in api
    assert 'extra="forbid"' in schemas
    assert "def delete(db: Session, account_id: str)" not in services
    assert "def delete(db: Session, channel_id: str)" not in services
    assert "def delete(db: Session, mapping_id: str)" not in services


@pytest.mark.parametrize(
    ("schema", "payload"),
    (
        (CollectionAccountCreate, {"name": "Destination", "is_active": True}),
        (CollectionAccountCreate, {"name": "Destination", "account_last4": "1234"}),
        (CollectionAccountUpdate, {"is_active": True}),
        (CollectionAccountUpdate, {"account_last4": "1234"}),
        (PaymentChannelCreate, {"name": "Card", "is_active": True}),
        (PaymentChannelCreate, {"name": "Card", "is_default": True}),
        (PaymentChannelUpdate, {"is_active": True}),
        (PaymentChannelUpdate, {"is_default": True}),
        (
            PaymentChannelAccountCreate,
            {
                "channel_id": "00000000-0000-0000-0000-000000000001",
                "collection_account_id": "00000000-0000-0000-0000-000000000002",
                "is_active": True,
            },
        ),
        (PaymentChannelAccountUpdate, {"is_default": True}),
    ),
)
def test_generic_schemas_reject_owner_managed_and_derived_fields(schema, payload):
    with pytest.raises(ValidationError):
        schema.model_validate(payload)


def test_configuration_templates_cannot_mutate_lifecycle_directly():
    sources = "\n".join(
        _read(path)
        for path in (
            "templates/admin/billing/payment_channels.html",
            "templates/admin/billing/payment_channel_form.html",
            "templates/admin/billing/payment_channel_accounts.html",
            "templates/admin/billing/payment_channel_account_form.html",
            "templates/admin/billing/collection_accounts.html",
            "templates/admin/billing/collection_account_form.html",
        )
    )
    assert 'name="is_active"' not in sources
    assert 'name="is_default"' not in sources
    assert "default_collection_account_id" not in sources
    assert "return confirm(" not in sources
    assert "/payment-configuration/" in sources


def test_payment_configuration_copy_preserves_checkout_owner_boundary():
    channel_page = _read("templates/admin/billing/payment_channels.html")
    mapping_page = _read("templates/admin/billing/payment_channel_accounts.html")
    projection = _read("app/services/web_payment_configuration.py")
    assert "Customer checkout gateways are configured separately" in channel_page
    assert "do not route customer checkout" in mapping_page
    assert "connector-backed Payment Routing" not in projection
    owner = _read("app/services/payment_configuration_staff_actions.py")
    assert "connector-backed Payment Routing owns checkout" in owner


def test_payment_configuration_templates_compile():
    environment = Jinja2Templates(directory=ROOT / "templates").env
    for template in (
        "admin/billing/payment_channels.html",
        "admin/billing/payment_channel_form.html",
        "admin/billing/payment_channel_accounts.html",
        "admin/billing/payment_channel_account_form.html",
        "admin/billing/collection_accounts.html",
        "admin/billing/collection_account_form.html",
        "admin/system/payment_configuration_review.html",
    ):
        environment.get_template(template)
