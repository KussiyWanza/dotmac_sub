from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.comms import (
    CustomerNotificationCreate,
    CustomerNotificationRead,
    CustomerNotificationUpdate,
    EtaUpdateCreate,
    EtaUpdateRead,
    SurveyCreate,
    SurveyRead,
    SurveyResponseCreate,
    SurveyResponseRead,
    SurveyUpdate,
)
from app.services import comms as comms_service
from app.services import surveys as survey_service
from app.services.owner_commands import CommandContext
from app.services.response import list_response

router = APIRouter(prefix="/comms", tags=["comms"])


def _survey_http_error(exc: survey_service.SurveyDomainError) -> HTTPException:
    status_code = {
        "invalid": 400,
        "forbidden": 403,
        "not_found": 404,
        "conflict": 409,
    }.get(exc.kind, 409)
    return HTTPException(status_code=status_code, detail=exc.message)


def _survey_actor(request: Request) -> tuple[UUID, str]:
    auth = getattr(request.state, "auth", {})
    try:
        principal_id = UUID(str(auth.get("principal_id") or ""))
    except ValueError as exc:
        raise HTTPException(
            status_code=403, detail="A resolved administrator is required."
        ) from exc
    return principal_id, str(auth.get("principal_type") or "")


def _survey_context(
    request: Request, *, reason: str, idempotency_key: str | None = None
) -> CommandContext:
    principal_id, principal_type = _survey_actor(request)
    return CommandContext.system(
        actor=f"{principal_type}:{principal_id}",
        scope="communications.surveys:write",
        reason=reason,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/customer-notifications",
    response_model=CustomerNotificationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_customer_notification(
    payload: CustomerNotificationCreate, db: Session = Depends(get_db)
):
    return comms_service.customer_notifications.create(db, payload)


@router.get(
    "/customer-notifications/{event_id}", response_model=CustomerNotificationRead
)
def get_customer_notification(event_id: str, db: Session = Depends(get_db)):
    return comms_service.customer_notifications.get(db, event_id)


@router.get(
    "/customer-notifications",
    response_model=ListResponse[CustomerNotificationRead],
)
def list_customer_notifications(
    entity_type: str | None = None,
    entity_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = comms_service.customer_notifications.list(
        db, entity_type, entity_id, status, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch(
    "/customer-notifications/{event_id}",
    response_model=CustomerNotificationRead,
)
def update_customer_notification(
    event_id: str, payload: CustomerNotificationUpdate, db: Session = Depends(get_db)
):
    return comms_service.customer_notifications.update(db, event_id, payload)


@router.post(
    "/eta-updates",
    response_model=EtaUpdateRead,
    status_code=status.HTTP_201_CREATED,
)
def create_eta_update(payload: EtaUpdateCreate, db: Session = Depends(get_db)):
    return comms_service.eta_updates.create(db, payload)


@router.get("/eta-updates/{update_id}", response_model=EtaUpdateRead)
def get_eta_update(update_id: str, db: Session = Depends(get_db)):
    return comms_service.eta_updates.get(db, update_id)


@router.get("/eta-updates", response_model=ListResponse[EtaUpdateRead])
def list_eta_updates(
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = comms_service.eta_updates.list(
        db, order_by=order_by, order_dir=order_dir, limit=limit, offset=offset
    )
    return list_response(items, limit, offset)


@router.post("/surveys", response_model=SurveyRead, status_code=status.HTTP_201_CREATED)
def create_survey(
    payload: SurveyCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    principal_id, principal_type = _survey_actor(request)
    key = (idempotency_key or str(uuid4())).strip()
    try:
        outcome = survey_service.create_survey(
            db,
            survey_service.CreateSurveyCommand(
                payload=payload,
                principal_id=principal_id,
                principal_type=principal_type,
                context=_survey_context(
                    request,
                    reason="create Survey through admin API",
                    idempotency_key=key,
                ),
            ),
        )
        return survey_service.get_survey(db, outcome.survey_id)
    except survey_service.SurveyDomainError as exc:
        raise _survey_http_error(exc) from exc


@router.get("/surveys/{survey_id}", response_model=SurveyRead)
def get_survey(survey_id: str, db: Session = Depends(get_db)):
    try:
        return survey_service.get_survey(db, survey_id)
    except survey_service.SurveyDomainError as exc:
        raise _survey_http_error(exc) from exc


@router.get("/surveys", response_model=ListResponse[SurveyRead])
def list_surveys(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = survey_service.list_surveys(
        db, is_active, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch("/surveys/{survey_id}", response_model=SurveyRead)
def update_survey(
    survey_id: str,
    payload: SurveyUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        outcome = survey_service.update_survey(
            db,
            survey_service.UpdateSurveyCommand(
                survey_id=UUID(survey_id),
                payload=payload,
                context=_survey_context(
                    request, reason="update Survey through admin API"
                ),
            ),
        )
        return survey_service.get_survey(db, outcome.survey_id)
    except (ValueError, survey_service.SurveyDomainError) as exc:
        if isinstance(exc, survey_service.SurveyDomainError):
            raise _survey_http_error(exc) from exc
        raise HTTPException(status_code=404, detail="Survey not found.") from exc


@router.delete("/surveys/{survey_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_survey(survey_id: str, request: Request, db: Session = Depends(get_db)):
    try:
        survey_service.transition_survey(
            db,
            survey_service.SurveyLifecycleCommand(
                survey_id=UUID(survey_id),
                action=survey_service.SurveyLifecycleAction.archive,
                context=_survey_context(
                    request, reason="archive Survey through admin API"
                ),
            ),
        )
    except (ValueError, survey_service.SurveyDomainError) as exc:
        if isinstance(exc, survey_service.SurveyDomainError):
            raise _survey_http_error(exc) from exc
        raise HTTPException(status_code=404, detail="Survey not found.") from exc


@router.post(
    "/survey-responses",
    response_model=SurveyResponseRead,
    status_code=status.HTTP_201_CREATED,
)
def create_survey_response(
    payload: SurveyResponseCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        outcome = survey_service.submit_response(
            db,
            survey_service.SubmitSurveyResponseCommand(
                public_reference=str(payload.survey_id),
                invitation_token=None,
                answers=tuple(
                    survey_service.SurveyAnswer(key=key, value=value)
                    for key, value in (payload.responses or {}).items()
                ),
                work_order_id=payload.work_order_id,
                ticket_id=payload.ticket_id,
                context=_survey_context(
                    request,
                    reason="record Survey response through admin API",
                    idempotency_key=str(uuid4()),
                ),
                legacy_rating=payload.rating,
                legacy_nps_value=payload.nps_value,
            ),
        )
        return survey_service.get_response(db, outcome.response_id)
    except survey_service.SurveyDomainError as exc:
        raise _survey_http_error(exc) from exc


@router.get("/survey-responses/{response_id}", response_model=SurveyResponseRead)
def get_survey_response(response_id: str, db: Session = Depends(get_db)):
    try:
        return survey_service.get_response(db, response_id)
    except survey_service.SurveyDomainError as exc:
        raise _survey_http_error(exc) from exc


@router.get("/survey-responses", response_model=ListResponse[SurveyResponseRead])
def list_survey_responses(
    survey_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = survey_service.list_responses(
        db,
        survey_id=survey_id,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )
    return list_response(items, limit, offset)
