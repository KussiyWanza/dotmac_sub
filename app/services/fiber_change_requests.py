import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Uuid, func, select
from sqlalchemy.orm import Session

from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestOperation,
    FiberChangeRequestStatus,
)
from app.models.fiber_physical import FiberConnectorPort, FiberPatchPanel, FiberRack
from app.models.fiber_support import FiberSupportStructure
from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberSegment,
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    FiberTerminationPoint,
    Splitter,
    SplitterPort,
)
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

ASSET_MODEL_MAP = {
    "fdh_cabinet": FdhCabinet,
    "fiber_access_point": FiberAccessPoint,
    "fiber_connector_port": FiberConnectorPort,
    "fiber_patch_panel": FiberPatchPanel,
    "fiber_rack": FiberRack,
    "splice_closure": FiberSpliceClosure,
    "fiber_segment": FiberSegment,
    "fiber_splice": FiberSplice,
    "fiber_splice_tray": FiberSpliceTray,
    "fiber_strand": FiberStrand,
    "fiber_termination_point": FiberTerminationPoint,
    "support_structure": FiberSupportStructure,
    "splitter": Splitter,
    "splitter_port": SplitterPort,
}


def _normalize_asset_type(asset_type: str) -> str:
    normalized = (asset_type or "").strip().lower()
    if normalized == "fiber_splice_closure":
        return "splice_closure"
    return normalized


def _geojson_to_geom(geojson: dict) -> object:
    geojson_str = json.dumps(geojson)
    return func.ST_SetSRID(func.ST_GeomFromGeoJSON(geojson_str), 4326)


def _prepare_payload(model, payload: dict) -> dict:
    data = dict(payload or {})
    # Reserved intake-provenance section: retained on the stored request for
    # audit, never applied to the asset.
    data.pop("provenance", None)
    geojson_value = data.pop("geojson", None)
    if geojson_value:
        if hasattr(model, "route_geom"):
            data["route_geom"] = geojson_value
        elif hasattr(model, "geom"):
            data["geom"] = geojson_value
    for key in ("route_geom", "geom"):
        if key in data and isinstance(data[key], dict):
            data[key] = _geojson_to_geom(data[key])
    for key, value in tuple(data.items()):
        column = model.__table__.columns.get(key)
        if (
            column is not None
            and isinstance(column.type, Uuid)
            and isinstance(value, str)
        ):
            data[key] = coerce_uuid(value)
    return data


def _get_model(asset_type: str):
    normalized = _normalize_asset_type(asset_type)
    model = ASSET_MODEL_MAP.get(normalized)
    if not model:
        raise HTTPException(status_code=400, detail="Unsupported asset type")
    return normalized, model


def create_request(
    db: Session,
    asset_type: str,
    asset_id: str | None,
    operation: FiberChangeRequestOperation,
    payload: dict,
    requested_by_person_id: str | None,
    requested_by_vendor_id: str | None,
    *,
    commit: bool = True,
):
    normalized, _ = _get_model(asset_type)
    if normalized == "fiber_splice" and not (payload or {}).get(
        "physical_link_decision_id"
    ):
        raise HTTPException(
            status_code=410,
            detail=(
                "Legacy strand-pair splice requests are retired; provide an exact "
                "reviewed network.fiber_physical_continuity decision."
            ),
        )
    request = FiberChangeRequest(
        asset_type=normalized,
        asset_id=asset_id,
        operation=operation,
        payload=payload,
        status=FiberChangeRequestStatus.pending,
        requested_by_person_id=coerce_uuid(requested_by_person_id)
        if requested_by_person_id
        else None,
        requested_by_vendor_id=coerce_uuid(requested_by_vendor_id)
        if requested_by_vendor_id
        else None,
    )
    db.add(request)
    if commit:
        db.commit()
        db.refresh(request)
    else:
        db.flush()
    return request


def list_requests(db: Session, status: FiberChangeRequestStatus | None = None):
    stmt = select(FiberChangeRequest)
    if status:
        stmt = stmt.where(FiberChangeRequest.status == status)
    return list(db.scalars(stmt.order_by(FiberChangeRequest.created_at.desc())).all())


def get_request(db: Session, request_id: str) -> FiberChangeRequest:
    request = db.get(FiberChangeRequest, request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Change request not found")
    return request


def reject_request(
    db: Session, request_id: str, reviewer_person_id: str, review_notes: str | None
):
    request = get_request(db, request_id)
    if request.status != FiberChangeRequestStatus.pending:
        raise HTTPException(status_code=400, detail="Change request already processed")
    if request.asset_type == "fiber_splice" and (request.payload or {}).get(
        "physical_link_decision_id"
    ):
        from app.models.fiber_physical import FiberPhysicalLinkDecision
        from app.services.network.fiber_physical_continuity import (
            FiberPhysicalContinuityError,
            decline_physical_link,
        )

        decision = db.get(
            FiberPhysicalLinkDecision,
            coerce_uuid(request.payload["physical_link_decision_id"]),
        )
        if decision is None or decision.link_type != "core_splice":
            raise HTTPException(
                status_code=422,
                detail="Exact core-splice decision not found",
            )
        if decision.status == "proposed":
            try:
                decline_physical_link(
                    db,
                    decision.id,
                    reviewed_by=f"fiber-change-reviewer:{reviewer_person_id}",
                    review_notes=review_notes
                    or "Exact core splice declined through fiber change request",
                    commit=False,
                )
            except FiberPhysicalContinuityError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        elif decision.status != "declined":
            raise HTTPException(
                status_code=400,
                detail="Exact core-splice decision is no longer reviewable",
            )
    request.status = FiberChangeRequestStatus.rejected
    request.reviewed_by_person_id = coerce_uuid(reviewer_person_id)
    request.review_notes = review_notes
    request.reviewed_at = datetime.now(UTC)
    db.commit()
    db.refresh(request)
    return request


def _apply_request(
    db: Session,
    request: FiberChangeRequest,
    *,
    reviewer_person_id: str,
    review_notes: str | None,
):
    normalized, model = _get_model(request.asset_type)
    if normalized == "fiber_splice":
        from app.models.fiber_physical import FiberPhysicalLinkDecision
        from app.services.network.fiber_physical_continuity import (
            FiberPhysicalContinuityError,
            approve_physical_link,
            execute_physical_link,
        )

        if request.operation != FiberChangeRequestOperation.create:
            raise HTTPException(
                status_code=410,
                detail="Legacy splice update/delete is retired; use an exact disconnect decision.",
            )
        raw_decision_id = (request.payload or {}).get("physical_link_decision_id")
        if raw_decision_id is None:
            raise HTTPException(
                status_code=410,
                detail="Legacy splice request has no exact physical-link decision.",
            )
        decision_id = coerce_uuid(raw_decision_id)
        decision = db.get(FiberPhysicalLinkDecision, decision_id)
        if decision is None or decision.link_type != "core_splice":
            raise HTTPException(
                status_code=422,
                detail="Exact core-splice decision not found",
            )
        actor = f"fiber-change-reviewer:{reviewer_person_id}"
        notes = (
            review_notes or "Exact core splice reviewed through fiber change request"
        )
        try:
            if decision.status == "proposed":
                decision = approve_physical_link(
                    db,
                    decision.id,
                    reviewed_by=actor,
                    review_notes=notes,
                    commit=False,
                )
            if decision.status == "approved":
                decision = execute_physical_link(
                    db,
                    decision.id,
                    executed_by=actor,
                    commit=False,
                )
        except FiberPhysicalContinuityError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if decision.status != "applied":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Exact core-splice decision closed without mutation: "
                    f"{decision.closed_reason or decision.status}"
                ),
            )
        request.asset_id = decision.id
        return
    if normalized == "support_structure":
        from app.services.network.fiber_support_structures import (
            FiberSupportStructureError,
            apply_reviewed_support_change,
        )

        try:
            support = apply_reviewed_support_change(
                db,
                operation=request.operation,
                asset_id=request.asset_id,
                payload=request.payload,
            )
        except FiberSupportStructureError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        request.asset_id = support.id
        return
    if normalized in {"fiber_rack", "fiber_patch_panel", "fiber_connector_port"}:
        from app.services.network.fiber_physical_continuity import (
            FiberPhysicalContinuityError,
            apply_reviewed_physical_inventory_change,
        )

        try:
            asset = apply_reviewed_physical_inventory_change(
                db,
                asset_type=normalized,
                operation=request.operation,
                asset_id=request.asset_id,
                payload=request.payload,
            )
        except FiberPhysicalContinuityError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        request.asset_id = asset.id
        return
    payload = _prepare_payload(model, request.payload)
    if normalized in {"splitter", "splitter_port"}:
        from app.services.network.splitters import splitter_ports, splitters

        owner = splitters if normalized == "splitter" else splitter_ports
        if request.operation == FiberChangeRequestOperation.create:
            delegated_asset = owner.create(db, payload, commit=False)
            request.asset_id = delegated_asset.id
            return
        if not request.asset_id:
            raise HTTPException(
                status_code=400,
                detail=f"Missing asset_id for {request.operation.value}",
            )
        if request.operation == FiberChangeRequestOperation.update:
            owner.update(db, str(request.asset_id), payload, commit=False)
            return
        if request.operation == FiberChangeRequestOperation.delete:
            owner.delete(db, str(request.asset_id), commit=False)
            return
        raise HTTPException(status_code=400, detail="Invalid operation")
    from app.services.network.fiber_plant_integrity import (
        FiberPlantIntegrityError,
        ensure_segment_strand_inventory,
        validate_active_segment,
        validate_operational_termination,
        validate_segment_retirement,
        validate_strand_retirement,
        validate_strand_segment_capacity,
        validate_termination_change,
    )

    def fail_closed(exc: FiberPlantIntegrityError) -> None:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if request.operation == FiberChangeRequestOperation.create:
        created_asset: Any = model(**payload)
        if getattr(created_asset, "id", None) is None:
            created_asset.id = uuid.uuid4()
        try:
            if isinstance(created_asset, FiberSegment):
                validate_active_segment(db, created_asset)
            elif isinstance(created_asset, FiberTerminationPoint):
                if created_asset.is_active:
                    validate_operational_termination(db, created_asset)
            elif isinstance(created_asset, FiberStrand):
                validate_strand_segment_capacity(db, created_asset)
        except FiberPlantIntegrityError as exc:
            fail_closed(exc)
        db.add(created_asset)
        db.flush()
        if isinstance(created_asset, FiberSegment):
            try:
                ensure_segment_strand_inventory(db, created_asset)
                db.flush()
            except FiberPlantIntegrityError as exc:
                fail_closed(exc)
        request.asset_id = created_asset.id
    elif request.operation == FiberChangeRequestOperation.update:
        if not request.asset_id:
            raise HTTPException(status_code=400, detail="Missing asset_id for update")
        target_asset: Any = db.get(model, request.asset_id)
        if not target_asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        try:
            if isinstance(target_asset, FiberSegment) and target_asset.is_active:
                if payload.get("is_active") is False:
                    validate_segment_retirement(db, target_asset)
            elif isinstance(target_asset, FiberTerminationPoint):
                validate_termination_change(db, target_asset, changes=payload)
            elif isinstance(target_asset, FiberStrand) and (
                payload.get("is_active") is False
                or (
                    "segment_id" in payload
                    and payload["segment_id"] != target_asset.segment_id
                )
            ):
                validate_strand_retirement(db, target_asset)
        except FiberPlantIntegrityError as exc:
            fail_closed(exc)
        for key, value in payload.items():
            if key in {"id", "created_at", "updated_at"}:
                continue
            if hasattr(target_asset, key):
                setattr(target_asset, key, value)
        try:
            if isinstance(target_asset, FiberSegment):
                validate_active_segment(db, target_asset)
                ensure_segment_strand_inventory(db, target_asset)
            elif isinstance(target_asset, FiberTerminationPoint):
                if target_asset.is_active:
                    validate_operational_termination(db, target_asset)
            elif isinstance(target_asset, FiberStrand):
                validate_strand_segment_capacity(db, target_asset)
            db.flush()
        except FiberPlantIntegrityError as exc:
            fail_closed(exc)
    elif request.operation == FiberChangeRequestOperation.delete:
        if not request.asset_id:
            raise HTTPException(status_code=400, detail="Missing asset_id for delete")
        target_asset = db.get(model, request.asset_id)
        if not target_asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        try:
            if isinstance(target_asset, FiberSegment):
                validate_segment_retirement(db, target_asset)
            elif isinstance(target_asset, FiberTerminationPoint):
                validate_termination_change(
                    db, target_asset, changes={"is_active": False}
                )
            elif isinstance(target_asset, FiberStrand):
                validate_strand_retirement(db, target_asset)
        except FiberPlantIntegrityError as exc:
            fail_closed(exc)
        if hasattr(target_asset, "is_active"):
            target_asset.is_active = False
        else:
            db.delete(target_asset)
    else:
        raise HTTPException(status_code=400, detail="Invalid operation")


def approve_request(
    db: Session, request_id: str, reviewer_person_id: str, review_notes: str | None
):
    request = get_request(db, request_id)
    if request.status != FiberChangeRequestStatus.pending:
        raise HTTPException(status_code=400, detail="Change request already processed")
    _apply_request(
        db,
        request,
        reviewer_person_id=reviewer_person_id,
        review_notes=review_notes,
    )
    request.status = FiberChangeRequestStatus.applied
    request.reviewed_by_person_id = coerce_uuid(reviewer_person_id)
    request.review_notes = review_notes
    request.reviewed_at = datetime.now(UTC)
    request.applied_at = datetime.now(UTC)
    db.commit()
    db.refresh(request)
    return request
