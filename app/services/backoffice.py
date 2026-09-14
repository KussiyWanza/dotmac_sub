"""Sub-local anti-corruption boundary for replaceable back-office systems.

Sub domain owners call this module using Sub business concepts. Provider
selection and provider-specific imports stay here, so replacing Dotmac ERP with
Zoho (or another back-office product) does not change Sub domain services.

This is not an enterprise-wide capability or shared runtime service. It is a
local outbound port owned and deployed by Sub.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.integrations.diagnostics import (
    DELIVERY_DIAGNOSTIC_KEY,
    OperationDiagnostic,
    parse_diagnostic_evidence,
)

if TYPE_CHECKING:
    from app.models.field_erp_sync import FieldErpSyncEvent
    from app.models.field_expense import FieldExpenseRequest
    from app.models.field_material import FieldMaterialRequest

logger = logging.getLogger(__name__)


class BackofficeUnavailableError(RuntimeError):
    """The configured local back-office adapter cannot serve the request."""


class BackofficeEnqueueStatus(str, Enum):
    ENQUEUED = "enqueued"
    NOT_OWNED = "not_owned"
    NOT_ENQUEUED = "not_enqueued"


class BackofficeDeliveryStatus(str, Enum):
    """Provider-neutral lifecycle of one durable back-office delivery."""

    PENDING = "pending"
    SENT = "sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class BackofficeEnqueueResult:
    """Outcome of asking the configured local adapter to stage delivery."""

    status: BackofficeEnqueueStatus
    provider: str | None = None
    event: FieldErpSyncEvent | None = None

    @property
    def requires_attention(self) -> bool:
        return self.status is not BackofficeEnqueueStatus.ENQUEUED


@dataclass(frozen=True, slots=True)
class BackofficeDeliveryView:
    """Provider-neutral projection of one durable outbound delivery."""

    flow_owner: str
    sub_owns_delivery: bool
    event_id: UUID | None
    event_status: str | None
    attempts: int
    last_error: str | None
    queued_at: datetime | None
    updated_at: datetime | None
    sent_at: datetime | None
    diagnostic: OperationDiagnostic | None


def _delivery_diagnostic(event: FieldErpSyncEvent | None) -> OperationDiagnostic | None:
    if event is None or not isinstance(event.erp_response, dict):
        return None
    return parse_diagnostic_evidence(event.erp_response.get(DELIVERY_DIAGNOSTIC_KEY))


@dataclass(frozen=True, slots=True)
class BackofficeExpensePaymentView:
    """Provider-neutral payment state projected on an expense request."""

    status: str | None
    intent_id: str | None
    command_id: str | None
    error: str | None
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class BackofficeExpenseRecoveryStaging:
    """Provider-neutral evidence for one append-only delivery replacement."""

    original_event_id: UUID
    replacement_event_id: UUID
    replacement_idempotency_key: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class BackofficeExpensePaymentRecoveryStaging:
    """Provider-neutral evidence for requeueing one existing payment event."""

    event_id: UUID
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class BackofficeExpensePaymentRecoveryView:
    """Provider-neutral evidence for one payment-delivery recovery check."""

    event_id: UUID
    expense_request_id: UUID
    idempotency_key: str
    status: BackofficeDeliveryStatus


def expense_payment_projection(
    request: FieldExpenseRequest,
) -> BackofficeExpensePaymentView:
    raw = dict((request.metadata_ or {}).get("erp_payment") or {})
    return BackofficeExpensePaymentView(
        status=str(raw["status"]) if raw.get("status") else None,
        intent_id=str(raw["intent_id"]) if raw.get("intent_id") else None,
        command_id=str(raw["command_id"]) if raw.get("command_id") else None,
        error=str(raw["error"]) if raw.get("error") else None,
        updated_at=str(raw["updated_at"]) if raw.get("updated_at") else None,
    )


@dataclass(frozen=True, slots=True)
class ExpenseCategoryView:
    """Provider-neutral ERP expense category exposed to field workflows."""

    category_code: str
    category_name: str
    requires_receipt: bool
    max_amount_per_claim: Decimal | None


class BackofficeGateway(Protocol):
    """Read-only capabilities currently consumed outside connector code."""

    def __enter__(self) -> BackofficeGateway: ...

    def __exit__(self, *args: object) -> None: ...

    def list_inventory_warehouses(self) -> list[dict]: ...

    def list_inventory(
        self,
        *,
        search: str | None = None,
        category_code: str | None = None,
        warehouse_id: str | None = None,
        include_zero_stock: bool = False,
        only_below_reorder: bool = False,
        only_with_available_serials: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict: ...

    def get_inventory_item(self, item_id: str) -> dict | None: ...

    def list_inventory_categories(self) -> list[dict]: ...

    def get_expense_categories(self) -> tuple[ExpenseCategoryView, ...]: ...

    def list_available_serials(
        self,
        *,
        item_code: str,
        warehouse_code: str,
        limit: int = 100,
        offset: int = 0,
    ) -> dict: ...

    def get_ncc_financials(
        self,
        *,
        year: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        as_of_date: str | None = None,
    ) -> dict: ...

    def get_ncc_staff_headcount(self) -> dict: ...


def _flow_owned_by_sub(db: Session, flow: str) -> bool:
    # The ownership table controls which originator may enqueue each migrated
    # flow. It does not confer authority on any back-office provider.
    from app.models.field_erp_sync import flow_owned_by_sub

    return flow_owned_by_sub(db, flow)


def external_material_fulfilment_active(db: Session) -> bool:
    """Return whether local issue/fulfil compatibility transitions are retired."""
    return _flow_owned_by_sub(db, "material_request")


def get_material_request_delivery(
    db: Session, request_id: UUID
) -> BackofficeDeliveryView:
    """Read material-request delivery state through the local back-office port."""
    from app.models.field_erp_sync import (
        FieldErpSyncEvent,
        FieldErpSyncFlow,
        SyncFlowOwner,
        get_flow_ownership,
    )

    flow = FieldErpSyncFlow.material_request.value
    owner = get_flow_ownership(db)[flow]
    event = (
        db.query(FieldErpSyncEvent)
        .filter(
            FieldErpSyncEvent.flow == flow,
            FieldErpSyncEvent.entity_type == "field_material_request",
            FieldErpSyncEvent.entity_id == request_id,
        )
        .order_by(FieldErpSyncEvent.created_at.desc())
        .first()
    )
    return BackofficeDeliveryView(
        flow_owner=owner,
        sub_owns_delivery=owner == SyncFlowOwner.sub.value,
        event_id=event.id if event is not None else None,
        event_status=event.status if event is not None else None,
        attempts=event.attempts if event is not None else 0,
        last_error=event.last_error if event is not None else None,
        queued_at=event.created_at if event is not None else None,
        updated_at=event.updated_at if event is not None else None,
        sent_at=event.sent_at if event is not None else None,
        diagnostic=_delivery_diagnostic(event),
    )


def get_expense_claim_deliveries(
    db: Session, request_ids: Collection[UUID]
) -> dict[UUID, BackofficeDeliveryView]:
    """Return the authoritative ERP outbox projection for expense requests."""
    from app.models.field_erp_sync import (
        FieldErpSyncEvent,
        FieldErpSyncFlow,
        SyncFlowOwner,
        get_flow_ownership,
    )

    ids = tuple(dict.fromkeys(request_ids))
    if not ids:
        return {}
    flow = FieldErpSyncFlow.expense_claim.value
    owner = get_flow_ownership(db)[flow]
    rows = (
        db.query(FieldErpSyncEvent)
        .filter(
            FieldErpSyncEvent.flow == flow,
            FieldErpSyncEvent.entity_type == "field_expense_request",
            FieldErpSyncEvent.entity_id.in_(ids),
        )
        .order_by(FieldErpSyncEvent.created_at.asc())
        .all()
    )
    rows = [
        row
        for row in rows
        if str((row.payload or {}).get("_expense_action") or "submit")
        in {"submit", "release_approved_v2", "expense_submit_v3"}
    ]
    latest = {row.entity_id: row for row in rows}
    return {
        request_id: BackofficeDeliveryView(
            flow_owner=owner,
            sub_owns_delivery=owner == SyncFlowOwner.sub.value,
            event_id=(row.id if row is not None else None),
            event_status=(row.status if row is not None else None),
            attempts=(row.attempts if row is not None else 0),
            last_error=(row.last_error if row is not None else None),
            queued_at=(row.created_at if row is not None else None),
            updated_at=(row.updated_at if row is not None else None),
            sent_at=(row.sent_at if row is not None else None),
            diagnostic=_delivery_diagnostic(row),
        )
        for request_id in ids
        for row in (latest.get(request_id),)
    }


def get_expense_payment_deliveries(
    db: Session, request_ids: Collection[UUID]
) -> dict[UUID, BackofficeDeliveryView]:
    """Return the latest payment-command delivery for each expense request."""
    from app.models.field_erp_sync import (
        FieldErpSyncEvent,
        FieldErpSyncFlow,
        SyncFlowOwner,
        get_flow_ownership,
    )

    ids = tuple(dict.fromkeys(request_ids))
    if not ids:
        return {}
    flow = FieldErpSyncFlow.expense_claim.value
    owner = get_flow_ownership(db)[flow]
    rows = (
        db.query(FieldErpSyncEvent)
        .filter(
            FieldErpSyncEvent.flow == flow,
            FieldErpSyncEvent.entity_type == "field_expense_payment",
            FieldErpSyncEvent.entity_id.in_(ids),
        )
        .order_by(FieldErpSyncEvent.created_at.asc())
        .all()
    )
    latest = {row.entity_id: row for row in rows}
    return {
        request_id: BackofficeDeliveryView(
            flow_owner=owner,
            sub_owns_delivery=owner == SyncFlowOwner.sub.value,
            event_id=(row.id if row is not None else None),
            event_status=(row.status if row is not None else None),
            attempts=(row.attempts if row is not None else 0),
            last_error=(row.last_error if row is not None else None),
            queued_at=(row.created_at if row is not None else None),
            updated_at=(row.updated_at if row is not None else None),
            sent_at=(row.sent_at if row is not None else None),
            diagnostic=_delivery_diagnostic(row),
        )
        for request_id in ids
        for row in (latest.get(request_id),)
    }


def get_expense_decision_delivery(
    db: Session, request_id: UUID, action: str
) -> BackofficeDeliveryView:
    """Return one expense manager-decision delivery projection."""
    from app.models.field_erp_sync import (
        FieldErpSyncEvent,
        FieldErpSyncFlow,
        SyncFlowOwner,
        get_flow_ownership,
    )

    flow = FieldErpSyncFlow.expense_claim.value
    owner = get_flow_ownership(db)[flow]
    rows = (
        db.query(FieldErpSyncEvent)
        .filter(
            FieldErpSyncEvent.flow == flow,
            FieldErpSyncEvent.entity_id == request_id,
        )
        .order_by(FieldErpSyncEvent.created_at.desc())
        .all()
    )
    accepted_actions = (
        {"approve", "release_approved_v2", "expense_approve_v3"}
        if action == "approve"
        else {"reject", "expense_reject_v3"}
    )
    matching = next(
        (
            candidate
            for candidate in rows
            if str((candidate.payload or {}).get("_expense_action")) in accepted_actions
        ),
        None,
    )
    return BackofficeDeliveryView(
        flow_owner=owner,
        sub_owns_delivery=owner == SyncFlowOwner.sub.value,
        event_id=matching.id if matching is not None else None,
        event_status=matching.status if matching is not None else None,
        attempts=matching.attempts if matching is not None else 0,
        last_error=matching.last_error if matching is not None else None,
        queued_at=matching.created_at if matching is not None else None,
        updated_at=matching.updated_at if matching is not None else None,
        sent_at=matching.sent_at if matching is not None else None,
        diagnostic=_delivery_diagnostic(matching),
    )


def build_gateway(db: Session) -> BackofficeGateway:
    """Build the default typed back-office capability facade.

    Installation bindings select the provider. Domain callers never select or
    import a product-specific connector.
    """
    from app.services.integrations.erp_capability import capability_client

    return capability_client(db)


def _provider_for_outbox(db: Session) -> str:
    from app.services.integrations import installations
    from app.services.integrations.backoffice_contracts import (
        ERP_OUTBOX_CAPABILITY,
    )

    try:
        binding = installations.require_enabled_capability_binding(
            db,
            capability_id=ERP_OUTBOX_CAPABILITY,
        )
    except installations.InstallationError as exc:
        raise BackofficeUnavailableError(str(exc)) from exc
    return str(binding.installation.connector_key)


def _enqueue_with_provider(
    db: Session,
    *,
    flow: str,
    source: Any,
) -> BackofficeEnqueueResult:
    """Delegate one source intent to its configured provider adapter.

    The source record remains Sub's durable business fact. An unavailable
    provider never changes the source decision; repair/reconciliation can retry
    once an adapter is configured.
    """
    if not _flow_owned_by_sub(db, flow):
        return BackofficeEnqueueResult(status=BackofficeEnqueueStatus.NOT_OWNED)

    provider = _provider_for_outbox(db)

    event: object | None
    if flow == "material_request":
        from app.services.dotmac_erp.material_sync import enqueue_material_request

        event = enqueue_material_request(db, source)
    elif flow == "purchase_order":
        from app.services.dotmac_erp.purchase_order_sync import enqueue_purchase_order

        event = enqueue_purchase_order(db, source)
    elif flow == "purchase_invoice":
        from app.services.dotmac_erp.purchase_invoice_sync import (
            enqueue_purchase_invoice,
        )

        event = enqueue_purchase_invoice(db, source)
    else:
        raise ValueError(f"Unsupported back-office flow: {flow}")
    return BackofficeEnqueueResult(
        status=(
            BackofficeEnqueueStatus.ENQUEUED
            if event is not None
            else BackofficeEnqueueStatus.NOT_ENQUEUED
        ),
        provider=provider,
        event=event,
    )


def enqueue_expense_decision(
    db: Session,
    request: FieldExpenseRequest,
    *,
    action: str,
    decision_id: UUID,
    decided_by_email: str,
    decided_at: datetime,
    reason: str | None = None,
) -> BackofficeEnqueueResult:
    from app.services.owner_commands import owner_command_active

    if not owner_command_active(db, owner="operations.expense_requests"):
        raise RuntimeError("Expense release requires the expense request owner")
    if action not in {"approve", "reject"}:
        raise ValueError("A manager approval or rejection is required")
    if not _flow_owned_by_sub(db, "expense_claim"):
        return BackofficeEnqueueResult(status=BackofficeEnqueueStatus.NOT_OWNED)

    from app.services.dotmac_erp.expense_sync import (
        ExpenseErpAction,
    )
    from app.services.dotmac_erp.expense_sync import (
        enqueue_expense_decision as enqueue,
    )

    event = enqueue(
        db,
        request,
        action=(
            ExpenseErpAction.APPROVE_V3
            if action == "approve"
            else ExpenseErpAction.REJECT_V3
        ),
        decision_id=decision_id,
        decided_by_email=decided_by_email,
        decided_at=decided_at,
        reason=reason,
        isolate=False,
    )
    return BackofficeEnqueueResult(
        status=BackofficeEnqueueStatus.ENQUEUED,
        provider="dotmac.erp",
        event=event,
    )


def enqueue_expense_submission(
    db: Session, request: FieldExpenseRequest
) -> BackofficeEnqueueResult:
    """Stage the submission consequence inside the expense owner's transaction."""
    from app.services.owner_commands import owner_command_active

    if not owner_command_active(db, owner="operations.expense_requests"):
        raise RuntimeError("Expense submission requires the expense request owner")
    if not _flow_owned_by_sub(db, "expense_claim"):
        return BackofficeEnqueueResult(status=BackofficeEnqueueStatus.NOT_OWNED)
    from app.services.dotmac_erp.expense_sync import (
        enqueue_expense_submission as enqueue,
    )

    event = enqueue(db, request, isolate=False)
    return BackofficeEnqueueResult(
        status=BackofficeEnqueueStatus.ENQUEUED,
        provider="dotmac.erp",
        event=event,
    )


def enqueue_expense_payment(
    db: Session,
    request: FieldExpenseRequest,
    *,
    command_id: UUID,
    initiated_by_email: str,
    initiated_at: datetime,
) -> BackofficeEnqueueResult:
    from app.services.owner_commands import owner_command_active

    if not owner_command_active(db, owner="operations.expense_requests"):
        raise RuntimeError("Expense payment requires the expense request owner")
    if not _flow_owned_by_sub(db, "expense_claim"):
        return BackofficeEnqueueResult(status=BackofficeEnqueueStatus.NOT_OWNED)
    from app.services.dotmac_erp.expense_sync import enqueue_expense_payment as enqueue

    event = enqueue(
        db,
        request,
        command_id=command_id,
        initiated_by_email=initiated_by_email,
        initiated_at=initiated_at,
        isolate=False,
    )
    return BackofficeEnqueueResult(
        status=BackofficeEnqueueStatus.ENQUEUED,
        provider="dotmac.erp",
        event=event,
    )


def stage_expense_delivery_recovery(
    db: Session,
    *,
    dead_event_id: UUID,
    replacement_idempotency_key: str,
    recovery_contract_version: str,
) -> BackofficeExpenseRecoveryStaging:
    """Append or reuse one replacement for a verified dead expense delivery."""

    from app.models.field_erp_sync import (
        FieldErpSyncEvent,
        FieldErpSyncFlow,
        FieldErpSyncStatus,
    )
    from app.services.dotmac_erp.outbox import enqueue
    from app.services.owner_commands import owner_command_active

    if not owner_command_active(db, owner="operations.expense_requests"):
        raise RuntimeError("Expense recovery requires the expense request owner")
    original = db.get(FieldErpSyncEvent, dead_event_id)
    if (
        original is None
        or original.flow != FieldErpSyncFlow.expense_claim.value
        or original.status != FieldErpSyncStatus.dead.value
        or str((original.payload or {}).get("_expense_action")) != "release_approved_v2"
    ):
        raise BackofficeUnavailableError(
            "The expense delivery is no longer recoverable"
        )
    existing = (
        db.query(FieldErpSyncEvent)
        .filter(FieldErpSyncEvent.idempotency_key == replacement_idempotency_key)
        .one_or_none()
    )
    replacement = existing
    if replacement is None:
        payload = dict(original.payload or {})
        payload.update(
            {
                "_replaces_event_id": str(original.id),
                "_recovery_contract_version": recovery_contract_version,
            }
        )
        replacement = enqueue(
            db,
            flow=FieldErpSyncFlow.expense_claim,
            entity_type="field_expense_request",
            entity_id=original.entity_id,
            idempotency_key=replacement_idempotency_key,
            payload=payload,
            isolate=False,
        )
        if isinstance(original.erp_response, dict):
            replacement.erp_response = dict(original.erp_response)
    return BackofficeExpenseRecoveryStaging(
        original_event_id=original.id,
        replacement_event_id=replacement.id,
        replacement_idempotency_key=replacement.idempotency_key,
        replayed=existing is not None,
    )


def requeue_expense_payment_delivery(
    db: Session,
    *,
    dead_event_id: UUID,
) -> BackofficeExpensePaymentRecoveryStaging:
    """Requeue one permission-denied payment event without changing its key."""

    from app.models.field_erp_sync import FieldErpSyncEvent, FieldErpSyncStatus
    from app.services.owner_commands import owner_command_active

    if not owner_command_active(db, owner="operations.expense_requests"):
        raise RuntimeError("Expense payment recovery requires the expense owner")
    recovery = inspect_expense_payment_recovery_delivery(
        db,
        event_id=dead_event_id,
        lock=True,
    )
    if recovery is None or recovery.status is not BackofficeDeliveryStatus.DEAD:
        raise BackofficeUnavailableError(
            "The expense payment delivery is no longer recoverable"
        )
    event = db.get(FieldErpSyncEvent, recovery.event_id)
    if event is None:
        raise BackofficeUnavailableError(
            "The expense payment delivery is no longer recoverable"
        )
    event.status = FieldErpSyncStatus.pending.value
    db.flush()
    return BackofficeExpensePaymentRecoveryStaging(
        event_id=event.id,
        idempotency_key=event.idempotency_key,
    )


def inspect_expense_payment_recovery_delivery(
    db: Session,
    *,
    event_id: UUID,
    lock: bool,
) -> BackofficeExpensePaymentRecoveryView | None:
    """Read provider-neutral evidence for an expense payment delivery."""

    from app.models.field_erp_sync import FieldErpSyncEvent, FieldErpSyncFlow

    query = db.query(FieldErpSyncEvent).filter(FieldErpSyncEvent.id == event_id)
    if lock:
        query = query.with_for_update()
    event = query.one_or_none()
    diagnostic = _delivery_diagnostic(event)
    if (
        event is None
        or event.flow != FieldErpSyncFlow.expense_claim.value
        or str((event.payload or {}).get("_expense_action")) != "initiate_payment"
        or diagnostic is None
        or diagnostic.code != "permission_denied"
        or diagnostic.http_status != 403
        or diagnostic.operation != "initiate_expense_payment"
    ):
        return None
    return BackofficeExpensePaymentRecoveryView(
        event_id=event.id,
        expense_request_id=event.entity_id,
        idempotency_key=event.idempotency_key,
        status=BackofficeDeliveryStatus(event.status),
    )


def enqueue_material_request_outbox(
    db: Session, request: FieldMaterialRequest
) -> FieldErpSyncEvent | None:
    """Stage the material-request ERP intent for a receipted consumer.

    Ownership-checked and savepoint-free: inside an owner command the
    pre-checked idempotent insert flushes directly, and provider capability
    gates delivery, not staging — the row waits as a durable pending
    delivery. Returns the outbox row, or ``None`` when the flow is not
    Sub-owned or the request is ineligible.
    """
    if not _flow_owned_by_sub(db, "material_request"):
        return None
    from app.services.dotmac_erp.material_sync import enqueue_material_request

    return enqueue_material_request(db, request, isolate=False)


def enqueue_material_request_cancellation_outbox(
    db: Session, request: FieldMaterialRequest
) -> FieldErpSyncEvent | None:
    """Stage an ERP cancellation from the receipted cancellation consumer."""
    if not _flow_owned_by_sub(db, "material_request"):
        return None
    from app.services.dotmac_erp.material_sync import (
        enqueue_material_request_cancellation,
    )

    return enqueue_material_request_cancellation(db, request, isolate=False)


def enqueue_purchase_invoice_outbox(db: Session, invoice: Any):
    """Stage the payables ERP export for a receipted consumer.

    Ownership-checked and savepoint-free; provider capability gates
    delivery, not staging. Returns the outbox row or ``None`` when the flow
    is not Sub-owned.
    """
    if not _flow_owned_by_sub(db, "purchase_invoice"):
        return None
    from app.services.dotmac_erp.purchase_invoice_sync import (
        enqueue_purchase_invoice,
    )

    return enqueue_purchase_invoice(db, invoice, isolate=False)


def enqueue_material_support(db: Session, request: Any) -> BackofficeEnqueueResult:
    return _enqueue_with_provider(db, flow="material_request", source=request)


def enqueue_purchase_order(db: Session, project: Any) -> BackofficeEnqueueResult:
    return _enqueue_with_provider(db, flow="purchase_order", source=project)


def enqueue_purchase_invoice(db: Session, invoice: Any) -> BackofficeEnqueueResult:
    return _enqueue_with_provider(db, flow="purchase_invoice", source=invoice)
