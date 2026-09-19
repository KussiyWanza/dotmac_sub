"""Legacy Quote callers retain all-time filtering after date-filter additions."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.sales import Quote
from app.models.subscriber import Subscriber
from app.services import sales


def test_legacy_quote_list_keeps_all_time_scope(
    db_session: Session, subscriber: Subscriber
) -> None:
    old = Quote(
        subscriber_id=subscriber.id,
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    future = Quote(
        subscriber_id=subscriber.id,
        created_at=datetime(2040, 1, 1, tzinfo=UTC),
    )
    inactive = Quote(
        subscriber_id=subscriber.id,
        created_at=datetime(2041, 1, 1, tzinfo=UTC),
        is_active=False,
    )
    db_session.add_all([old, future, inactive])
    db_session.commit()
    rows = sales.quotes.list(
        db_session,
        lead_id=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    typed = sales.quotes.query(db_session, sales.QuoteListQueryInput())
    assert [row.id for row in rows] == [future.id, old.id]
    assert [row.id for row in typed.items] == [row.id for row in rows]
    assert typed.total_count == len(rows) == 2
