from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import pytest

from app.models.support import Ticket, TicketComment, TicketCommentAuthorType
from app.schemas.support import TicketCommentCreate, TicketCommentUpdate, TicketCreate
from app.services import support as support_service
from app.services.realtime_platform import EventType
from app.services.support_ticket_contracts import SupportTicketCommentRealtimeChange

PublishedEvent = tuple[str, EventType, dict[str, object]]


def _capture_publications(monkeypatch) -> list[PublishedEvent]:
    published: list[PublishedEvent] = []

    def publish(
        topic: str,
        *,
        event_type: EventType,
        payload: dict[str, object],
        refresh_required: bool = True,
    ) -> bool:
        assert refresh_required is True
        published.append((topic, event_type, payload))
        return True

    monkeypatch.setattr(support_service, "publish_topic_event", publish)
    return published


def _ticket(db_session, subscriber_id: UUID | None) -> Ticket:
    return support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Realtime comment target",
            subscriber_id=subscriber_id,
            customer_account_id=subscriber_id,
        ),
        actor_id=str(subscriber_id) if subscriber_id is not None else None,
    )


def _comment_payload(
    *,
    author_type: TicketCommentAuthorType,
    subscriber_id: UUID,
    is_internal: bool,
    body: str = "Sensitive comment body",
) -> TicketCommentCreate:
    return TicketCommentCreate(
        body=body,
        is_internal=is_internal,
        author_type=author_type,
        author_person_id=(
            subscriber_id if author_type is TicketCommentAuthorType.customer else None
        ),
    )


def _assert_comment_hint(
    event: PublishedEvent,
    *,
    subscriber_id: UUID,
    ticket_id: UUID,
    change: SupportTicketCommentRealtimeChange,
    comment_id: UUID | None,
) -> None:
    topic, event_type, payload = event
    assert topic == f"principal:{subscriber_id}"
    assert event_type is EventType.SUPPORT_TICKET_COMMENT_CHANGED
    assert payload == {
        "ticket_id": str(ticket_id),
        "change": change.value,
        "comment_id": str(comment_id) if comment_id is not None else None,
    }
    assert set(payload) == {"ticket_id", "change", "comment_id"}


@pytest.mark.parametrize(
    "author_type",
    [TicketCommentAuthorType.customer, TicketCommentAuthorType.staff],
)
def test_public_comment_publishes_identifier_only_hint_after_commit(
    db_session,
    subscriber,
    monkeypatch,
    author_type,
) -> None:
    published = _capture_publications(monkeypatch)
    ticket = _ticket(db_session, subscriber.id)

    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        _comment_payload(
            author_type=author_type,
            subscriber_id=subscriber.id,
            is_internal=False,
        ),
        actor_id=str(subscriber.id),
    )

    assert len(published) == 1
    _assert_comment_hint(
        published[0],
        subscriber_id=subscriber.id,
        ticket_id=ticket.id,
        change=SupportTicketCommentRealtimeChange.comment_created,
        comment_id=comment.id,
    )
    assert "Sensitive comment body" not in str(published[0])


def test_internal_or_customerless_comment_does_not_publish(
    db_session, subscriber, monkeypatch
) -> None:
    published = _capture_publications(monkeypatch)
    ticket = _ticket(db_session, subscriber.id)
    customerless = _ticket(db_session, None)

    support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        _comment_payload(
            author_type=TicketCommentAuthorType.staff,
            subscriber_id=subscriber.id,
            is_internal=True,
        ),
    )
    support_service.tickets.create_comment(
        db_session,
        str(customerless.id),
        _comment_payload(
            author_type=TicketCommentAuthorType.staff,
            subscriber_id=subscriber.id,
            is_internal=False,
        ),
    )

    assert published == []


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_comment_hint_follows_the_owning_transaction(
    db_session, subscriber, monkeypatch, finish: str
) -> None:
    published = _capture_publications(monkeypatch)
    ticket = _ticket(db_session, subscriber.id)
    raw_create: Callable[..., TicketComment] = (
        support_service.Tickets.create_comment.__wrapped__
    )

    raw_create(
        db_session,
        str(ticket.id),
        _comment_payload(
            author_type=TicketCommentAuthorType.customer,
            subscriber_id=subscriber.id,
            is_internal=False,
        ),
        actor_id=str(subscriber.id),
    )
    assert published == []

    getattr(db_session, finish)()

    assert len(published) == (1 if finish == "commit" else 0)


def test_public_comment_update_and_delete_publish(
    db_session, subscriber, monkeypatch
) -> None:
    published = _capture_publications(monkeypatch)
    ticket = _ticket(db_session, subscriber.id)
    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        _comment_payload(
            author_type=TicketCommentAuthorType.staff,
            subscriber_id=subscriber.id,
            is_internal=False,
        ),
    )
    published.clear()

    comment = support_service.ticket_comments.update(
        db_session,
        comment=comment,
        payload=TicketCommentUpdate(body="Corrected public reply"),
        actor_id=None,
    )
    _assert_comment_hint(
        published.pop(),
        subscriber_id=subscriber.id,
        ticket_id=ticket.id,
        change=SupportTicketCommentRealtimeChange.comment_updated,
        comment_id=comment.id,
    )

    support_service.ticket_comments.delete(
        db_session,
        comment=comment,
        actor_id=None,
    )
    _assert_comment_hint(
        published.pop(),
        subscriber_id=subscriber.id,
        ticket_id=ticket.id,
        change=SupportTicketCommentRealtimeChange.comment_deleted,
        comment_id=comment.id,
    )
    assert published == []


def test_visibility_changes_publish_but_internal_edits_do_not(
    db_session, subscriber, monkeypatch
) -> None:
    published = _capture_publications(monkeypatch)
    ticket = _ticket(db_session, subscriber.id)
    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        _comment_payload(
            author_type=TicketCommentAuthorType.staff,
            subscriber_id=subscriber.id,
            is_internal=True,
        ),
    )

    comment = support_service.ticket_comments.update(
        db_session,
        comment=comment,
        payload=TicketCommentUpdate(body="Still private"),
        actor_id=None,
    )
    assert published == []

    comment = support_service.ticket_comments.update(
        db_session,
        comment=comment,
        payload=TicketCommentUpdate(is_internal=False),
        actor_id=None,
    )
    _assert_comment_hint(
        published.pop(),
        subscriber_id=subscriber.id,
        ticket_id=ticket.id,
        change=SupportTicketCommentRealtimeChange.comment_visibility_changed,
        comment_id=comment.id,
    )

    support_service.ticket_comments.update(
        db_session,
        comment=comment,
        payload=TicketCommentUpdate(is_internal=True),
        actor_id=None,
    )
    _assert_comment_hint(
        published.pop(),
        subscriber_id=subscriber.id,
        ticket_id=ticket.id,
        change=SupportTicketCommentRealtimeChange.comment_visibility_changed,
        comment_id=comment.id,
    )
    assert published == []


def test_bulk_comments_publish_one_coalesced_hint(
    db_session, subscriber, monkeypatch
) -> None:
    published = _capture_publications(monkeypatch)
    ticket = _ticket(db_session, subscriber.id)

    comments = support_service.tickets.bulk_create_comments(
        db_session,
        str(ticket.id),
        [
            _comment_payload(
                author_type=TicketCommentAuthorType.staff,
                subscriber_id=subscriber.id,
                is_internal=False,
                body="First public reply",
            ),
            _comment_payload(
                author_type=TicketCommentAuthorType.staff,
                subscriber_id=subscriber.id,
                is_internal=False,
                body="Second public reply",
            ),
            _comment_payload(
                author_type=TicketCommentAuthorType.staff,
                subscriber_id=subscriber.id,
                is_internal=True,
                body="Private note",
            ),
        ],
    )

    assert len(comments) == 3
    assert len(published) == 1
    _assert_comment_hint(
        published[0],
        subscriber_id=subscriber.id,
        ticket_id=ticket.id,
        change=SupportTicketCommentRealtimeChange.comment_created,
        comment_id=None,
    )


def test_realtime_delivery_failure_does_not_fail_comment_creation(
    db_session, subscriber, monkeypatch
) -> None:
    monkeypatch.setattr(support_service, "publish_topic_event", lambda *_a, **_k: False)
    ticket = _ticket(db_session, subscriber.id)

    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        _comment_payload(
            author_type=TicketCommentAuthorType.customer,
            subscriber_id=subscriber.id,
            is_internal=False,
        ),
        actor_id=str(subscriber.id),
    )

    assert db_session.get(TicketComment, comment.id) is not None
