"""Native sales-orders API ported from CRM.

Ported from ``dotmac_crm/app/api/sales_orders.py`` with the crm#233 fix:
the legacy ``account_id`` query parameter — which the CRM passed
positionally into the service's ``quote_id`` slot — is gone, and the list
call passes every filter by keyword. Permission tightening per §2.4: the
CRM left sales orders auth-only; sub gates them on
``crm:sales_order:{read,write}``, which are part of the native sales RBAC contract.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permission
from app.schemas.common import ListResponse
from app.schemas.sales_order import (
    SalesOrderCreate,
    SalesOrderLineCreate,
    SalesOrderLineRead,
    SalesOrderLineUpdate,
    SalesOrderRead,
    SalesOrderUpdate,
    SalesOrderWaiverGrant,
    SalesOrderWaiverRead,
    SalesOrderWaiverRevoke,
)
from app.services import sales_orders as sales_order_service
from app.services.common import coerce_uuid
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.sales_orders import SalesOrderWaivers

router = APIRouter(prefix="/sales-orders", tags=["sales-orders"])


@router.post(
    "",
    response_model=SalesOrderRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:sales_order:write"))],
)
def create_sales_order(payload: SalesOrderCreate, db: Session = Depends(get_db)):
    return sales_order_service.sales_orders.create(db, payload)


@router.get(
    "/{sales_order_id}",
    response_model=SalesOrderRead,
    dependencies=[Depends(require_permission("crm:sales_order:read"))],
)
def get_sales_order(sales_order_id: str, db: Session = Depends(get_db)):
    return sales_order_service.sales_orders.get(db, sales_order_id)


@router.get(
    "",
    response_model=ListResponse[SalesOrderRead],
    dependencies=[Depends(require_permission("crm:sales_order:read"))],
)
def list_sales_orders(
    subscriber_id: str | None = None,
    quote_id: str | None = None,
    status: str | None = None,
    payment_status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return sales_order_service.sales_orders.list_response(
        db,
        subscriber_id=subscriber_id,
        quote_id=quote_id,
        status=status,
        payment_status=payment_status,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/{sales_order_id}",
    response_model=SalesOrderRead,
    dependencies=[Depends(require_permission("crm:sales_order:write"))],
)
def update_sales_order(
    sales_order_id: str, payload: SalesOrderUpdate, db: Session = Depends(get_db)
):
    return sales_order_service.sales_orders.update(db, sales_order_id, payload)


@router.delete(
    "/{sales_order_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("crm:sales_order:write"))],
)
def delete_sales_order(sales_order_id: str, db: Session = Depends(get_db)):
    sales_order_service.sales_orders.delete(db, sales_order_id)


@router.post(
    "/{sales_order_id}/lines",
    response_model=SalesOrderLineRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:sales_order:write"))],
)
def create_sales_order_line(
    sales_order_id: str, payload: SalesOrderLineCreate, db: Session = Depends(get_db)
):
    data = payload.model_copy(update={"sales_order_id": sales_order_id})
    return sales_order_service.sales_order_lines.create(db, data)


@router.get(
    "/{sales_order_id}/lines",
    response_model=ListResponse[SalesOrderLineRead],
    dependencies=[Depends(require_permission("crm:sales_order:read"))],
)
def list_sales_order_lines(
    sales_order_id: str,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return sales_order_service.sales_order_lines.list_response(
        db, sales_order_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/lines/{line_id}",
    response_model=SalesOrderLineRead,
    dependencies=[Depends(require_permission("crm:sales_order:write"))],
)
def update_sales_order_line(
    line_id: str, payload: SalesOrderLineUpdate, db: Session = Depends(get_db)
):
    return sales_order_service.sales_order_lines.update(db, line_id, payload)


def _waiver_actor(principal: dict) -> str:
    """A waiver with no accountable actor is not evidence."""
    actor_id = str(principal.get("principal_id") or "") or None
    if actor_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An order waiver requires an identified actor.",
        )
    return f"{principal.get('principal_type') or 'user'}:{actor_id}"


@router.post(
    "/{sales_order_id}/waiver",
    response_model=SalesOrderWaiverRead,
    status_code=status.HTTP_201_CREATED,
)
def grant_order_waiver(
    sales_order_id: str,
    payload: SalesOrderWaiverGrant,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_permission("crm:sales_order:waive")),
):
    """Record a decision not to pursue what this order is worth.

    Gated on ``crm:sales_order:waive``, deliberately NOT on
    ``crm:sales_order:write``. This writes no payment field, creates no
    settlement evidence and stages no funding event — a waived order was not
    paid. While the waiver is active the order's commercial terms are frozen.
    """
    # The request already opened an implicit read transaction (permission
    # check, then `coerce_uuid`'s caller reads). An owner command requires a
    # transaction-free session at entry and fails closed otherwise, so release
    # it here exactly as every other owner-command route does.
    db_session_adapter.release_read_transaction(db)
    return SalesOrderWaivers.grant(
        db,
        sales_order_id=coerce_uuid(sales_order_id),
        waived_amount=payload.waived_amount,
        reason_code=payload.reason_code,
        reason_text=payload.reason_text,
        context=CommandContext.system(
            actor=_waiver_actor(principal),
            scope="api.sales_order.waiver.grant",
            reason=payload.reason_code,
            idempotency_key=payload.idempotency_key,
        ),
    )


@router.post(
    "/{sales_order_id}/waiver/revoke",
    response_model=SalesOrderWaiverRead,
)
def revoke_order_waiver(
    sales_order_id: str,
    payload: SalesOrderWaiverRevoke,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_permission("crm:sales_order:waive")),
):
    """Withdraw an active waiver, making the order pursuable again."""
    db_session_adapter.release_read_transaction(db)
    return SalesOrderWaivers.revoke(
        db,
        sales_order_id=coerce_uuid(sales_order_id),
        reason_code=payload.reason_code,
        reason_text=payload.reason_text,
        context=CommandContext.system(
            actor=_waiver_actor(principal),
            scope="api.sales_order.waiver.revoke",
            reason=payload.reason_code,
            idempotency_key=payload.idempotency_key,
        ),
    )
