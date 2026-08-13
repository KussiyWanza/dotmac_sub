import logging

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.party import Party, PartyIdentityStatus
from app.models.subscriber import Subscriber, SubscriberCategory
from app.services.response import list_response

logger = logging.getLogger(__name__)

_MAX_SEARCH_LIMIT = 50


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


def search(
    db: Session, query: str, limit: int = 20, *, reviewed_only: bool = False
) -> list[dict]:
    term = (query or "").strip()
    if not term:
        return []
    limit = min(limit, _MAX_SEARCH_LIMIT)
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
    if reviewed_only:
        statement = statement.join(Party, Subscriber.party_id == Party.id).where(
            Subscriber.party_id.is_not(None),
            Subscriber.party_bound_at.is_not(None),
            Subscriber.party_binding_source.is_not(None),
            Subscriber.party_binding_reason.is_not(None),
            Party.status == PartyIdentityStatus.active.value,
        )
    people = list(
        db.scalars(
            statement.order_by(Subscriber.last_name, Subscriber.first_name).limit(limit)
        ).all()
    )
    items: list[dict] = []
    for subscriber in people:
        if subscriber.category == SubscriberCategory.business:
            label = (
                subscriber.company_name
                or subscriber.display_name
                or subscriber.full_name
            )
            if subscriber.domain:
                label = f"{label} ({subscriber.domain})"
            items.append(
                {
                    "id": subscriber.id,
                    "type": "business",
                    "label": label,
                    "ref": f"business:{subscriber.id}",
                }
            )
            continue
        label = f"{subscriber.first_name} {subscriber.last_name}"
        if subscriber.email:
            label = f"{label} ({subscriber.email})"
        items.append(
            {
                "id": subscriber.id,
                # Backwards-compat: historically this search returned "person"
                # and other form helpers still parse refs like "person:<uuid>".
                "type": "person",
                "label": label,
                "ref": f"person:{subscriber.id}",
            }
        )
    items.sort(key=lambda item: item["label"].lower())
    return items[:limit]


def search_response(
    db: Session, query: str, limit: int = 20, *, reviewed_only: bool = False
) -> dict:
    items = search(db, query, limit, reviewed_only=reviewed_only)
    return list_response(items, limit, 0)
