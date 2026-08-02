"""Canonical owner for previewed debits against prepaid account funding.

Public confirmations enter one manifest-verified transaction. Financial
coordinators use the explicit nested staging collaborators so their wider
business transition and this exact debit can commit atomically. The append-only
ledger remains the monetary record writer; this owner records why the debit or
reversal was allowed and links that decision to the exact ledger entry.

Credits are deliberately excluded. Customer account credits belong to
``financial.credit_notes``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.audit import AuditActorType
from app.models.billing import (
    AccountAdjustment,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import (
    AccountAdjustmentConfirm,
    AccountAdjustmentPreviewRequest,
    AccountAdjustmentReversalConfirm,
    AccountAdjustmentReversalPreviewRequest,
)
from app.services import settings_spec
from app.services.audit import AuditEvents
from app.services.billing._common import get_account_credit_balance, lock_account
from app.services.billing.ledger import (
    LedgerAccountAdjustmentError,
    LedgerEntries,
)
from app.services.common import round_money
from app.services.customer_financial_position import get_customer_financial_position
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

ACCOUNT_ADJUSTMENT_SCOPE = "billing:ledger:write"


class AccountAdjustmentOrigin(StrEnum):
    """Closed provenance vocabulary for account-debit decisions."""

    manual = "manual"
    addon_purchase = "addon_purchase"
    prepaid_plan_change = "prepaid_plan_change"
    prepaid_service_renewal = "prepaid_service_renewal"


_CONFIRM_COMMAND = OwnerCommandDefinition(
    owner="financial.account_adjustments",
    concern="locked account-debit confirmation",
    name="confirm_account_adjustment",
)
_REVERSE_COMMAND = OwnerCommandDefinition(
    owner="financial.account_adjustments",
    concern="previewed account-adjustment reversal evidence",
    name="reverse_account_adjustment",
)


class AccountAdjustmentError(DomainError):
    """Stable, transport-neutral account-adjustment failure."""


def _error(code: str, message: str, **details: object) -> AccountAdjustmentError:
    return AccountAdjustmentError(
        code=f"financial.account_adjustments.{code}",
        message=message,
        details=details,
    )


@dataclass(frozen=True)
class PreviewAccountAdjustmentQuery:
    request: AccountAdjustmentPreviewRequest
    origin: AccountAdjustmentOrigin = AccountAdjustmentOrigin.manual
    origin_ref: str | None = None


@dataclass(frozen=True)
class ConfirmAccountAdjustmentCommand:
    context: CommandContext
    confirmation: AccountAdjustmentConfirm
    origin: AccountAdjustmentOrigin = AccountAdjustmentOrigin.manual
    origin_ref: str | None = None
    ledger_effective_date: datetime | None = None


@dataclass(frozen=True)
class StageSystemAccountAdjustmentCommand:
    """Typed nested request from a larger financial coordinator."""

    context: CommandContext
    request: AccountAdjustmentPreviewRequest
    origin: AccountAdjustmentOrigin
    origin_ref: str | None
    idempotency_key: str
    ledger_effective_date: datetime | None = None


@dataclass(frozen=True)
class PreviewAccountAdjustmentReversalQuery:
    adjustment_id: UUID
    request: AccountAdjustmentReversalPreviewRequest


@dataclass(frozen=True)
class ReverseAccountAdjustmentCommand:
    context: CommandContext
    adjustment_id: UUID
    confirmation: AccountAdjustmentReversalConfirm


@dataclass(frozen=True)
class AccountAdjustmentPreview:
    account_id: UUID
    category: LedgerCategory
    amount: Decimal
    currency: str
    memo: str
    reason: str
    origin: str
    origin_ref: str | None
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    postpaid_receivables: Decimal
    collection_blocking_balance: Decimal
    shortfall: Decimal
    allowed: bool
    rejection_reason: str | None
    fingerprint: str
    ledger_entry_type: LedgerEntryType = LedgerEntryType.debit
    ledger_source: LedgerSource = LedgerSource.adjustment
    access_consequence: str = "none_adjustment_only"

    @property
    def ledger_amount(self) -> Decimal:
        return self.amount

    def as_dict(self) -> dict[str, object]:
        return {**self.__dict__, "ledger_amount": self.ledger_amount}


@dataclass(frozen=True)
class AccountAdjustmentResult:
    adjustment: AccountAdjustment
    ledger_entry: LedgerEntry
    preview: AccountAdjustmentPreview
    replayed: bool = False


@dataclass(frozen=True)
class AccountAdjustmentReversalPreview:
    adjustment_id: UUID
    account_id: UUID
    amount: Decimal
    currency: str
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    reverses_ledger_entry_id: UUID
    reason: str
    fingerprint: str
    ledger_entry_type: LedgerEntryType = LedgerEntryType.credit
    ledger_source: LedgerSource = LedgerSource.adjustment
    access_consequence: str = "none_adjustment_reversal_only"

    def as_dict(self) -> dict[str, object]:
        return self.__dict__


@dataclass(frozen=True)
class AccountAdjustmentReversalResult:
    adjustment: AccountAdjustment
    ledger_entry: LedgerEntry
    preview: AccountAdjustmentReversalPreview
    replayed: bool = False


@dataclass(frozen=True)
class AccountAdjustmentEvidenceIssue:
    adjustment_id: UUID
    issue_codes: tuple[str, ...]


@dataclass(frozen=True)
class AccountAdjustmentEvidenceReport:
    scanned_count: int
    issues: tuple[AccountAdjustmentEvidenceIssue, ...]

    @property
    def drift_count(self) -> int:
        return len(self.issues)


def _fingerprint(kind: str, **values: object) -> str:
    normalized = {
        key: f"{value:.2f}" if isinstance(value, Decimal) else str(value)
        for key, value in values.items()
    }
    payload = json.dumps(
        {"kind": kind, **normalized}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise _error(
            "invalid_command",
            f"Account-adjustment {field} is required.",
            field=field,
        )
    return normalized


def _validate_origin(origin: AccountAdjustmentOrigin) -> AccountAdjustmentOrigin:
    if not isinstance(origin, AccountAdjustmentOrigin):
        raise _error(
            "invalid_command",
            "Account-adjustment origin is not supported.",
            field="origin",
        )
    return origin


def _command_actor(
    context: CommandContext,
    *,
    idempotency_key: str,
) -> tuple[AuditActorType, str]:
    if context.scope != ACCOUNT_ADJUSTMENT_SCOPE:
        raise _error(
            "invalid_command",
            "Account adjustment requires authorized ledger-write evidence.",
            field="scope",
        )
    if context.idempotency_key != idempotency_key:
        raise _error(
            "invalid_command",
            "Command and financial idempotency evidence do not match.",
            field="idempotency_key",
        )
    actor_type_value, separator, actor_id = context.actor.partition(":")
    try:
        actor_type = AuditActorType(actor_type_value)
    except ValueError as exc:
        raise _error(
            "invalid_command",
            "Account-adjustment actor type is not supported.",
            field="actor",
        ) from exc
    if not separator or not actor_id.strip():
        raise _error(
            "invalid_command",
            "Account-adjustment actor identity is incomplete.",
            field="actor",
        )
    return actor_type, actor_id.strip()


def _context_evidence(context: CommandContext) -> dict[str, object]:
    return {
        "schema_version": 1,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
        "causation_id": str(context.causation_id) if context.causation_id else None,
        "idempotency_key_sha256": (
            hashlib.sha256(context.idempotency_key.encode()).hexdigest()
            if context.idempotency_key
            else None
        ),
        "scope": context.scope,
        "reason": context.reason,
    }


def _stored_preview(adjustment: AccountAdjustment) -> AccountAdjustmentPreview:
    return AccountAdjustmentPreview(
        account_id=adjustment.account_id,
        category=adjustment.category,
        amount=round_money(adjustment.amount),
        currency=adjustment.currency,
        memo=adjustment.memo,
        reason=adjustment.reason,
        origin=adjustment.origin,
        origin_ref=adjustment.origin_ref,
        prepaid_funding_before=round_money(adjustment.prepaid_funding_before),
        prepaid_funding_after=round_money(adjustment.prepaid_funding_after),
        postpaid_receivables=round_money(adjustment.postpaid_receivables),
        collection_blocking_balance=round_money(adjustment.collection_blocking_balance),
        shortfall=Decimal("0.00"),
        allowed=True,
        rejection_reason=None,
        fingerprint=adjustment.preview_fingerprint,
        access_consequence=adjustment.access_consequence,
    )


def _original_evidence_issue_codes(adjustment: AccountAdjustment) -> tuple[str, ...]:
    entry = adjustment.ledger_entry
    if entry is None:
        return ("missing_debit_ledger_entry",)
    issues: list[str] = []
    if entry.id != adjustment.ledger_entry_id:
        issues.append("debit_link_mismatch")
    if entry.account_id != adjustment.account_id:
        issues.append("debit_account_mismatch")
    if entry.entry_type is not LedgerEntryType.debit:
        issues.append("debit_type_mismatch")
    if entry.source is not LedgerSource.adjustment:
        issues.append("debit_source_mismatch")
    if entry.category is not adjustment.category:
        issues.append("debit_category_mismatch")
    if round_money(entry.amount) != round_money(adjustment.amount):
        issues.append("debit_amount_mismatch")
    if entry.currency != adjustment.currency:
        issues.append("debit_currency_mismatch")
    if entry.reversal_of_entry_id is not None:
        issues.append("debit_is_itself_a_reversal")
    return tuple(issues)


def _reversal_evidence_issue_codes(adjustment: AccountAdjustment) -> tuple[str, ...]:
    evidence_values = (
        adjustment.reversal_ledger_entry_id,
        adjustment.reversal_preview_fingerprint,
        adjustment.reversal_idempotency_key,
        adjustment.reversal_reason,
        adjustment.reversal_prepaid_funding_before,
        adjustment.reversal_prepaid_funding_after,
        adjustment.reversed_at,
    )
    if not any(value is not None for value in evidence_values):
        return ()
    if any(value is None for value in evidence_values):
        return ("partial_reversal_evidence",)
    entry = adjustment.reversal_ledger_entry
    if entry is None:
        return ("missing_reversal_ledger_entry",)
    issues: list[str] = []
    if entry.id != adjustment.reversal_ledger_entry_id:
        issues.append("reversal_link_mismatch")
    if entry.account_id != adjustment.account_id:
        issues.append("reversal_account_mismatch")
    if entry.entry_type is not LedgerEntryType.credit:
        issues.append("reversal_type_mismatch")
    if entry.source is not LedgerSource.adjustment:
        issues.append("reversal_source_mismatch")
    if entry.category is not adjustment.category:
        issues.append("reversal_category_mismatch")
    if round_money(entry.amount) != round_money(adjustment.amount):
        issues.append("reversal_amount_mismatch")
    if entry.currency != adjustment.currency:
        issues.append("reversal_currency_mismatch")
    if entry.reversal_of_entry_id != adjustment.ledger_entry_id:
        issues.append("reversal_target_mismatch")
    return tuple(issues)


def _require_original_evidence(adjustment: AccountAdjustment) -> None:
    issues = _original_evidence_issue_codes(adjustment)
    if issues:
        raise _error(
            "incomplete_evidence",
            "Account-adjustment debit evidence is incomplete or inconsistent.",
            adjustment_id=str(adjustment.id),
            issue_codes=issues,
        )


def _require_reversal_evidence(adjustment: AccountAdjustment) -> None:
    issues = _reversal_evidence_issue_codes(adjustment)
    if issues:
        raise _error(
            "incomplete_evidence",
            "Account-adjustment reversal evidence is incomplete or inconsistent.",
            adjustment_id=str(adjustment.id),
            issue_codes=issues,
        )


def inspect_account_adjustment_evidence(
    db: Session,
    *,
    account_id: UUID | None = None,
    limit: int = 1000,
) -> AccountAdjustmentEvidenceReport:
    """Return structural drift without guessing or mutating monetary evidence."""

    if limit < 1 or limit > 5000:
        raise _error(
            "invalid_command",
            "Evidence inspection limit must be between 1 and 5000.",
            field="limit",
        )
    stmt = (
        select(AccountAdjustment)
        .options(
            selectinload(AccountAdjustment.ledger_entry),
            selectinload(AccountAdjustment.reversal_ledger_entry),
        )
        .order_by(AccountAdjustment.created_at, AccountAdjustment.id)
        .limit(limit)
    )
    if account_id is not None:
        stmt = stmt.where(AccountAdjustment.account_id == account_id)
    rows = tuple(db.scalars(stmt).all())
    issues = tuple(
        AccountAdjustmentEvidenceIssue(
            adjustment_id=row.id,
            issue_codes=(
                *_original_evidence_issue_codes(row),
                *_reversal_evidence_issue_codes(row),
            ),
        )
        for row in rows
        if _original_evidence_issue_codes(row) or _reversal_evidence_issue_codes(row)
    )
    return AccountAdjustmentEvidenceReport(scanned_count=len(rows), issues=issues)


def preview_account_adjustment(
    db: Session,
    query: PreviewAccountAdjustmentQuery,
) -> AccountAdjustmentPreview:
    origin = _validate_origin(query.origin)
    payload = query.request
    account_id = payload.account_id
    if db.get(Subscriber, account_id) is None:
        raise _error(
            "account_not_found",
            "Subscriber account was not found.",
            account_id=str(account_id),
        )

    amount = round_money(payload.amount)
    if amount <= Decimal("0.00"):
        raise _error(
            "invalid_command",
            "Adjustment amount must be positive.",
            field="amount",
        )
    configured_currency = settings_spec.resolve_value(
        db, SettingDomain.billing, "default_currency"
    )
    currency = str(payload.currency or configured_currency or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise _error(
            "invalid_configuration",
            "Billing default currency must be a three-letter code.",
            field="currency",
        )
    memo = _text(payload.memo, "memo")
    reason = _text(payload.reason, "reason")
    normalized_ref = (query.origin_ref or "").strip() or None

    position = get_customer_financial_position(db, account_id)
    funding_before = round_money(
        position.prepaid_available_balance
        if origin == "prepaid_service_renewal"
        else get_account_credit_balance(db, str(account_id), currency=currency)
    )
    shortfall = round_money(max(Decimal("0.00"), amount - funding_before))
    funding_after = round_money(funding_before - amount)
    allowed = shortfall == Decimal("0.00")
    rejection_reason = None if allowed else "insufficient_prepaid_funding"
    postpaid_receivables = round_money(position.open_invoice_balance)
    collection_blocking_balance = round_money(position.collection_blocking_balance)
    fingerprint = _fingerprint(
        "account_adjustment",
        account_id=account_id,
        category=payload.category.value,
        amount=amount,
        currency=currency,
        memo=memo,
        reason=reason,
        origin=origin.value,
        origin_ref=normalized_ref or "",
        prepaid_funding_before=funding_before,
        prepaid_funding_after=funding_after,
        postpaid_receivables=postpaid_receivables,
        collection_blocking_balance=collection_blocking_balance,
        allowed=allowed,
    )
    return AccountAdjustmentPreview(
        account_id=account_id,
        category=payload.category,
        amount=amount,
        currency=currency,
        memo=memo,
        reason=reason,
        origin=origin.value,
        origin_ref=normalized_ref,
        prepaid_funding_before=funding_before,
        prepaid_funding_after=funding_after,
        postpaid_receivables=postpaid_receivables,
        collection_blocking_balance=collection_blocking_balance,
        shortfall=shortfall,
        allowed=allowed,
        rejection_reason=rejection_reason,
        fingerprint=fingerprint,
    )


def preview_account_adjustment_reversal(
    db: Session,
    query: PreviewAccountAdjustmentReversalQuery,
) -> AccountAdjustmentReversalPreview:
    adjustment = db.get(AccountAdjustment, query.adjustment_id)
    if adjustment is None:
        raise _error(
            "adjustment_not_found",
            "Account adjustment was not found.",
            adjustment_id=str(query.adjustment_id),
        )
    _require_original_evidence(adjustment)
    if adjustment.reversal_ledger_entry_id is not None:
        _require_reversal_evidence(adjustment)
        raise _error(
            "already_reversed",
            "Account adjustment is already reversed.",
            adjustment_id=str(adjustment.id),
        )
    reason = _text(query.request.reason, "reason")
    funding_before = round_money(
        get_account_credit_balance(
            db, str(adjustment.account_id), currency=adjustment.currency
        )
    )
    funding_after = round_money(funding_before + adjustment.amount)
    fingerprint = _fingerprint(
        "account_adjustment_reversal",
        adjustment_id=adjustment.id,
        account_id=adjustment.account_id,
        amount=adjustment.amount,
        currency=adjustment.currency,
        prepaid_funding_before=funding_before,
        prepaid_funding_after=funding_after,
        reverses_ledger_entry_id=adjustment.ledger_entry_id,
        reason=reason,
    )
    return AccountAdjustmentReversalPreview(
        adjustment_id=adjustment.id,
        account_id=adjustment.account_id,
        amount=round_money(adjustment.amount),
        currency=adjustment.currency,
        prepaid_funding_before=funding_before,
        prepaid_funding_after=funding_after,
        reverses_ledger_entry_id=adjustment.ledger_entry_id,
        reason=reason,
        fingerprint=fingerprint,
    )


def _existing_for_key(
    db: Session,
    *,
    origin: str,
    idempotency_key: str,
) -> AccountAdjustment | None:
    return db.scalar(
        select(AccountAdjustment)
        .where(
            AccountAdjustment.origin == origin,
            AccountAdjustment.idempotency_key == idempotency_key,
        )
        .options(selectinload(AccountAdjustment.ledger_entry))
    )


def _existing_for_reversal_key(
    db: Session,
    *,
    origin: str,
    idempotency_key: str,
) -> AccountAdjustment | None:
    return db.scalar(
        select(AccountAdjustment)
        .where(
            AccountAdjustment.origin == origin,
            AccountAdjustment.reversal_idempotency_key == idempotency_key,
        )
        .options(
            selectinload(AccountAdjustment.ledger_entry),
            selectinload(AccountAdjustment.reversal_ledger_entry),
        )
    )


def _same_effective_date(actual: datetime | None, expected: datetime | None) -> bool:
    if expected is None:
        return True
    if actual is None:
        return False
    actual_utc = actual.replace(tzinfo=UTC) if actual.tzinfo is None else actual
    expected_utc = expected.replace(tzinfo=UTC) if expected.tzinfo is None else expected
    return actual_utc == expected_utc


def _confirmation_replay(
    existing: AccountAdjustment,
    command: ConfirmAccountAdjustmentCommand,
) -> AccountAdjustmentResult:
    payload = command.confirmation
    if existing.account_id != payload.account_id:
        raise _error(
            "idempotency_conflict",
            "Idempotency key belongs to another account.",
        )
    if existing.preview_fingerprint != payload.preview_fingerprint:
        raise _error(
            "idempotency_conflict",
            "Idempotency key was used for another adjustment preview.",
        )
    _require_original_evidence(existing)
    if not _same_effective_date(
        existing.ledger_entry.effective_date,
        command.ledger_effective_date,
    ):
        raise _error(
            "idempotency_conflict",
            "Idempotency key belongs to another ledger effective date.",
        )
    return AccountAdjustmentResult(
        adjustment=existing,
        ledger_entry=existing.ledger_entry,
        preview=_stored_preview(existing),
        replayed=True,
    )


def _stage_audit(
    db: Session,
    *,
    adjustment: AccountAdjustment,
    action: str,
    context: CommandContext,
    actor_type: AuditActorType,
    actor_id: str,
    metadata: dict[str, object],
) -> None:
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type="account_adjustment",
            entity_id=str(adjustment.id),
            request_id=str(context.correlation_id),
            metadata_={
                "owner": "financial.account_adjustments",
                **_context_evidence(context),
                **metadata,
            },
        ),
    )


def _stage_event(
    db: Session,
    *,
    adjustment: AccountAdjustment,
    event_type: EventType,
    context: CommandContext,
    ledger_entry_id: UUID,
    reverses_ledger_entry_id: UUID | None,
    prepaid_funding_before: Decimal,
    prepaid_funding_after: Decimal,
    access_consequence: str,
) -> None:
    emit_event(
        db,
        event_type,
        {
            **_context_evidence(context),
            "aggregate_type": "account_adjustment",
            "aggregate_id": str(adjustment.id),
            "aggregate_version": str(context.command_id),
            "account_id": str(adjustment.account_id),
            "category": adjustment.category.value,
            "amount": str(round_money(adjustment.amount)),
            "currency": adjustment.currency,
            "origin": adjustment.origin,
            "origin_ref": adjustment.origin_ref,
            "ledger_entry_id": str(ledger_entry_id),
            "reverses_ledger_entry_id": (
                str(reverses_ledger_entry_id) if reverses_ledger_entry_id else None
            ),
            "prepaid_funding_before": str(round_money(prepaid_funding_before)),
            "prepaid_funding_after": str(round_money(prepaid_funding_after)),
            "access_consequence": access_consequence,
        },
        actor=context.actor,
        subscriber_id=adjustment.account_id,
    )


def _stage_confirmation(
    db: Session,
    command: ConfirmAccountAdjustmentCommand,
) -> AccountAdjustmentResult:
    origin = _validate_origin(command.origin)
    payload = command.confirmation
    actor_type, actor_id = _command_actor(
        command.context,
        idempotency_key=payload.idempotency_key,
    )
    existing = _existing_for_key(
        db,
        origin=origin.value,
        idempotency_key=payload.idempotency_key,
    )
    if existing is not None:
        return _confirmation_replay(existing, command)

    lock_account(db, str(payload.account_id))
    preview = preview_account_adjustment(
        db,
        PreviewAccountAdjustmentQuery(
            request=AccountAdjustmentPreviewRequest(
                **payload.model_dump(exclude={"preview_fingerprint", "idempotency_key"})
            ),
            origin=origin,
            origin_ref=command.origin_ref,
        ),
    )
    if preview.fingerprint != payload.preview_fingerprint:
        raise _error(
            "stale_preview",
            "Financial state changed after preview; preview again.",
        )
    if not preview.allowed:
        raise _error(
            "insufficient_funding",
            "Insufficient prepaid funding for this adjustment.",
            shortfall=str(preview.shortfall),
            currency=preview.currency,
        )
    existing = _existing_for_key(
        db,
        origin=origin.value,
        idempotency_key=payload.idempotency_key,
    )
    if existing is not None:
        return _confirmation_replay(existing, command)

    try:
        entry = LedgerEntries.stage_account_adjustment_debit(
            db,
            account_id=preview.account_id,
            category=preview.category,
            amount=preview.amount,
            currency=preview.currency,
            memo=preview.memo,
            effective_date=command.ledger_effective_date,
        )
    except LedgerAccountAdjustmentError as exc:
        raise _error(
            "incomplete_evidence",
            "Canonical ledger rejected the account-adjustment debit.",
            ledger_error=exc.code,
        ) from exc

    adjustment = AccountAdjustment(
        account_id=preview.account_id,
        category=preview.category,
        amount=preview.amount,
        currency=preview.currency,
        memo=preview.memo,
        reason=preview.reason,
        origin=preview.origin,
        origin_ref=preview.origin_ref,
        prepaid_funding_before=preview.prepaid_funding_before,
        prepaid_funding_after=preview.prepaid_funding_after,
        postpaid_receivables=preview.postpaid_receivables,
        collection_blocking_balance=preview.collection_blocking_balance,
        access_consequence=preview.access_consequence,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=payload.idempotency_key,
        ledger_entry_id=entry.id,
    )
    db.add(adjustment)
    db.flush()
    _stage_audit(
        db,
        adjustment=adjustment,
        action="confirm",
        context=command.context,
        actor_type=actor_type,
        actor_id=actor_id,
        metadata={
            "origin": preview.origin,
            "origin_ref": preview.origin_ref,
            "ledger_entry_id": str(entry.id),
            "ledger_entry_type": LedgerEntryType.debit.value,
            "ledger_source": LedgerSource.adjustment.value,
            "amount": str(preview.amount),
            "currency": preview.currency,
            "prepaid_funding_before": str(preview.prepaid_funding_before),
            "prepaid_funding_after": str(preview.prepaid_funding_after),
            "postpaid_receivables": str(preview.postpaid_receivables),
            "preview_fingerprint": preview.fingerprint,
            "access_consequence": preview.access_consequence,
        },
    )
    _stage_event(
        db,
        adjustment=adjustment,
        event_type=EventType.account_adjustment_confirmed,
        context=command.context,
        ledger_entry_id=entry.id,
        reverses_ledger_entry_id=None,
        prepaid_funding_before=preview.prepaid_funding_before,
        prepaid_funding_after=preview.prepaid_funding_after,
        access_consequence=preview.access_consequence,
    )
    _stage_adjustment_posting(db, adjustment, preview)
    return AccountAdjustmentResult(
        adjustment=adjustment,
        ledger_entry=entry,
        preview=preview,
    )


def confirm_account_adjustment(
    db: Session,
    command: ConfirmAccountAdjustmentCommand,
) -> AccountAdjustmentResult:
    try:
        return execute_owner_command(
            db,
            definition=_CONFIRM_COMMAND,
            context=command.context,
            operation=lambda: _stage_confirmation(db, command),
        )
    except IntegrityError as exc:
        raise _error(
            "write_conflict",
            "Account-adjustment evidence conflicted with a concurrent command; retry.",
        ) from exc


def stage_account_adjustment(
    db: Session,
    command: ConfirmAccountAdjustmentCommand,
) -> AccountAdjustmentResult:
    """Stage a debit inside an approved coordinator-owned transaction."""

    try:
        return _stage_confirmation(db, command)
    except IntegrityError as exc:
        raise _error(
            "write_conflict",
            "Account-adjustment evidence conflicted with a concurrent command; retry.",
        ) from exc


def stage_system_account_adjustment(
    db: Session,
    command: StageSystemAccountAdjustmentCommand,
) -> AccountAdjustmentResult:
    """Preview and stage a system debit inside a larger owner transaction."""

    preview = preview_account_adjustment(
        db,
        PreviewAccountAdjustmentQuery(
            request=command.request,
            origin=command.origin,
            origin_ref=command.origin_ref,
        ),
    )
    return stage_account_adjustment(
        db,
        ConfirmAccountAdjustmentCommand(
            context=command.context,
            confirmation=AccountAdjustmentConfirm(
                **command.request.model_dump(),
                preview_fingerprint=preview.fingerprint,
                idempotency_key=command.idempotency_key,
            ),
            origin=command.origin,
            origin_ref=command.origin_ref,
            ledger_effective_date=command.ledger_effective_date,
        ),
    )


def _reversal_replay(
    reused: AccountAdjustment,
    command: ReverseAccountAdjustmentCommand,
) -> AccountAdjustmentReversalResult:
    payload = command.confirmation
    if reused.id != command.adjustment_id:
        raise _error(
            "idempotency_conflict",
            "Reversal idempotency key belongs to another adjustment.",
        )
    if reused.reversal_preview_fingerprint != payload.preview_fingerprint:
        raise _error(
            "idempotency_conflict",
            "Idempotency key was used for another reversal preview.",
        )
    _require_original_evidence(reused)
    _require_reversal_evidence(reused)
    preview = AccountAdjustmentReversalPreview(
        adjustment_id=reused.id,
        account_id=reused.account_id,
        amount=round_money(reused.amount),
        currency=reused.currency,
        prepaid_funding_before=round_money(
            cast(Decimal, reused.reversal_prepaid_funding_before)
        ),
        prepaid_funding_after=round_money(
            cast(Decimal, reused.reversal_prepaid_funding_after)
        ),
        reverses_ledger_entry_id=reused.ledger_entry_id,
        reason=reused.reversal_reason or payload.reason,
        fingerprint=reused.reversal_preview_fingerprint or "",
    )
    return AccountAdjustmentReversalResult(
        adjustment=reused,
        ledger_entry=cast(LedgerEntry, reused.reversal_ledger_entry),
        preview=preview,
        replayed=True,
    )


def _stage_reversal(
    db: Session,
    command: ReverseAccountAdjustmentCommand,
) -> AccountAdjustmentReversalResult:
    payload = command.confirmation
    actor_type, actor_id = _command_actor(
        command.context,
        idempotency_key=payload.idempotency_key,
    )
    unlocked = db.get(AccountAdjustment, command.adjustment_id)
    if unlocked is None:
        raise _error(
            "adjustment_not_found",
            "Account adjustment was not found.",
            adjustment_id=str(command.adjustment_id),
        )
    reused = _existing_for_reversal_key(
        db,
        origin=unlocked.origin,
        idempotency_key=payload.idempotency_key,
    )
    if reused is not None:
        return _reversal_replay(reused, command)

    lock_account(db, str(unlocked.account_id))
    adjustment = db.scalar(
        select(AccountAdjustment)
        .where(AccountAdjustment.id == command.adjustment_id)
        .with_for_update()
        .options(
            selectinload(AccountAdjustment.ledger_entry),
            selectinload(AccountAdjustment.reversal_ledger_entry),
        )
    )
    if adjustment is None:
        raise _error(
            "adjustment_not_found",
            "Account adjustment was not found.",
            adjustment_id=str(command.adjustment_id),
        )
    reused = _existing_for_reversal_key(
        db,
        origin=adjustment.origin,
        idempotency_key=payload.idempotency_key,
    )
    if reused is not None:
        return _reversal_replay(reused, command)
    _require_original_evidence(adjustment)
    if adjustment.reversal_ledger_entry_id is not None:
        _require_reversal_evidence(adjustment)
        raise _error(
            "already_reversed",
            "Account adjustment is already reversed.",
            adjustment_id=str(adjustment.id),
        )

    preview = preview_account_adjustment_reversal(
        db,
        PreviewAccountAdjustmentReversalQuery(
            adjustment_id=adjustment.id,
            request=AccountAdjustmentReversalPreviewRequest(reason=payload.reason),
        ),
    )
    if preview.fingerprint != payload.preview_fingerprint:
        raise _error(
            "stale_preview",
            "Financial state changed after preview; preview again.",
        )
    try:
        reversal = LedgerEntries.stage_account_adjustment_reversal(
            db,
            ledger_entry_id=adjustment.ledger_entry_id,
            adjustment_id=adjustment.id,
            reason=preview.reason,
        )
    except LedgerAccountAdjustmentError as exc:
        raise _error(
            "incomplete_evidence",
            "Canonical ledger rejected the account-adjustment reversal.",
            ledger_error=exc.code,
        ) from exc

    adjustment.reversal_ledger_entry_id = reversal.id
    adjustment.reversal_preview_fingerprint = preview.fingerprint
    adjustment.reversal_idempotency_key = payload.idempotency_key
    adjustment.reversal_reason = preview.reason
    adjustment.reversal_prepaid_funding_before = preview.prepaid_funding_before
    adjustment.reversal_prepaid_funding_after = preview.prepaid_funding_after
    adjustment.reversed_at = datetime.now(UTC)
    db.flush()
    _stage_audit(
        db,
        adjustment=adjustment,
        action="reverse",
        context=command.context,
        actor_type=actor_type,
        actor_id=actor_id,
        metadata={
            "ledger_entry_id": str(reversal.id),
            "reverses_ledger_entry_id": str(adjustment.ledger_entry_id),
            "amount": str(preview.amount),
            "currency": preview.currency,
            "prepaid_funding_before": str(preview.prepaid_funding_before),
            "prepaid_funding_after": str(preview.prepaid_funding_after),
            "preview_fingerprint": preview.fingerprint,
            "access_consequence": preview.access_consequence,
        },
    )
    _stage_event(
        db,
        adjustment=adjustment,
        event_type=EventType.account_adjustment_reversed,
        context=command.context,
        ledger_entry_id=reversal.id,
        reverses_ledger_entry_id=adjustment.ledger_entry_id,
        prepaid_funding_before=preview.prepaid_funding_before,
        prepaid_funding_after=preview.prepaid_funding_after,
        access_consequence=preview.access_consequence,
    )
    _stage_adjustment_reversal_posting(db, adjustment, reversal)
    return AccountAdjustmentReversalResult(
        adjustment=adjustment,
        ledger_entry=reversal,
        preview=preview,
    )


def _stage_adjustment_reversal_posting(db: Session, adjustment, reversal) -> None:
    """Link the shadow reversal to the original adjustment posting group.

    A true reversal negates and links one complete original group. When the
    original adjustment predates the forward-shadow (no group exists),
    nothing is staged — the verifier owns the gap rather than inventing
    history.
    """
    from app.services.owner_commands import (
        current_command_context,
        owner_command_active,
    )

    if not owner_command_active(db):
        return
    from app.models.customer_subledger import (
        CustomerPostingGroup,
        PostingCommandKind,
        PostingProducer,
        PostingSourceKind,
    )
    from app.services.billing.customer_subledger import (
        StageReversalCommand,
        stage_reversal,
    )

    original = (
        db.execute(
            select(CustomerPostingGroup).where(
                CustomerPostingGroup.source_kind
                == PostingSourceKind.account_adjustment.value,
                CustomerPostingGroup.source_id == adjustment.id,
                CustomerPostingGroup.command_kind != PostingCommandKind.reversal,
                CustomerPostingGroup.reverses_group_id.is_(None),
            )
        )
        .scalars()
        .first()
    )
    if original is None:
        return
    occurred = adjustment.reversed_at or datetime.now(UTC)
    stage_reversal(
        db,
        StageReversalCommand(
            original_group_id=original.id,
            producer_owner=PostingProducer.account_adjustments,
            source_kind=PostingSourceKind.ledger_entry,
            source_id=reversal.id,
            occurred_at=(
                occurred.replace(tzinfo=UTC) if occurred.tzinfo is None else occurred
            ),
            idempotency_key=f"posting:adjustment_reversal:{reversal.id}",
        ),
        context=current_command_context(db),
    )


def reverse_account_adjustment(
    db: Session,
    command: ReverseAccountAdjustmentCommand,
) -> AccountAdjustmentReversalResult:
    try:
        return execute_owner_command(
            db,
            definition=_REVERSE_COMMAND,
            context=command.context,
            operation=lambda: _stage_reversal(db, command),
        )
    except IntegrityError as exc:
        raise _error(
            "write_conflict",
            "Adjustment reversal conflicted with a concurrent command; retry.",
        ) from exc


__all__ = [
    "ACCOUNT_ADJUSTMENT_SCOPE",
    "AccountAdjustmentError",
    "AccountAdjustmentEvidenceIssue",
    "AccountAdjustmentEvidenceReport",
    "AccountAdjustmentOrigin",
    "AccountAdjustmentPreview",
    "AccountAdjustmentResult",
    "AccountAdjustmentReversalPreview",
    "AccountAdjustmentReversalResult",
    "ConfirmAccountAdjustmentCommand",
    "PreviewAccountAdjustmentQuery",
    "PreviewAccountAdjustmentReversalQuery",
    "ReverseAccountAdjustmentCommand",
    "StageSystemAccountAdjustmentCommand",
    "confirm_account_adjustment",
    "inspect_account_adjustment_evidence",
    "preview_account_adjustment",
    "preview_account_adjustment_reversal",
    "reverse_account_adjustment",
    "stage_account_adjustment",
    "stage_system_account_adjustment",
]


def _stage_adjustment_posting(db: Session, adjustment, preview) -> None:
    """One shadow posting group per confirmed adjustment (ADR 0007 Ph. 3).

    Renewals-driven system adjustments run without an owner command today
    and skip: that gap is the verifier's producer_not_owner_wrapped debt.
    """
    from app.services.owner_commands import (
        current_command_context,
        owner_command_active,
    )

    if not owner_command_active(db):
        return
    from decimal import Decimal as _Decimal

    from app.models.customer_subledger import (
        PositionEffectKind,
        PostingCommandKind,
        PostingProducer,
        PostingSourceKind,
    )
    from app.services.billing.customer_subledger import (
        EffectInput,
        StagePostingGroupCommand,
        stage_posting_group,
    )

    effective = adjustment.created_at
    stage_posting_group(
        db,
        StagePostingGroupCommand(
            account_id=adjustment.account_id,
            currency=preview.currency,
            command_kind=PostingCommandKind.adjustment,
            producer_owner=PostingProducer.account_adjustments,
            source_kind=PostingSourceKind.account_adjustment,
            source_id=adjustment.id,
            occurred_at=(
                effective.replace(tzinfo=UTC) if effective.tzinfo is None else effective
            ),
            effects=(
                EffectInput(
                    effect=PositionEffectKind.adjustment_applied,
                    amount=abs(_Decimal(str(preview.amount))),
                ),
            ),
            idempotency_key=f"posting:account_adjustment:{adjustment.id}",
        ),
        context=current_command_context(db),
    )
