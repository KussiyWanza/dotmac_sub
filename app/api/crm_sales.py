"""Native leads, pipeline, and quotes API ported from CRM.

Ported from ``dotmac_crm/app/api/crm/sales.py`` (same paths, mounted under
``/api/v1``). Permission tightening per §2.4: the CRM gated leads on
``crm:lead:{read,write}`` but left pipelines and quotes auth-only —
during the port quotes gain ``crm:quote:{read,write}`` and pipelines ride
``crm:lead:*`` (they are lead-vertical infrastructure; §2.4 introduces no
dedicated pipeline key). The native sales RBAC contract owns seeding these
keys; until they exist, the permissions resolve for admins only.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_permission
from app.schemas.common import ListResponse
from app.schemas.sales import (
    LeadAccountConversionRead,
    LeadAccountConversionRequest,
    LeadCaptureRead,
    LeadCaptureRequest,
    LeadCreate,
    LeadRead,
    LeadUpdate,
    PipelineCreate,
    PipelineRead,
    PipelineStageCreate,
    PipelineStageRead,
    PipelineStageUpdate,
    PipelineUpdate,
    QuoteCreate,
    QuoteLineItemCreate,
    QuoteLineItemRead,
    QuoteLineItemUpdate,
    QuoteRead,
    QuoteUpdate,
)
from app.services import sales as sales_service
from app.services.sales import account_conversion, capture

router = APIRouter(prefix="/crm", tags=["crm-sales"])


def _actor(principal: dict) -> str:
    return str(
        principal.get("user_id")
        or principal.get("subscriber_id")
        or principal.get("principal_id")
        or "authenticated-user"
    )


def _capture_error(exc: capture.LeadCaptureError):
    status_code = {"not_found": 404, "invalid": 422}.get(exc.kind, 409)
    from fastapi import HTTPException

    raise HTTPException(
        status_code=status_code, detail={"code": exc.code, "message": str(exc)}
    ) from exc


def _conversion_error(exc: account_conversion.LeadAccountConversionError):
    status_code = {"not_found": 404, "invalid": 422}.get(exc.kind, 409)
    from fastapi import HTTPException

    raise HTTPException(
        status_code=status_code, detail={"code": exc.code, "message": str(exc)}
    ) from exc


@router.post(
    "/pipelines",
    response_model=PipelineRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def create_pipeline(payload: PipelineCreate, db: Session = Depends(get_db)):
    return sales_service.pipelines.create(db, payload)


@router.get(
    "/pipelines",
    response_model=ListResponse[PipelineRead],
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def list_pipelines(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return sales_service.pipelines.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.get(
    "/pipelines/{pipeline_id}",
    response_model=PipelineRead,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def get_pipeline(pipeline_id: str, db: Session = Depends(get_db)):
    return sales_service.pipelines.get(db, pipeline_id)


@router.patch(
    "/pipelines/{pipeline_id}",
    response_model=PipelineRead,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def update_pipeline(
    pipeline_id: str, payload: PipelineUpdate, db: Session = Depends(get_db)
):
    return sales_service.pipelines.update(db, pipeline_id, payload)


@router.delete(
    "/pipelines/{pipeline_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def delete_pipeline(pipeline_id: str, db: Session = Depends(get_db)):
    sales_service.pipelines.delete(db, pipeline_id)


@router.post(
    "/pipelines/{pipeline_id}/stages",
    response_model=PipelineStageRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def create_pipeline_stage(
    pipeline_id: str, payload: PipelineStageCreate, db: Session = Depends(get_db)
):
    data = payload.model_copy(update={"pipeline_id": pipeline_id})
    return sales_service.pipeline_stages.create(db, data)


@router.get(
    "/pipelines/{pipeline_id}/stages",
    response_model=ListResponse[PipelineStageRead],
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def list_pipeline_stages(
    pipeline_id: str,
    is_active: bool | None = None,
    order_by: str = Query(default="order_index"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return sales_service.pipeline_stages.list_response(
        db, pipeline_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/pipeline-stages/{stage_id}",
    response_model=PipelineStageRead,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def update_pipeline_stage(
    stage_id: str, payload: PipelineStageUpdate, db: Session = Depends(get_db)
):
    return sales_service.pipeline_stages.update(db, stage_id, payload)


@router.post(
    "/leads",
    response_model=LeadRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def create_lead(payload: LeadCreate, db: Session = Depends(get_db)):
    return sales_service.leads.create(db, payload)


@router.post(
    "/leads/capture",
    response_model=LeadCaptureRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def capture_lead(
    payload: LeadCaptureRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(get_current_user),
):
    try:
        result = capture.capture_lead(db, payload, actor_id=_actor(principal))
    except capture.LeadCaptureError as exc:
        return _capture_error(exc)
    return LeadCaptureRead(
        lead_id=result.lead.id,
        party_id=result.party_id,
        origin_capture_id=result.origin.id,
        replayed=result.replayed,
    )


@router.post(
    "/leads/capture/integration/{receipt_id}",
    response_model=LeadCaptureRead,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def capture_verified_lead_receipt(
    receipt_id: UUID,
    payload: LeadCaptureRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(get_current_user),
):
    try:
        result = capture.capture_verified_receipt(
            db, receipt_id=receipt_id, payload=payload, actor_id=_actor(principal)
        )
    except capture.LeadCaptureError as exc:
        return _capture_error(exc)
    return LeadCaptureRead(
        lead_id=result.lead.id,
        party_id=result.party_id,
        origin_capture_id=result.origin.id,
        replayed=result.replayed,
    )


@router.post(
    "/leads/{lead_id}/account",
    response_model=LeadAccountConversionRead,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def convert_lead_account(
    lead_id: UUID,
    payload: LeadAccountConversionRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(get_current_user),
):
    try:
        result = account_conversion.convert_lead_account(
            db,
            lead_id=lead_id,
            party_id=payload.party_id,
            subscriber_id=payload.subscriber_id,
            new_account=payload.new_account,
            actor_id=_actor(principal),
        )
    except account_conversion.LeadAccountConversionError as exc:
        return _conversion_error(exc)
    return LeadAccountConversionRead(**result.__dict__)


@router.get(
    "/leads",
    response_model=ListResponse[LeadRead],
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def list_leads(
    pipeline_id: str | None = None,
    stage_id: str | None = None,
    owner_agent_id: str | None = None,
    status: str | None = None,
    lead_source: str | None = None,
    search: str | None = Query(default=None, max_length=200),
    is_active: bool | None = None,
    order_by: str = Query(default="updated_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return sales_service.leads.list_response(
        db,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        owner_agent_id=owner_agent_id,
        status=status,
        lead_source=lead_source,
        search=search,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/leads/{lead_id}",
    response_model=LeadRead,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def get_lead(lead_id: str, db: Session = Depends(get_db)):
    return sales_service.leads.get(db, lead_id)


@router.patch(
    "/leads/{lead_id}",
    response_model=LeadRead,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def update_lead(lead_id: str, payload: LeadUpdate, db: Session = Depends(get_db)):
    return sales_service.leads.update(db, lead_id, payload)


@router.delete(
    "/leads/{lead_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("crm:lead:delete"))],
)
def delete_lead(lead_id: str, db: Session = Depends(get_db)):
    sales_service.leads.delete(db, lead_id)


@router.post(
    "/quotes",
    response_model=QuoteRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def create_quote(payload: QuoteCreate, db: Session = Depends(get_db)):
    return sales_service.quotes.create(db, payload)


@router.get(
    "/quotes",
    response_model=ListResponse[QuoteRead],
    dependencies=[Depends(require_permission("crm:quote:read"))],
)
def list_quotes(
    lead_id: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="updated_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return sales_service.quotes.list_response(
        db, lead_id, status, is_active, order_by, order_dir, limit, offset
    )


@router.get(
    "/quotes/{quote_id}",
    response_model=QuoteRead,
    dependencies=[Depends(require_permission("crm:quote:read"))],
)
def get_quote(quote_id: str, db: Session = Depends(get_db)):
    return sales_service.quotes.get(db, quote_id)


@router.patch(
    "/quotes/{quote_id}",
    response_model=QuoteRead,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def update_quote(quote_id: str, payload: QuoteUpdate, db: Session = Depends(get_db)):
    return sales_service.quotes.update(db, quote_id, payload)


@router.delete(
    "/quotes/{quote_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def delete_quote(quote_id: str, db: Session = Depends(get_db)):
    sales_service.quotes.delete(db, quote_id)


@router.post(
    "/quotes/{quote_id}/line-items",
    response_model=QuoteLineItemRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def create_quote_line_item(
    quote_id: str, payload: QuoteLineItemCreate, db: Session = Depends(get_db)
):
    data = payload.model_copy(update={"quote_id": quote_id})
    return sales_service.quote_line_items.create(db, data)


@router.get(
    "/quotes/{quote_id}/line-items",
    response_model=ListResponse[QuoteLineItemRead],
    dependencies=[Depends(require_permission("crm:quote:read"))],
)
def list_quote_line_items(
    quote_id: str,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return sales_service.quote_line_items.list_response(
        db, quote_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/quote-line-items/{item_id}",
    response_model=QuoteLineItemRead,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def update_quote_line_item(
    item_id: str, payload: QuoteLineItemUpdate, db: Session = Depends(get_db)
):
    return sales_service.quote_line_items.update(db, item_id, payload)
