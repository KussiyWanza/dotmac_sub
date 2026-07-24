"""Service helpers for billing collection-account web routes."""

from __future__ import annotations

import logging
from uuid import UUID

from app.models.billing import CollectionAccountType
from app.services import billing as billing_service
from app.services import display_format
from app.services.billing import configuration as billing_config_service

logger = logging.getLogger(__name__)


def _default_currency(db) -> str:
    return display_format.default_currency(db)


def list_data(db, *, show_inactive: bool) -> dict[str, object]:
    accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    if show_inactive:
        accounts.extend(
            billing_service.collection_accounts.list(
                db=db,
                is_active=False,
                order_by="created_at",
                order_dir="desc",
                limit=500,
                offset=0,
            )
        )
    return {
        "accounts": accounts,
        "account_types": [item.value for item in CollectionAccountType],
        "show_inactive": show_inactive,
        "default_currency": _default_currency(db),
    }


def edit_data(db, *, account_id: str) -> dict[str, object] | None:
    account = billing_service.collection_accounts.get(db, account_id)
    if not account:
        return None
    return {
        "account": account,
        "account_types": [item.value for item in CollectionAccountType],
        "default_currency": _default_currency(db),
    }


def create_collection_account_from_form(
    db,
    *,
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
    return billing_config_service.create_collection_account(
        db=db,
        name=name,
        account_type=account_type,
        currency=currency,
        bank_name=bank_name,
        account_name=account_name,
        account_number=account_number,
        sort_code=sort_code,
        accounting_code=accounting_code,
        presentment_priority=presentment_priority,
        notes=notes,
    )


def update_collection_account_from_form(
    db,
    *,
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
    return billing_config_service.update_collection_account(
        db=db,
        account_id=account_id,
        name=name,
        account_type=account_type,
        currency=currency,
        bank_name=bank_name,
        account_name=account_name,
        account_number=account_number,
        sort_code=sort_code,
        accounting_code=accounting_code,
        presentment_priority=presentment_priority,
        notes=notes,
    )
