"""One-time Splynx-history opening targets for customer-subledger completion.

This migration resolver consumes the frozen, isolated audit restore produced by
``scripts.one_off.reconstruct_splynx_mirror``.  It never contacts Splynx and it
never writes money.  For every requested account the source position is the
mathematical net of the complete active transaction set; an empty set is zero.

The final target adds canonical Sub-native financial facts strictly after the
legacy handoff and no later than the reviewed opening instant. Any missing
migrated source account or malformed/unreconciled source row fails the complete
query. A native account created after handoff has an explicit zero history
component. There is no per-account unknown or quarantine outcome.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import Boolean, Integer, Numeric, Uuid, column, inspect, select, table
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid, round_money
from app.services.domain_errors import DomainError

OWNER = "billing.splynx_history_opening"
SOURCE_TABLE = "audit_splynx_final_balances"


class SplynxHistoryOpeningError(DomainError):
    """Complete-cohort source-integrity failure."""


class SplynxHistoryOrigin(StrEnum):
    """Why an account has its opening history position."""

    migrated_history = "migrated_history"
    native_after_handoff = "native_after_handoff"


def _error(suffix: str, message: str, **details: object) -> SplynxHistoryOpeningError:
    return SplynxHistoryOpeningError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise _error(
            "invalid_query",
            "History opening timestamps must be timezone-aware.",
        )
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class SplynxHistoryOpeningQuery:
    """Exact full-cohort source snapshot to resolve."""

    account_ids: tuple[UUID, ...]
    currency: str
    native_after: datetime
    position_at: datetime


@dataclass(frozen=True, slots=True)
class SplynxHistoryOpeningRow:
    """One history-derived target with typed provenance."""

    account_id: UUID
    currency: str
    origin: SplynxHistoryOrigin
    splynx_customer_id: int | None
    history_transaction_count: int
    history_position: Decimal
    native_position: Decimal
    target_position: Decimal
    evidence_fingerprint: str


@dataclass(frozen=True, slots=True)
class SplynxHistoryOpeningSnapshot:
    """Complete result; partial snapshots are never returned."""

    native_after: datetime
    position_at: datetime
    currency: str
    rows: tuple[SplynxHistoryOpeningRow, ...]
    source_fingerprint: str


def resolve_splynx_history_opening_targets(
    db: Session,
    query: SplynxHistoryOpeningQuery,
) -> SplynxHistoryOpeningSnapshot:
    """Resolve every target or fail the entire source snapshot."""

    ids = tuple(sorted({coerce_uuid(value) for value in query.account_ids}, key=str))
    if not ids:
        raise _error("invalid_query", "History opening cohort cannot be empty.")
    currency = query.currency.strip().upper()
    if currency != "NGN":
        raise _error(
            "unsupported_currency",
            "The retained Splynx transaction history is NGN-only.",
            currency=currency,
        )
    native_after = _utc(query.native_after)
    position_at = _utc(query.position_at)
    if position_at <= native_after:
        raise _error(
            "invalid_query",
            "History opening position must follow the legacy handoff.",
        )
    bind = db.get_bind()
    if bind is None or not inspect(bind).has_table(SOURCE_TABLE):
        raise _error(
            "source_snapshot_missing",
            "The frozen Splynx final-balance evidence table is unavailable.",
        )

    accounts = {
        account.id: account
        for account in db.scalars(
            select(Subscriber).where(Subscriber.id.in_(ids))
        ).all()
    }
    missing_accounts = sorted(set(ids) - set(accounts), key=str)
    if missing_accounts:
        raise _error(
            "source_cohort_incomplete",
            "The requested Sub customer cohort is incomplete.",
            account_ids=[str(value) for value in missing_accounts],
        )
    future_accounts = sorted(
        (
            account_id
            for account_id, account in accounts.items()
            if _stored_utc(account.created_at) > position_at
        ),
        key=str,
    )
    if future_accounts:
        raise _error(
            "source_cohort_incomplete",
            "A customer created after the reviewed instant is outside the cohort.",
            account_ids=[str(value) for value in future_accounts],
        )
    missing_source_ids = sorted(
        (
            account_id
            for account_id, account in accounts.items()
            if account.splynx_customer_id is None
            and _stored_utc(account.created_at) <= native_after
        ),
        key=str,
    )
    if missing_source_ids:
        raise _error(
            "source_cohort_incomplete",
            "Every customer migrated at the handoff must retain its Splynx identity.",
            account_ids=[str(value) for value in missing_source_ids],
        )
    source_ids = [
        int(account.splynx_customer_id)
        for account in accounts.values()
        if account.splynx_customer_id is not None
    ]
    if len(set(source_ids)) != len(source_ids):
        raise _error(
            "source_identity_duplicate",
            "A Splynx customer identity maps to more than one Sub customer.",
        )

    source = table(
        SOURCE_TABLE,
        column("splynx_customer_id", Integer),
        column("subscriber_id", Uuid(as_uuid=True)),
        column("final_deposit", Numeric(19, 4)),
        column("active_transaction_net", Numeric(19, 4)),
        column("active_transaction_rows", Integer),
        column("transaction_reconciled", Boolean),
    )
    evidence_rows = list(
        db.execute(
            select(
                source.c.splynx_customer_id,
                source.c.subscriber_id,
                source.c.final_deposit,
                source.c.active_transaction_net,
                source.c.active_transaction_rows,
                source.c.transaction_reconciled,
            ).where(source.c.subscriber_id.in_(ids))
        ).all()
    )
    by_account = {row.subscriber_id: row for row in evidence_rows}
    if len(by_account) != len(evidence_rows):
        raise _error(
            "source_identity_duplicate",
            "The frozen source contains duplicate customer evidence.",
        )
    missing_history = sorted(
        (
            account_id
            for account_id in ids
            if account_id not in by_account
            and accounts[account_id].splynx_customer_id is not None
        ),
        key=str,
    )
    if missing_history:
        raise _error(
            "source_cohort_incomplete",
            "The frozen Splynx history does not cover the complete customer cohort.",
            account_ids=[str(value) for value in missing_history],
        )

    from app.services.customer_financial_ledger import (
        native_customer_financial_balances_by_currency,
    )

    native = native_customer_financial_balances_by_currency(
        db,
        ids,
        after=native_after,
        before=position_at,
    )
    canonical_rows: list[dict[str, object]] = []
    resolved: list[SplynxHistoryOpeningRow] = []
    for account_id in ids:
        account = accounts[account_id]
        evidence = by_account.get(account_id)
        if evidence is None:
            origin = SplynxHistoryOrigin.native_after_handoff
            source_id = None
            transaction_count = 0
            history_position = Decimal("0.00")
            source_deposit: Decimal | None = None
            reconciled: bool | None = None
        else:
            origin = SplynxHistoryOrigin.migrated_history
            source_id = int(evidence.splynx_customer_id)
            if source_id != account.splynx_customer_id:
                raise _error(
                    "source_identity_mismatch",
                    "Frozen history belongs to a different Splynx customer.",
                    account_id=str(account_id),
                )
            transaction_count = int(evidence.active_transaction_rows or 0)
            source_deposit = round_money(Decimal(str(evidence.final_deposit or 0)))
            raw_net = evidence.active_transaction_net
            history_position = round_money(Decimal(str(raw_net or 0)))
            empty_history = transaction_count == 0 and raw_net is None
            reconciled = bool(evidence.transaction_reconciled)
            if transaction_count < 0 or (
                transaction_count == 0
                and (not empty_history or source_deposit != Decimal("0.00"))
            ):
                raise _error(
                    "source_history_malformed",
                    "An empty Splynx transaction set must have no net and a zero position.",
                    account_id=str(account_id),
                )
            if transaction_count > 0 and (
                raw_net is None or not reconciled or history_position != source_deposit
            ):
                raise _error(
                    "source_history_unreconciled",
                    "Splynx credits minus debits do not equal the frozen source position.",
                    account_id=str(account_id),
                )
        native_position = round_money(
            native.get(account_id, {}).get(currency, Decimal("0.00"))
        )
        target = round_money(history_position + native_position)
        canonical: dict[str, object] = {
            "account_id": str(account_id),
            "currency": currency,
            "origin": origin.value,
            "splynx_customer_id": source_id,
            "history_transaction_count": transaction_count,
            "history_position": str(history_position),
            "source_final_position": (
                str(source_deposit) if source_deposit is not None else None
            ),
            "source_transaction_reconciled": reconciled,
            "native_after": native_after.isoformat(),
            "native_position": str(native_position),
            "position_at": position_at.isoformat(),
            "target_position": str(target),
        }
        canonical_rows.append(canonical)
        resolved.append(
            SplynxHistoryOpeningRow(
                account_id=account_id,
                currency=currency,
                origin=origin,
                splynx_customer_id=source_id,
                history_transaction_count=transaction_count,
                history_position=history_position,
                native_position=native_position,
                target_position=target,
                evidence_fingerprint=_digest(canonical),
            )
        )
    return SplynxHistoryOpeningSnapshot(
        native_after=native_after,
        position_at=position_at,
        currency=currency,
        rows=tuple(resolved),
        source_fingerprint=_digest(canonical_rows),
    )


__all__ = [
    "SplynxHistoryOpeningError",
    "SplynxHistoryOrigin",
    "SplynxHistoryOpeningQuery",
    "SplynxHistoryOpeningRow",
    "SplynxHistoryOpeningSnapshot",
    "resolve_splynx_history_opening_targets",
]
