"""Atomic application owner for administrative invoice draft authoring."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    TaxApplication,
    TaxRate,
)
from app.models.idempotency import IdempotencyKey
from app.models.subscriber import Subscriber
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import InvoiceCreate, InvoiceLineCreate, InvoiceUpdate
from app.services.audit import AuditEvents
from app.services.billing._common import (
    _validate_invoice_line_amount,
    lock_account,
)
from app.services.billing.invoices import (
    DraftInvoiceLineReplacement,
    DraftInvoiceParticipantError,
    InvoiceLines,
    Invoices,
)
from app.services.common import round_money
from app.services.customer_tax_policies import get_customer_vat_exemption_policy
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "financial.invoice_draft_authoring"
CONCERN = "administrative invoice draft authoring coordination"
_CREATE_SCOPE = "invoice-draft-authoring:create"

_CREATE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="create_invoice_draft",
)
_UPDATE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="update_invoice_draft",
)


class InvoiceDraftAuthoringError(DomainError, ValueError):
    """Stable rejection from the invoice draft authoring owner."""


def _error(suffix: str, message: str, **details: object) -> InvoiceDraftAuthoringError:
    return InvoiceDraftAuthoringError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class DraftLineCommand:
    """One complete line in the desired draft document."""

    description: str
    quantity: Decimal
    unit_price: Decimal
    tax_rate_id: UUID | None = None
    line_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CreateInvoiceDraftCommand:
    """Complete desired state for a new administrative invoice draft."""

    account_id: UUID
    invoice_number: str | None
    currency: str
    issued_at: datetime | None
    due_at: datetime | None
    memo: str | None
    is_proforma: bool
    lines: tuple[DraftLineCommand, ...]


@dataclass(frozen=True, slots=True)
class UpdateInvoiceDraftCommand:
    """Complete desired state for an existing administrative invoice draft."""

    invoice_id: UUID
    account_id: UUID
    invoice_number: str | None
    currency: str
    issued_at: datetime | None
    due_at: datetime | None
    memo: str | None
    is_proforma: bool
    lines: tuple[DraftLineCommand, ...]


@dataclass(frozen=True, slots=True)
class InvoiceDraftResult:
    """Immutable result returned after the owner transaction commits."""

    invoice_id: UUID
    account_id: UUID
    invoice_number: str | None
    status: InvoiceStatus
    total: Decimal
    balance_due: Decimal
    is_proforma: bool
    replayed: bool = False


def _result(invoice: Invoice, *, replayed: bool = False) -> InvoiceDraftResult:
    return InvoiceDraftResult(
        invoice_id=invoice.id,
        account_id=invoice.account_id,
        invoice_number=invoice.invoice_number,
        status=invoice.status,
        total=round_money(invoice.total),
        balance_due=round_money(invoice.balance_due),
        is_proforma=bool(invoice.is_proforma),
        replayed=replayed,
    )


def _normalized_key(context: CommandContext) -> str:
    source = context.idempotency_key or str(context.command_id)
    return hashlib.sha256(source.strip().encode()).hexdigest()


def _create_fingerprint(command: CreateInvoiceDraftCommand) -> str:
    payload = {
        "account_id": str(command.account_id),
        "invoice_number": (command.invoice_number or "").strip() or None,
        "currency": command.currency.strip().upper(),
        "issued_at": command.issued_at.isoformat() if command.issued_at else None,
        "due_at": command.due_at.isoformat() if command.due_at else None,
        "memo": command.memo,
        "is_proforma": command.is_proforma,
        "lines": [
            {
                "description": line.description.strip(),
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
                "tax_rate_id": str(line.tax_rate_id) if line.tax_rate_id else None,
            }
            for line in command.lines
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validate_account(db: Session, account_id: UUID) -> None:
    lock_account(db, str(account_id))
    if db.get(Subscriber, account_id) is None:
        raise _error(
            "account_not_found",
            "Subscriber account was not found.",
            account_id=str(account_id),
        )


def _validate_header(
    db: Session,
    *,
    account_id: UUID,
    invoice_number: str | None,
    currency: str,
    exclude_invoice_id: UUID | None = None,
) -> tuple[str | None, str]:
    _validate_account(db, account_id)
    normalized_currency = currency.strip().upper()
    if len(normalized_currency) != 3:
        raise _error("currency_invalid", "Invoice currency must be a 3-letter code.")
    normalized_number = (invoice_number or "").strip() or None
    if normalized_number:
        duplicate = (
            select(Invoice.id)
            .where(Invoice.invoice_number == normalized_number)
            .where(Invoice.is_active.is_(True))
        )
        if exclude_invoice_id is not None:
            duplicate = duplicate.where(Invoice.id != exclude_invoice_id)
        if db.scalar(duplicate) is not None:
            raise _error(
                "invoice_number_conflict",
                "Invoice number is already in use.",
                invoice_number=normalized_number,
            )
    return normalized_number, normalized_currency


def _validated_lines(
    db: Session,
    lines: tuple[DraftLineCommand, ...],
) -> tuple[tuple[DraftLineCommand, Decimal, TaxApplication], ...]:
    if not lines:
        raise _error(
            "line_required",
            "A draft invoice must contain at least one line item.",
        )
    validated: list[tuple[DraftLineCommand, Decimal, TaxApplication]] = []
    for line in lines:
        description = line.description.strip()
        if not description:
            raise _error("line_invalid", "Invoice line description is required.")
        if len(description) > 255:
            raise _error(
                "line_invalid",
                "Invoice line description cannot exceed 255 characters.",
            )
        try:
            amount = _validate_invoice_line_amount(
                line.quantity,
                line.unit_price,
                None,
            )
        except Exception as exc:
            raise _error(
                "line_invalid",
                "Invoice line quantity or price is invalid.",
                description=description,
            ) from exc
        if line.tax_rate_id is not None and db.get(TaxRate, line.tax_rate_id) is None:
            raise _error(
                "tax_rate_not_found",
                "Invoice tax rate was not found.",
                tax_rate_id=str(line.tax_rate_id),
            )
        validated.append(
            (
                DraftLineCommand(
                    description=description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    tax_rate_id=line.tax_rate_id,
                    line_id=line.line_id,
                ),
                amount,
                TaxApplication.exclusive,
            )
        )
    return tuple(validated)


def _stage_lines(
    db: Session,
    *,
    invoice: Invoice,
    lines: tuple[DraftLineCommand, ...],
) -> None:
    validated = _validated_lines(db, lines)
    vat_policy = get_customer_vat_exemption_policy(
        db,
        account_id=invoice.account_id,
    )
    replacements = tuple(
        DraftInvoiceLineReplacement(
            line_id=command.line_id,
            payload=InvoiceLineCreate(
                invoice_id=invoice.id,
                description=command.description,
                quantity=command.quantity,
                unit_price=command.unit_price,
                amount=amount,
                tax_rate_id=None if vat_policy.vat_exempt else command.tax_rate_id,
                tax_application=tax_application,
                is_active=True,
            ),
        )
        for command, amount, tax_application in validated
    )
    try:
        InvoiceLines.replace_admin_draft_lines(db, invoice.id, replacements)
    except DraftInvoiceParticipantError as exc:
        if exc.reason == "line_not_found":
            raise _error(
                "line_not_found",
                "Invoice line does not belong to this draft.",
            ) from exc
        raise _error(exc.reason, str(exc)) from exc


def _stage_audit(
    db: Session,
    *,
    invoice: Invoice,
    context: CommandContext,
    action: str,
) -> None:
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_id=context.actor,
            action=action,
            entity_type="invoice",
            entity_id=str(invoice.id),
            request_id=str(context.correlation_id),
            metadata_={
                "account_id": str(invoice.account_id),
                "invoice_number": invoice.invoice_number,
                "status": invoice.status.value,
                "is_proforma": bool(invoice.is_proforma),
                "total": str(invoice.total),
                "command_id": str(context.command_id),
                "reason": context.reason,
                "financial_effect": "draft_document_authored",
            },
        ),
    )


def _emit_created(db: Session, invoice: Invoice) -> None:
    emit_event(
        db,
        EventType.invoice_created,
        {
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number,
            "amount": str(invoice.total),
            "total": str(invoice.total),
            "due_date": invoice.due_at.date().isoformat() if invoice.due_at else None,
            "currency": invoice.currency,
            "status": invoice.status.value,
            "is_proforma": bool(invoice.is_proforma),
        },
        account_id=invoice.account_id,
        invoice_id=invoice.id,
    )


def create_invoice_draft(
    db: Session,
    command: CreateInvoiceDraftCommand,
    *,
    context: CommandContext,
) -> InvoiceDraftResult:
    """Create one complete draft, its lines, audit, and event atomically."""

    return execute_owner_command(
        db,
        definition=_CREATE_COMMAND,
        context=context,
        operation=lambda: _create_invoice_draft(db, command=command, context=context),
    )


def _create_invoice_draft(
    db: Session,
    *,
    command: CreateInvoiceDraftCommand,
    context: CommandContext,
) -> InvoiceDraftResult:
    key = _normalized_key(context)
    fingerprint = _create_fingerprint(command)
    _validate_account(db, command.account_id)
    replay = db.scalar(
        select(IdempotencyKey)
        .where(IdempotencyKey.scope == _CREATE_SCOPE)
        .where(IdempotencyKey.key == key)
        .with_for_update()
    )
    if replay is not None:
        if replay.account_id != command.account_id or not replay.ref_id:
            raise _error(
                "idempotency_conflict",
                "Draft creation key is already reserved for another result.",
            )
        result_parts = replay.ref_id.split("|", maxsplit=1)
        if len(result_parts) != 2 or result_parts[1] != fingerprint:
            raise _error(
                "idempotency_conflict",
                "Draft creation key was reused with different invoice data.",
            )
        try:
            replay_invoice_id = UUID(result_parts[0])
        except ValueError as exc:
            raise _error(
                "idempotency_conflict",
                "Draft creation result evidence is invalid.",
            ) from exc
        invoice = db.get(Invoice, replay_invoice_id)
        if invoice is None:
            raise _error(
                "idempotency_conflict",
                "Draft creation result can no longer be resolved.",
            )
        return _result(invoice, replayed=True)

    invoice_number, currency = _validate_header(
        db,
        account_id=command.account_id,
        invoice_number=command.invoice_number,
        currency=command.currency,
    )
    reservation = IdempotencyKey(
        scope=_CREATE_SCOPE,
        key=key,
        account_id=command.account_id,
    )
    db.add(reservation)
    db.flush()

    invoice = Invoices.stage_admin_draft(
        db,
        InvoiceCreate(
            account_id=command.account_id,
            invoice_number=invoice_number,
            status=InvoiceStatus.draft,
            currency=currency,
            issued_at=command.issued_at,
            due_at=command.due_at,
            memo=command.memo,
            is_proforma=command.is_proforma,
            subtotal=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("0.00"),
            balance_due=Decimal("0.00"),
            is_active=True,
        ),
    )
    _stage_lines(db, invoice=invoice, lines=command.lines)
    _stage_audit(
        db,
        invoice=invoice,
        context=context,
        action="create_invoice_draft",
    )
    _emit_created(db, invoice)
    reservation.ref_id = f"{invoice.id}|{fingerprint}"
    db.flush()
    return _result(invoice)


def update_invoice_draft(
    db: Session,
    command: UpdateInvoiceDraftCommand,
    *,
    context: CommandContext,
) -> InvoiceDraftResult:
    """Replace one draft's editable header and line state atomically."""

    return execute_owner_command(
        db,
        definition=_UPDATE_COMMAND,
        context=context,
        operation=lambda: _update_invoice_draft(db, command=command, context=context),
    )


def _update_invoice_draft(
    db: Session,
    *,
    command: UpdateInvoiceDraftCommand,
    context: CommandContext,
) -> InvoiceDraftResult:
    invoice_number, currency = _validate_header(
        db,
        account_id=command.account_id,
        invoice_number=command.invoice_number,
        currency=command.currency,
        exclude_invoice_id=command.invoice_id,
    )
    invoice = lock_for_update(db, Invoice, command.invoice_id)
    if invoice is None or not invoice.is_active:
        raise _error(
            "invoice_not_found",
            "Invoice draft was not found.",
            invoice_id=str(command.invoice_id),
        )
    if invoice.account_id != command.account_id:
        raise _error(
            "account_mismatch",
            "Invoice account cannot be changed.",
            invoice_id=str(invoice.id),
        )
    if invoice.status != InvoiceStatus.draft:
        raise _error(
            "invoice_not_editable",
            "Only draft invoices can be edited.",
            invoice_id=str(invoice.id),
            status=invoice.status.value,
        )
    if invoice.currency != currency:
        raise _error(
            "currency_mismatch",
            "Invoice currency cannot be changed after creation.",
            invoice_id=str(invoice.id),
        )

    invoice = Invoices.stage_admin_draft_header(
        db,
        invoice.id,
        InvoiceUpdate(
            invoice_number=invoice_number or invoice.invoice_number,
            issued_at=command.issued_at,
            due_at=command.due_at,
            memo=command.memo,
            is_proforma=command.is_proforma,
        ),
    )
    _stage_lines(db, invoice=invoice, lines=command.lines)
    _stage_audit(
        db,
        invoice=invoice,
        context=context,
        action="update_invoice_draft",
    )
    db.flush()
    return _result(invoice)
