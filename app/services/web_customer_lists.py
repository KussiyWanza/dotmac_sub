"""Service helpers for web/admin customer listing routes."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address as ParsedIPv4Address
from typing import Any, Literal, cast
from uuid import UUID

logger = logging.getLogger(__name__)

from sqlalchemy import and_, func, not_, or_
from sqlalchemy.orm import Query, Session, selectinload

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, OntAssignment, OntUnit
from app.models.network_monitoring import PopSite
from app.models.subscriber import (
    Subscriber,
    SubscriberCategory,
    SubscriberStatus,
    UserType,
)
from app.services.customer_account_visibility import splynx_deleted_import_clause
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
    SortDirection,
)
from app.services.status_presentation import account_status_presentation

_SUBSCRIBER_CATEGORY_COL: Any = Subscriber.metadata_["subscriber_category"].as_string()
_UNSPECIFIED_IPV4 = ParsedIPv4Address(0)

CustomerListSort = Literal["created_at", "name", "status"]
_CUSTOMER_STATUS_FILTERS = frozenset(
    {
        "active",
        "blocked",
        "suspended",
        "disabled",
        "canceled",
        "new",
        "delinquent",
        "inactive",
    }
)

CUSTOMER_LIST_DEFINITION = ListDefinition(
    key="customers",
    fields=(
        ListFieldDefinition("name", "Customer", searchable=True, sortable=True),
        ListFieldDefinition("email", "Email", searchable=True),
        ListFieldDefinition("phone", "Phone", searchable=True),
        ListFieldDefinition("account_number", "Account", searchable=True),
        ListFieldDefinition("pppoe_login", "PPPoE login", searchable=True),
        ListFieldDefinition("ipv4", "IPv4 address", searchable=True),
        ListFieldDefinition("customer_type", "Customer type", filterable=True),
        ListFieldDefinition("status", "Status", filterable=True, sortable=True),
        ListFieldDefinition("nas_id", "NAS device", filterable=True),
        ListFieldDefinition("pop_site_id", "Location", filterable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
    ),
    default_sort="created_at",
    default_sort_dir="desc",
)

_LEGACY_CUSTOMER_TABLE_PARAMS = frozenset(
    {
        "_ts",
        "activation_state",
        "customer_type",
        "limit",
        "nas_id",
        "offset",
        "pop_site_id",
        "q",
        "search",
        "sort_by",
        "sort_dir",
        "status",
        "table_key",
    }
)
CUSTOMER_TABLE_SORT_ALIASES: dict[str, CustomerListSort] = {
    "created_at": "created_at",
    "customer_name": "name",
    "name": "name",
    "status": "status",
}


@dataclass(frozen=True, slots=True)
class CustomerListPage:
    """One canonical customer-list page before its transport projection."""

    query: Query
    list_query: ListQuery
    page_meta: PageMeta


def _customer_user_clause():
    return Subscriber.user_type == UserType.customer


def _looks_like_uuid(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if not normalized:
        return False
    try:
        return str(UUID(normalized)) == normalized.lower()
    except (ValueError, AttributeError, TypeError):
        return False


def _customer_display_identifier(*values: str | None) -> str | None:
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or _looks_like_uuid(normalized):
            continue
        return normalized
    return None


def _parse_ipv4_search(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        parsed = ParsedIPv4Address(normalized)
    except ValueError:
        return None
    if parsed == _UNSPECIFIED_IPV4:
        return None
    return str(parsed)


def _valid_ipv4_text(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = ParsedIPv4Address(text)
    except ValueError:
        return None
    if parsed == _UNSPECIFIED_IPV4:
        return None
    return str(parsed)


def _normalize_search(search: str | None) -> str | None:
    normalized = str(search or "").strip()
    return normalized or None


def _normalize_per_page(per_page: int | str | None) -> int:
    try:
        normalized = int(str(per_page or "").strip())
    except ValueError:
        return CUSTOMER_LIST_DEFINITION.default_per_page
    if normalized in CUSTOMER_LIST_DEFINITION.per_page_options:
        return normalized
    return CUSTOMER_LIST_DEFINITION.default_per_page


def build_customer_list_query(
    *,
    search: str | None,
    status: str | None,
    customer_type: str | None,
    nas_id: str | None,
    pop_site_id: str | None,
    sort_by: CustomerListSort = "created_at",
    sort_dir: SortDirection = "desc",
    page: int = 1,
    per_page: int | str | None = 25,
) -> ListQuery:
    """Normalize raw adapter parameters through the customer list contract."""

    raw_customer_type = str(customer_type or "").strip()
    normalized_customer_type = _normalize_customer_type(raw_customer_type)
    if raw_customer_type and normalized_customer_type is None:
        raise ValueError(f"Unsupported customer_type filter: {raw_customer_type}")

    normalized_status = str(status or "").strip().lower() or None
    if normalized_status and normalized_status not in _CUSTOMER_STATUS_FILTERS:
        raise ValueError(f"Unsupported status filter: {normalized_status}")

    def _uuid_filter(value: str | None, name: str) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        try:
            return str(UUID(normalized))
        except ValueError as exc:
            raise ValueError(f"{name} must be a valid UUID") from exc

    return CUSTOMER_LIST_DEFINITION.build_query(
        search=search,
        filters={
            "status": normalized_status,
            "customer_type": normalized_customer_type,
            "nas_id": _uuid_filter(nas_id, "nas_id"),
            "pop_site_id": _uuid_filter(pop_site_id, "pop_site_id"),
        },
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=_normalize_per_page(per_page),
    )


def build_customer_list_query_from_legacy_params(
    request_params: Mapping[str, Any],
) -> ListQuery:
    """Translate the legacy offset API onto the canonical customer contract.

    This compatibility adapter deliberately accepts only capabilities declared by
    ``CUSTOMER_LIST_DEFINITION``. The old generic column-filter path must not
    reintroduce customer-list decisions that the canonical owner does not expose.
    """

    unsupported = sorted(
        key
        for key, value in request_params.items()
        if key not in _LEGACY_CUSTOMER_TABLE_PARAMS and str(value or "").strip()
    )
    if unsupported:
        raise ValueError(
            "Unsupported customer list parameters: " + ", ".join(unsupported)
        )

    try:
        limit = int(request_params.get("limit", 50) or 50)
        offset = int(request_params.get("offset", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit and offset must be integers") from exc

    if limit not in CUSTOMER_LIST_DEFINITION.per_page_options:
        allowed = ", ".join(
            str(size) for size in CUSTOMER_LIST_DEFINITION.per_page_options
        )
        raise ValueError(f"limit must be one of: {allowed}")
    if offset < 0:
        raise ValueError("offset must be at least 0")
    if offset % limit:
        raise ValueError("offset must align to the requested limit")

    legacy_sort = str(request_params.get("sort_by") or "created_at").strip()
    sort_by = CUSTOMER_TABLE_SORT_ALIASES.get(legacy_sort)
    if sort_by is None:
        raise ValueError(f"Unsupported sort field for customers: {legacy_sort}")

    status = str(request_params.get("status") or "").strip()
    activation_state = str(request_params.get("activation_state") or "").strip()
    if status and activation_state and status.lower() != activation_state.lower():
        raise ValueError("status and activation_state filters conflict")

    raw_sort_dir = str(request_params.get("sort_dir") or "desc").strip().lower()
    if raw_sort_dir not in {"asc", "desc"}:
        raise ValueError("sort_dir must be asc or desc")

    return build_customer_list_query(
        search=str(
            request_params.get("q") or request_params.get("search") or ""
        ).strip(),
        status=status or activation_state,
        customer_type=str(request_params.get("customer_type") or "").strip(),
        nas_id=str(request_params.get("nas_id") or "").strip(),
        pop_site_id=str(request_params.get("pop_site_id") or "").strip(),
        sort_by=sort_by,
        sort_dir=cast(SortDirection, raw_sort_dir),
        page=(offset // limit) + 1,
        per_page=limit,
    )


def _customer_name_sort_expression():
    return func.lower(
        func.coalesce(
            func.nullif(func.trim(Subscriber.company_name), ""),
            func.nullif(func.trim(Subscriber.display_name), ""),
            func.nullif(func.trim(Subscriber.legal_name), ""),
            func.nullif(func.trim(Subscriber.last_name), ""),
            func.nullif(func.trim(Subscriber.first_name), ""),
            Subscriber.email,
            "",
        )
    )


def _apply_customer_sort(query, list_query: ListQuery):
    if list_query.sort_by == "name":
        expression = _customer_name_sort_expression()
    elif list_query.sort_by == "status":
        expression = Subscriber.status
    else:
        expression = Subscriber.created_at

    ordered = expression.asc() if list_query.sort_dir == "asc" else expression.desc()
    return query.order_by(ordered, Subscriber.id.asc())


def _active_subscription_clause():
    return Subscription.status == SubscriptionStatus.active


def _active_ipam_ipv4_match(ip_address: str):
    return Subscriber.ip_assignments.any(
        and_(
            IPAssignment.is_active.is_(True),
            IPAssignment.ipv4_address.has(IPv4Address.address == ip_address),
            or_(
                IPAssignment.subscription.has(_active_subscription_clause()),
                and_(
                    IPAssignment.subscription_id.is_(None),
                    Subscriber.subscriptions.any(_active_subscription_clause()),
                ),
            ),
        )
    )


def _active_ont_ipv4_match(ip_address: str):
    return Subscriber.ont_assignments.any(
        and_(
            OntAssignment.active.is_(True),
            or_(
                OntAssignment.static_ip == ip_address,
                OntAssignment.ont_unit.has(OntUnit.observed_wan_ip == ip_address),
            ),
        )
    )


def _business_customer_clause():
    return (
        (
            func.coalesce(_SUBSCRIBER_CATEGORY_COL, "")
            == SubscriberCategory.business.value
        )
        | (func.trim(func.coalesce(Subscriber.company_name, "")) != "")
        | (func.trim(func.coalesce(Subscriber.legal_name, "")) != "")
    )


def _individual_customer_clause():
    return not_(_business_customer_clause())


def _build_customer_dict(person: Subscriber) -> dict[str, Any]:
    """Build a customer dict from a subscriber, including subscription info."""
    pppoe_login = None
    ipv4 = None
    ipv4_label = None
    nas_name = None
    pop_site_name = None
    active_subscriptions = [
        sub
        for sub in (person.subscriptions or [])
        if sub.status == SubscriptionStatus.active
    ]
    suspended_subscriptions = [
        sub
        for sub in (person.subscriptions or [])
        if sub.status == SubscriptionStatus.suspended
    ]
    active_subscription_ids = {sub.id for sub in active_subscriptions}
    active_ipam_assignments = sorted(
        (
            assignment
            for assignment in (person.ip_assignments or [])
            if assignment.is_active
            and assignment.ipv4_address
            and (
                (
                    assignment.subscription_id is not None
                    and assignment.subscription_id in active_subscription_ids
                )
                or (assignment.subscription_id is None and bool(active_subscriptions))
            )
        ),
        key=lambda assignment: assignment.created_at,
        reverse=True,
    )
    if active_ipam_assignments:
        ipv4 = _valid_ipv4_text(active_ipam_assignments[0].ipv4_address.address)
        ipv4_label = "Current IPAM IPv4"

    for sub in active_subscriptions:
        if sub.login:
            pppoe_login = sub.login
        if not ipv4 and sub.ipv4_address:
            service_ipv4 = _valid_ipv4_text(sub.ipv4_address)
            if service_ipv4:
                ipv4 = service_ipv4
                ipv4_label = "Service IPv4"
        if sub.provisioning_nas_device:
            nas_name = sub.provisioning_nas_device.name
            if getattr(sub.provisioning_nas_device, "pop_site", None):
                pop_site_name = sub.provisioning_nas_device.pop_site.name
        if pppoe_login:
            break

    if not ipv4:
        active_ont_assignments = [
            assignment
            for assignment in (person.ont_assignments or [])
            if assignment.active
        ]
        for assignment in active_ont_assignments:
            static_ip = _valid_ipv4_text(assignment.static_ip)
            if static_ip:
                ipv4 = static_ip
                ipv4_label = "ONT WAN IPv4"
                break
            observed_ip = _valid_ipv4_text(
                getattr(assignment.ont_unit, "observed_wan_ip", None)
            )
            if observed_ip:
                ipv4 = observed_ip
                ipv4_label = "Observed ONT WAN IPv4"
                break

    display_name = person.company_name or person.display_name or person.full_name
    status_presentation = account_status_presentation(
        person.status,
        is_active=person.is_active,
    )
    return {
        "id": str(person.id),
        "type": "business" if person.is_business else "person",
        "name": display_name,
        "subscriber_number": person.subscriber_number,
        "account_number": person.account_number,
        "account_label": _customer_display_identifier(
            person.account_number,
            person.subscriber_number,
        ),
        "display_identifier": _customer_display_identifier(
            person.subscriber_number,
            person.account_number,
            pppoe_login,
        ),
        "pppoe_login": pppoe_login,
        "active_subscription_count": len(active_subscriptions),
        "suspended_subscription_count": len(suspended_subscriptions),
        "ipv4": ipv4,
        "ipv4_label": ipv4_label,
        "nas_name": nas_name,
        "pop_site_name": pop_site_name,
        "email": person.email,
        "phone": person.phone,
        "is_active": person.is_active,
        "status": status_presentation.value,
        "status_presentation": status_presentation,
        "is_business": person.is_business,
        "business_name": person.legal_name if person.is_business else None,
        "created_at": person.created_at,
        "raw": person,
    }


def _normalize_customer_type(customer_type: str | None) -> str | None:
    normalized = (customer_type or "").strip().lower()
    if normalized in {"individual", "person"}:
        return "person"
    if normalized == "business":
        return "business"
    return None


def _status_filter_clause(status: str | None) -> Any:
    normalized = (status or "").strip().lower()
    if not normalized:
        return None
    if normalized == "inactive":
        return Subscriber.is_active.is_(False)
    if normalized in (
        "active",
        "blocked",
        "suspended",
        "disabled",
        "canceled",
        "new",
        "delinquent",
    ):
        return Subscriber.status == SubscriberStatus(normalized)
    return None


def _apply_customer_filters(
    query,
    *,
    search: str | None,
    status: str | None,
    customer_type: str | None,
    nas_id: str | None,
    pop_site_id: str | None,
):
    normalized_customer_type = _normalize_customer_type(customer_type)
    status_filter = _status_filter_clause(status)

    if normalized_customer_type == "business":
        query = query.filter(_business_customer_clause())
    elif normalized_customer_type == "person":
        query = query.filter(_individual_customer_clause())

    if status_filter is not None:
        query = query.filter(status_filter)
    normalized_search = _normalize_search(search)
    if normalized_search:
        exact_ipv4 = _parse_ipv4_search(normalized_search)
        if exact_ipv4:
            query = query.filter(
                or_(
                    Subscriber.subscriptions.any(
                        and_(
                            _active_subscription_clause(),
                            Subscription.ipv4_address == exact_ipv4,
                        )
                    ),
                    _active_ipam_ipv4_match(exact_ipv4),
                    _active_ont_ipv4_match(exact_ipv4),
                )
            )
        else:
            like = f"%{normalized_search}%"
            query = query.filter(
                Subscriber.first_name.ilike(like)
                | Subscriber.last_name.ilike(like)
                | Subscriber.display_name.ilike(like)
                | Subscriber.email.ilike(like)
                | Subscriber.phone.ilike(like)
                | Subscriber.subscriber_number.ilike(like)
                | Subscriber.account_number.ilike(like)
                | Subscriber.subscriptions.any(
                    and_(_active_subscription_clause(), Subscription.login.ilike(like))
                )
            )
    if nas_id:
        query = query.filter(
            Subscriber.subscriptions.any(
                Subscription.provisioning_nas_device_id == nas_id
            )
        )
    if pop_site_id:
        query = query.filter(Subscriber.pop_site_id == pop_site_id)
    return query


def customer_scope_query(
    db: Session,
    *,
    search: str | None,
    status: str | None,
    customer_type: str | None,
    nas_id: str | None,
    pop_site_id: str | None,
    include_related: bool = True,
):
    query = db.query(Subscriber)
    if include_related:
        query = query.options(
            selectinload(Subscriber.subscriptions)
            .selectinload(Subscription.provisioning_nas_device)
            .selectinload(NasDevice.pop_site),
            selectinload(Subscriber.ip_assignments).selectinload(
                IPAssignment.ipv4_address
            ),
            selectinload(Subscriber.ont_assignments).selectinload(
                OntAssignment.ont_unit
            ),
            selectinload(Subscriber.channels),
        )
    query = query.filter(_customer_user_clause()).filter(
        not_(splynx_deleted_import_clause())
    )
    return _apply_customer_filters(
        query,
        search=search,
        status=status,
        customer_type=customer_type,
        nas_id=nas_id,
        pop_site_id=pop_site_id,
    )


def build_customer_list_page(
    db: Session,
    *,
    list_query: ListQuery,
    include_related: bool = False,
) -> CustomerListPage:
    """Apply canonical customer filters, count, page clamping, and stable sort."""

    if list_query.definition.key != CUSTOMER_LIST_DEFINITION.key:
        raise ValueError("Customer list page requires the customers definition")

    search = list_query.search
    status = list_query.filter_value("status")
    customer_type = list_query.filter_value("customer_type")
    nas_id = list_query.filter_value("nas_id")
    pop_site_id = list_query.filter_value("pop_site_id")
    query = customer_scope_query(
        db,
        search=search,
        status=status,
        customer_type=customer_type,
        nas_id=nas_id,
        pop_site_id=pop_site_id,
        include_related=include_related,
    )
    total = (
        customer_scope_query(
            db,
            search=search,
            status=status,
            customer_type=customer_type,
            nas_id=nas_id,
            pop_site_id=pop_site_id,
            include_related=False,
        )
        .order_by(None)
        .count()
    )
    page_meta = PageMeta.from_query(list_query, total)
    effective_query = list_query.with_page(page_meta.page)
    page_query = (
        _apply_customer_sort(query, effective_query)
        .limit(effective_query.per_page)
        .offset(effective_query.offset)
    )
    return CustomerListPage(
        query=page_query,
        list_query=effective_query,
        page_meta=page_meta,
    )


def list_customers_for_scope(
    db: Session,
    *,
    search: str | None,
    status: str | None,
    customer_type: str | None,
    nas_id: str | None,
    pop_site_id: str | None,
) -> list[Subscriber]:
    return (
        customer_scope_query(
            db,
            search=search,
            status=status,
            customer_type=customer_type,
            nas_id=nas_id,
            pop_site_id=pop_site_id,
            include_related=True,
        )
        .order_by(Subscriber.created_at.desc())
        .all()
    )


def active_customer_filter_count(
    *,
    search: str | None,
    status: str | None,
    customer_type: str | None,
    nas_id: str | None,
    pop_site_id: str | None,
) -> int:
    return sum(
        1
        for value in (search, status, customer_type, nas_id, pop_site_id)
        if str(value or "").strip()
    )


def build_customers_index_context(
    db: Session,
    *,
    list_query: ListQuery,
) -> dict[str, Any]:
    """Build the customer list projection from its normalized query contract."""

    page = build_customer_list_page(
        db,
        list_query=list_query,
        include_related=True,
    )
    list_query = page.list_query
    page_meta = page.page_meta
    search = list_query.search
    status = list_query.filter_value("status")
    customer_type = list_query.filter_value("customer_type")
    nas_id = list_query.filter_value("nas_id")
    pop_site_id = list_query.filter_value("pop_site_id")
    people = page.query.all()
    customers: list[dict[str, Any]] = [_build_customer_dict(p) for p in people]

    business_clause = _business_customer_clause()
    stats_row = (
        db.query(
            func.count().filter(business_clause).label("businesses"),
            func.count().filter(not_(business_clause)).label("people"),
        )
        .filter(_customer_user_clause())
        .filter(not_(splynx_deleted_import_clause()))
        .one()
    )
    total_businesses = int(stats_row.businesses or 0)
    total_people = int(stats_row.people or 0)

    # Load filter dropdown options
    nas_options = (
        db.query(NasDevice.id, NasDevice.name)
        .filter(NasDevice.is_active.is_(True))
        .order_by(NasDevice.name)
        .all()
    )
    pop_site_options = db.query(PopSite.id, PopSite.name).order_by(PopSite.name).all()

    return {
        "customers": customers,
        "list_definition": CUSTOMER_LIST_DEFINITION,
        "list_query": list_query,
        "page_meta": page_meta,
        "stats": {
            "total_customers": page_meta.total_items,
            "total_people": total_people,
            "total_organizations": total_businesses,
        },
        # Transitional aliases for page-level widgets. The contract objects above
        # own these values; callers must not recompute them.
        "page": page_meta.page,
        "per_page": page_meta.per_page,
        "total": page_meta.total_items,
        "total_pages": page_meta.total_pages,
        "search": search,
        "status": status or "",
        "customer_type": customer_type,
        "nas_id": nas_id or "",
        "pop_site_id": pop_site_id or "",
        "nas_options": nas_options,
        "pop_site_options": pop_site_options,
        "active_filter_count": active_customer_filter_count(
            search=search,
            status=status,
            customer_type=customer_type,
            nas_id=nas_id,
            pop_site_id=pop_site_id,
        ),
    }
