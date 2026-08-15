"""Native sales orders ported from CRM.

Faithful port of ``dotmac_crm/app/services/sales_orders.py`` onto sub's
native models, with Sub ownership deltas applied:

* Customer party: CRM ``person_id`` becomes ``subscriber_id`` — the CRM's
  person-mediated SO → sub identity chain (SO → project → person →
  ``selfcare_id``) collapses to the first-class column.
* The crm#233 ``account_id`` slot fix is ported as the FIXED shape: the
  legacy ``account_id``/``invoice_id`` schema fields are gone and the list
  API passes filters by keyword, so nothing can land in the wrong slot.
* ``order_number`` continues the CRM ``SO-%06d`` sequence via sub's
  ``document_sequences`` (key ``sales_order_number``, ``with_for_update``).
* **Financial side-effects are rewired native:** the CRM's HTTP
  pushes to sub become direct in-process calls —

  - ``push_sales_order_subscription_to_selfcare`` →
    :func:`app.services.crm_api.create_subscription` per offer-tagged line,
    ``external_ref="sales_order:{id}:subscription:{line_id}"`` (unchanged);
  - ``push_sales_order_payment_to_selfcare`` →
    :func:`app.services.crm_api.record_external_payment`,
    ``external_ref="sales_order:{id}:payment"`` (unchanged);
  - ``ensure_installation_invoice_for_sales_order`` →
    :func:`app.services.crm_api.create_installation_invoice`,
    ``external_ref="project:{project_id}"`` (unchanged), still row-locking
    the project for the invoice-dedup metadata write.

  Every ``external_ref`` idempotency key is byte-identical to the HTTP era,
  so re-runs and historical rows stay deduped (risk #12 analogue).
* ``_accrue_reseller_commission`` is a stub until the native referral and
  reseller-commission capability owns that side effect.
* Install-project creation for manual (quote-less) sales orders is deferred
  to the projects service port (see ``_ensure_project_for_manual_sales_order``).
* Statuses are stored as plain strings (sub convention, ).
* Native services emit sub events from day one (risk #13):
  ``sales_order.paid`` plus the chained ``sales_order.funding_satisfied``
  output staged atomically with the paid transition; the registered
  lifecycle projection handler applies the funded-service consequences with
  durable retry (see :func:`apply_funding_consequences`).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import Subscription
from app.models.project import Project, ProjectTask
from app.models.sales import (
    Quote,
    QuoteLineItem,
    QuoteStatus,
    SalesOrder,
    SalesOrderInvoiceLink,
    SalesOrderLine,
    SalesOrderPaymentStatus,
    SalesOrderStatus,
)
from app.models.subscriber import Subscriber
from app.services import numbering
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    get_by_id,
    round_money,
    validate_enum,
)
from app.services.events import EventType, emit_event
from app.services.response import ListResponseMixin
from app.services.sales import lifecycle as lead_lifecycle

logger = logging.getLogger(__name__)

_PAID = SalesOrderPaymentStatus.paid.value
SALES_ORDER_VAT_RATE = Decimal("0.075")


@dataclass(frozen=True)
class SalesOrderTotals:
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal


def fixed_vat_amount(subtotal: Decimal) -> Decimal:
    """Return the canonical fixed VAT amount for a manually priced order."""

    return round_money(Decimal(subtotal) * SALES_ORDER_VAT_RATE)


def calculate_manual_order_totals(
    lines: Sequence[tuple[Decimal, Decimal]],
) -> SalesOrderTotals:
    """Price manual lines and apply the canonical fixed VAT policy."""

    subtotal = round_money(
        sum((quantity * unit_price for quantity, unit_price in lines), Decimal("0"))
    )
    tax_total = fixed_vat_amount(subtotal)
    return SalesOrderTotals(
        subtotal=subtotal,
        tax_total=tax_total,
        total=round_money(subtotal + tax_total),
    )


def validate_manual_payment_amount(*, amount_paid: Decimal, total: Decimal) -> Decimal:
    """Validate and round a manual order's collected amount."""

    rounded = round_money(amount_paid)
    if rounded < 0 or rounded > total:
        raise ValueError("Amount paid must be between zero and the grand total.")
    return rounded


_PARTIAL = SalesOrderPaymentStatus.partial.value
_PENDING = SalesOrderPaymentStatus.pending.value
_WAIVED = SalesOrderPaymentStatus.waived.value


class SalesOrderLifecycleError(ValueError):
    def __init__(self, code: str, message: str, *, kind: str = "conflict") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


def fulfill_from_customer_experience(
    db: Session,
    *,
    sales_order_id: UUID,
    handoff_id: UUID,
    actor_id: str,
) -> bool:
    """Apply accepted CX evidence through the SalesOrder lifecycle owner."""

    actor = str(actor_id or "").strip()
    if not actor:
        raise SalesOrderLifecycleError(
            "actor_required", "Sales-order fulfilment actor is required", kind="invalid"
        )
    order = db.scalars(
        select(SalesOrder)
        .where(SalesOrder.id == coerce_uuid(sales_order_id))
        .with_for_update()
    ).one_or_none()
    if order is None or not order.is_active:
        raise SalesOrderLifecycleError(
            "sales_order_not_found", "Sales order not found", kind="not_found"
        )
    metadata = dict(order.metadata_ or {})
    recorded_handoff_id = metadata.get("cx_handoff_id")
    if order.status == SalesOrderStatus.fulfilled.value:
        if recorded_handoff_id not in {None, str(handoff_id)}:
            raise SalesOrderLifecycleError(
                "handoff_evidence_conflict",
                "Sales order was fulfilled by different CX evidence",
            )
        return False
    if order.status != SalesOrderStatus.paid.value:
        raise SalesOrderLifecycleError(
            "sales_order_not_paid",
            "Only a fully paid sales order can be fulfilled",
        )
    order.status = SalesOrderStatus.fulfilled.value
    metadata["cx_handoff_id"] = str(handoff_id)
    order.metadata_ = metadata
    emit_event(
        db,
        EventType.sales_order_fulfilled,
        {
            "sales_order_id": str(order.id),
            "cx_handoff_id": str(handoff_id),
            "from_status": SalesOrderStatus.paid.value,
            "to_status": SalesOrderStatus.fulfilled.value,
        },
        actor=actor,
        subscriber_id=order.subscriber_id,
    )
    db.flush()
    return True


def _enum_str(value, enum_cls, label: str) -> str | None:
    member = validate_enum(value, enum_cls, label)
    return member.value if member is not None else None


def _ensure_subscriber(db: Session, subscriber_id) -> Subscriber:
    subscriber = get_by_id(db, Subscriber, subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    return subscriber


def _validate_quote_subscriber_context(
    db: Session,
    *,
    quote: Quote,
    subscriber: Subscriber,
) -> None:
    if quote.subscriber_id != subscriber.id:
        raise HTTPException(
            status_code=409,
            detail="Sales order Subscriber does not match the Quote Subscriber",
        )
    if quote.lead is None:
        return
    try:
        lead_lifecycle.validate_lead_subscriber_alignment(
            db,
            lead=quote.lead,
            subscriber=subscriber,
        )
    except lead_lifecycle.LeadLifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="Invalid decimal value") from None


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid datetime value") from exc


_SALES_ORDER_SEQUENCE_KEY = "sales_order_number"
_SALES_ORDER_NUMBER_PREFIX = "SO-"


def _parse_order_number(order_number: str | None) -> int | None:
    value = str(order_number or "").strip()
    if not value.startswith(_SALES_ORDER_NUMBER_PREFIX):
        return None
    digits = value[len(_SALES_ORDER_NUMBER_PREFIX) :]
    return int(digits) if digits.isdigit() else None


def _highest_existing_order_number(db: Session) -> int:
    highest = 0
    existing_numbers = db.scalars(
        select(SalesOrder.order_number).where(
            SalesOrder.order_number.like(f"{_SALES_ORDER_NUMBER_PREFIX}%")
        )
    )
    for order_number in existing_numbers:
        parsed = _parse_order_number(order_number)
        if parsed is not None:
            highest = max(highest, parsed)
    return highest


def _generate_order_number(db: Session) -> str:
    """Reserve a collision-free number and repair stale sequence state.

    Existing SalesOrders are authoritative issued-number evidence. The locked
    document sequence serializes allocators, but imported or manually restored
    data can leave its cursor behind that evidence. Advance the cursor before
    reserving so Quote acceptance repairs that drift instead of failing its
    atomic conversion on the unique order-number constraint.
    """

    sequence = numbering.lock_sequence(db, _SALES_ORDER_SEQUENCE_KEY, 1)
    value = max(sequence.next_value, _highest_existing_order_number(db) + 1)
    sequence.next_value = value + 1
    db.flush()
    return f"{_SALES_ORDER_NUMBER_PREFIX}{value:06d}"


# ---------------------------------------------------------------------------
# — sales-order financial side-effects, rewired native.
# CRM source: app/services/events/handlers/selfcare_customer.py (the
# push_sales_order_* + ensure_installation_invoice_for_sales_order pushers).
# ---------------------------------------------------------------------------


def _resolve_project_for_sales_order(db: Session, sales_order_id: object):
    """The active project a sales order spawned.

    The structural foreign key is authoritative.  Metadata lookup remains a
    bounded compatibility fallback for rows predating migration 389.
    """
    if not sales_order_id:
        return None
    existing = (
        db.query(Project)
        .filter(Project.sales_order_id == coerce_uuid(str(sales_order_id)))
        .filter(Project.is_active.is_(True))
        .one_or_none()
    )
    if existing:
        return existing
    existing = (
        db.query(Project)
        .filter(Project.is_active.is_(True))
        .filter(Project.metadata_["sales_order_id"].as_string() == str(sales_order_id))
        .order_by(Project.created_at.desc())
        .first()
    )
    if existing:
        return existing

    # SQLite JSON path comparisons are not reliable across SQLAlchemy/SQLite
    # builds. Fall back to an in-Python metadata check for tests/dev.
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "sqlite":
        rows = (
            db.query(Project)
            .filter(Project.is_active.is_(True))
            .order_by(Project.created_at.desc())
            .all()
        )
        for row in rows:
            metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
            if str(metadata.get("sales_order_id")) == str(sales_order_id):
                return row
    return None


def _has_existing_installation_invoice(project: Project) -> bool:
    metadata = project.metadata_ if isinstance(project.metadata_, dict) else {}
    return bool(str(metadata.get("selfcare_installation_invoice_id") or "").strip())


def _find_existing_related_installation_invoice(
    db: Session, project: Project
) -> tuple[str, Decimal | None] | None:
    metadata = project.metadata_ if isinstance(project.metadata_, dict) else {}
    sales_order_id = metadata.get("sales_order_id")
    quote_id = metadata.get("quote_id")
    if not sales_order_id and not quote_id:
        return None

    filters = []
    if sales_order_id:
        filters.append(
            Project.metadata_["sales_order_id"].as_string() == str(sales_order_id)
        )
    if quote_id:
        filters.append(Project.metadata_["quote_id"].as_string() == str(quote_id))

    rows = (
        db.query(Project)
        .filter(Project.id != project.id)
        .filter(or_(*filters))
        .order_by(Project.created_at.desc())
        .all()
    )
    for row in rows:
        row_meta = row.metadata_ if isinstance(row.metadata_, dict) else {}
        invoice_id = str(row_meta.get("selfcare_installation_invoice_id") or "").strip()
        if invoice_id:
            return invoice_id, _parse_invoice_amount(
                row_meta.get("selfcare_installation_invoice_amount")
            )

    # SQLite JSON-path fallback (idempotency in tests/dev).
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "sqlite":
        for row in db.query(Project).filter(Project.id != project.id).all():
            row_meta = row.metadata_ if isinstance(row.metadata_, dict) else {}
            same_sales_order = sales_order_id and str(
                row_meta.get("sales_order_id")
            ) == str(sales_order_id)
            same_quote = quote_id and str(row_meta.get("quote_id")) == str(quote_id)
            if not (same_sales_order or same_quote):
                continue
            invoice_id = str(
                row_meta.get("selfcare_installation_invoice_id") or ""
            ).strip()
            if invoice_id:
                return invoice_id, _parse_invoice_amount(
                    row_meta.get("selfcare_installation_invoice_amount")
                )
    return None


def link_sales_order_invoice(
    db: Session,
    *,
    sales_order_id: str | UUID,
    invoice_id: str | UUID,
    purpose: str = "installation",
) -> SalesOrderInvoiceLink | None:
    """Record structurally that an invoice was raised for a sales order.

    Sale-to-money is otherwise joined through ``Project.metadata_`` — a JSON
    string comparison with no foreign key, no uniqueness and no referential
    integrity — so settlement evidence cannot be attributed to the commercial
    order and nothing can derive its financial status.

    Idempotent: an invoice already linked is left alone, including when it was
    recovered by the backfill, so replaying an invoice attachment neither
    duplicates the row nor rewrites its provenance.
    """
    try:
        order_uuid = coerce_uuid(str(sales_order_id))
        invoice_uuid = coerce_uuid(str(invoice_id))
    except (TypeError, ValueError):
        return None

    existing = db.scalar(
        select(SalesOrderInvoiceLink).where(
            SalesOrderInvoiceLink.invoice_id == invoice_uuid
        )
    )
    if existing is not None:
        return existing

    sales_order = db.get(SalesOrder, order_uuid)
    if sales_order is None or sales_order.subscriber_id is None:
        # A dangling id is left for review rather than forced through a
        # RESTRICT foreign key mid-transaction.
        return None

    link = SalesOrderInvoiceLink(
        sales_order_id=sales_order.id,
        invoice_id=invoice_uuid,
        account_id=sales_order.subscriber_id,
        purpose=purpose,
        origin="native",
    )
    db.add(link)
    db.flush()
    return link


def _store_invoice_metadata(
    db: Session, project: Project, invoice_id: str, amount: Decimal | None
) -> None:
    """Record the installation invoice on the project and the sales order.

    Dual-write for the sale-to-money migration: the historical metadata keys
    remain the read path, and ``sales_order_invoice_links`` records the same
    fact structurally, so a parity check can compare the two before any read
    cutover.
    """
    # Metadata keys keep their historical names — they are local Fact now
    #: the ids point at sub's own invoice rows.
    metadata = dict(project.metadata_ or {})
    metadata["selfcare_installation_invoice_id"] = str(invoice_id)
    if amount is not None:
        metadata["selfcare_installation_invoice_amount"] = str(amount)
    metadata.pop("selfcare_installation_invoice_error", None)
    project.metadata_ = metadata

    # The order comes from ``projects.sales_order_id`` — a real FK with a
    # unique constraint — not from the metadata key beside it. ADR 0007 makes
    # metadata provenance only, and reading the identity out of it here would
    # build the structural link on exactly the join it exists to replace.
    if project.sales_order_id is not None:
        link_sales_order_invoice(
            db, sales_order_id=project.sales_order_id, invoice_id=invoice_id
        )


def _record_invoice_failure(project: Project, detail: str) -> None:
    metadata = dict(project.metadata_ or {})
    metadata["selfcare_installation_invoice_error"] = {
        "detail": detail[:500],
        "at": datetime.now(UTC).isoformat(),
    }
    project.metadata_ = metadata


def _parse_invoice_amount(value: object) -> Decimal | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _sum_installation_lines(lines) -> Decimal:
    total = Decimal("0.00")
    for line in lines:
        description = str(getattr(line, "description", "") or "").lower()
        if "installation" not in description:
            continue
        amount = Decimal(getattr(line, "amount", 0) or 0)
        if amount > 0:
            total += amount
    return total


def _installation_amount_from_sales_order(db: Session, sales_order_id) -> Decimal:
    if not sales_order_id:
        return Decimal("0.00")
    lines = (
        db.query(SalesOrderLine)
        .filter(SalesOrderLine.sales_order_id == coerce_uuid(str(sales_order_id)))
        .filter(SalesOrderLine.is_active.is_(True))
        .all()
    )
    return _sum_installation_lines(lines)


def _installation_amount_from_quote(db: Session, quote_id) -> Decimal:
    if not quote_id:
        return Decimal("0.00")
    lines = (
        db.query(QuoteLineItem)
        .filter(QuoteLineItem.quote_id == coerce_uuid(str(quote_id)))
        .all()
    )
    return _sum_installation_lines(lines)


def _resolve_installation_amount(db: Session, project: Project) -> Decimal:
    metadata = project.metadata_ if isinstance(project.metadata_, dict) else {}
    amount = _installation_amount_from_sales_order(
        db, project.sales_order_id or metadata.get("sales_order_id")
    )
    if amount > 0:
        return amount
    return _installation_amount_from_quote(
        db, project.quote_id or metadata.get("quote_id")
    )


def ensure_installation_invoice_for_sales_order(
    db: Session,
    sales_order_id,
    *,
    commit: bool = True,
) -> None:
    """Create the installation invoice for a sales order's project.

    Native rewire of the CRM's ``ensure_installation_invoice_for_sales_order``:
    ``selfcare.create_installation_invoice`` (HTTP → ``POST /crm/invoices``)
    becomes an in-process :func:`app.services.crm_api.create_installation_invoice`
    call. The ``external_ref="project:{project_id}"`` idempotency key is
    unchanged, and the project row-lock still serializes concurrent triggers
    of the read-then-create-then-store sequence.
    """
    if not sales_order_id:
        return
    sales_order = db.get(SalesOrder, coerce_uuid(str(sales_order_id)))
    if not sales_order:
        return

    project = _resolve_project_for_sales_order(db, sales_order_id)
    if not project:
        return

    locked = (
        db.query(Project)
        .filter(Project.id == project.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if locked is None:
        return
    project = locked
    if _has_existing_installation_invoice(project):
        return

    related_invoice = _find_existing_related_installation_invoice(db, project)
    if related_invoice:
        invoice_id, amount = related_invoice
        _store_invoice_metadata(db, project, invoice_id, amount)
        db.add(project)
        if commit:
            db.commit()
            db.refresh(project)
        else:
            db.flush()
        logger.info(
            "installation_invoice_reused project_id=%s invoice_id=%s",
            project.id,
            invoice_id,
        )
        return

    amount = _resolve_installation_amount(db, project)
    if amount <= 0:
        logger.info("invoice_skip_no_installation_cost project_id=%s", project.id)
        return

    subscriber_id = sales_order.subscriber_id or project.subscriber_id
    if not subscriber_id:
        return

    from app.services import crm_api

    try:
        invoice = crm_api.create_installation_invoice(
            db,
            subscriber_id=str(subscriber_id),
            amount=amount,
            description="Installation cost",
            external_ref=f"project:{project.id}",
            currency=sales_order.currency or "NGN",
            commit=commit,
        )
    except LookupError as exc:
        if not commit:
            raise
        # Record the failure so it surfaces and a later trigger (or operator)
        # can retry — the external_ref dedup makes the retry safe.
        _record_invoice_failure(project, str(exc))
        db.add(project)
        db.commit()
        logger.error(
            "installation_invoice_failed project_id=%s error=%s", project.id, exc
        )
        return
    if not invoice:
        return

    _store_invoice_metadata(db, project, str(invoice.id), amount)
    db.add(project)
    if commit:
        db.commit()
        db.refresh(project)
    else:
        db.flush()
    logger.info(
        "installation_invoice_created project_id=%s subscriber_id=%s "
        "invoice_id=%s amount=%s",
        project.id,
        subscriber_id,
        invoice.id,
        amount,
    )


def _line_offer_ref(line: object) -> str | None:
    """The sub CatalogOffer id/code a sales-order line was tagged with at
    quote time (metadata.sub_offer_id), identifying a recurring subscription
    charge vs a one-off installation line."""
    meta = getattr(line, "metadata_", None)
    if not isinstance(meta, dict):
        return None
    for key in ("sub_offer_id", "sub_offer_code", "offer_id", "offer_code"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    return None


def _line_billing_cycle(line: object) -> str | None:
    """The contracted billing cadence captured on a sales-order line at quote
    time (metadata.billing_cycle). SOT: the contract line is the fact-of-record
    for cadence; absent => the subscription inherits the offer price cadence."""
    meta = getattr(line, "metadata_", None)
    if not isinstance(meta, dict):
        return None
    for key in ("billing_cycle", "sub_billing_cycle", "billing_period"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    return None


def _line_add_on_ref(line: object) -> str | None:
    meta = getattr(line, "metadata_", None)
    if not isinstance(meta, dict):
        return None
    for key in ("sub_add_on_id", "add_on_id", "addon_id"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    return None


def _active_sales_order_lines(db: Session, sales_order_id) -> list[SalesOrderLine]:
    return (
        db.query(SalesOrderLine)
        .filter(SalesOrderLine.sales_order_id == sales_order_id)
        .filter(SalesOrderLine.is_active.is_(True))
        .all()
    )


def _resolve_activation_task_for_project(
    db: Session, project: Project | None
) -> ProjectTask | None:
    """Resolve the single named activation gate from the native project graph."""
    if project is None:
        return None
    from app.services import projects

    return projects.resolve_activation_gate_task(db, project.id)


def _ensure_provisioning_order_for_sales_line(
    db: Session,
    *,
    sales_order: SalesOrder,
    line: SalesOrderLine,
    subscription: Subscription,
) -> None:
    """Stage one idempotent provisioning order for a closed sale line."""
    if sales_order.status not in {
        SalesOrderStatus.paid.value,
        SalesOrderStatus.fulfilled.value,
    }:
        return

    from app.models.provisioning import (
        ServiceOrder,
        ServiceOrderStatus,
        ServiceOrderType,
    )
    from app.schemas.provisioning import ServiceOrderCreate
    from app.services.provisioning_managers import service_orders

    subscription_id = getattr(subscription, "id", None)
    persisted_subscription = (
        db.get(Subscription, subscription_id) if subscription_id else None
    )
    if persisted_subscription is None:
        return
    from app.services import sales_fulfillment

    scope = sales_fulfillment.ensure_implementation_scope(
        db,
        sales_order_id=sales_order.id,
        actor_id="sales.orders",
        commit=False,
    )
    activation_task = _resolve_activation_task_for_project(db, scope.project)
    idempotency_key = f"sales-order-line:{line.id}:new_install"
    existing = (
        db.query(ServiceOrder)
        .filter(
            (ServiceOrder.sales_order_line_id == line.id)
            | (ServiceOrder.idempotency_key == idempotency_key)
        )
        .first()
    )
    if existing is not None:
        if existing.project_id is None:
            existing.project_id = scope.project.id
        if existing.installation_project_id is None:
            existing.installation_project_id = scope.installation_project.id
        if existing.activation_project_task_id is None and activation_task is not None:
            existing.activation_project_task_id = activation_task.id
        db.flush()
        return
    execution_context = _build_staged_device_intent(
        db,
        sales_order=sales_order,
        line=line,
        subscription=persisted_subscription,
    )
    order = service_orders.create(
        db,
        ServiceOrderCreate(
            subscriber_id=sales_order.subscriber_id,
            subscription_id=persisted_subscription.id,
            sales_order_id=sales_order.id,
            sales_order_line_id=line.id,
            project_id=scope.project.id,
            installation_project_id=scope.installation_project.id,
            idempotency_key=idempotency_key,
            activation_project_task_id=(
                activation_task.id if activation_task is not None else None
            ),
            # Sales-linked work cannot enter provisioning until implementation
            # verification releases it through service_order_lifecycle.
            status=ServiceOrderStatus.draft,
            order_type=ServiceOrderType.new_install,
            notes=f"Provisioning for {sales_order.order_number or sales_order.id}",
            execution_context=execution_context,
        ),
        actor_id="sales.orders",
        commit=False,
    )
    metadata = dict(line.metadata_ or {})
    metadata["service_order_id"] = str(order.id)
    line.metadata_ = metadata
    db.add(line)
    db.flush()


def _build_staged_device_intent(
    db: Session,
    *,
    sales_order: SalesOrder,
    line: SalesOrderLine,
    subscription: Subscription,
) -> dict[str, object]:
    """Stage separate ONT and BNG intent without touching either control plane."""
    from app.models.catalog import AccessCredential, ConnectionType, SubscriptionAddOn
    from app.services.connection_type_provisioning import resolve_connection_type
    from app.services.ipv6_pd import pd_enabled, resolve_pd_pool
    from app.services.pppoe_credentials import auto_generate_pppoe_credential

    subscription_id = subscription.id
    subscriber_id = subscription.subscriber_id
    credential = (
        db.query(AccessCredential)
        .filter(
            AccessCredential.subscription_id == subscription_id,
            AccessCredential.is_active.is_(True),
        )
        .first()
    )
    if credential is None:
        credential = auto_generate_pppoe_credential(
            db,
            str(subscriber_id),
            radius_profile_id=(
                str(subscription.radius_profile_id)
                if getattr(subscription, "radius_profile_id", None)
                else None
            ),
            subscription_id=str(subscription_id),
        )
    if credential is not None and not str(getattr(subscription, "login", "") or ""):
        subscription.login = credential.username

    nas = getattr(subscription, "provisioning_nas_device", None)
    connection_type = resolve_connection_type(db, subscription, nas)
    wan_mode = {
        ConnectionType.pppoe: "pppoe",
        ConnectionType.dhcp: "dhcp",
        ConnectionType.ipoe: "dhcp",
        ConnectionType.static: "static_ip",
        ConnectionType.hotspot: "dhcp",
    }[connection_type]
    pd_pool = resolve_pd_pool(db, subscription) if pd_enabled() else None
    ip_protocol = "dual_stack" if pd_pool is not None else "ipv4"
    desired_config: dict[str, object] = {
        "wan.mode": wan_mode,
        "wan.ip_protocol": ip_protocol,
    }
    if connection_type == ConnectionType.pppoe and credential is not None:
        desired_config.update(
            {
                "wan.pppoe_username": credential.username,
                "wan.pppoe_password": credential.secret_hash,
            }
        )

    add_ons = []
    for link in (
        db.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == subscription_id)
        .all()
    ):
        add_on_type = getattr(getattr(link, "add_on", None), "addon_type", None)
        add_ons.append(
            {
                "subscription_add_on_id": str(link.id),
                "add_on_id": str(link.add_on_id),
                "quantity": int(link.quantity or 1),
                "type": add_on_type.value if add_on_type is not None else None,
            }
        )
    return {
        "source": "closed_sales_order",
        "sales_order_id": str(sales_order.id),
        "sales_order_line_id": str(line.id),
        "subscription_id": str(subscription_id),
        "subscriber_id": str(subscriber_id),
        "service_address_id": str(getattr(subscription, "service_address_id", "") or "")
        or None,
        "catalog_offer_id": str(subscription.offer_id),
        "device_intent": {
            "version": 1,
            "connection_type": connection_type.value,
            "desired_config": desired_config,
            "add_ons": add_ons,
        },
        # Subscriber addresses are BNG/RADIUS policy. The ONT intent only
        # enables the access method and, for dual stack, the DHCPv6-PD client.
        # It must never carry Framed-IP, delegated prefixes, or routed add-ons.
        "bng_intent": {
            "version": 1,
            "subscription_id": str(subscription_id),
            "connection_type": connection_type.value,
            "radius_username": credential.username if credential is not None else None,
            "ipv4": {
                "source": "ipam",
                "assignment_scope": "subscription",
                "nat_policy": "pool_defined",
            },
            "ipv6": {
                "source": "ipam",
                "assignment_scope": "subscription",
                "pd_enabled": pd_pool is not None,
                "pd_pool_id": str(pd_pool.id) if pd_pool is not None else None,
            },
            "additional_routes": {
                "source": "subscription_add_ons",
                "radius_attribute": "Framed-Route",
                "nat_policy": "no_nat",
            },
        },
    }


def _sync_sales_order_add_ons(
    db: Session,
    *,
    lines: list[SalesOrderLine],
    subscriptions: list[Subscription],
) -> None:
    """Attach explicitly sold add-ons to an unambiguous subscription."""
    from app.models.catalog import AddOn, SubscriptionAddOn
    from app.schemas.catalog import SubscriptionAddOnCreate
    from app.services.catalog import subscription_add_ons
    from app.services.web_catalog_subscriptions import (
        _route_range_options_for_ipam,
        normalize_additional_routes,
        sync_additional_routes_for_subscription,
    )

    persisted = {
        str(subscription.id): subscription
        for candidate in subscriptions
        if (subscription := db.get(Subscription, getattr(candidate, "id", None)))
        is not None
    }
    for line in lines:
        add_on_ref = _line_add_on_ref(line)
        if not add_on_ref:
            continue
        meta = line.metadata_ if isinstance(line.metadata_, dict) else {}
        target_id = str(meta.get("subscription_id") or "").strip()
        if not target_id and len(persisted) == 1:
            target_id = next(iter(persisted))
        subscription = persisted.get(target_id)
        try:
            add_on = db.get(AddOn, coerce_uuid(add_on_ref))
        except (TypeError, ValueError):
            add_on = None
        if subscription is None or add_on is None or not add_on.is_active:
            logger.warning(
                "sales_order_addon_unresolved line_id=%s addon=%s subscription=%s",
                line.id,
                add_on_ref,
                target_id or "ambiguous",
            )
            continue
        quantity = max(1, int(meta.get("quantity") or line.quantity or 1))
        existing = (
            db.query(SubscriptionAddOn)
            .filter(
                SubscriptionAddOn.subscription_id == subscription.id,
                SubscriptionAddOn.add_on_id == add_on.id,
                SubscriptionAddOn.end_at.is_(None),
            )
            .first()
        )
        if existing is None:
            existing = subscription_add_ons.create(
                db,
                SubscriptionAddOnCreate(
                    subscription_id=subscription.id,
                    add_on_id=add_on.id,
                    quantity=quantity,
                    start_at=datetime.now(UTC),
                ),
                commit=False,
            )
        else:
            existing.quantity = quantity

        if add_on.ip_is_public and add_on.ip_prefix_length:
            raw_cidrs = meta.get("additional_route_cidrs") or meta.get("cidrs") or []
            if isinstance(raw_cidrs, str):
                raw_cidrs = [raw_cidrs]
            cidrs = [str(value) for value in raw_cidrs if str(value).strip()]
            if not cidrs:
                options = _route_range_options_for_ipam(db)
                for option in options:
                    groups = option.get("children_by_prefix")
                    if not isinstance(groups, dict):
                        continue
                    children = groups.get(str(add_on.ip_prefix_length), [])
                    if not isinstance(children, list):
                        continue
                    for child in children:
                        cidrs.append(str(child["cidr"]))
                        if len(cidrs) >= quantity:
                            break
                    if len(cidrs) >= quantity:
                        break
            if len(cidrs) < quantity:
                raise ValueError(
                    f"No available /{add_on.ip_prefix_length} routed block for add-on"
                )
            normalized = normalize_additional_routes(cidrs[:quantity])
            sync_additional_routes_for_subscription(
                db,
                subscription_obj=subscription,
                cidrs=[item[0] for item in normalized],
                add_on_id=str(add_on.id),
                quantity=quantity,
                commit=False,
            )

        next_meta = dict(line.metadata_ or {})
        next_meta["subscription_id"] = str(subscription.id)
        next_meta["subscription_add_on_id"] = str(existing.id)
        line.metadata_ = next_meta
        db.add(line)
    db.flush()


def apply_funding_consequences(
    db: Session,
    *,
    sales_order_id: UUID | str,
    actor_id: str,
    record_order_payment: bool = True,
) -> str:
    """Apply the committed service consequences of a fully funded sale.

    Consumer side of ``sales_order.funding_satisfied``: one pending
    Subscription (plus its first invoice,
    ``external_ref="sales_order:{id}:subscription:{line_id}"``) and one draft
    ServiceOrder per offer-tagged line, add-on sync, and — when the producer
    recorded cash against the order itself — the order payment evidence.
    Line metadata keeps the resolved ids, so replays are exact no-ops.

    A consequence that cannot be applied raises so the event delivery stays
    failed and retryable; it is never downgraded to a warning log. The
    self-serve deposit path sets ``record_order_payment=False`` because its
    only ledger event is the verified deposit-invoice payment.
    """
    actor = str(actor_id or "").strip()
    if not actor:
        raise SalesOrderLifecycleError(
            "actor_required", "Funding-consequence actor is required", kind="invalid"
        )
    sales_order = db.scalars(
        select(SalesOrder)
        .where(SalesOrder.id == coerce_uuid(str(sales_order_id)))
        .with_for_update()
    ).one_or_none()
    if sales_order is None:
        raise SalesOrderLifecycleError(
            "sales_order_not_found", "Sales order not found", kind="not_found"
        )
    if (
        not sales_order.is_active
        or sales_order.status == SalesOrderStatus.cancelled.value
    ):
        return "skipped_inactive"
    if sales_order.payment_status != _PAID:
        # Stale delivery after an owner correction; the corrected order emits
        # its own compensating outputs.
        return "skipped_not_funded"
    if not sales_order.subscriber_id:
        raise SalesOrderLifecycleError(
            "subscriber_required",
            "Funded sales order has no subscriber",
            kind="invalid",
        )

    from app.services import crm_api

    lines = _active_sales_order_lines(db, sales_order.id)
    offer_lines = [(line, ref) for line in lines if (ref := _line_offer_ref(line))]
    staged_subscriptions: list[tuple[SalesOrderLine, Subscription]] = []
    for line, offer_ref in offer_lines:
        meta = line.metadata_ if isinstance(line.metadata_, dict) else {}
        existing_subscription_id = str(
            meta.get("selfcare_subscription_id") or ""
        ).strip()
        subscription = None
        invoice = None
        if existing_subscription_id:
            subscription = db.get(Subscription, coerce_uuid(existing_subscription_id))
        else:
            try:
                result = crm_api.create_subscription(
                    db,
                    subscriber_id=str(sales_order.subscriber_id),
                    offer_ref=offer_ref,
                    external_ref=f"sales_order:{sales_order.id}:subscription:{line.id}",
                    unit_price=line.unit_price,
                    service_address_id=meta.get("service_address_id"),
                    billing_cycle=_line_billing_cycle(line),
                    commit=False,
                )
            except LookupError as exc:
                raise SalesOrderLifecycleError(
                    "funding_consequence_unresolved",
                    f"Sales-order line {line.id} offer {offer_ref!r} does not "
                    "resolve to a catalog offer",
                    kind="invalid",
                ) from exc
            subscription = result.get("subscription") if result else None
            invoice = result.get("invoice") if result else None
        if subscription is None:
            raise SalesOrderLifecycleError(
                "funding_consequence_unresolved",
                f"Sales-order line {line.id} has no resolvable subscription",
                kind="invalid",
            )
        new_meta = dict(line.metadata_ or {})
        new_meta["selfcare_subscription_id"] = str(subscription.id)
        if invoice is not None:
            new_meta["selfcare_subscription_invoice_id"] = str(invoice.id)
        line.metadata_ = new_meta
        db.add(line)
        logger.info(
            "sales_order_subscription_created sales_order_id=%s line_id=%s "
            "subscription_id=%s",
            sales_order.id,
            line.id,
            subscription.id,
        )
        staged_subscriptions.append((line, subscription))
    if staged_subscriptions:
        _sync_sales_order_add_ons(
            db,
            lines=lines,
            subscriptions=[item[1] for item in staged_subscriptions],
        )
        for line, subscription in staged_subscriptions:
            _ensure_provisioning_order_for_sales_line(
                db,
                sales_order=sales_order,
                line=line,
                subscription=subscription,
            )
    if record_order_payment:
        _record_order_payment_evidence(db, sales_order, commit=False)
    db.flush()
    return "applied"


def _record_order_payment_evidence(
    db: Session,
    sales_order: SalesOrder,
    *,
    commit: bool = True,
) -> None:
    """Record the customer's payment against their account.

    Native rewire of ``push_sales_order_payment_to_selfcare``:
    ``selfcare.record_payment`` (HTTP → ``POST /crm/payments``) becomes an
    in-process :func:`app.services.crm_api.record_external_payment` call
    keyed on the unchanged ``external_ref="sales_order:{id}:payment"``.

    The payment is charged to the account, not pinned to one invoice — the
    ledger auto-allocates it across open invoices (installation + the
    subscription's first invoice) oldest/soonest-due first, so a single
    upfront payment settles whatever the sale covered. Idempotent (the
    external_ref dedups in the ledger); failures propagate to the caller.
    """
    from app.services import crm_api

    amount_paid = sales_order.amount_paid
    if amount_paid is None or Decimal(str(amount_paid)) <= 0:
        return
    sales_order_id = sales_order.id
    if not sales_order_id or not sales_order.subscriber_id:
        return

    # Ensure the installation invoice exists so the payment has something
    # to settle. On the funded path the subscription's first invoice is
    # created by the funding consumer before this, so a single payment can
    # settle both.
    ensure_installation_invoice_for_sales_order(
        db,
        sales_order_id,
        commit=commit,
    )

    crm_api.record_external_payment(
        db,
        subscriber_id=str(sales_order.subscriber_id),
        amount=amount_paid,
        external_ref=f"sales_order:{sales_order_id}:payment",
        paid_at=sales_order.paid_at,
        memo=f"Sales order {sales_order.order_number or sales_order_id}",
        currency=sales_order.currency or "NGN",
        commit=commit,
    )


def _record_sales_order_payment(db: Session, sales_order: SalesOrder) -> None:
    """Best-effort partial-payment evidence recording (legacy direct path)."""
    try:
        _record_order_payment_evidence(db, sales_order)
    except Exception:
        logger.warning(
            "sales_order_payment_record_failed sales_order_id=%s",
            getattr(sales_order, "id", None),
            exc_info=True,
        )
        db.rollback()


def _sync_sales_order_financials(db: Session, sales_order: SalesOrder) -> None:
    """Record partial-payment financial evidence directly.

    A partial receipt is financial evidence only. It must not create a
    service contract or provisioning order before the sale is fully funded.
    The fully funded consequences (subscription, provisioning order, order
    payment evidence) belong to the ``sales_order.funding_satisfied``
    consumer, delivered durably by the event dispatcher — never to this
    in-request best-effort path.
    """
    if sales_order.payment_status != _PARTIAL:
        return
    _record_sales_order_payment(db, sales_order)


#: Fields whose value asserts that money was received. Writing one of these
#: declares coverage, and coverage is what ``stage_funding_transition`` turns
#: into subscriptions and provisioning. They are therefore NOT operator input:
#: a generic order edit that could set them would let anyone with ordinary
#: sales-order write permission manufacture funding.
#:
#: ``total`` is deliberately absent. Changing what an order is worth is a real
#: sales edit, and coverage stays DERIVED from it and from recorded receipts.
FUNDING_CONTROLLED_FIELDS: frozenset[str] = frozenset(
    {"payment_status", "amount_paid", "paid_at"}
)


class FundingAuthority(str, Enum):
    """Why a caller is permitted to assert coverage on a sales order.

    A member of this enum is evidence that money was independently confirmed.
    There is deliberately no ``operator`` member: an operator's assertion that
    an order is paid is not settlement evidence, and the only way to fund an
    order is to record the receipt through the owner that saw it.
    """

    #: A payment accepted and recorded by the billing settlement owner.
    settlement = "settlement"
    #: Verified deposit evidence returned by quote acceptance.
    deposit_verification = "deposit_verification"
    #: Exact obligation resolution through :mod:`app.services.sales_order_funding`.
    funding_gate = "funding_gate"
    #: Derivation from the order's own lines — never a new assertion of money.
    derived_recalculation = "derived_recalculation"


def assert_funding_authority(
    data: dict[str, Any], *, funding_authority: FundingAuthority | None
) -> None:
    """Refuse operator-supplied coverage fields.

    Raises 422 naming the offending field rather than silently dropping it —
    a dropped field would let a caller believe it had recorded a payment.

    ``funding_authority`` must be an actual :class:`FundingAuthority` member.
    A truthy value of any other type is a programming error and is refused
    rather than honoured: the whole point of the parameter is that it cannot
    be satisfied by a generic boolean, a request-derived string, or a
    ``True`` that leaked in from a caller's own flag.
    """
    if funding_authority is not None:
        if not isinstance(funding_authority, FundingAuthority):
            raise TypeError(
                "funding_authority must be a FundingAuthority member, got "
                f"{type(funding_authority).__name__!r}. Coverage authority is "
                "not a boolean and not a request value."
            )
        return
    offending = sorted(FUNDING_CONTROLLED_FIELDS & set(data))
    if not offending:
        return
    raise HTTPException(
        status_code=422,
        detail=(
            "Funding fields cannot be set through a sales-order edit: "
            f"{', '.join(offending)}. Coverage is derived from recorded "
            "settlement evidence. Record the payment through the billing "
            "settlement owner, or resolve the order's funding obligations "
            "through the funding gate."
        ),
    )


#: Fields that change what the customer was sold or what it is worth. While an
#: order carries an active waiver these are frozen: the waiver recorded an exact
#: amount as not-pursued, and re-pricing underneath it would silently change
#: what was forgiven without any new decision being taken.
COMMERCIAL_FIELDS: frozenset[str] = frozenset(
    {"subtotal", "tax_total", "total", "discount_type", "discount_value"}
)

#: The same rule at line level.
COMMERCIAL_LINE_FIELDS: frozenset[str] = frozenset(
    {"description", "quantity", "unit_price", "amount", "inventory_item_id"}
)


def assert_no_active_waiver(
    db: Session, sales_order_id, data: dict[str, Any], fields: frozenset[str]
) -> None:
    """Refuse a commercial change while a waiver is active.

    Revoke the waiver first — that is a recorded decision with an accountable
    actor, which is exactly what re-pricing a waived order should require.
    """
    offending = sorted(fields & set(data))
    if not offending:
        return
    from app.services.sales_order_waiver import active_waiver

    if active_waiver(db, coerce_uuid(sales_order_id)) is None:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            "This order has an active waiver, so its commercial terms are "
            f"frozen: {', '.join(offending)}. Revoke the waiver first — the "
            "waiver recorded an exact amount as not-pursued, and re-pricing "
            "underneath it would change what was forgiven with no new decision."
        ),
    )


def stage_funding_transition(
    db: Session,
    sales_order: SalesOrder,
    *,
    previous_payment_status: str | None,
    record_order_payment: bool = True,
) -> bool:
    """Stage the durable funding outputs on the pending/partial→paid edge.

    Must run inside the transaction that commits the payment transition so
    the authoritative state change and its outbox events commit atomically.
    ``sales_order.funding_satisfied`` drives the service consequences through
    the registered lifecycle projection handler; ``sales_order.paid`` remains
    the notification/webhook fact.
    """
    if sales_order.payment_status != _PAID or previous_payment_status == _PAID:
        return False
    emit_event(
        db,
        EventType.sales_order_funding_satisfied,
        {
            "sales_order_id": str(sales_order.id),
            "order_number": sales_order.order_number,
            "total": str(sales_order.total or 0),
            "amount_paid": str(sales_order.amount_paid or 0),
            "currency": sales_order.currency,
            "from_payment_status": previous_payment_status,
            "to_payment_status": _PAID,
            "record_order_payment": record_order_payment,
        },
        subscriber_id=sales_order.subscriber_id,
    )
    emit_event(
        db,
        EventType.sales_order_paid,
        {
            "sales_order_id": str(sales_order.id),
            "order_number": sales_order.order_number,
            "total": str(sales_order.total or 0),
            "amount_paid": str(sales_order.amount_paid or 0),
            "currency": sales_order.currency,
        },
        subscriber_id=sales_order.subscriber_id,
    )
    return True


def _apply_payment_fields(sales_order: SalesOrder, data: dict) -> None:
    if "amount_paid" in data or "total" in data:
        total = Decimal(data.get("total") or sales_order.total or 0)
        amount_paid = Decimal(data.get("amount_paid") or sales_order.amount_paid or 0)
        balance_due = round_money(total - amount_paid)
        sales_order.total = round_money(total)
        sales_order.amount_paid = round_money(amount_paid)
        sales_order.balance_due = balance_due
        if total > 0 and balance_due <= 0:
            sales_order.payment_status = _PAID
            if not sales_order.paid_at:
                sales_order.paid_at = datetime.now(UTC)
        elif amount_paid > 0:
            sales_order.payment_status = _PARTIAL
        else:
            sales_order.payment_status = _PENDING
    if sales_order.payment_status == _PAID:
        if sales_order.status in {
            SalesOrderStatus.draft.value,
            SalesOrderStatus.confirmed.value,
        }:
            sales_order.status = SalesOrderStatus.paid.value
    elif (
        sales_order.payment_status == _WAIVED
        and sales_order.status == SalesOrderStatus.draft.value
    ):
        sales_order.status = SalesOrderStatus.confirmed.value


def _recalculate_order_totals(db: Session, sales_order_id: str) -> None:
    sales_order = db.get(SalesOrder, coerce_uuid(sales_order_id))
    if not sales_order:
        return
    previous_payment_status = sales_order.payment_status
    totals = (
        db.query(func.coalesce(func.sum(SalesOrderLine.amount), 0))
        .filter(SalesOrderLine.sales_order_id == sales_order.id)
        .filter(SalesOrderLine.is_active.is_(True))
        .scalar()
    )
    subtotal = round_money(Decimal(totals or 0))
    sales_order.subtotal = subtotal
    sales_order.total = round_money(subtotal + Decimal(sales_order.tax_total or 0))
    _apply_payment_fields(sales_order, {"total": sales_order.total})
    stage_funding_transition(
        db, sales_order, previous_payment_status=previous_payment_status
    )
    db.flush()


def _ensure_fulfillment(db: Session, sales_order: SalesOrder) -> None:
    """Ensure one structural implementation scope for the SalesOrder."""
    if (
        not sales_order.is_active
        or sales_order.status == SalesOrderStatus.cancelled.value
    ):
        return
    from app.services import sales_fulfillment

    sales_fulfillment.ensure_implementation_scope(
        db,
        sales_order_id=sales_order.id,
        actor_id="sales.orders",
        commit=False,
    )


def _accrue_reseller_commission(db: Session, sales_order: SalesOrder | None) -> None:
    """Stub until the native referral capability owns reseller commissions.

    The call sites remain so the owner can implement the side effect without
    changing sales-order orchestration; a commission hiccup
    must never break sales-order processing either way.
    """
    logger.debug(
        "reseller_commission_accrual_deferred sales_order_id=%s",
        getattr(sales_order, "id", None),
    )
    return None


def _ensure_project_for_manual_sales_order(db: Session, sales_order: SalesOrder):
    """Compatibility wrapper around the canonical fulfillment coordinator."""
    if sales_order.quote_id:
        return sales_order.project
    _ensure_fulfillment(db, sales_order)
    return sales_order.project


class SalesOrders(ListResponseMixin):
    @staticmethod
    def create(
        db: Session, payload, *, funding_authority: FundingAuthority | None = None
    ):
        data = payload.model_dump()
        # A create carries every field, so only an EXPLICITLY-SET funding field
        # counts as an assertion — a schema default of pending/0 is not one.
        # ``SalesOrderPaymentStatus`` is a plain Enum, so unwrap before
        # comparing: the member is not equal to its own string value.
        assert_funding_authority(
            {
                key: value
                for key, value in data.items()
                if key in FUNDING_CONTROLLED_FIELDS
                and key in payload.model_fields_set
                and getattr(value, "value", value)
                not in (None, _PENDING, Decimal("0.00"))
            },
            funding_authority=funding_authority,
        )
        if data.get("status"):
            data["status"] = _enum_str(data["status"], SalesOrderStatus, "status")
        if data.get("payment_status"):
            data["payment_status"] = _enum_str(
                data["payment_status"], SalesOrderPaymentStatus, "payment_status"
            )
        total_value = Decimal(data.get("total") or 0)
        amount_paid_value = Decimal(data.get("amount_paid") or 0)

        subscriber = _ensure_subscriber(db, data.get("subscriber_id"))
        if data.get("quote_id"):
            quote = db.get(
                Quote, coerce_uuid(data["quote_id"]), options=[selectinload(Quote.lead)]
            )
            if not quote:
                raise HTTPException(status_code=404, detail="Quote not found")
            if quote.status != QuoteStatus.accepted.value:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "A Quote-linked Sales Order is created only by Quote acceptance"
                    ),
                )
            _validate_quote_subscriber_context(db, quote=quote, subscriber=subscriber)
            existing = (
                db.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).first()
            )
            if existing:
                raise HTTPException(
                    status_code=400, detail="Sales order already exists for this quote"
                )
            if quote.lead:
                data["owner_agent_id"] = (
                    data.get("owner_agent_id") or quote.lead.owner_agent_id
                )
                data["source"] = data.get("source") or quote.lead.lead_source

        if not data.get("order_number"):
            data["order_number"] = _generate_order_number(db)

        if data.get("total") is not None and data.get("balance_due") is None:
            data["amount_paid"] = round_money(amount_paid_value)
            data["balance_due"] = round_money(total_value - amount_paid_value)

        sales_order = SalesOrder(**data)
        _apply_payment_fields(sales_order, data)
        db.add(sales_order)
        db.flush()
        stage_funding_transition(db, sales_order, previous_payment_status=None)
        db.commit()
        db.refresh(sales_order)
        _ensure_fulfillment(db, sales_order)
        _ensure_project_for_manual_sales_order(db, sales_order)
        db.commit()
        db.refresh(sales_order)
        _accrue_reseller_commission(db, sales_order)
        _sync_sales_order_financials(db, sales_order)
        return sales_order

    @staticmethod
    def _stage_from_quote_acceptance(
        db: Session, *, quote: Quote, subscriber_id: UUID
    ) -> SalesOrder:
        """Stage the unique SalesOrder and copied lines without committing."""

        existing = db.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).first()
        if existing:
            if existing.subscriber_id != subscriber_id:
                raise SalesOrderLifecycleError(
                    "quote_order_subscriber_mismatch",
                    "Existing SalesOrder does not match the accepted Quote account",
                )
            return existing

        subscriber = _ensure_subscriber(db, subscriber_id)
        _validate_quote_subscriber_context(db, quote=quote, subscriber=subscriber)
        sales_order = SalesOrder(
            quote_id=quote.id,
            subscriber_id=subscriber_id,
            owner_agent_id=quote.lead.owner_agent_id if quote.lead else None,
            source=quote.lead.lead_source if quote.lead else None,
            order_number=_generate_order_number(db),
            status=SalesOrderStatus.confirmed.value,
            payment_status=SalesOrderPaymentStatus.pending.value,
            currency=quote.currency,
            subtotal=quote.subtotal,
            discount_type=quote.discount_type,
            discount_value=quote.discount_value,
            discount_amount=quote.discount_amount,
            tax_total=quote.tax_total,
            total=quote.total,
            amount_paid=Decimal("0.00"),
            balance_due=quote.total,
        )
        db.add(sales_order)
        db.flush()
        for item in quote.line_items:
            amount = item.amount
            if amount is None:
                amount = Decimal(item.quantity or 0) * Decimal(item.unit_price or 0)
            db.add(
                SalesOrderLine(
                    sales_order_id=sales_order.id,
                    inventory_item_id=item.inventory_item_id,
                    description=item.description,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    amount=amount,
                    metadata_=dict(item.metadata_) if item.metadata_ else None,
                )
            )
        db.flush()
        return sales_order

    @staticmethod
    def create_from_quote(
        db: Session, quote_id: str, *, commit: bool = True
    ) -> SalesOrder:
        quote = db.get(
            Quote,
            coerce_uuid(quote_id),
            options=[selectinload(Quote.line_items), selectinload(Quote.lead)],
        )
        if not quote:
            raise HTTPException(status_code=404, detail="Quote not found")
        if quote.status != QuoteStatus.accepted.value:
            raise SalesOrderLifecycleError(
                "quote_acceptance_required",
                "A Sales Order is created only by accepting its Quote",
            )
        if quote.subscriber_id is None:
            raise SalesOrderLifecycleError(
                "quote_subscriber_required",
                "Only an accepted Quote with a converted account can create an order",
            )
        existing = db.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).first()
        if existing is None:
            raise SalesOrderLifecycleError(
                "quote_acceptance_repair_required",
                "Replay Quote acceptance to repair its missing Sales Order",
            )
        return existing

    @staticmethod
    def get(db: Session, sales_order_id: str):
        sales_order = db.get(
            SalesOrder,
            coerce_uuid(sales_order_id),
            options=[selectinload(SalesOrder.lines)],
        )
        if not sales_order:
            raise HTTPException(status_code=404, detail="Sales order not found")
        return sales_order

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None = None,
        quote_id: str | None = None,
        status: str | None = None,
        payment_status: str | None = None,
        is_active: bool | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ):
        query = db.query(SalesOrder)
        if subscriber_id:
            query = query.filter(SalesOrder.subscriber_id == coerce_uuid(subscriber_id))
        if quote_id:
            query = query.filter(SalesOrder.quote_id == coerce_uuid(quote_id))
        if status:
            query = query.filter(
                SalesOrder.status == _enum_str(status, SalesOrderStatus, "status")
            )
        if payment_status:
            query = query.filter(
                SalesOrder.payment_status
                == _enum_str(payment_status, SalesOrderPaymentStatus, "payment_status")
            )
        if is_active is None:
            query = query.filter(SalesOrder.is_active.is_(True))
        else:
            query = query.filter(SalesOrder.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SalesOrder.created_at, "updated_at": SalesOrder.updated_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(
        db: Session,
        sales_order_id: str,
        payload,
        *,
        funding_authority: FundingAuthority | None = None,
    ):
        sales_order = db.get(SalesOrder, coerce_uuid(sales_order_id))
        if not sales_order:
            raise HTTPException(status_code=404, detail="Sales order not found")
        previous_payment_status = sales_order.payment_status
        data = payload.model_dump(exclude_unset=True)
        assert_funding_authority(data, funding_authority=funding_authority)
        assert_no_active_waiver(db, sales_order_id, data, COMMERCIAL_FIELDS)
        if "status" in data:
            data["status"] = _enum_str(data["status"], SalesOrderStatus, "status")
        if "payment_status" in data:
            data["payment_status"] = _enum_str(
                data["payment_status"], SalesOrderPaymentStatus, "payment_status"
            )
        prospective_subscriber_id = (
            data["subscriber_id"]
            if "subscriber_id" in data
            else sales_order.subscriber_id
        )
        if prospective_subscriber_id is None:
            raise HTTPException(status_code=400, detail="subscriber_id is required")
        subscriber = _ensure_subscriber(db, prospective_subscriber_id)
        prospective_quote_id = (
            data["quote_id"] if "quote_id" in data else sales_order.quote_id
        )
        quote = None
        if prospective_quote_id is not None:
            quote = db.get(
                Quote,
                coerce_uuid(prospective_quote_id),
                options=[selectinload(Quote.lead)],
            )
            if not quote:
                raise HTTPException(status_code=404, detail="Quote not found")
            _validate_quote_subscriber_context(db, quote=quote, subscriber=subscriber)
        if "quote_id" in data and quote is not None:
            existing = (
                db.query(SalesOrder)
                .filter(
                    SalesOrder.quote_id == quote.id, SalesOrder.id != sales_order.id
                )
                .first()
            )
            if existing:
                raise HTTPException(
                    status_code=400, detail="Sales order already exists for this quote"
                )
            if quote.lead:
                data["owner_agent_id"] = (
                    data.get("owner_agent_id")
                    or sales_order.owner_agent_id
                    or quote.lead.owner_agent_id
                )
                data["source"] = (
                    data.get("source") or sales_order.source or quote.lead.lead_source
                )

        if data.get("payment_status") == _PAID:
            resolved_total = Decimal(data.get("total") or sales_order.total or 0)
            resolved_amount_paid = Decimal(
                data.get("amount_paid") or sales_order.amount_paid or 0
            )
            if resolved_amount_paid < resolved_total:
                data["amount_paid"] = round_money(resolved_total)
            data["balance_due"] = Decimal("0.00")
            if "paid_at" not in data or data.get("paid_at") is None:
                data["paid_at"] = datetime.now(UTC)
            if "status" not in data and sales_order.status in {
                SalesOrderStatus.draft.value,
                SalesOrderStatus.confirmed.value,
            }:
                data["status"] = SalesOrderStatus.paid.value

        for key, value in data.items():
            setattr(sales_order, key, value)

        _apply_payment_fields(sales_order, data)
        _ensure_fulfillment(db, sales_order)
        stage_funding_transition(
            db, sales_order, previous_payment_status=previous_payment_status
        )
        db.commit()
        db.refresh(sales_order)
        # Accrue on any transition into paid (idempotent). Covers
        # update_from_input too.
        _accrue_reseller_commission(db, sales_order)
        _sync_sales_order_financials(db, sales_order)
        return sales_order

    @staticmethod
    def update_from_input(
        db: Session,
        sales_order_id: str,
        *,
        status: str | None = None,
        total: str | None = None,
        notes: str | None = None,
        owner_agent_id: str | None = None,
        source: str | None = None,
        **rejected: Any,
    ):
        """Update a sales order using raw string inputs (e.g. web forms).

        This is the operator surface, so it carries no funding fields at all.
        ``**rejected`` exists so a caller still passing ``payment_status``,
        ``amount_paid`` or ``paid_at`` fails loudly with the same 422 as the
        typed path, instead of a ``TypeError`` or — worse — a silent drop.
        """
        assert_funding_authority(rejected, funding_authority=None)
        if rejected:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown sales-order fields: {', '.join(sorted(rejected))}",
            )
        update_data: dict[str, Any] = {}
        if status:
            update_data["status"] = validate_enum(status, SalesOrderStatus, "status")

        total_value = _parse_decimal(total)
        if total_value is not None:
            update_data["total"] = total_value

        if notes is not None:
            update_data["notes"] = notes.strip() or None
        if owner_agent_id is not None:
            update_data["owner_agent_id"] = (
                coerce_uuid(owner_agent_id) if owner_agent_id.strip() else None
            )
        if source is not None:
            update_data["source"] = source.strip() or None

        from app.schemas.sales_order import SalesOrderUpdate

        payload = SalesOrderUpdate(**update_data)
        return SalesOrders.update(db, sales_order_id, payload)

    @staticmethod
    def delete(db: Session, sales_order_id: str):
        sales_order = db.get(SalesOrder, coerce_uuid(sales_order_id))
        if not sales_order:
            raise HTTPException(status_code=404, detail="Sales order not found")
        sales_order.is_active = False
        db.commit()


class SalesOrderLines(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload):
        sales_order = db.get(SalesOrder, payload.sales_order_id)
        if not sales_order:
            raise HTTPException(status_code=404, detail="Sales order not found")
        data = payload.model_dump()
        # Adding a line changes what the order is worth, so it is a commercial
        # mutation even though no existing line moves.
        assert_no_active_waiver(
            db, sales_order.id, {"amount": data.get("amount")}, COMMERCIAL_LINE_FIELDS
        )
        if not data.get("amount"):
            data["amount"] = Decimal(data.get("quantity") or 0) * Decimal(
                data.get("unit_price") or 0
            )
        line = SalesOrderLine(**data)
        db.add(line)
        db.flush()
        _recalculate_order_totals(db, str(sales_order.id))
        db.commit()
        db.refresh(line)
        ensure_installation_invoice_for_sales_order(db, sales_order.id)
        db.refresh(sales_order)
        _accrue_reseller_commission(db, sales_order)
        return line

    @staticmethod
    def update(db: Session, line_id: str, payload):
        line = db.get(SalesOrderLine, coerce_uuid(line_id))
        if not line:
            raise HTTPException(status_code=404, detail="Sales order line not found")
        data = payload.model_dump(exclude_unset=True)
        assert_no_active_waiver(db, line.sales_order_id, data, COMMERCIAL_LINE_FIELDS)
        for key, value in data.items():
            setattr(line, key, value)
        if "quantity" in data or "unit_price" in data:
            line.amount = Decimal(line.quantity or 0) * Decimal(line.unit_price or 0)
        db.flush()
        _recalculate_order_totals(db, str(line.sales_order_id))
        db.commit()
        db.refresh(line)
        ensure_installation_invoice_for_sales_order(db, line.sales_order_id)
        sales_order = db.get(SalesOrder, line.sales_order_id)
        _accrue_reseller_commission(db, sales_order)
        return line

    @staticmethod
    def list(
        db: Session,
        sales_order_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SalesOrderLine)
        if sales_order_id:
            query = query.filter(
                SalesOrderLine.sales_order_id == coerce_uuid(sales_order_id)
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SalesOrderLine.created_at},
        )
        return apply_pagination(query, limit, offset).all()


sales_orders = SalesOrders()
sales_order_lines = SalesOrderLines()
