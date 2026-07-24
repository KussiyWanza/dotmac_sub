"""Service helpers for admin billing payment-channel routes."""

from __future__ import annotations

import logging
from uuid import UUID

from app.models.billing import PaymentChannelType
from app.services import billing as billing_service
from app.services.billing import configuration as billing_config_service

logger = logging.getLogger(__name__)


def list_payment_channels_data(db, *, show_inactive: bool = False) -> dict[str, object]:
    channels = billing_service.payment_channels.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    if show_inactive:
        channels.extend(
            billing_service.payment_channels.list(
                db=db,
                is_active=False,
                order_by="created_at",
                order_dir="desc",
                limit=500,
                offset=0,
            )
        )
    providers = billing_service.payment_providers.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    return {
        "channels": channels,
        "providers": providers,
        "channel_types": [item.value for item in PaymentChannelType],
        "show_inactive": show_inactive,
    }


def load_payment_channel_edit_data(db, channel_id: str) -> dict[str, object] | None:
    channel = billing_service.payment_channels.get(db, channel_id)
    if not channel:
        return None
    providers = billing_service.payment_providers.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    return {
        "channel": channel,
        "providers": providers,
        "channel_types": [item.value for item in PaymentChannelType],
    }


def list_payment_channel_accounts_data(
    db, *, show_inactive: bool = False
) -> dict[str, object]:
    mappings = billing_service.payment_channel_accounts.list(
        db=db,
        channel_id=None,
        collection_account_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    if show_inactive:
        mappings.extend(
            billing_service.payment_channel_accounts.list(
                db=db,
                channel_id=None,
                collection_account_id=None,
                is_active=False,
                order_by="created_at",
                order_dir="desc",
                limit=500,
                offset=0,
            )
        )
    channels = billing_service.payment_channels.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    collection_accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    return {
        "mappings": mappings,
        "channels": channels,
        "collection_accounts": collection_accounts,
        "show_inactive": show_inactive,
    }


def load_payment_channel_account_edit_data(
    db, mapping_id: str
) -> dict[str, object] | None:
    mapping = billing_service.payment_channel_accounts.get(db, mapping_id)
    if not mapping:
        return None
    channels = billing_service.payment_channels.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    collection_accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    return {
        "mapping": mapping,
        "channels": channels,
        "collection_accounts": collection_accounts,
    }


def create_payment_channel_from_form(
    db,
    *,
    name: str,
    channel_type: str,
    provider_id: str | None,
    fee_rules: str | None,
    accounting_code: str | None,
    notes: str | None,
):
    return billing_config_service.create_payment_channel(
        db=db,
        name=name,
        channel_type=channel_type,
        provider_id=provider_id,
        fee_rules=fee_rules,
        accounting_code=accounting_code,
        notes=notes,
    )


def update_payment_channel_from_form(
    db,
    *,
    channel_id: UUID,
    name: str,
    channel_type: str,
    provider_id: str | None,
    fee_rules: str | None,
    accounting_code: str | None,
    notes: str | None,
):
    return billing_config_service.update_payment_channel(
        db=db,
        channel_id=channel_id,
        name=name,
        channel_type=channel_type,
        provider_id=provider_id,
        fee_rules=fee_rules,
        accounting_code=accounting_code,
        notes=notes,
    )


def create_payment_channel_account_from_form(
    db,
    *,
    channel_id: str,
    collection_account_id: str,
    currency: str | None,
    priority: int,
):
    return billing_config_service.create_payment_channel_account(
        db=db,
        channel_id=channel_id,
        collection_account_id=collection_account_id,
        currency=currency,
        priority=priority,
    )


def update_payment_channel_account_from_form(
    db,
    *,
    mapping_id: UUID,
    channel_id: str,
    collection_account_id: str,
    currency: str | None,
    priority: int,
):
    return billing_config_service.update_payment_channel_account(
        db=db,
        mapping_id=mapping_id,
        channel_id=channel_id,
        collection_account_id=collection_account_id,
        currency=currency,
        priority=priority,
    )
