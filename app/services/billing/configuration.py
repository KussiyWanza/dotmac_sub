"""Billing configuration helpers for admin UI."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.billing import CollectionAccountType, PaymentChannelType
from app.schemas.billing import (
    CollectionAccountCreate,
    CollectionAccountUpdate,
    PaymentChannelAccountCreate,
    PaymentChannelAccountUpdate,
    PaymentChannelCreate,
    PaymentChannelUpdate,
)
from app.services import billing as billing_service

logger = logging.getLogger(__name__)


def _parse_json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        loaded = json.loads(value)
        if not isinstance(loaded, dict):
            raise HTTPException(status_code=400, detail="JSON object required")
        return loaded
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc


def create_collection_account(
    db: Session,
    name: str,
    account_type: str,
    currency: str,
    bank_name: str | None,
    account_name: str | None,
    account_number: str | None,
    sort_code: str | None,
    accounting_code: str | None,
    presentment_priority: int,
    notes: str | None,
):
    payload = CollectionAccountCreate(
        name=name.strip(),
        account_type=CollectionAccountType(account_type),
        currency=currency.strip().upper(),
        bank_name=bank_name.strip() if bank_name else None,
        account_name=account_name.strip() if account_name else None,
        account_number=account_number.strip() if account_number else None,
        sort_code=sort_code.strip() if sort_code else None,
        accounting_code=accounting_code.strip() if accounting_code else None,
        presentment_priority=presentment_priority,
        notes=notes.strip() if notes else None,
    )
    return billing_service.collection_accounts.create(db, payload)


def update_collection_account(
    db: Session,
    account_id: UUID,
    name: str,
    account_type: str,
    currency: str,
    bank_name: str | None,
    account_name: str | None,
    account_number: str | None,
    sort_code: str | None,
    accounting_code: str | None,
    presentment_priority: int,
    notes: str | None,
):
    payload = CollectionAccountUpdate(
        name=name.strip(),
        account_type=CollectionAccountType(account_type),
        currency=currency.strip().upper(),
        bank_name=bank_name.strip() if bank_name else None,
        account_name=account_name.strip() if account_name else None,
        account_number=account_number.strip() if account_number else None,
        sort_code=sort_code.strip() if sort_code else None,
        accounting_code=accounting_code.strip() if accounting_code else None,
        presentment_priority=presentment_priority,
        notes=notes.strip() if notes else None,
    )
    return billing_service.collection_accounts.update(db, str(account_id), payload)


def create_payment_channel(
    db: Session,
    name: str,
    channel_type: str,
    provider_id: str | None,
    fee_rules: str | None,
    accounting_code: str | None,
    notes: str | None,
):
    payload = PaymentChannelCreate(
        name=name.strip(),
        channel_type=PaymentChannelType(channel_type),
        provider_id=UUID(provider_id) if provider_id else None,
        fee_rules=_parse_json(fee_rules),
        accounting_code=accounting_code.strip() if accounting_code else None,
        notes=notes.strip() if notes else None,
    )
    return billing_service.payment_channels.create(db, payload)


def update_payment_channel(
    db: Session,
    channel_id: UUID,
    name: str,
    channel_type: str,
    provider_id: str | None,
    fee_rules: str | None,
    accounting_code: str | None,
    notes: str | None,
):
    payload = PaymentChannelUpdate(
        name=name.strip(),
        channel_type=PaymentChannelType(channel_type),
        provider_id=UUID(provider_id) if provider_id else None,
        fee_rules=_parse_json(fee_rules),
        accounting_code=accounting_code.strip() if accounting_code else None,
        notes=notes.strip() if notes else None,
    )
    return billing_service.payment_channels.update(db, str(channel_id), payload)


def create_payment_channel_account(
    db: Session,
    channel_id: str,
    collection_account_id: str,
    currency: str | None,
    priority: int,
):
    payload = PaymentChannelAccountCreate(
        channel_id=UUID(channel_id),
        collection_account_id=UUID(collection_account_id),
        currency=currency.strip().upper() if currency else None,
        priority=priority,
    )
    return billing_service.payment_channel_accounts.create(db, payload)


def update_payment_channel_account(
    db: Session,
    mapping_id: UUID,
    channel_id: str,
    collection_account_id: str,
    currency: str | None,
    priority: int,
):
    payload = PaymentChannelAccountUpdate(
        channel_id=UUID(channel_id),
        collection_account_id=UUID(collection_account_id),
        currency=currency.strip().upper() if currency else None,
        priority=priority,
    )
    return billing_service.payment_channel_accounts.update(db, str(mapping_id), payload)
