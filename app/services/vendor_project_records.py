"""Canonical participant writers for vendor quote, route, and as-built records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.vendor_routes import (
    AsBuiltLineItem,
    AsBuiltRoute,
    AsBuiltRouteReviewEvent,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteLineItem,
    ProjectQuoteStatus,
    ProposedRouteRevision,
    ProposedRouteRevisionReviewEvent,
    ProposedRouteRevisionStatus,
)
from app.services.common import coerce_uuid
from app.services.events import EventType, emit_event
from app.services.owner_commands import CommandContext
from app.services.vendor_portal_errors import VendorPortalOperationError
from app.services.vendor_portal_operations import (
    _EDITABLE_QUOTES,
    AddVendorQuoteLineCommand,
    CreateVendorQuoteCommand,
    CreateVendorRouteRevisionCommand,
    DeleteVendorQuoteLineCommand,
    ReviewVendorQuoteCommand,
    StageVendorAsBuiltSubmission,
    StageVendorQuoteSubmission,
    SubmitVendorRouteRevisionCommand,
    UpdateVendorQuoteLineCommand,
    VendorPortalOperations,
    _as_built,
    _as_built_submission_eligibility,
    _error,
    _money,
    _project,
    _quote,
    _quote_creation_eligibility,
    _serialize_as_built_review,
    _serialize_quote,
)


@dataclass(frozen=True, slots=True)
class StageVendorAsBuiltReview:
    as_built_id: str
    action: str
    actor_id: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class StageVendorRouteRevisionReview:
    revision_id: str
    action: str
    actor_id: str
    reason: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _geom(geojson: dict):
    return func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(geojson)), 4326)


def _emit_change(
    db: Session,
    event_type: EventType,
    context: CommandContext,
    *,
    action: str,
    aggregate_id: object,
    project_id: object,
    vendor_id: object,
) -> None:
    emit_event(
        db,
        event_type,
        {
            "schema_version": 1,
            "action": action,
            "aggregate_id": str(aggregate_id),
            "project_id": str(project_id),
            "vendor_id": str(vendor_id),
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
        },
        actor=context.actor,
    )


def _recalculate(quote: ProjectQuote) -> None:
    subtotal = sum(
        (_money(item.amount) for item in quote.line_items if item.is_active),
        Decimal("0"),
    )
    quote.subtotal = _money(subtotal)
    quote.tax_total = _money(subtotal * Decimal(str(quote.vat_rate_percent or 0)) / 100)
    quote.total = _money(quote.subtotal + quote.tax_total)


def stage_create_quote(db: Session, command: CreateVendorQuoteCommand) -> dict:
    if command.resolved_currency is None or command.quote_validity_days is None:
        raise _error(
            "invalid_write_evidence",
            "Vendor quote currency and validity evidence are required.",
        )
    project = _project(db, str(command.payload.project_id), for_update=True)
    existing = (
        db.query(ProjectQuote)
        .filter(ProjectQuote.project_id == project.id)
        .filter(ProjectQuote.vendor_id == coerce_uuid(command.vendor_id))
        .filter(ProjectQuote.status.in_(tuple(_EDITABLE_QUOTES)))
        .order_by(ProjectQuote.created_at.desc())
        .first()
    )
    if existing:
        # Returning the vendor's own open draft is a read of a row they already
        # own, so it stays available regardless of the project's current state.
        return _serialize_quote(_quote(db, str(existing.id), command.vendor_id))
    allowed, reason = _quote_creation_eligibility(
        project, command.vendor_id, now=_now()
    )
    if not allowed:
        raise _error("quote_creation_not_allowed", str(reason))
    quote = ProjectQuote(
        project_id=project.id,
        vendor_id=coerce_uuid(command.vendor_id),
        currency=command.resolved_currency,
        vat_rate_percent=command.payload.vat_rate_percent,
        valid_from=_now(),
        valid_until=_now() + timedelta(days=command.quote_validity_days),
        created_by_person_id=coerce_uuid(command.user_id),
    )
    db.add(quote)
    db.flush()
    _emit_change(
        db,
        EventType.vendor_quote_changed,
        command.context,
        action="created",
        aggregate_id=quote.id,
        project_id=project.id,
        vendor_id=command.vendor_id,
    )
    return _serialize_quote(_quote(db, str(quote.id), command.vendor_id))


def stage_add_quote_line(db: Session, command: AddVendorQuoteLineCommand) -> dict:
    quote = _quote(db, command.quote_id, command.vendor_id, for_update=True)
    if quote.status not in _EDITABLE_QUOTES:
        raise _error("quote_not_editable", "Quote is not editable.")
    line = ProjectQuoteLineItem(
        quote_id=quote.id,
        **command.payload.model_dump(),
        amount=_money(command.payload.quantity * command.payload.unit_price),
        is_active=True,
    )
    quote.line_items.append(line)
    _recalculate(quote)
    db.flush()
    _emit_change(
        db,
        EventType.vendor_quote_changed,
        command.context,
        action="line_added",
        aggregate_id=quote.id,
        project_id=quote.project_id,
        vendor_id=quote.vendor_id,
    )
    return _serialize_quote(_quote(db, command.quote_id, command.vendor_id))


def stage_update_quote_line(
    db: Session,
    command: UpdateVendorQuoteLineCommand,
) -> dict:
    quote = _quote(db, command.quote_id, command.vendor_id, for_update=True)
    if quote.status not in _EDITABLE_QUOTES:
        raise _error("quote_not_editable", "Quote is not editable.")
    line = next(
        (
            item
            for item in quote.line_items
            if str(item.id) == command.line_id and item.is_active
        ),
        None,
    )
    if line is None:
        raise _error("quote_line_not_found", "Quote line not found.")
    changes = command.payload.model_dump(exclude_unset=True)
    if "item_type" in changes:
        line.item_type = changes["item_type"]
    if "description" in changes:
        line.description = changes["description"]
    if "cable_type" in changes:
        line.cable_type = changes["cable_type"]
    if "fiber_count" in changes:
        line.fiber_count = changes["fiber_count"]
    if "splice_count" in changes:
        line.splice_count = changes["splice_count"]
    if "quantity" in changes:
        line.quantity = changes["quantity"]
    if "unit_price" in changes:
        line.unit_price = changes["unit_price"]
    if "notes" in changes:
        line.notes = changes["notes"]
    line.amount = _money(line.quantity * line.unit_price)
    _recalculate(quote)
    db.flush()
    _emit_change(
        db,
        EventType.vendor_quote_changed,
        command.context,
        action="line_updated",
        aggregate_id=quote.id,
        project_id=quote.project_id,
        vendor_id=quote.vendor_id,
    )
    return _serialize_quote(_quote(db, command.quote_id, command.vendor_id))


def stage_delete_quote_line(
    db: Session,
    command: DeleteVendorQuoteLineCommand,
) -> dict:
    quote = _quote(db, command.quote_id, command.vendor_id, for_update=True)
    if quote.status not in _EDITABLE_QUOTES:
        raise _error("quote_not_editable", "Quote is not editable.")
    line = next(
        (
            item
            for item in quote.line_items
            if str(item.id) == command.line_id and item.is_active
        ),
        None,
    )
    if line is None:
        raise _error("quote_line_not_found", "Quote line not found.")
    line.is_active = False
    _recalculate(quote)
    db.flush()
    _emit_change(
        db,
        EventType.vendor_quote_changed,
        command.context,
        action="line_deleted",
        aggregate_id=quote.id,
        project_id=quote.project_id,
        vendor_id=quote.vendor_id,
    )
    return _serialize_quote(_quote(db, command.quote_id, command.vendor_id))


def stage_quote_submission(
    db: Session,
    command: StageVendorQuoteSubmission,
) -> dict:
    quote = _quote(db, command.quote_id, command.vendor_id, for_update=True)
    if quote.status not in _EDITABLE_QUOTES:
        raise _error("quote_not_submittable", "Quote is not submittable.")
    active = [item for item in quote.line_items if item.is_active]
    if not active:
        raise _error("quote_line_required", "Quote requires at least one line.")
    _recalculate(quote)
    quote.status = ProjectQuoteStatus.submitted.value
    quote.submitted_at = _now()
    if quote.project.status == InstallationProjectStatus.open_for_bidding.value:
        quote.project.status = InstallationProjectStatus.quoted.value
    db.flush()
    _emit_change(
        db,
        EventType.vendor_quote_changed,
        command.context,
        action="submitted",
        aggregate_id=quote.id,
        project_id=quote.project_id,
        vendor_id=quote.vendor_id,
    )
    return _serialize_quote(_quote(db, command.quote_id, command.vendor_id))


def stage_review_quote(db: Session, command: ReviewVendorQuoteCommand) -> dict:
    quote = _quote(db, command.quote_id, for_update=True)
    project = _project(db, str(quote.project_id), for_update=True)
    if quote.status not in {
        ProjectQuoteStatus.submitted.value,
        ProjectQuoteStatus.under_review.value,
    }:
        raise _error("quote_not_reviewable", "Quote is not reviewable.")
    quote.reviewed_at = _now()
    quote.reviewed_by_person_id = coerce_uuid(command.reviewer_id)
    quote.review_notes = (command.notes or "").strip() or None
    action = "approved" if command.approve else "revision_requested"
    if command.approve:
        quote.status = ProjectQuoteStatus.approved.value
        project.approved_quote_id = quote.id
        project.assigned_vendor_id = quote.vendor_id
        project.status = InstallationProjectStatus.approved.value
        db.flush()
        from app.models.field_erp_sync import FieldErpSyncFlow, flow_owned_by_sub

        if flow_owned_by_sub(db, FieldErpSyncFlow.purchase_order):
            from app.services.dotmac_erp.purchase_order_sync import (
                enqueue_purchase_order,
            )

            enqueue_purchase_order(db, project)
    else:
        quote.status = ProjectQuoteStatus.revision_requested.value
    db.flush()
    _emit_change(
        db,
        EventType.vendor_quote_changed,
        command.context,
        action=action,
        aggregate_id=quote.id,
        project_id=quote.project_id,
        vendor_id=quote.vendor_id,
    )
    return _serialize_quote(_quote(db, command.quote_id))


def stage_create_route_revision(
    db: Session,
    command: CreateVendorRouteRevisionCommand,
) -> dict:
    quote = _quote(db, command.quote_id, command.vendor_id, for_update=True)
    next_number = (
        db.query(func.coalesce(func.max(ProposedRouteRevision.revision_number), 0))
        .filter(ProposedRouteRevision.quote_id == quote.id)
        .scalar()
        + 1
    )
    revision = ProposedRouteRevision(
        quote_id=quote.id,
        revision_number=next_number,
        route_geom=_geom(command.payload.geojson),
        length_meters=command.payload.length_meters,
    )
    db.add(revision)
    db.flush()
    _emit_change(
        db,
        EventType.vendor_route_revision_changed,
        command.context,
        action="created",
        aggregate_id=revision.id,
        project_id=quote.project_id,
        vendor_id=quote.vendor_id,
    )
    return {
        "id": revision.id,
        "quote_id": revision.quote_id,
        "revision_number": revision.revision_number,
        "status": revision.status,
        "length_meters": revision.length_meters,
    }


def stage_submit_route_revision(
    db: Session,
    command: SubmitVendorRouteRevisionCommand,
) -> dict:
    revision = (
        db.query(ProposedRouteRevision)
        .filter(ProposedRouteRevision.id == coerce_uuid(command.revision_id))
        .with_for_update(of=ProposedRouteRevision)
        .one_or_none()
    )
    if revision is None or str(revision.quote.vendor_id) != str(command.vendor_id):
        raise _error("route_revision_not_found", "Route revision not found.")
    if revision.status != ProposedRouteRevisionStatus.draft.value:
        raise _error("route_revision_not_draft", "Route revision is not draft.")
    revision.status = ProposedRouteRevisionStatus.submitted.value
    revision.submitted_at = _now()
    revision.submitted_by_person_id = coerce_uuid(command.user_id)
    db.flush()
    _emit_change(
        db,
        EventType.vendor_route_revision_changed,
        command.context,
        action="submitted",
        aggregate_id=revision.id,
        project_id=revision.quote.project_id,
        vendor_id=revision.quote.vendor_id,
    )
    return {"id": revision.id, "status": revision.status}


def stage_route_revision_review(
    db: Session,
    command: StageVendorRouteRevisionReview,
) -> dict:
    """Stage one locked staff proposed-route decision and immutable event."""

    normalized_actor = command.actor_id.strip()
    if not normalized_actor:
        raise VendorPortalOperationError(
            "actor_required",
            "Review actor is required",
            kind="invalid",
        )
    preview = VendorPortalOperations.preview_route_revision_review(
        db,
        command.revision_id,
        action=command.action,
        reason=command.reason,
        for_update=True,
    )
    revision = (
        db.query(ProposedRouteRevision)
        .filter(ProposedRouteRevision.id == coerce_uuid(command.revision_id))
        .with_for_update(of=ProposedRouteRevision)
        .one()
    )
    previous = str(preview["state"]["from_status"])
    target = str(preview["state"]["to_status"])
    normalized_reason = preview["state"]["reason"]
    event_type = (
        EventType.vendor_route_revision_accepted
        if command.action == "accept"
        else EventType.vendor_route_revision_rejected
    )
    revision.status = target
    revision.reviewed_at = _now()
    revision.reviewed_by_person_id = coerce_uuid(normalized_actor)
    revision.review_notes = normalized_reason
    quote = revision.quote
    project = quote.project
    domain_event = emit_event(
        db,
        event_type,
        {
            "schema_version": 1,
            "revision_id": str(revision.id),
            "quote_id": str(revision.quote_id),
            "project_id": str(quote.project_id),
            "native_project_id": str(project.project_id),
            "vendor_id": str(quote.vendor_id),
            "revision_number": revision.revision_number,
            "from_status": previous,
            "to_status": target,
            "actor_type": "staff_user",
            "actor_id": normalized_actor,
            "reason": normalized_reason,
        },
        actor=normalized_actor,
        subscriber_id=project.subscriber_id,
        account_id=project.subscriber_id,
    )
    evidence = ProposedRouteRevisionReviewEvent(
        event_id=domain_event.event_id,
        revision_id=revision.id,
        quote_id=revision.quote_id,
        project_id=quote.project_id,
        vendor_id=quote.vendor_id,
        event_type=domain_event.event_type.value,
        from_status=previous,
        to_status=target,
        actor_type="staff_user",
        actor_id=normalized_actor,
        reason=normalized_reason,
        occurred_at=domain_event.occurred_at,
    )
    db.add(evidence)
    db.flush()
    result = {
        "id": revision.id,
        "quote_id": revision.quote_id,
        "project_id": quote.project_id,
        "status": revision.status,
        "review_notes": revision.review_notes,
        "reviewed_at": domain_event.occurred_at,
        "review_event_id": str(evidence.id),
        "domain_event_id": str(domain_event.event_id),
    }
    return result


def stage_as_built_submission(
    db: Session,
    command: StageVendorAsBuiltSubmission,
) -> dict:
    project = _project(db, str(command.payload.project_id), for_update=True)
    if str(project.assigned_vendor_id) != str(command.vendor_id):
        raise _error(
            "project_not_assigned",
            "Project is assigned to another vendor.",
        )
    allowed, reason = _as_built_submission_eligibility(project)
    if not allowed:
        raise _error("as_built_submission_not_allowed", str(reason))
    if not command.payload.geojson and not command.payload.line_items:
        raise _error(
            "as_built_evidence_required",
            "Provide a route or line items.",
        )
    row = AsBuiltRoute(
        project_id=project.id,
        proposed_revision_id=command.payload.proposed_revision_id,
        route_geom=(
            _geom(command.payload.geojson) if command.payload.geojson else None
        ),
        actual_length_meters=command.payload.actual_length_meters,
        submitted_at=_now(),
        submitted_by_person_id=coerce_uuid(command.user_id),
        variation_type=command.payload.variation_type,
        variation_reason=command.payload.variation_reason,
        work_order_ref=command.payload.work_order_ref,
        version=max(
            (item.version for item in project.as_built_routes),
            default=0,
        )
        + 1,
    )
    for item in command.payload.line_items:
        row.line_items.append(
            AsBuiltLineItem(
                **item.model_dump(),
                amount=_money(item.quantity * item.unit_price),
            )
        )
    db.add(row)
    db.flush()
    _emit_change(
        db,
        EventType.vendor_as_built_submitted,
        command.context,
        action="submitted",
        aggregate_id=row.id,
        project_id=project.id,
        vendor_id=command.vendor_id,
    )
    return {
        "id": row.id,
        "project_id": row.project_id,
        "status": row.status,
        "actual_length_meters": row.actual_length_meters,
        "submitted_at": row.submitted_at,
        "line_items": row.line_items,
    }


def stage_as_built_review(
    db: Session,
    command: StageVendorAsBuiltReview,
) -> dict:
    """Stage one locked staff evidence decision and immutable event."""

    normalized_actor = command.actor_id.strip()
    if not normalized_actor:
        raise VendorPortalOperationError(
            "actor_required", "Review actor is required", kind="invalid"
        )
    preview = VendorPortalOperations.preview_as_built_review(
        db,
        command.as_built_id,
        action=command.action,
        reason=command.reason,
        for_update=True,
    )
    row = _as_built(db, command.as_built_id, for_update=True)
    previous = str(preview["state"]["from_status"])
    target = str(preview["state"]["to_status"])
    normalized_reason = preview["state"]["reason"]
    event_type = (
        EventType.vendor_as_built_accepted
        if command.action == "accept"
        else EventType.vendor_as_built_rejected
    )
    row.status = target
    row.reviewed_at = _now()
    row.reviewed_by_person_id = coerce_uuid(normalized_actor)
    row.review_notes = normalized_reason
    project = row.project
    domain_event = emit_event(
        db,
        event_type,
        {
            "schema_version": 1,
            "as_built_id": str(row.id),
            "project_id": str(row.project_id),
            "native_project_id": str(project.project_id),
            "vendor_id": str(project.assigned_vendor_id),
            "from_status": previous,
            "to_status": target,
            "actor_type": "staff_user",
            "actor_id": normalized_actor,
            "reason": normalized_reason,
        },
        actor=normalized_actor,
        subscriber_id=project.subscriber_id,
        account_id=project.subscriber_id,
    )
    evidence = AsBuiltRouteReviewEvent(
        event_id=domain_event.event_id,
        as_built_id=row.id,
        project_id=row.project_id,
        vendor_id=project.assigned_vendor_id,
        event_type=domain_event.event_type.value,
        from_status=previous,
        to_status=target,
        actor_type="staff_user",
        actor_id=normalized_actor,
        reason=normalized_reason,
        occurred_at=domain_event.occurred_at,
    )
    db.add(evidence)
    db.flush()
    result = _serialize_as_built_review(row)
    result["review_event_id"] = str(evidence.id)
    result["domain_event_id"] = str(domain_event.event_id)
    result["reviewed_at"] = domain_event.occurred_at
    return result
