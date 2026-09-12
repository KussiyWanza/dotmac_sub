"""Material-request (ISSUE) flow for the Sub → DotMac ERP outbox.

Second money flow to move onto sub's ``field_erp_sync_events`` outbox. Ports
``dotmac_crm/app/services/dotmac_erp/material_request_sync.py`` onto sub's native
``FieldMaterialRequest`` with its ERP mirror fields
``support_reference`` / ``support_status``). Structurally identical to
``expense_sync.py``.

Three responsibilities live here:

* **map + enqueue** — ``enqueue_material_request`` builds the ERP payload (a port
  of CRM's ``_map_material_request`` shape, ``request_type='ISSUE'``), computes
  the stable idempotency key ``mr-{id}-approve-v1``, and hands it to
  ``outbox.enqueue``. It does NOT deliver — the worker owns delivery, and the
  outbox refuses any flow sub does not own in ``sync_flow_ownership``.
* **write-back** — ``apply_erp_response`` runs on the outbox's accepted/rejected
  path, extracts ERP's request id / status, and delegates the Sub projection to
  ``operations.material_dependencies``. ERP ``issued`` is terminal for the
  support request and resumes the Sub material dependency.
* **reconcile** — ``refresh_material_request_statuses`` polls ERP for in-flight
  requests and refreshes the mirror fields (ports CRM's status-poll refresh).

INERT UNTIL CUTOVER: nothing here sends while
``sync_flow_ownership.material_request`` remains ``crm``. Delivery and status
workers are scheduled only when their validated ERP capability bindings are
enabled; the retired ``dotmac_erp_sync_enabled`` setting is not a runtime gate.
Ownership must be explicitly assigned to Sub after acceptance verification
before a request reaches ERP.

Warehouse and serial selection are first-class Sub fields. Stock and serial
availability remain read-only ERP data, but each approved ISSUE records the
selected warehouse and exact serialized units for an auditable handoff.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    flow_owned_by_sub,
)
from app.models.field_material import FieldMaterialRequest, FieldMaterialRequestItem
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.dotmac_erp import outbox
from app.services.dotmac_erp.client import DotMacERPClient
from app.services.field import material_requests
from app.services.integrations.backoffice_contracts import ERP_STATUS_CAPABILITY
from app.services.integrations.erp_capability import (
    ErpCapabilityClient,
    capability_client,
)
from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)

ENTITY_TYPE = "field_material_request"
PROVIDER = "dotmac_erp"

# ERP status pushed for an ISSUE material request (verbatim CRM parity: CRM sends
# ``MaterialRequestStatus.issued.value``).
_ERP_SUBMISSION_STATUS = "submitted"

# The sub-side statuses a request can still change while ERP owns fulfillment;
# only these get polled for a status refresh.
_IN_FLIGHT_STATUSES = ("submitted", "approved", "accepted_by_erp", "pending_stock")


@dataclass(frozen=True, slots=True)
class MaterialStatusRefreshCandidate:
    """One bounded ERP reconciliation target with its comparison state."""

    request_id: UUID
    status: str
    support_reference: str
    support_status: str | None


@dataclass(frozen=True, slots=True)
class MaterialStatusRefreshOutcome:
    """Typed result for one bounded material-status reconciliation cycle."""

    processed: int
    observed: int
    updated: int
    skipped_not_owned: int
    failed: int
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "processed": self.processed,
            "observed": self.observed,
            "updated": self.updated,
            "skipped_not_owned": self.skipped_not_owned,
            "failed": self.failed,
            "errors": list(self.errors),
        }


class MaterialStatusResponseError(DomainError):
    """ERP returned a response that cannot enter the material owner."""


# ---------------------------------------------------------------------------
# Mapping + idempotency key (port of CRM's _map_material_request)
# ---------------------------------------------------------------------------


def material_request_idempotency_key(request: FieldMaterialRequest) -> str:
    """Stable per-request key: ``mr-{id}-approve-v1``.

    Constant across re-approvals of the same request, so a re-enqueue returns the
    existing outbox row and a re-delivery is a no-op on the ERP side.
    """
    return f"mr-{request.id}-approve-v1"


def _requester_email(request: FieldMaterialRequest) -> str | None:
    """Resolve the requesting employee's email (ERP matches employees by email)."""
    user = request.requested_by_system_user
    email = (getattr(user, "email", None) or "").strip()
    return email or None


def _item_serial_numbers(item: FieldMaterialRequestItem) -> list[str]:
    raw = item.serial_numbers
    if not raw and isinstance(item.metadata_, dict):
        raw = item.metadata_.get("serial_numbers")
    if not isinstance(raw, list):
        return []
    return [str(serial).strip() for serial in raw if str(serial).strip()]


def _from_warehouse_code(request: FieldMaterialRequest) -> str | None:
    code = request.source_warehouse_code
    if not code and isinstance(request.metadata_, dict):
        code = request.metadata_.get("from_warehouse_code") or request.metadata_.get(
            "warehouse_code"
        )
    cleaned = str(code).strip() if code else ""
    return cleaned or None


def build_material_request_payload(request: FieldMaterialRequest) -> dict:
    """Map a ``FieldMaterialRequest`` to ERP's ``SubMaterialRequestPayload`` shape.

    Ports the historical mapper into a neutral contract: ``source_request_id``
    is Sub's request UUID,
    ``request_type='ISSUE'``, ``status='submitted'``, each line carries
    ``item_code`` / ``quantity`` / ``uom`` / ``from_warehouse_code`` (and
    ``serial_numbers`` when known). ``requested_by_email`` lets ERP match the
    employee; ``ticket_source_reference`` comes from retained work-order
    provenance (Sub has no direct FK). See the module docstring for the
    serials/warehouse fidelity gap.
    """
    warehouse_code = _from_warehouse_code(request)

    item_rows: list[dict[str, object]] = []
    for item in request.items:
        inv_item = item.item
        row: dict[str, object] = {
            "item_code": (
                getattr(inv_item, "sku", None)
                or getattr(inv_item, "name", None)
                or str(item.item_id)
            ),
            "quantity": item.quantity,
            "uom": getattr(inv_item, "unit", None) or "PCS",
            "from_warehouse_code": warehouse_code,
        }
        serial_numbers = _item_serial_numbers(item)
        if serial_numbers:
            row["serial_numbers"] = serial_numbers
        item_rows.append(row)

    schedule_date = (
        (request.approved_at or request.submitted_at or request.created_at)
        .date()
        .isoformat()
    )

    mirror = request.work_order_mirror

    return {
        "source_request_id": str(request.id),
        "request_type": "ISSUE",
        "status": _ERP_SUBMISSION_STATUS,
        "schedule_date": schedule_date,
        "requested_by_email": _requester_email(request),
        "ticket_source_reference": getattr(mirror, "crm_ticket_id", None),
        "remarks": request.notes or "",
        "items": item_rows,
    }


def material_request_eligibility_error(request: FieldMaterialRequest) -> str | None:
    """Return a reason string if the request is NOT eligible for ERP sync, else None.

    Ports CRM's ``_validate_material_request_for_sync`` onto sub: must be
    ``approved``, have at least one line, and carry a requester email (ERP needs
    it to match the employee). The source-warehouse requirement is relaxed vs CRM
    (sub has no source_location) — see the module docstring gap note.
    """
    if request.status not in {"submitted", "approved"}:
        return (
            f"Material request {request.id} is in {request.status} status and "
            "cannot be synced"
        )
    if not request.items:
        return f"Material request {request.id} has no items — cannot sync to ERP"
    if not _requester_email(request):
        return "Requester has no email address; ERP needs it to match the employee"
    if not _from_warehouse_code(request):
        return "A source warehouse is required before ERP material issue"
    for item in request.items:
        serials = _item_serial_numbers(item)
        if serials and len(serials) != item.quantity:
            return (
                f"Material request item {item.id} has {len(serials)} serials for "
                f"quantity {item.quantity}"
            )
    return None


# ---------------------------------------------------------------------------
# Enqueue (approve hook target)
# ---------------------------------------------------------------------------


def enqueue_material_request(
    db: Session, request: FieldMaterialRequest, *, isolate: bool = True
) -> FieldErpSyncEvent | None:
    """Enqueue the material-request outbox intent for an approved request.

    Validates eligibility, builds the payload + stable key, and calls
    ``outbox.enqueue`` (idempotent on the key). Returns the outbox row, or ``None``
    when the request is not eligible (logged, not raised — an ineligible request
    must never break the approve transaction). Does NOT deliver.
    """
    reason = material_request_eligibility_error(request)
    if reason:
        logger.info(
            "material_sync: not enqueuing material request %s — %s", request.id, reason
        )
        return None

    payload = build_material_request_payload(request)
    return outbox.enqueue(
        db,
        flow=FieldErpSyncFlow.material_request,
        entity_type=ENTITY_TYPE,
        entity_id=request.id,
        idempotency_key=material_request_idempotency_key(request),
        payload=payload,
        isolate=isolate,
    )


# ---------------------------------------------------------------------------
# Response write-back (outbox accepted/rejected path + status reconcile)
# ---------------------------------------------------------------------------


def _extract_request_id(response: dict | None) -> str | None:
    if not isinstance(response, dict):
        return None
    erp_id = (
        response.get("request_id")
        or response.get("material_request_id")
        or response.get("request_number")
    )
    return str(erp_id) if erp_id else None


def _extract_material_status(response: dict | None) -> str | None:
    if not isinstance(response, dict):
        return None
    raw = (
        response.get("material_status")
        or response.get("erp_material_status")
        or response.get("support_status")
        or response.get("status")
    )
    if not raw:
        return None
    status = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    return status[:40] if status else None


def _serial_numbers_by_sequence(
    response: dict[str, object],
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    raw_items = response.get("items")
    if not isinstance(raw_items, list):
        return ()
    observed: list[tuple[int, tuple[str, ...]]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        try:
            sequence = int(raw_item.get("sequence", 0))
        except (TypeError, ValueError):
            continue
        raw_serials = raw_item.get("serial_numbers")
        if sequence < 1 or not isinstance(raw_serials, list):
            continue
        serials = tuple(
            cleaned for value in raw_serials if (cleaned := str(value).strip())
        )
        observed.append((sequence, serials))
    return tuple(observed)


def _observation_command(
    candidate: MaterialStatusRefreshCandidate,
    response: dict[str, object],
    *,
    observed_at: datetime,
) -> material_requests.ObserveErpMaterialStatus:
    provider_request_id = _extract_request_id(response)
    provider_status = _extract_material_status(response)
    if provider_request_id is None or provider_status is None:
        raise MaterialStatusResponseError(
            code=("integration.dotmac_erp_material_support_adapter.invalid_outcome"),
            message="ERP material status response is missing identity or status.",
            details={"request_id": str(candidate.request_id)},
        )
    command_id = uuid5(
        NAMESPACE_URL,
        f"erp-material-poll:{candidate.request_id}:{observed_at.isoformat()}",
    )
    return material_requests.ObserveErpMaterialStatus(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="integration:dotmac-erp-material-status-poller",
            scope=ERP_STATUS_CAPABILITY,
            reason="Observe polled ERP material request status",
            idempotency_key=str(command_id),
        ),
        request_id=candidate.request_id,
        provider_request_id=provider_request_id,
        provider_status=provider_status,
        observed_at=observed_at,
        serial_numbers_by_sequence=_serial_numbers_by_sequence(response),
    )


def _linked_refresh_candidates(
    db: Session, *, limit: int
) -> tuple[MaterialStatusRefreshCandidate, ...]:
    """Return never or least recently observed linked requests first."""
    rows = db.execute(
        select(
            FieldMaterialRequest.id,
            FieldMaterialRequest.status,
            FieldMaterialRequest.support_reference,
            FieldMaterialRequest.support_status,
        )
        .where(
            FieldMaterialRequest.is_active.is_(True),
            FieldMaterialRequest.support_system == PROVIDER,
            FieldMaterialRequest.support_reference.isnot(None),
            FieldMaterialRequest.status.in_(_IN_FLIGHT_STATUSES),
        )
        .order_by(
            FieldMaterialRequest.last_reconciled_at.asc().nullsfirst(),
            FieldMaterialRequest.id.asc(),
        )
        .limit(limit)
    ).all()
    return tuple(
        MaterialStatusRefreshCandidate(
            request_id=row.id,
            status=str(row.status),
            support_reference=str(row.support_reference),
            support_status=str(row.support_status) if row.support_status else None,
        )
        for row in rows
    )


def apply_material_response(
    db: Session, request: FieldMaterialRequest, response: dict | None
) -> bool:
    """Write an ERP material-request response back onto a ``FieldMaterialRequest``.

    Shared by the outbox accepted/rejected path and status reconciliation.  This
    adapter extracts the wire values, then delegates every Sub-side transition
    to ``operations.material_dependencies``.  ERP ``issued`` is a terminal
    support outcome: stock has been posted out of ERP and Sub may resume the
    service workflow with the resulting allocation projection.
    """
    if not isinstance(response, dict):
        return False

    erp_id = _extract_request_id(response)
    material_status = _extract_material_status(response)
    from app.services.field.material_requests import field_material_requests

    return field_material_requests.apply_backoffice_outcome(
        db,
        request,
        support_system=PROVIDER,
        support_reference=erp_id,
        support_status=material_status,
    )


def apply_erp_response(db: Session, event: FieldErpSyncEvent) -> None:
    """Outbox write-back hook: apply a delivered event's ERP response to its source.

    Called by ``outbox.deliver_pending`` after a 2xx classify, within the same
    transaction (the outbox commits the row). Loads the ``FieldMaterialRequest``
    the event pushed and applies ``apply_material_response``. Missing source rows
    are logged, not raised — the event still records its terminal outcome.
    """
    request = db.get(FieldMaterialRequest, event.entity_id)
    if request is None:
        logger.warning(
            "material_sync: outbox event %s has no FieldMaterialRequest %s to "
            "write ERP response back to",
            event.id,
            event.entity_id,
        )
        return
    apply_material_response(db, request, event.erp_response)


# ---------------------------------------------------------------------------
# Status reconcile (beat-driven, gated by dotmac_erp_sync_enabled)
# ---------------------------------------------------------------------------


def _poll_unlinked_material_requests(
    db: Session,
    *,
    client: DotMacERPClient | ErpCapabilityClient,
    limit: int,
) -> tuple[int, int, int, list[str]]:
    """Poll ``sent``/``accepted`` outbox rows whose request never got a reference.

    A ``sent`` row was never eligible for the reference-gated query below (it
    has no reference BY DEFINITION). An ``accepted`` row can also land here if
    the same-transaction write-back failed after delivery. Keyed on Sub's own
    request id — see ``client.get_material_request_status``'s docstring.

    OWNERSHIP GUARD: ``flow_owned_by_sub`` is checked once up front, since
    ownership is a per-flow switch, not per-row. A status poll is a real ERP
    API call about a row that may belong to a flow ownership has since moved
    back to CRM — skipped, not polled, when not owned. Skipped rows are
    counted separately so the caller's own sweep numbers stay honest.
    """
    processed = 0
    updated = 0
    skipped_not_owned = 0
    errors: list[str] = []
    owned = flow_owned_by_sub(db, FieldErpSyncFlow.material_request)
    for row in outbox.unlinked_delivered_events(
        db, flow=FieldErpSyncFlow.material_request, limit=limit
    ):
        request = db.get(FieldMaterialRequest, row.entity_id)
        if request is None or request.support_reference:
            continue
        if not owned:
            skipped_not_owned += 1
            logger.info(
                "material_sync: skipping unlinked status poll for %s — sub "
                "does not own flow 'material_request' (sync_flow_ownership)",
                row.id,
            )
            continue
        processed += 1
        request_id = str(request.id)
        try:
            response = client.get_material_request_status(request_id)
        except Exception as exc:  # noqa: BLE001 — one bad row can't stall the batch
            db.rollback()
            errors.append(f"{row.id}: {exc}")
            logger.warning(
                "material_sync: unlinked status poll failed for %s: %s", row.id, exc
            )
            continue
        if not response:
            continue
        outbox.record_polled_outcome(db, row, response)
        db.commit()
        if request.support_reference:
            updated += 1
    return processed, updated, skipped_not_owned, errors


def refresh_material_request_statuses(
    db: Session,
    *,
    client: DotMacERPClient | ErpCapabilityClient | None = None,
    limit: int = 100,
) -> MaterialStatusRefreshOutcome:
    """Poll ERP for in-flight material requests and refresh their mirror fields.

    Two candidate sets, both keyed by Sub's own request id (never the ERP id):

    1. Already-linked requests (``support_reference`` set) still awaiting ERP
       fulfillment (``approved`` / ``issued``) — the historical behaviour,
       ported from CRM's material status refresh.
    2. Delivered-but-unlinked outbox rows (``sent``/``accepted`` with no
       reference yet) — the dead end where a ``sent`` row could never satisfy
       set 1's ``.isnot(None)`` filter since it never carries a reference by
       construction. Routed through ``outbox.record_polled_outcome`` so the
       response is classified and written back through the same path a fresh
       delivery uses.

    Read-only against ERP; idempotent; safe to re-run.
    """
    limit = max(1, min(int(limit or 100), 200))
    errors: list[str] = []

    owned_client = client
    created_client = False
    if owned_client is None:
        owned_client = capability_client(db)
        created_client = True

    processed = 0
    observed = 0
    updated = 0
    skipped_not_owned = 0
    try:
        (
            unlinked_processed,
            unlinked_updated,
            unlinked_skipped_not_owned,
            unlinked_errors,
        ) = _poll_unlinked_material_requests(db, client=owned_client, limit=limit)
        processed += unlinked_processed
        updated += unlinked_updated
        skipped_not_owned += unlinked_skipped_not_owned
        errors.extend(unlinked_errors)

        pending = _linked_refresh_candidates(db, limit=limit)
        linked_owned = flow_owned_by_sub(db, FieldErpSyncFlow.material_request)
        if not linked_owned:
            skipped_not_owned += len(pending)
            pending = ()
        db_session_adapter.release_read_transaction(db)
        for candidate in pending:
            processed += 1
            request_id = str(candidate.request_id)
            try:
                response = owned_client.get_material_request_status(request_id)
                if not isinstance(response, dict):
                    continue
                command = _observation_command(
                    candidate,
                    response,
                    observed_at=datetime.now(UTC),
                )
                outcome = material_requests.observe_erp_material_status(db, command)
                observed += 1
                if (
                    outcome.status.value != candidate.status
                    or outcome.support_reference != candidate.support_reference
                    or outcome.support_status != candidate.support_status
                ):
                    updated += 1
            except Exception as exc:  # noqa: BLE001 — one bad row can't stall the batch
                db_session_adapter.discard_failed_transaction(db)
                errors.append(f"{request_id}: {exc}")
                logger.warning(
                    "material_sync: status refresh failed for %s: %s", request_id, exc
                )
                continue
    finally:
        if created_client:
            owned_client.close()

    return MaterialStatusRefreshOutcome(
        processed=processed,
        observed=observed,
        updated=updated,
        skipped_not_owned=skipped_not_owned,
        failed=len(errors),
        errors=tuple(errors),
    )


def run_refresh_material_request_statuses() -> dict[str, object]:
    """Own the background session for ERP material-outcome reconciliation."""
    from app.db import task_session

    with task_session() as db:
        return refresh_material_request_statuses(db).as_dict()
