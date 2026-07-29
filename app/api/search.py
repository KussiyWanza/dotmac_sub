from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.typeahead import TypeaheadItem
from app.services import customer_search as customer_search_service
from app.services import typeahead as typeahead_service
from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/search", tags=["search"])


@router.get("/accounts", response_model=ListResponse[TypeaheadItem])
def search_accounts(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return typeahead_service.accounts_response(db, q, limit)


@router.get("/subscribers", response_model=ListResponse[TypeaheadItem])
def search_subscribers(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return typeahead_service.subscribers_response(db, q, limit)


@router.get("/reseller-subscribers", response_model=ListResponse[TypeaheadItem])
def search_reseller_linkable_subscribers(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return typeahead_service.reseller_linkable_subscribers_response(db, q, limit)


@router.get("/subscriptions", response_model=ListResponse[TypeaheadItem])
def search_subscriptions(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return typeahead_service.subscriptions_response(db, q, limit)


@router.get("/contacts", response_model=ListResponse[TypeaheadItem])
def search_contacts(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return typeahead_service.contacts_response(db, q, limit)


@router.get("/people", response_model=ListResponse[TypeaheadItem])
def search_people(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return typeahead_service.people_response(db, q, limit)


@router.get("/customers", response_model=ListResponse[TypeaheadItem])
def search_customers(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return customer_search_service.search_response(db, q, limit)


@router.get("/invoices", response_model=ListResponse[TypeaheadItem])
def search_invoices(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    account_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return typeahead_service.invoices_response(db, q, limit, account_id)


@router.get("/nas-devices", response_model=ListResponse[TypeaheadItem])
def search_nas_devices(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return typeahead_service.nas_devices_response(db, q, limit)


@router.get("/network-devices", response_model=ListResponse[TypeaheadItem])
def search_network_devices(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return typeahead_service.network_devices_response(db, q, limit)


@router.get("/pop-sites", response_model=ListResponse[TypeaheadItem])
def search_pop_sites(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return typeahead_service.pop_sites_response(db, q, limit)


@router.get("/resellers", response_model=ListResponse[TypeaheadItem])
def search_resellers(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return typeahead_service.resellers_response(db, q, limit)


@router.get("/business-accounts", response_model=ListResponse[TypeaheadItem])
def search_business_accounts(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return typeahead_service.business_accounts_response(db, q, limit)


@router.get("/catalog-offers", response_model=ListResponse[TypeaheadItem])
def search_catalog_offers(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return typeahead_service.catalog_offers_response(db, q, limit)


@router.get(
    "/sales-agents",
    response_model=ListResponse[TypeaheadItem],
    dependencies=[Depends(require_permission("crm:sales_order:read"))],
)
def search_sales_agents(
    q: str = Query(default="", max_length=120),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return typeahead_service.sales_agents_response(db, q, limit)


@router.get("/global")
def global_search(
    q: str = Query(min_length=2),
    limit: int = Query(default=3, ge=1, le=10),
    db: Session = Depends(get_db),
):
    """
    Global search across multiple entity types.
    Returns categorized results with navigation URLs.
    """
    return typeahead_service.global_search(db, q, limit)
