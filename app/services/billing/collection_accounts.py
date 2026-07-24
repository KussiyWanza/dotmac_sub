"""Canonical owner for collection-account identity and presentment."""

from __future__ import annotations

import builtins
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing import CollectionAccount, CollectionAccountType
from app.schemas.billing import CollectionAccountCreate, CollectionAccountUpdate
from app.services.common import apply_ordering, apply_pagination, get_by_id
from app.services.response import ListResponseMixin


def _clean_optional(value: object) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _normalise_account_number(value: object) -> str | None:
    cleaned = re.sub(r"\s+", "", str(value or "").strip())
    return cleaned or None


def _normalise_data(
    data: dict[str, Any],
) -> dict[str, Any]:
    for key in ("account_name", "sort_code", "accounting_code", "notes"):
        if key in data:
            data[key] = _clean_optional(data[key])
    if "bank_name" in data:
        bank_name = _clean_optional(data["bank_name"])
        data["bank_name"] = bank_name.upper() if bank_name else None
    if "name" in data and data["name"] is not None:
        data["name"] = str(data["name"]).strip()
        if not data["name"]:
            raise HTTPException(status_code=400, detail="Account name is required")
    if "currency" in data and data["currency"] is not None:
        data["currency"] = str(data["currency"]).strip().upper()
    if "account_number" in data:
        account_number = _normalise_account_number(data["account_number"])
        data["account_number"] = account_number
        data["account_last4"] = account_number[-4:] if account_number else None
    return data


def _assert_unique_destination(
    db: Session,
    data: dict[str, Any],
    *,
    existing: CollectionAccount | None = None,
) -> None:
    bank_name = data.get("bank_name", existing.bank_name if existing else None)
    account_number = data.get(
        "account_number", existing.account_number if existing else None
    )
    currency = data.get("currency", existing.currency if existing else "NGN")
    if not (bank_name and account_number and currency):
        return
    query = db.query(CollectionAccount.id).filter(
        func.lower(CollectionAccount.bank_name) == str(bank_name).casefold(),
        CollectionAccount.account_number == account_number,
        CollectionAccount.currency == str(currency).upper(),
    )
    if existing is not None:
        query = query.filter(CollectionAccount.id != existing.id)
    if query.first() is not None:
        raise HTTPException(
            status_code=409,
            detail="That bank account already exists for this currency",
        )


class CollectionAccounts(ListResponseMixin):
    """Own collection-account writes and customer-presentable projections."""

    @staticmethod
    def create(db: Session, payload: CollectionAccountCreate) -> CollectionAccount:
        data = _normalise_data(payload.model_dump())
        data["is_active"] = False
        _assert_unique_destination(db, data)
        account = CollectionAccount(**data)
        db.add(account)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Collection account name or bank destination already exists",
            ) from exc
        db.refresh(account)
        return account

    @staticmethod
    def get(db: Session, account_id: str) -> CollectionAccount:
        account = get_by_id(db, CollectionAccount, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Collection account not found")
        return account

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ) -> builtins.list[CollectionAccount]:
        query = db.query(CollectionAccount)
        if is_active is None:
            query = query.filter(CollectionAccount.is_active.is_(True))
        else:
            query = query.filter(CollectionAccount.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": CollectionAccount.created_at,
                "name": CollectionAccount.name,
                "presentment_priority": CollectionAccount.presentment_priority,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(
        db: Session, account_id: str, payload: CollectionAccountUpdate
    ) -> CollectionAccount:
        account = CollectionAccounts.get(db, account_id)
        data = _normalise_data(payload.model_dump(exclude_unset=True))
        _assert_unique_destination(db, data, existing=account)
        for key, value in data.items():
            setattr(account, key, value)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Collection account name or bank destination already exists",
            ) from exc
        db.refresh(account)
        return account

    @staticmethod
    def stage_active(account: CollectionAccount, *, active: bool) -> None:
        account.is_active = active

    @staticmethod
    def presentment_accounts(
        db: Session, *, currency: str = "NGN"
    ) -> builtins.list[dict[str, str]]:
        """Return active, complete transfer destinations in explicit order."""
        accounts = (
            db.query(CollectionAccount)
            .filter(CollectionAccount.is_active.is_(True))
            .filter(CollectionAccount.account_type == CollectionAccountType.bank)
            .filter(CollectionAccount.currency == currency.strip().upper())
            .filter(CollectionAccount.bank_name.is_not(None))
            .filter(CollectionAccount.account_name.is_not(None))
            .filter(CollectionAccount.account_number.is_not(None))
            .order_by(
                CollectionAccount.presentment_priority.desc(),
                CollectionAccount.name,
                CollectionAccount.id,
            )
            .all()
        )
        return [
            {
                "id": str(account.id),
                "enabled": "true",
                "bank_name": account.bank_name or "",
                "account_name": account.account_name or "",
                "account_number": account.account_number or "",
                "sort_code": account.sort_code or "",
            }
            for account in accounts
        ]

    @staticmethod
    def primary_presentment_account(
        db: Session, *, currency: str = "NGN"
    ) -> dict[str, str] | None:
        accounts = CollectionAccounts.presentment_accounts(db, currency=currency)
        return accounts[0] if accounts else None
