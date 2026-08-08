from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

from app.models.sequence import DocumentSequence
from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketComment
from app.services import support as support_service
from app.services.crm_ticket_pull import pull_tickets


def test_pull_pushes_customer_when_ticket_resolved(db_session, subscriber):
    """A ticket transitioning to resolved during a pull notifies the customer."""
    subscriber.splynx_customer_id = 24296
    db_session.commit()
    tid = str(uuid4())

    # First pull: open → created, no notification.
    open_client = FakeCrmClient(
        tickets=[_crm_ticket(tid, "40001", "2026-06-09T10:00:00Z", status="open")],
        subscribers={},
        comments={tid: []},
    )
    with patch("app.services.push.send_push") as push:
        pull_tickets(db_session, client=open_client)
        db_session.commit()
    push.assert_not_called()

    # Second pull: same ticket now resolved (bumped updated_at) → one push.
    resolved_client = FakeCrmClient(
        tickets=[_crm_ticket(tid, "40001", "2026-06-10T10:00:00Z", status="resolved")],
        subscribers={},
        comments={tid: []},
    )
    with patch("app.services.push.send_push") as push:
        pull_tickets(db_session, client=resolved_client)
        db_session.commit()
    push.assert_called_once()


class FakeCrmClient:
    def __init__(self, tickets, subscribers, comments):
        self._tickets = tickets
        self._subscribers = subscribers
        self._comments = comments

    def list_tickets(self, **kwargs):
        limit = kwargs.get("limit", 200)
        offset = kwargs.get("offset", 0)
        return self._tickets[offset : offset + limit]

    def get_subscriber(self, subscriber_id):
        return self._subscribers[subscriber_id]

    def list_subscribers(self, **kwargs):
        external_system = kwargs.get("external_system")
        items = list(self._subscribers.values())
        if external_system:
            items = [
                item for item in items if item.get("external_system") == external_system
            ]
        return items

    def list_ticket_comments(self, ticket_id, **kwargs):
        return self._comments.get(ticket_id, [])


def test_pull_crm_ticket_preserves_number_and_maps_subscriber(db_session, subscriber):
    subscriber.splynx_customer_id = 201
    db_session.commit()
    crm_subscriber_id = str(uuid4())
    crm_ticket_id = str(uuid4())
    client = FakeCrmClient(
        tickets=[
            {
                "id": crm_ticket_id,
                "subscriber_id": crm_subscriber_id,
                "number": "20151",
                "title": "Router offline",
                "description": "Customer cannot browse",
                "status": "open",
                "priority": "high",
                "channel": "phone",
                "ticket_type": "support",
                "tags": ["noc"],
                "is_active": True,
            }
        ],
        subscribers={
            crm_subscriber_id: {
                "id": crm_subscriber_id,
                "external_system": "splynx",
                "external_id": "201",
            }
        },
        comments={
            crm_ticket_id: [
                {
                    "id": str(uuid4()),
                    "body": "Checked power.",
                    "is_internal": False,
                    "attachments": [],
                }
            ]
        },
    )

    result = pull_tickets(db_session, client=client)
    db_session.commit()

    assert result.created == 1
    ticket = db_session.query(Ticket).filter(Ticket.number == "20151").one()
    assert ticket.subscriber_id == subscriber.id
    assert ticket.title == "Router offline"
    assert ticket.metadata_["crm_ticket_id"] == crm_ticket_id
    comments = db_session.query(TicketComment).filter_by(ticket_id=ticket.id).all()
    assert len(comments) == 1
    assert comments[0].is_internal is True


def test_pull_advances_native_ticket_sequence_past_imported_numbers(
    db_session, subscriber
):
    subscriber.splynx_customer_id = 201
    db_session.add(DocumentSequence(key="support_ticket", next_value=1))
    db_session.commit()
    crm_subscriber_id = str(uuid4())
    client = FakeCrmClient(
        tickets=[
            {
                "id": str(uuid4()),
                "subscriber_id": crm_subscriber_id,
                "number": "20151",
                "title": "Imported ticket",
                "status": "open",
                "priority": "normal",
                "channel": "phone",
                "is_active": True,
            }
        ],
        subscribers={
            crm_subscriber_id: {
                "id": crm_subscriber_id,
                "external_system": "splynx",
                "external_id": "201",
            }
        },
        comments={},
    )

    pull_tickets(db_session, client=client)

    sequence = (
        db_session.query(DocumentSequence)
        .filter(DocumentSequence.key == "support_ticket")
        .one()
    )
    assert sequence.next_value == 20152


def test_native_allocation_reconciles_a_dense_imported_number_range(db_session):
    db_session.add(DocumentSequence(key="support_ticket", next_value=1))
    db_session.add_all(
        [
            Ticket(number=str(number), title=f"Imported ticket {number}")
            for number in range(1, 10_001)
        ]
    )
    db_session.commit()

    number = support_service.Tickets._resolve_ticket_number(db_session)

    assert number == "10001"


def test_pull_crm_ticket_is_idempotent_for_ticket_and_comments(db_session, subscriber):
    subscriber.splynx_customer_id = 201
    db_session.commit()
    crm_subscriber_id = str(uuid4())
    crm_ticket_id = str(uuid4())
    crm_comment_id = str(uuid4())
    client = FakeCrmClient(
        tickets=[
            {
                "id": crm_ticket_id,
                "subscriber_id": crm_subscriber_id,
                "number": "20151",
                "title": "Router offline",
                "status": "open",
                "priority": "high",
                "channel": "phone",
                "is_active": True,
            }
        ],
        subscribers={
            crm_subscriber_id: {
                "id": crm_subscriber_id,
                "external_system": "splynx",
                "external_id": "201",
            }
        },
        comments={
            crm_ticket_id: [
                {
                    "id": crm_comment_id,
                    "body": "Checked power.",
                    "is_internal": False,
                    "attachments": [],
                }
            ]
        },
    )

    first = pull_tickets(db_session, client=client)
    second = pull_tickets(db_session, client=client)
    db_session.commit()

    assert first.created == 1
    assert second.updated == 1
    assert db_session.query(Ticket).filter(Ticket.number == "20151").count() == 1
    assert db_session.query(TicketComment).count() == 1


def test_pull_merges_metadata_preserving_sub_owned_state(db_session, subscriber):
    """A CRM re-pull must never wipe sub-owned ticket decision state (csat,
    resolution confirmation, the field-visit work_order_id link): metadata is
    merged, with CRM-derived keys updating in place."""
    subscriber.splynx_customer_id = 201
    db_session.commit()
    crm_subscriber_id = str(uuid4())
    crm_ticket_id = str(uuid4())
    crm_ticket = {
        "id": crm_ticket_id,
        "subscriber_id": crm_subscriber_id,
        "number": "20152",
        "title": "Slow speeds",
        "status": "open",
        "priority": "normal",
        "channel": "phone",
        "is_active": True,
    }
    client = FakeCrmClient(
        tickets=[crm_ticket],
        subscribers={
            crm_subscriber_id: {
                "id": crm_subscriber_id,
                "external_system": "splynx",
                "external_id": "201",
            }
        },
        comments={crm_ticket_id: []},
    )
    pull_tickets(db_session, client=client)
    db_session.commit()

    ticket = db_session.query(Ticket).filter(Ticket.number == "20152").one()
    # Sub-owned decision state lands on the ticket between pulls.
    ticket.metadata_ = {
        **(ticket.metadata_ or {}),
        "csat": {"rating": 5, "comment": "great"},
        "resolution_confirmation": {"state": "pending_confirmation"},
        "work_order_id": "sub-abc123",
    }
    db_session.commit()

    # CRM updates its side; the re-pull must merge, not replace.
    crm_ticket["priority"] = "high"
    crm_ticket["updated_at"] = "2026-07-16T18:00:00+00:00"
    pull_tickets(db_session, client=client)
    db_session.commit()

    db_session.refresh(ticket)
    assert ticket.metadata_["csat"] == {"rating": 5, "comment": "great"}
    assert ticket.metadata_["resolution_confirmation"] == {
        "state": "pending_confirmation"
    }
    assert ticket.metadata_["work_order_id"] == "sub-abc123"
    # CRM-derived keys still refresh.
    assert ticket.metadata_["crm_ticket_id"] == crm_ticket_id
    assert ticket.metadata_["crm_updated_at"] == "2026-07-16T18:00:00+00:00"
    assert ticket.priority == "high"


def test_pull_crm_ticket_skips_lead_only_tickets(db_session):
    client = FakeCrmClient(
        tickets=[
            {
                "id": str(uuid4()),
                "lead_id": str(uuid4()),
                "subscriber_id": None,
                "number": "20151",
                "title": "Lead ticket",
            }
        ],
        subscribers={},
        comments={},
    )

    result = pull_tickets(db_session, client=client)

    assert result.skipped_leads == 1
    assert db_session.query(Ticket).count() == 0


def test_pull_crm_ticket_maps_single_customer_id_pair_from_title(
    db_session, subscriber
):
    subscriber.splynx_customer_id = 24294
    db_session.commit()
    crm_ticket_id = str(uuid4())
    client = FakeCrmClient(
        tickets=[
            {
                "id": crm_ticket_id,
                "subscriber_id": str(uuid4()),
                "number": "21404",
                "title": "Gems Communications Ltd (100024294 - 24294)",
                "status": "open",
                "priority": "normal",
                "channel": "phone",
                "is_active": True,
            }
        ],
        subscribers={},
        comments={},
    )

    result = pull_tickets(db_session, client=client)
    db_session.commit()

    assert result.created == 1
    ticket = db_session.query(Ticket).filter(Ticket.number == "21404").one()
    assert ticket.subscriber_id == subscriber.id


def test_pull_crm_ticket_skips_ambiguous_customer_id_pairs(db_session, subscriber):
    subscriber.splynx_customer_id = 8270
    other_subscriber = Subscriber(
        first_name="Other",
        last_name="User",
        email=f"other-{uuid4()}@example.com",
        splynx_customer_id=10035,
    )
    db_session.add(other_subscriber)
    db_session.commit()
    crm_ticket_id = str(uuid4())
    client = FakeCrmClient(
        tickets=[
            {
                "id": crm_ticket_id,
                "subscriber_id": None,
                "number": "21217",
                "title": "Multiple customer link disconnection",
                "description": "(100008270 - 8270) and (100010035 - 10035)",
            }
        ],
        subscribers={},
        comments={},
    )

    result = pull_tickets(db_session, client=client)

    assert result.skipped_unmapped_subscribers == 1
    assert db_session.query(Ticket).count() == 0


def test_pull_crm_ticket_maps_via_stored_crm_subscriber_id(db_session, subscriber):
    crm_subscriber_id = uuid4()
    subscriber.splynx_customer_id = None
    subscriber.crm_subscriber_id = crm_subscriber_id
    db_session.commit()
    crm_ticket_id = str(uuid4())
    client = FakeCrmClient(
        tickets=[
            {
                "id": crm_ticket_id,
                "subscriber_id": str(crm_subscriber_id),
                "number": "30001",
                "title": "ERPNext customer ticket",
                "status": "open",
                "priority": "normal",
                "channel": "phone",
                "is_active": True,
            }
        ],
        subscribers={},
        comments={},
    )

    result = pull_tickets(db_session, client=client)
    db_session.commit()

    assert result.created == 1
    ticket = db_session.query(Ticket).filter(Ticket.number == "30001").one()
    assert ticket.subscriber_id == subscriber.id


def test_pull_crm_ticket_persists_crm_link_after_legacy_resolution(
    db_session, subscriber
):
    subscriber.splynx_customer_id = 202
    db_session.commit()
    crm_subscriber_id = str(uuid4())
    client = FakeCrmClient(
        tickets=[
            {
                "id": str(uuid4()),
                "subscriber_id": crm_subscriber_id,
                "number": "30002",
                "title": "Imported customer ticket",
                "status": "open",
                "priority": "normal",
                "channel": "phone",
                "is_active": True,
            }
        ],
        subscribers={
            crm_subscriber_id: {
                "id": crm_subscriber_id,
                "external_system": "splynx",
                "external_id": "202",
            }
        },
        comments={},
    )

    result = pull_tickets(db_session, client=client)
    db_session.commit()
    db_session.refresh(subscriber)

    assert result.created == 1
    assert str(subscriber.crm_subscriber_id) == crm_subscriber_id


def test_pull_crm_ticket_text_match_does_not_persist_crm_link(db_session, subscriber):
    subscriber.splynx_customer_id = 24295
    db_session.commit()
    client = FakeCrmClient(
        tickets=[
            {
                "id": str(uuid4()),
                "subscriber_id": str(uuid4()),  # unknown CRM subscriber
                "number": "30003",
                "title": "Acme Ltd (100024295 - 24295)",
                "status": "open",
                "priority": "normal",
                "channel": "phone",
                "is_active": True,
            }
        ],
        subscribers={},
        comments={},
    )

    result = pull_tickets(db_session, client=client)
    db_session.commit()
    db_session.refresh(subscriber)

    assert result.created == 1
    assert subscriber.crm_subscriber_id is None


def test_pull_crm_ticket_maps_via_alias_crm_id(db_session, subscriber):
    primary_crm_id = uuid4()
    alias_crm_id = uuid4()
    subscriber.splynx_customer_id = None
    subscriber.crm_subscriber_id = primary_crm_id
    subscriber.metadata_ = {"crm_alias_ids": [str(alias_crm_id)]}
    db_session.commit()
    client = FakeCrmClient(
        tickets=[
            {
                "id": str(uuid4()),
                "subscriber_id": str(alias_crm_id),  # duplicate CRM record
                "number": "30004",
                "title": "Ticket on the erpnext duplicate record",
                "status": "open",
                "priority": "normal",
                "channel": "phone",
                "is_active": True,
            }
        ],
        subscribers={},
        comments={},
    )

    result = pull_tickets(db_session, client=client)
    db_session.commit()

    assert result.created == 1
    ticket = db_session.query(Ticket).filter(Ticket.number == "30004").one()
    assert ticket.subscriber_id == subscriber.id


from datetime import UTC, datetime

from app.services.crm_ticket_pull import latest_crm_updated_at


class CountingCrmClient(FakeCrmClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.comment_calls: list[str] = []

    def list_ticket_comments(self, ticket_id, **kwargs):
        self.comment_calls.append(ticket_id)
        return super().list_ticket_comments(ticket_id, **kwargs)


def _crm_ticket(crm_ticket_id, number, updated_at, status="open", **extra):
    return {
        "id": crm_ticket_id,
        "subscriber_id": None,
        "number": number,
        "title": "Acme Ltd (100024296 - 24296)",
        "status": status,
        "priority": "normal",
        "channel": "phone",
        "is_active": True,
        "updated_at": updated_at,
        **extra,
    }


def test_incremental_unchanged_skips_rewrite_and_comments(db_session, subscriber):
    subscriber.splynx_customer_id = 24296
    db_session.commit()
    crm_ticket_id = str(uuid4())
    ts = "2026-06-09T10:00:00Z"
    client = CountingCrmClient(
        tickets=[_crm_ticket(crm_ticket_id, "40001", ts)],
        subscribers={},
        comments={crm_ticket_id: []},
    )

    first = pull_tickets(db_session, client=client)
    db_session.commit()
    assert first.created == 1
    calls_after_first = len(client.comment_calls)

    since = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)
    second = pull_tickets(db_session, client=client, since=since)
    db_session.commit()

    assert second.unchanged == 1
    assert second.created == 0 and second.updated == 0
    # open-state ticket gets a comment look via the sweep, not the per-ticket
    # fetch — exactly one extra call for this open ticket.
    assert len(client.comment_calls) == calls_after_first + 1


def test_incremental_stops_at_watermark(db_session, subscriber):
    subscriber.splynx_customer_id = 24296
    db_session.commit()
    new_id, old_id = str(uuid4()), str(uuid4())
    client = CountingCrmClient(
        tickets=[
            _crm_ticket(new_id, "40002", "2026-06-09T12:00:00Z"),
            _crm_ticket(old_id, "40003", "2026-06-09T08:00:00Z"),
        ],
        subscribers={},
        comments={},
    )
    since = datetime(2026, 6, 9, 10, 0, tzinfo=UTC)

    result = pull_tickets(db_session, client=client, since=since)

    assert result.fetched == 1  # stopped before the pre-watermark ticket
    assert result.created == 1


def test_incremental_sweep_pulls_new_comment_on_unchanged_open_ticket(
    db_session, subscriber
):
    subscriber.splynx_customer_id = 24296
    db_session.commit()
    crm_ticket_id = str(uuid4())
    ts = "2026-06-09T10:00:00Z"
    client = CountingCrmClient(
        tickets=[_crm_ticket(crm_ticket_id, "40004", ts)],
        subscribers={},
        comments={crm_ticket_id: []},
    )
    pull_tickets(db_session, client=client)
    db_session.commit()

    # New CRM comment arrives without bumping the ticket's updated_at.
    client._comments[crm_ticket_id] = [
        {"id": str(uuid4()), "body": "Any update?", "is_internal": False}
    ]
    since = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)
    result = pull_tickets(db_session, client=client, since=since)
    db_session.commit()

    assert result.unchanged == 1
    assert result.comments_created == 1


def test_full_mode_fetches_comments_for_unchanged_closed_ticket(db_session, subscriber):
    subscriber.splynx_customer_id = 24296
    db_session.commit()
    crm_ticket_id = str(uuid4())
    ts = "2026-06-09T10:00:00Z"
    client = CountingCrmClient(
        tickets=[_crm_ticket(crm_ticket_id, "40005", ts, status="closed")],
        subscribers={},
        comments={crm_ticket_id: []},
    )
    pull_tickets(db_session, client=client)
    db_session.commit()

    client._comments[crm_ticket_id] = [
        {"id": str(uuid4()), "body": "Late note", "is_internal": True}
    ]
    result = pull_tickets(db_session, client=client)  # full mode: no since
    db_session.commit()

    assert result.unchanged == 1
    assert result.comments_created == 1


def test_latest_crm_updated_at_watermark(db_session, subscriber):
    subscriber.splynx_customer_id = 24296
    db_session.commit()
    client = CountingCrmClient(
        tickets=[
            _crm_ticket(str(uuid4()), "40006", "2026-06-09T12:30:00Z"),
            _crm_ticket(str(uuid4()), "40007", "2026-06-08T01:00:00Z"),
        ],
        subscribers={},
        comments={},
    )
    pull_tickets(db_session, client=client)
    db_session.commit()

    watermark = latest_crm_updated_at(db_session)
    assert watermark == datetime(2026, 6, 9, 12, 30, tzinfo=UTC)
