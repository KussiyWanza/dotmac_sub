import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.party import Party, PartyIdentityStatus
from app.models.subscriber import Subscriber, SubscriberCategory
from app.services.response import list_response

logger = logging.getLogger(__name__)

_MAX_SEARCH_LIMIT = 50


@dataclass(frozen=True, slots=True)
class CustomerSearchQuery:
    """Typed read request over canonical active customer accounts."""

    term: str
    limit: int = 20
    reviewed_only: bool = False


@dataclass(frozen=True, slots=True)
class CustomerSearchMatch:
    """Canonical customer match before an adapter chooses its wire shape."""

    id: UUID
    customer_type: Literal["person", "business"]
    name: str
    label: str
    ref: str
    email: str | None
    account_number: str | None
    subscriber_number: str | None


@dataclass(frozen=True, slots=True)
class CustomerSearchPage:
    items: tuple[CustomerSearchMatch, ...]
    count: int
    limit: int
    offset: int = 0


def _business_clause():
    return (
        func.lower(
            func.coalesce(Subscriber.metadata_["subscriber_category"].as_string(), "")
        )
        == SubscriberCategory.business.value
    )


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards in user input."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def query_customers(db: Session, query: CustomerSearchQuery) -> CustomerSearchPage:
    """Return a bounded typed customer-search projection."""

    term = (query.term or "").strip()
    limit = max(1, min(query.limit, _MAX_SEARCH_LIMIT))
    if not term:
        return CustomerSearchPage(items=(), count=0, limit=limit)
    escaped = _escape_like(term)
    like_term = f"%{escaped}%"

    # Split query into words for multi-word name matching
    # e.g. "Dotmac Karu" matches first_name~Dotmac AND last_name~Karu
    words = term.split()
    conditions: list[ColumnElement[bool]] = [
        Subscriber.first_name.ilike(like_term),
        Subscriber.last_name.ilike(like_term),
        Subscriber.company_name.ilike(like_term),
        Subscriber.legal_name.ilike(like_term),
        Subscriber.domain.ilike(like_term),
        Subscriber.email.ilike(like_term),
        Subscriber.phone.ilike(like_term),
        Subscriber.account_number.ilike(like_term),
        Subscriber.subscriber_number.ilike(like_term),
    ]
    try:
        account_id = UUID(term)
    except ValueError:
        account_id = None
    if account_id is not None:
        conditions.append(Subscriber.id == account_id)
    if len(words) >= 2:
        # Also match first word against first_name + second against last_name
        first_like = f"%{_escape_like(words[0])}%"
        rest_like = f"%{_escape_like(' '.join(words[1:]))}%"
        from sqlalchemy import and_

        conditions.append(
            and_(
                Subscriber.first_name.ilike(first_like),
                Subscriber.last_name.ilike(rest_like),
            )
        )

    statement = select(Subscriber).where(
        Subscriber.is_active.is_(True),
        or_(*conditions),
    )
    if query.reviewed_only:
        statement = statement.join(Party, Subscriber.party_id == Party.id).where(
            Subscriber.party_id.is_not(None),
            Subscriber.party_bound_at.is_not(None),
            Subscriber.party_binding_source.is_not(None),
            Subscriber.party_binding_reason.is_not(None),
            Party.status == PartyIdentityStatus.active.value,
        )
    people = list(
        db.scalars(
            statement.order_by(
                Subscriber.last_name,
                Subscriber.first_name,
                Subscriber.id,
            ).limit(limit)
        ).all()
    )
    items: list[CustomerSearchMatch] = []
    for subscriber in people:
        if subscriber.category == SubscriberCategory.business:
            name = str(
                subscriber.company_name
                or subscriber.display_name
                or subscriber.full_name
            )
            label = name
            if subscriber.domain:
                label = f"{label} ({subscriber.domain})"
            items.append(
                CustomerSearchMatch(
                    id=subscriber.id,
                    customer_type="business",
                    name=name,
                    label=label,
                    ref=f"business:{subscriber.id}",
                    email=subscriber.email or None,
                    account_number=subscriber.account_number or None,
                    subscriber_number=subscriber.subscriber_number or None,
                )
            )
            continue
        name = f"{subscriber.first_name} {subscriber.last_name}".strip()
        label = name
        if subscriber.email:
            label = f"{label} ({subscriber.email})"
        items.append(
            CustomerSearchMatch(
                id=subscriber.id,
                customer_type="person",
                name=name,
                label=label,
                # Backwards-compatible adapters retain the historical ref.
                ref=f"person:{subscriber.id}",
                email=subscriber.email or None,
                account_number=subscriber.account_number or None,
                subscriber_number=subscriber.subscriber_number or None,
            )
        )
    items.sort(key=lambda item: (item.label.casefold(), str(item.id)))
    bounded = tuple(items[:limit])
    return CustomerSearchPage(items=bounded, count=len(bounded), limit=limit)


def get_customer_match(
    db: Session, customer_id: UUID, *, active_only: bool = False
) -> CustomerSearchMatch | None:
    """Resolve one canonical customer for a selected-value projection."""

    subscriber = db.get(Subscriber, customer_id)
    if subscriber is None or (active_only and not subscriber.is_active):
        return None
    customer_type: Literal["person", "business"] = (
        "business" if subscriber.category == SubscriberCategory.business else "person"
    )
    name = (
        str(subscriber.company_name or subscriber.display_name or subscriber.full_name)
        if customer_type == "business"
        else f"{subscriber.first_name} {subscriber.last_name}".strip()
    )
    label = name
    if customer_type == "business" and subscriber.domain:
        label = f"{label} ({subscriber.domain})"
    elif customer_type == "person" and subscriber.email:
        label = f"{label} ({subscriber.email})"
    return CustomerSearchMatch(
        id=subscriber.id,
        customer_type=customer_type,
        name=name,
        label=label,
        ref=f"{customer_type}:{subscriber.id}",
        email=subscriber.email or None,
        account_number=subscriber.account_number or None,
        subscriber_number=subscriber.subscriber_number or None,
    )


def search(
    db: Session, query: str, limit: int = 20, *, reviewed_only: bool = False
) -> list[dict]:
    """Compatibility adapter for existing customer-search consumers."""

    page = query_customers(
        db,
        CustomerSearchQuery(term=query or "", limit=limit, reviewed_only=reviewed_only),
    )
    return [
        {
            "id": item.id,
            "type": item.customer_type,
            "label": item.label,
            "ref": item.ref,
        }
        for item in page.items
    ]


def search_response(
    db: Session, query: str, limit: int = 20, *, reviewed_only: bool = False
) -> dict:
    items = search(db, query, limit, reviewed_only=reviewed_only)
    return list_response(items, limit, 0)
