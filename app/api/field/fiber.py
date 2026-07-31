from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.common import ListResponse
from app.schemas.field import (
    FieldCableRegistrationCreate,
    FieldCableRegistrationResponse,
    FieldFiberCustomerTraceRead,
    FieldFiberSourceObservationCreate,
    FieldFiberSourceObservationRead,
    FieldFiberTestCreate,
    FieldFiberTestRead,
    FieldFiberWorkOrderEvidenceMapRead,
    FieldJobEvidenceRead,
    FieldOntAttachmentCreate,
    FieldOntAttachmentResponse,
    FieldSpliceCreate,
    FieldSplicePlanResponse,
    FieldSpliceProposalResponse,
    FieldSpliceProposalStatusRead,
    FieldStrandDamageCreate,
    FieldStrandDamageResponse,
)
from app.services.auth_dependencies import require_user_auth
from app.services.field import fiber as field_fiber

router = APIRouter(prefix="/fiber", tags=["field-fiber"])


@router.post(
    "/splices",
    response_model=FieldSpliceProposalResponse,
    status_code=status.HTTP_201_CREATED,
)
def propose_field_splice(
    payload: FieldSpliceCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    receipt = field_fiber.propose_splice(
        db,
        auth,
        closure_id=str(payload.closure_id),
        from_strand_id=str(payload.from_strand_id),
        from_strand_end=payload.from_strand_end,
        to_strand_id=str(payload.to_strand_id),
        to_strand_end=payload.to_strand_end,
        tray_id=str(payload.tray_id) if payload.tray_id else None,
        position=payload.position,
        splice_type=payload.splice_type,
        loss_db=payload.loss_db,
        note=payload.note,
        work_order_id=payload.work_order_id,
        plan_item_id=str(payload.plan_item_id) if payload.plan_item_id else None,
    )
    return receipt.to_dict()


@router.post(
    "/tests",
    response_model=FieldFiberTestRead,
    status_code=status.HTTP_201_CREATED,
)
def record_field_fiber_test(
    payload: FieldFiberTestCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_fiber.record_test(
        db,
        auth,
        crm_work_order_id=payload.crm_work_order_id,
        asset_type=payload.asset_type,
        asset_id=str(payload.asset_id),
        test_type=payload.test_type,
        wavelength_nm=payload.wavelength_nm,
        value_db=payload.value_db,
        unit=payload.unit,
        passed=payload.passed,
        instrument=payload.instrument,
        measured_at=payload.measured_at,
        notes=payload.notes,
        attachment_id=str(payload.attachment_id) if payload.attachment_id else None,
        client_ref=str(payload.client_ref) if payload.client_ref else None,
    )


@router.get("/tests", response_model=ListResponse[FieldFiberTestRead])
def list_field_fiber_tests(
    crm_work_order_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    items = field_fiber.list_tests(db, auth, crm_work_order_id=crm_work_order_id)
    return {"items": items, "count": len(items), "limit": len(items), "offset": 0}


@router.post(
    "/source-observations",
    response_model=FieldFiberSourceObservationRead,
    status_code=status.HTTP_201_CREATED,
)
def record_field_fiber_source_observation(
    payload: FieldFiberSourceObservationCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_fiber.record_source_observation(
        db,
        auth,
        work_order_public_id=payload.work_order_id,
        staged_feature_id=str(payload.staged_feature_id),
        expected_feature_content_sha256=payload.expected_feature_content_sha256,
        verification_scope=payload.verification_scope,
        outcome=payload.outcome,
        observed_at=payload.observed_at,
        client_ref=str(payload.client_ref),
        observed_external_label=payload.observed_external_label,
        observed_asset_type=payload.observed_asset_type,
        observed_asset_id=(
            str(payload.observed_asset_id) if payload.observed_asset_id else None
        ),
        start_endpoint_type=payload.start_endpoint_type,
        start_endpoint_ref_id=(
            str(payload.start_endpoint_ref_id)
            if payload.start_endpoint_ref_id
            else None
        ),
        end_endpoint_type=payload.end_endpoint_type,
        end_endpoint_ref_id=(
            str(payload.end_endpoint_ref_id) if payload.end_endpoint_ref_id else None
        ),
        latitude=payload.latitude,
        longitude=payload.longitude,
        accuracy_m=payload.accuracy_m,
        instrument=payload.instrument,
        measurement_payload=payload.measurement_payload,
        attachment_ids=[str(value) for value in payload.attachment_ids],
        notes=payload.notes,
    )


@router.get(
    "/source-observations",
    response_model=ListResponse[FieldFiberSourceObservationRead],
)
def list_field_fiber_source_observations(
    work_order_id: str = Query(min_length=1, max_length=64),
    staged_feature_id: str | None = Query(default=None),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    items = field_fiber.list_source_observations(
        db,
        auth,
        work_order_public_id=work_order_id,
        staged_feature_id=staged_feature_id,
    )
    return {"items": items, "count": len(items), "limit": len(items), "offset": 0}


@router.post(
    "/ont-attachments",
    response_model=FieldOntAttachmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def propose_field_ont_attachment(
    payload: FieldOntAttachmentCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    receipt = field_fiber.propose_ont_attachment(
        db,
        auth,
        crm_work_order_id=payload.work_order_id,
        ont_unit_id=str(payload.ont_unit_id),
        splitter_port_id=str(payload.splitter_port_id),
        note=payload.note,
    )
    return receipt.to_dict()


@router.post(
    "/cable-registrations",
    response_model=FieldCableRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_field_cable(
    payload: FieldCableRegistrationCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    receipt = field_fiber.register_cable(
        db,
        auth,
        name=payload.name,
        fiber_count=payload.fiber_count,
        segment_type=payload.segment_type,
        cable_type=payload.cable_type,
        fibers_per_tube=payload.fibers_per_tube,
        color_standard=payload.color_standard,
        length_m=payload.length_m,
        notes=payload.notes,
        work_order_id=payload.work_order_id,
    )
    return receipt.to_dict()


@router.post(
    "/strand-damage-reports",
    response_model=FieldStrandDamageResponse,
    status_code=status.HTTP_201_CREATED,
)
def report_field_strand_damage(
    payload: FieldStrandDamageCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    receipt = field_fiber.report_strand_damage(
        db,
        auth,
        note=payload.note,
        strand_id=str(payload.strand_id) if payload.strand_id else None,
        segment_id=str(payload.segment_id) if payload.segment_id else None,
        tube_number=payload.tube_number,
        work_order_id=payload.work_order_id,
    )
    return receipt.to_dict()


@router.get(
    "/job-evidence",
    response_model=FieldJobEvidenceRead,
)
def get_field_job_evidence(
    work_order_id: str = Query(min_length=1, max_length=64),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_fiber.get_job_evidence(db, auth, crm_work_order_id=work_order_id)


@router.get(
    "/splice-plan",
    response_model=FieldSplicePlanResponse,
)
def get_field_splice_plan(
    work_order_id: str = Query(min_length=1, max_length=64),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_fiber.get_splice_plan(db, auth, crm_work_order_id=work_order_id)


@router.get(
    "/customer-trace",
    response_model=ListResponse[FieldFiberCustomerTraceRead],
)
def list_field_fiber_customer_traces(
    work_order_id: str = Query(min_length=1, max_length=64),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    traces = field_fiber.list_customer_traces(
        db,
        auth,
        crm_work_order_id=work_order_id,
    )
    items = [trace.to_dict() for trace in traces]
    return {"items": items, "count": len(items), "limit": len(items), "offset": 0}


@router.get(
    "/splice-proposals",
    response_model=ListResponse[FieldSpliceProposalStatusRead],
)
def list_field_splice_proposals(
    limit: int = Query(default=50, ge=1, le=200),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    proposals = field_fiber.list_splice_proposals(db, auth, limit=limit)
    items = [proposal.to_dict() for proposal in proposals]
    return {"items": items, "count": len(items), "limit": limit, "offset": 0}


@router.get(
    "/work-order-evidence-map",
    response_model=FieldFiberWorkOrderEvidenceMapRead,
)
def get_field_fiber_work_order_evidence_map(
    work_order_id: str = Query(min_length=1, max_length=64),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    report = field_fiber.get_work_order_evidence_map(
        db,
        auth,
        work_order_public_id=work_order_id,
    )
    return report.to_dict()
