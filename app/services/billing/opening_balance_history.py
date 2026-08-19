"""One-time opening-balance targets for customer-subledger completion.

This migration resolver consumes a frozen, isolated audit restore of the
position carried in at the handoff. It contacts no external system and it never
writes money. For every requested account the source position is the
mathematical net of the complete active transaction set; an empty set is zero.

The final target adds canonical Sub-native financial facts no later than the
reviewed opening instant. A migrated account starts at the handoff; a reviewed
Sub-native account that predates the handoff starts at account inception. Any
missing carried-in account or malformed/unreconciled source row fails the
complete query. There is no guessed identity, per-account unknown balance, or
quarantine outcome.
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

from app.models.carried_source_identity import (
    CarriedSourceIdentityAdjudication,
    CarriedSourceIdentityDisposition,
)
from app.models.subscriber import Subscriber
from app.services.carried_source_identity_adjudication import (
    preview_carried_source_identity_adjudication,
)
from app.services.common import coerce_uuid, round_money
from app.services.domain_errors import DomainError

OWNER = "billing.opening_balance_history"
# Physical table name, deliberately unchanged here: renaming it is a schema
# change and belongs with the rest of the column/table rename, not with this
# vocabulary pass.
SOURCE_TABLE = "audit_splynx_final_balances"


class OpeningBalanceHistoryError(DomainError):
    """Complete-cohort source-integrity failure."""


class OpeningBalanceHistoryOrigin(StrEnum):
    """Why an account has its opening history position."""

    migrated_history = "migrated_history"
    native_before_handoff = "native_before_handoff"
    native_after_handoff = "native_after_handoff"


class OpeningBalanceSourceIdentityDisposition(StrEnum):
    """Whether one customer has lawful carried-source identity provenance."""

    migrated_identity_present = "migrated_identity_present"
    native_before_handoff = "native_before_handoff"
    native_after_handoff = "native_after_handoff"
    unresolved_carried_identity = "unresolved_carried_identity"


def _error(suffix: str, message: str, **details: object) -> OpeningBalanceHistoryError:
    return OpeningBalanceHistoryError(
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
class OpeningBalanceHistoryQuery:
    """Exact full-cohort source snapshot to resolve."""

    account_ids: tuple[UUID, ...]
    currency: str
    native_after: datetime
    position_at: datetime


@dataclass(frozen=True, slots=True)
class OpeningBalanceSourceIdentityQuery:
    """Exact customer cohort whose carried-source identity must be classified."""

    account_ids: tuple[UUID, ...]
    native_after: datetime
    position_at: datetime


@dataclass(frozen=True, slots=True)
class OpeningBalanceSourceIdentityRow:
    """One explicit, evidence-derived source-identity disposition."""

    account_id: UUID
    disposition: OpeningBalanceSourceIdentityDisposition
    splynx_customer_id: int | None
    customer_created_at: datetime
    adjudication_id: UUID | None
    adjudication_fingerprint: str | None
    evidence_fingerprint: str


@dataclass(frozen=True, slots=True)
class OpeningBalanceSourceIdentitySnapshot:
    """Complete source-identity classification for the requested cohort."""

    native_after: datetime
    position_at: datetime
    rows: tuple[OpeningBalanceSourceIdentityRow, ...]
    source_fingerprint: str

    @property
    def unresolved_account_ids(self) -> tuple[UUID, ...]:
        return tuple(
            row.account_id
            for row in self.rows
            if row.disposition
            is OpeningBalanceSourceIdentityDisposition.unresolved_carried_identity
        )


@dataclass(frozen=True, slots=True)
class OpeningBalanceHistoryRow:
    """One history-derived target with typed provenance."""

    account_id: UUID
    currency: str
    origin: OpeningBalanceHistoryOrigin
    splynx_customer_id: int | None
    adjudication_id: UUID | None
    adjudication_fingerprint: str | None
    history_transaction_count: int
    history_position: Decimal
    native_position: Decimal
    target_position: Decimal
    evidence_fingerprint: str


@dataclass(frozen=True, slots=True)
class OpeningBalanceHistorySnapshot:
    """Complete result; partial snapshots are never returned."""

    native_after: datetime
    position_at: datetime
    currency: str
    rows: tuple[OpeningBalanceHistoryRow, ...]
    source_fingerprint: str


def classify_opening_balance_source_identities(
    db: Session,
    query: OpeningBalanceSourceIdentityQuery,
) -> OpeningBalanceSourceIdentitySnapshot:
    """Classify every requested customer without inventing a source identity."""

    ids = tuple(sorted({coerce_uuid(value) for value in query.account_ids}, key=str))
    if not ids:
        raise _error("invalid_query", "Source-identity cohort cannot be empty.")
    native_after = _utc(query.native_after)
    position_at = _utc(query.position_at)
    if position_at <= native_after:
        raise _error(
            "invalid_query",
            "Source-identity position must follow the handoff.",
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
    source_ids = [
        int(account.splynx_customer_id)
        for account in accounts.values()
        if account.splynx_customer_id is not None
    ]
    if len(set(source_ids)) != len(source_ids):
        raise _error(
            "source_identity_duplicate",
            "A carried-in customer identity maps to more than one Sub customer.",
        )
    decisions = {
        decision.account_id: decision
        for decision in db.scalars(
            select(CarriedSourceIdentityAdjudication).where(
                CarriedSourceIdentityAdjudication.account_id.in_(ids)
            )
        ).all()
    }

    canonical_rows: list[dict[str, object]] = []
    rows: list[OpeningBalanceSourceIdentityRow] = []
    for account_id in ids:
        account = accounts[account_id]
        created_at = _stored_utc(account.created_at)
        source_id = (
            int(account.splynx_customer_id)
            if account.splynx_customer_id is not None
            else None
        )
        decision = decisions.get(account_id)
        adjudication_id: UUID | None = None
        adjudication_fingerprint: str | None = None
        if source_id is not None:
            if decision is not None:
                raise _error(
                    "source_adjudication_stale",
                    "A reviewed native decision conflicts with carried source identity.",
                    account_id=str(account_id),
                )
            disposition = (
                OpeningBalanceSourceIdentityDisposition.migrated_identity_present
            )
        elif created_at > native_after:
            if decision is not None:
                raise _error(
                    "source_adjudication_stale",
                    "A reviewed pre-handoff decision conflicts with account creation.",
                    account_id=str(account_id),
                )
            disposition = OpeningBalanceSourceIdentityDisposition.native_after_handoff
        elif decision is not None:
            current = preview_carried_source_identity_adjudication(db, account_id)
            if (
                decision.disposition
                is not CarriedSourceIdentityDisposition.native_before_handoff
                or decision.source_system != current.source_system
                or _stored_utc(decision.financial_handoff_at) != native_after
                or _stored_utc(decision.account_created_at) != created_at
                or not current.eligible
                or current.fingerprint != decision.preview_fingerprint
            ):
                raise _error(
                    "source_adjudication_stale",
                    "The reviewed native provenance no longer matches current evidence.",
                    account_id=str(account_id),
                )
            disposition = OpeningBalanceSourceIdentityDisposition.native_before_handoff
            adjudication_id = decision.id
            adjudication_fingerprint = decision.preview_fingerprint
        else:
            disposition = (
                OpeningBalanceSourceIdentityDisposition.unresolved_carried_identity
            )
        canonical: dict[str, object] = {
            "account_id": str(account_id),
            "customer_created_at": created_at.isoformat(),
            "disposition": disposition.value,
            "native_after": native_after.isoformat(),
            "position_at": position_at.isoformat(),
            "splynx_customer_id": source_id,
            "adjudication_id": (
                str(adjudication_id) if adjudication_id is not None else None
            ),
            "adjudication_fingerprint": adjudication_fingerprint,
        }
        canonical_rows.append(canonical)
        rows.append(
            OpeningBalanceSourceIdentityRow(
                account_id=account_id,
                disposition=disposition,
                splynx_customer_id=source_id,
                customer_created_at=created_at,
                adjudication_id=adjudication_id,
                adjudication_fingerprint=adjudication_fingerprint,
                evidence_fingerprint=_digest(canonical),
            )
        )
    return OpeningBalanceSourceIdentitySnapshot(
        native_after=native_after,
        position_at=position_at,
        rows=tuple(rows),
        source_fingerprint=_digest(canonical_rows),
    )


def resolve_opening_balance_history_targets(
    db: Session,
    query: OpeningBalanceHistoryQuery,
) -> OpeningBalanceHistorySnapshot:
    """Resolve every target or fail the entire source snapshot."""

    ids = tuple(sorted({coerce_uuid(value) for value in query.account_ids}, key=str))
    if not ids:
        raise _error("invalid_query", "History opening cohort cannot be empty.")
    currency = query.currency.strip().upper()
    if currency != "NGN":
        raise _error(
            "unsupported_currency",
            "The retained opening-balance transaction history is NGN-only.",
            currency=currency,
        )
    native_after = _utc(query.native_after)
    position_at = _utc(query.position_at)
    if position_at <= native_after:
        raise _error(
            "invalid_query",
            "History opening position must follow the handoff.",
        )
    bind = db.get_bind()
    if bind is None or not inspect(bind).has_table(SOURCE_TABLE):
        raise _error(
            "source_snapshot_missing",
            "The frozen opening-balance evidence table is unavailable.",
        )

    identity = classify_opening_balance_source_identities(
        db,
        OpeningBalanceSourceIdentityQuery(
            account_ids=ids,
            native_after=native_after,
            position_at=position_at,
        ),
    )
    if identity.unresolved_account_ids:
        raise _error(
            "source_cohort_incomplete",
            "Every customer carried in at the handoff must retain its source identity.",
            account_ids=[str(value) for value in identity.unresolved_account_ids],
            reason="missing_carried_source_identity",
        )

    accounts = {
        account.id: account
        for account in db.scalars(
            select(Subscriber).where(Subscriber.id.in_(ids))
        ).all()
    }
    identity_by_account = {row.account_id: row for row in identity.rows}

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
            "The frozen opening-balance history does not cover the complete customer cohort.",
            account_ids=[str(value) for value in missing_history],
        )

    from app.services.customer_financial_ledger import (
        customer_financial_balances_by_currency,
        native_customer_financial_balances_by_currency,
    )

    post_handoff_native = native_customer_financial_balances_by_currency(
        db,
        ids,
        after=native_after,
        before=position_at,
    )
    native_before_ids = tuple(
        row.account_id
        for row in identity.rows
        if row.disposition
        is OpeningBalanceSourceIdentityDisposition.native_before_handoff
    )
    complete_native = customer_financial_balances_by_currency(
        db,
        native_before_ids,
        end=position_at,
    )
    canonical_rows: list[dict[str, object]] = []
    resolved: list[OpeningBalanceHistoryRow] = []
    for account_id in ids:
        account = accounts[account_id]
        identity_row = identity_by_account[account_id]
        evidence = by_account.get(account_id)
        if evidence is None:
            if (
                identity_row.disposition
                is OpeningBalanceSourceIdentityDisposition.native_before_handoff
            ):
                origin = OpeningBalanceHistoryOrigin.native_before_handoff
            else:
                origin = OpeningBalanceHistoryOrigin.native_after_handoff
            source_id = None
            transaction_count = 0
            history_position = Decimal("0.00")
            source_deposit: Decimal | None = None
            reconciled: bool | None = None
        else:
            if (
                identity_row.disposition
                is OpeningBalanceSourceIdentityDisposition.native_before_handoff
            ):
                raise _error(
                    "source_identity_mismatch",
                    "Reviewed Sub-native history conflicts with frozen Splynx evidence.",
                    account_id=str(account_id),
                )
            origin = OpeningBalanceHistoryOrigin.migrated_history
            source_id = int(evidence.splynx_customer_id)
            if source_id != account.splynx_customer_id:
                raise _error(
                    "source_identity_mismatch",
                    "Frozen history belongs to a different carried-in customer.",
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
                    "An empty carried-in transaction set must have no net and a zero position.",
                    account_id=str(account_id),
                )
            if transaction_count > 0 and (
                raw_net is None or not reconciled or history_position != source_deposit
            ):
                raise _error(
                    "source_history_unreconciled",
                    "Credits minus debits do not equal the frozen source position.",
                    account_id=str(account_id),
                )
        native_balances = (
            complete_native
            if origin is OpeningBalanceHistoryOrigin.native_before_handoff
            else post_handoff_native
        )
        native_position = round_money(
            native_balances.get(account_id, {}).get(currency, Decimal("0.00"))
        )
        target = round_money(history_position + native_position)
        canonical: dict[str, object] = {
            "account_id": str(account_id),
            "currency": currency,
            "origin": origin.value,
            "splynx_customer_id": source_id,
            "source_identity_disposition": identity_row.disposition.value,
            "adjudication_id": (
                str(identity_row.adjudication_id)
                if identity_row.adjudication_id is not None
                else None
            ),
            "adjudication_fingerprint": identity_row.adjudication_fingerprint,
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
            OpeningBalanceHistoryRow(
                account_id=account_id,
                currency=currency,
                origin=origin,
                splynx_customer_id=source_id,
                adjudication_id=identity_row.adjudication_id,
                adjudication_fingerprint=identity_row.adjudication_fingerprint,
                history_transaction_count=transaction_count,
                history_position=history_position,
                native_position=native_position,
                target_position=target,
                evidence_fingerprint=_digest(canonical),
            )
        )
    return OpeningBalanceHistorySnapshot(
        native_after=native_after,
        position_at=position_at,
        currency=currency,
        rows=tuple(resolved),
        source_fingerprint=_digest(canonical_rows),
    )


__all__ = [
    "OpeningBalanceHistoryError",
    "OpeningBalanceHistoryOrigin",
    "OpeningBalanceHistoryQuery",
    "OpeningBalanceHistoryRow",
    "OpeningBalanceHistorySnapshot",
    "OpeningBalanceSourceIdentityDisposition",
    "OpeningBalanceSourceIdentityQuery",
    "OpeningBalanceSourceIdentityRow",
    "OpeningBalanceSourceIdentitySnapshot",
    "classify_opening_balance_source_identities",
    "resolve_opening_balance_history_targets",
]
