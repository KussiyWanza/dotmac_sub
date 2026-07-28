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

INERT UNTIL CUTOVER: nothing here sends. The approve hook only enqueues when the
master flag ``dotmac_erp_sync_enabled`` is on, and delivery is additionally gated
per-flow by ``sync_flow_ownership.material_request`` (seeded ``crm``). Both must
flip at cutover before a single request reaches ERP.

Warehouse and serial selection are first-class Sub fields. Stock and serial
availability remain read-only ERP data, but each approved ISSUE records the
selected warehouse and exact serialized units for an auditable handoff.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session, selectinload

from app.models.field_erp_sync import FieldErpSyncEvent, FieldErpSyncFlow
from app.models.field_material import FieldMaterialRequest, FieldMaterialRequestItem
from app.services.dotmac_erp import outbox
from app.services.dotmac_erp.client import DotMacERPClient
from app.services.integrations.erp_capability import (
    ErpCapabilityClient,
    capability_client,
)

logger = logging.getLogger(__name__)

ENTITY_TYPE = "field_material_request"
PROVIDER = "dotmac_erp"

# ERP status pushed for an ISSUE material request (verbatim CRM parity: CRM sends
# ``MaterialRequestStatus.issued.value``).
_ERP_ISSUE_STATUS = "issued"

# The sub-side statuses a request can still change while ERP owns fulfillment;
# only these get polled for a status refresh.
_IN_FLIGHT_STATUSES = ("approved", "issued")

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
    """Map a ``FieldMaterialRequest`` to ERP's ``CRMMaterialRequestPayload`` shape.

    Mirrors CRM's ``_map_material_request``: ``omni_id`` is sub's request UUID,
    ``request_type='ISSUE'``, ``status='issued'``, each line carries
    ``item_code`` / ``quantity`` / ``uom`` / ``from_warehouse_code`` (and
    ``serial_numbers`` when known). ``requested_by_email`` lets ERP match the
    employee; ``ticket_crm_id`` comes off the work-order mirror (sub has no direct
    FK). See the module docstring for the serials/warehouse fidelity gap.
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
        "omni_id": str(request.id),
        "request_type": "ISSUE",
        "status": _ERP_ISSUE_STATUS,
        "schedule_date": schedule_date,
        "requested_by_email": _requester_email(request),
        "ticket_crm_id": getattr(mirror, "crm_ticket_id", None),
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
    if request.status != "approved":
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


def refresh_material_request_statuses(
    db: Session,
    *,
    client: DotMacERPClient | ErpCapabilityClient | None = None,
    limit: int = 100,
) -> dict:
    """Poll ERP for in-flight material requests and refresh their mirror fields.

    Selects synced (``support_reference`` set) requests still awaiting ERP
    fulfillment (``approved`` / ``issued``), polls
    ``get_material_request_status(request.id)`` for each, and applies the response
    via ``apply_material_response``. Ports CRM's material status refresh.
    Read-only against ERP; idempotent; safe to re-run.
    """
    limit = max(1, min(int(limit or 100), 200))
    pending = (
        db.query(FieldMaterialRequest)
        .options(
            selectinload(FieldMaterialRequest.items).selectinload(
                FieldMaterialRequestItem.item
            )
        )
        .filter(FieldMaterialRequest.is_active.is_(True))
        .filter(FieldMaterialRequest.support_system == PROVIDER)
        .filter(FieldMaterialRequest.support_reference.isnot(None))
        .filter(FieldMaterialRequest.status.in_(_IN_FLIGHT_STATUSES))
        .order_by(FieldMaterialRequest.updated_at.asc())
        .limit(limit)
        .all()
    )

    errors: list[str] = []
    result: dict[str, object] = {"processed": 0, "updated": 0, "errors": errors}
    if not pending:
        return result

    owned_client = client
    created_client = False
    if owned_client is None:
        owned_client = capability_client(db)
        created_client = True

    processed = 0
    updated = 0
    try:
        for request in pending:
            processed += 1
            request_id = str(request.id)
            try:
                response = owned_client.get_material_request_status(request_id)
                if not response:
                    continue
                if apply_material_response(db, request, response):
                    updated += 1
                db.commit()
            except Exception as exc:  # noqa: BLE001 — one bad row can't stall the batch
                db.rollback()
                errors.append(f"{request_id}: {exc}")
                logger.warning(
                    "material_sync: status refresh failed for %s: %s", request_id, exc
                )
                continue
    finally:
        if created_client:
            owned_client.close()

    result["processed"] = processed
    result["updated"] = updated
    return result


def run_refresh_material_request_statuses() -> dict[str, object]:
    """Own the background session for ERP material-outcome reconciliation."""
    from app.db import task_session

    with task_session() as db:
        return refresh_material_request_statuses(db)
