import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.db import finish_read_response, get_db
from app.models.subscriber import (
    Subscriber,
    SubscriberNINVerification,
)
from app.schemas.common import ListResponse
from app.schemas.subscriber import (
    AddressCreate,
    AddressRead,
    AddressUpdate,
    ResellerCreate,
    ResellerRead,
    ResellerSyncRead,
    ResellerUpdate,
    SubscriberAccountStatusConfirmRequest,
    SubscriberAccountStatusOutcomeRead,
    SubscriberAccountStatusPreviewRead,
    SubscriberAccountStatusPreviewRequest,
    SubscriberBillingApprovalRead,
    SubscriberBillingApprovalUpdate,
    SubscriberCreate,
    SubscriberCustomFieldCreate,
    SubscriberCustomFieldRead,
    SubscriberCustomFieldUpdate,
    SubscriberRead,
    SubscriberSyncRead,
    SubscriberUpdate,
)
from app.services import account_billing_approval, account_status_commands
from app.services import subscriber as subscriber_service
from app.services.auth_dependencies import require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.nin_matching import normalize_nin, validate_nin
from app.services.nin_verifications import (
    begin_subscriber_nin_verification_committed,
    latest_nin_verification,
)
from app.services.owner_commands import CommandContext
from app.services.sync_feeds import SYNC_FEED_MAX_PAGE_SIZE

router = APIRouter()


def _nin_verification_payload(
    verification: SubscriberNINVerification | None,
) -> dict[str, object | None]:
    if verification is None:
        return {
            "status": "not_found",
            "is_match": None,
            "match_score": None,
            "failure_reason": None,
            "verified_at": None,
            "created_at": None,
        }
    return {
        "status": verification.status.value if verification.status else None,
        "is_match": verification.is_match,
        "match_score": verification.match_score,
        "failure_reason": verification.failure_reason,
        "verified_at": (
            verification.verified_at.isoformat() if verification.verified_at else None
        ),
        "created_at": verification.created_at.isoformat()
        if verification.created_at
        else None,
    }


@router.post(
    "/resellers",
    response_model=ResellerRead,
    status_code=status.HTTP_201_CREATED,
    tags=["resellers"],
    dependencies=[Depends(require_permission("customer:create"))],
)
def create_reseller(payload: ResellerCreate, db: Session = Depends(get_db)):
    return subscriber_service.resellers.create(db, payload)


@router.get(
    "/resellers/sync",
    response_model=ListResponse[ResellerSyncRead],
    tags=["resellers"],
    dependencies=[Depends(require_permission("customer:read"))],
)
def sync_resellers(
    is_active: bool | None = None,
    updated_since: datetime | None = None,
    limit: int = Query(
        default=SYNC_FEED_MAX_PAGE_SIZE, ge=1, le=SYNC_FEED_MAX_PAGE_SIZE
    ),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        subscriber_service.resellers.sync_list_response(
            db,
            is_active=is_active,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ),
    )


@router.get(
    "/resellers/{reseller_id}",
    response_model=ResellerRead,
    tags=["resellers"],
    dependencies=[Depends(require_permission("customer:read"))],
)
def get_reseller(reseller_id: str, db: Session = Depends(get_db)):
    return finish_read_response(db, subscriber_service.resellers.get(db, reseller_id))


@router.get(
    "/resellers",
    response_model=ListResponse[ResellerRead],
    tags=["resellers"],
    dependencies=[Depends(require_permission("customer:read"))],
)
def list_resellers(
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        subscriber_service.resellers.list_response(
            db, is_active, order_by, order_dir, limit, offset
        ),
    )


@router.patch(
    "/resellers/{reseller_id}",
    response_model=ResellerRead,
    tags=["resellers"],
    dependencies=[Depends(require_permission("customer:update"))],
)
def update_reseller(
    reseller_id: str, payload: ResellerUpdate, db: Session = Depends(get_db)
):
    return subscriber_service.resellers.update(db, reseller_id, payload)


@router.delete(
    "/resellers/{reseller_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["resellers"],
    dependencies=[Depends(require_permission("customer:delete"))],
)
def delete_reseller(reseller_id: str, db: Session = Depends(get_db)):
    subscriber_service.resellers.delete(db, reseller_id)


@router.post(
    "/subscribers",
    response_model=SubscriberRead,
    status_code=status.HTTP_201_CREATED,
    tags=["subscribers"],
    dependencies=[Depends(require_permission("customer:create"))],
)
def create_subscriber(payload: SubscriberCreate, db: Session = Depends(get_db)):
    return subscriber_service.subscribers.create(db, payload)


@router.get(
    "/subscribers/sync",
    response_model=ListResponse[SubscriberSyncRead],
    tags=["subscribers"],
    dependencies=[Depends(require_permission("customer:read"))],
)
def sync_subscribers(
    subscriber_type: str | None = None,
    updated_since: datetime | None = Query(
        default=None,
        description="Inclusive updated_at watermark for integration synchronization.",
    ),
    limit: int = Query(
        default=SYNC_FEED_MAX_PAGE_SIZE, ge=1, le=SYNC_FEED_MAX_PAGE_SIZE
    ),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        subscriber_service.subscribers.sync_list_response(
            db,
            subscriber_type=subscriber_type,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ),
    )


@router.get(
    "/subscribers/{subscriber_id}",
    response_model=SubscriberRead,
    tags=["subscribers"],
    dependencies=[Depends(require_permission("customer:read"))],
)
def get_subscriber(subscriber_id: str, db: Session = Depends(get_db)):
    return finish_read_response(
        db, subscriber_service.subscribers.get(db, subscriber_id)
    )


@router.get(
    "/subscribers",
    response_model=ListResponse[SubscriberRead],
    tags=["subscribers"],
    dependencies=[Depends(require_permission("customer:read"))],
)
def list_subscribers(
    subscriber_type: str | None = None,
    person_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        subscriber_service.subscribers.list_response(
            db,
            person_id,
            None,
            subscriber_type,
            order_by,
            order_dir,
            limit,
            offset,
        ),
    )


@router.patch(
    "/subscribers/{subscriber_id}",
    response_model=SubscriberRead,
    tags=["subscribers"],
    dependencies=[Depends(require_permission("customer:update"))],
)
def update_subscriber(
    subscriber_id: str, payload: SubscriberUpdate, db: Session = Depends(get_db)
):
    return subscriber_service.subscribers.update(db, subscriber_id, payload)


def _account_status_action(value: str) -> account_status_commands.AccountStatusAction:
    try:
        return account_status_commands.AccountStatusAction(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "customer.account_status_actions.invalid_action",
                "message": "Account-status action is invalid.",
            },
        ) from exc


def _account_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Subscriber not found") from exc


def _account_status_actor(request: Request) -> str:
    auth = getattr(request.state, "auth", None) or {}
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    actor_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    return f"{actor_type}:{principal_id}"


def _raise_account_status_error(exc: DomainError) -> None:
    status_code = 404 if exc.code.endswith("account_not_found") else 409
    raise HTTPException(
        status_code=status_code,
        detail={
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    ) from exc


@router.post(
    "/subscribers/{subscriber_id}/account-status/preview",
    response_model=SubscriberAccountStatusPreviewRead,
    tags=["subscribers"],
    dependencies=[Depends(require_permission("customer:update"))],
)
def preview_subscriber_account_status(
    subscriber_id: str,
    payload: SubscriberAccountStatusPreviewRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    _account_status_actor(request)
    try:
        return account_status_commands.preview_account_status_change(
            db,
            account_status_commands.PreviewAccountStatusRequest(
                account_id=_account_id(subscriber_id),
                action=_account_status_action(payload.action),
            ),
        )
    except DomainError as exc:
        _raise_account_status_error(exc)


@router.post(
    "/subscribers/{subscriber_id}/account-status/confirm",
    response_model=SubscriberAccountStatusOutcomeRead,
    tags=["subscribers"],
    dependencies=[Depends(require_permission("customer:update"))],
)
def confirm_subscriber_account_status(
    subscriber_id: str,
    payload: SubscriberAccountStatusConfirmRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    actor = _account_status_actor(request)
    account_id = _account_id(subscriber_id)
    command_id = uuid.uuid4()
    db_session_adapter.release_read_transaction(db)
    try:
        return account_status_commands.confirm_account_status_change(
            db,
            account_status_commands.ConfirmAccountStatusCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=actor,
                    scope=account_status_commands.ACCOUNT_STATUS_WRITE_SCOPE,
                    reason=payload.reason,
                    idempotency_key=payload.idempotency_key,
                ),
                account_id=account_id,
                action=_account_status_action(payload.action),
                expected_preview_fingerprint=payload.preview_fingerprint,
            ),
        )
    except DomainError as exc:
        _raise_account_status_error(exc)


@router.post(
    "/subscribers/{subscriber_id}/billing-approval",
    response_model=SubscriberBillingApprovalRead,
    tags=["subscribers"],
    dependencies=[Depends(require_permission("customer:update"))],
)
def change_subscriber_billing_approval(
    subscriber_id: str,
    payload: SubscriberBillingApprovalUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    auth = getattr(request.state, "auth", None) or {}
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    actor_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    try:
        account_id = uuid.UUID(subscriber_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Subscriber not found") from exc
    db_session_adapter.release_read_transaction(db)
    command_id = uuid.uuid4()
    context = CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"{actor_type}:{principal_id}",
        scope=account_billing_approval.BILLING_APPROVAL_WRITE_SCOPE,
        reason=payload.reason,
        idempotency_key=payload.idempotency_key
        or f"subscriber-billing-approval:{account_id}:{payload.approved}:{command_id}",
    )
    try:
        return account_billing_approval.change_account_billing_approval(
            db,
            account_billing_approval.ChangeAccountBillingApprovalCommand(
                context=context,
                account_id=account_id,
                approved=payload.approved,
            ),
        )
    except DomainError as exc:
        status_code = 404 if exc.code.endswith("account_not_found") else 409
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        ) from exc


@router.delete(
    "/subscribers/{subscriber_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["subscribers"],
    dependencies=[Depends(require_permission("customer:delete"))],
)
def delete_subscriber(subscriber_id: str, db: Session = Depends(get_db)):
    subscriber_service.subscribers.delete(db, subscriber_id)


@router.post(
    "/subscribers/{subscriber_id}/verify-nin",
    tags=["subscribers"],
    dependencies=[Depends(require_permission("customer:update"))],
)
async def verify_subscriber_nin(
    subscriber_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    nin: str | None = None
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid NIN") from exc
        if isinstance(payload, dict):
            nin = str(payload.get("nin") or "")
    else:
        form = await request.form()
        nin = str(form.get("nin") or "")

    normalized_nin = normalize_nin(nin or "")
    if not validate_nin(normalized_nin):
        raise HTTPException(status_code=400, detail="Invalid NIN")

    return begin_subscriber_nin_verification_committed(
        db,
        subscriber_id=subscriber_id,
        normalized_nin=normalized_nin,
    )


@router.get(
    "/subscribers/{subscriber_id}/nin-verification",
    tags=["subscribers"],
    dependencies=[Depends(require_permission("customer:read"))],
)
def get_subscriber_nin_verification(
    subscriber_id: str,
    db: Session = Depends(get_db),
) -> dict[str, object | None]:
    try:
        subscriber_uuid = uuid.UUID(str(subscriber_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Subscriber not found") from exc

    subscriber = db.get(Subscriber, subscriber_uuid)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    return finish_read_response(
        db, _nin_verification_payload(latest_nin_verification(db, subscriber_uuid))
    )


@router.post(
    "/addresses",
    response_model=AddressRead,
    status_code=status.HTTP_201_CREATED,
    tags=["addresses"],
    dependencies=[Depends(require_permission("customer:create"))],
)
def create_address(payload: AddressCreate, db: Session = Depends(get_db)):
    return subscriber_service.addresses.create(db, payload)


@router.get(
    "/addresses/{address_id}",
    response_model=AddressRead,
    tags=["addresses"],
    dependencies=[Depends(require_permission("customer:read"))],
)
def get_address(address_id: str, db: Session = Depends(get_db)):
    return finish_read_response(db, subscriber_service.addresses.get(db, address_id))


@router.get(
    "/addresses",
    response_model=ListResponse[AddressRead],
    tags=["addresses"],
    dependencies=[Depends(require_permission("customer:read"))],
)
def list_addresses(
    subscriber_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        subscriber_service.addresses.list_response(
            db, subscriber_id, order_by, order_dir, limit, offset
        ),
    )


@router.patch(
    "/addresses/{address_id}",
    response_model=AddressRead,
    tags=["addresses"],
    dependencies=[Depends(require_permission("customer:update"))],
)
def update_address(
    address_id: str, payload: AddressUpdate, db: Session = Depends(get_db)
):
    return subscriber_service.addresses.update(db, address_id, payload)


@router.delete(
    "/addresses/{address_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["addresses"],
    dependencies=[Depends(require_permission("customer:delete"))],
)
def delete_address(address_id: str, db: Session = Depends(get_db)):
    subscriber_service.addresses.delete(db, address_id)


@router.post(
    "/subscriber-custom-fields",
    response_model=SubscriberCustomFieldRead,
    status_code=status.HTTP_201_CREATED,
    tags=["subscriber-custom-fields"],
    dependencies=[Depends(require_permission("customer:create"))],
)
def create_subscriber_custom_field(
    payload: SubscriberCustomFieldCreate, db: Session = Depends(get_db)
):
    return subscriber_service.subscriber_custom_fields.create(db, payload)


@router.get(
    "/subscriber-custom-fields/{custom_field_id}",
    response_model=SubscriberCustomFieldRead,
    tags=["subscriber-custom-fields"],
    dependencies=[Depends(require_permission("customer:read"))],
)
def get_subscriber_custom_field(custom_field_id: str, db: Session = Depends(get_db)):
    return finish_read_response(
        db, subscriber_service.subscriber_custom_fields.get(db, custom_field_id)
    )


@router.get(
    "/subscriber-custom-fields",
    response_model=ListResponse[SubscriberCustomFieldRead],
    tags=["subscriber-custom-fields"],
    dependencies=[Depends(require_permission("customer:read"))],
)
def list_subscriber_custom_fields(
    subscriber_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        subscriber_service.subscriber_custom_fields.list_response(
            db, subscriber_id, is_active, order_by, order_dir, limit, offset
        ),
    )


@router.patch(
    "/subscriber-custom-fields/{custom_field_id}",
    response_model=SubscriberCustomFieldRead,
    tags=["subscriber-custom-fields"],
    dependencies=[Depends(require_permission("customer:update"))],
)
def update_subscriber_custom_field(
    custom_field_id: str,
    payload: SubscriberCustomFieldUpdate,
    db: Session = Depends(get_db),
):
    return subscriber_service.subscriber_custom_fields.update(
        db, custom_field_id, payload
    )


@router.delete(
    "/subscriber-custom-fields/{custom_field_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["subscriber-custom-fields"],
    dependencies=[Depends(require_permission("customer:delete"))],
)
def delete_subscriber_custom_field(custom_field_id: str, db: Session = Depends(get_db)):
    subscriber_service.subscriber_custom_fields.delete(db, custom_field_id)
