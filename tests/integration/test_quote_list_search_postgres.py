"""PostgreSQL acceptance for the authoritative Quote list/search contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event

from app.models.party import Party, PartyContactPoint
from app.models.sales import Lead, Quote
from app.models.subscriber import Subscriber
from app.services import sales
from app.services.subscriber import _default_reseller_id


@dataclass(frozen=True, slots=True)
class QuoteSearchGraph:
    quote: Quote
    other_quote: Quote
    party_first_quote: Quote
    lead: Lead
    party_first_lead: Lead


def _party_lead(
    db_session,
    *,
    party_name: str,
    lead_title: str,
    subscriber: Subscriber | None = None,
) -> tuple[Party, Lead]:
    party = Party(display_name=party_name, party_type="person", status="active")
    db_session.add(party)
    db_session.flush()
    if subscriber is not None:
        subscriber.party_id = party.id
        subscriber.party_bound_at = datetime.now(UTC)
        subscriber.party_binding_source = "pytest-postgresql"
        subscriber.party_binding_reason = "Quote list PostgreSQL fixture"
    lead = Lead(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest-postgresql",
        party_binding_reason="Quote list PostgreSQL fixture",
        subscriber_id=subscriber.id if subscriber is not None else None,
        title=lead_title,
        status="qualified",
    )
    db_session.add(lead)
    db_session.flush()
    return party, lead


@pytest.fixture
def quote_search_graph(db_session) -> QuoteSearchGraph:
    subscriber = Subscriber(
        display_name="Aurora Subscriber Display",
        first_name="Adaora",
        last_name="Okafor",
        email="aurora.quote@example.com",
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(subscriber)
    db_session.flush()
    party, lead = _party_lead(
        db_session,
        party_name="North Star Holdings",
        lead_title="Fiber Expansion Alpha",
        subscriber=subscriber,
    )
    db_session.add_all(
        [
            PartyContactPoint(
                party_id=party.id,
                channel_type="email",
                normalized_value="active.quote@example.com",
                display_value="Active.Quote@example.com",
                is_active=True,
            ),
            PartyContactPoint(
                party_id=party.id,
                channel_type="phone",
                normalized_value="+2348031234567",
                display_value="+234 (803) 123-4567",
                is_active=True,
            ),
            PartyContactPoint(
                party_id=party.id,
                channel_type="whatsapp",
                normalized_value="multi-match-token",
                display_value="Multi Match Token",
                is_active=True,
            ),
            PartyContactPoint(
                party_id=party.id,
                channel_type="linkedin",
                normalized_value="multi-match-token-linkedin",
                display_value="Multi Match Token LinkedIn",
                is_active=True,
            ),
            PartyContactPoint(
                party_id=party.id,
                channel_type="email",
                normalized_value="inactive.quote@example.com",
                display_value="inactive.quote@example.com",
                is_active=False,
            ),
        ]
    )
    quote = Quote(
        subscriber_id=subscriber.id,
        lead_id=lead.id,
        status="sent",
        metadata_={"regression": {"postgres_json": True}},
    )

    other_subscriber = Subscriber(
        display_name="Different Subscriber",
        first_name="Grace",
        last_name="Eze",
        email="different.quote@example.com",
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(other_subscriber)
    db_session.flush()
    _, other_lead = _party_lead(
        db_session,
        party_name="Different Party",
        lead_title="Different Opportunity",
        subscriber=other_subscriber,
    )
    other_quote = Quote(
        subscriber_id=other_subscriber.id,
        lead_id=other_lead.id,
        status="draft",
        metadata_={"regression": {"postgres_json": "other"}},
    )

    party_first_party, party_first_lead = _party_lead(
        db_session,
        party_name="Party First Beacon",
        lead_title="Party First Opportunity",
    )
    db_session.add(
        PartyContactPoint(
            party_id=party_first_party.id,
            channel_type="email",
            normalized_value="party-first@example.com",
            display_value="party-first@example.com",
            is_active=True,
        )
    )
    party_first_quote = Quote(
        lead_id=party_first_lead.id,
        subscriber_id=None,
        status="sent",
        metadata_={"regression": {"party_first": True}},
    )
    db_session.add_all([quote, other_quote, party_first_quote])
    db_session.flush()
    return QuoteSearchGraph(
        quote=quote,
        other_quote=other_quote,
        party_first_quote=party_first_quote,
        lead=lead,
        party_first_lead=party_first_lead,
    )


@pytest.mark.parametrize(
    "search_value",
    (
        "Aurora Subscriber Display",
        "Adaora",
        "Okafor",
        "aurora.quote@example.com",
        "Fiber Expansion Alpha",
        "North Star Holdings",
        "active.quote@example.com",
        "+234 (803) 123-4567",
        "8031234567",
        "multi-match-token-linkedin",
    ),
)
def test_quote_search_matches_each_authoritative_projection(
    db_session,
    quote_search_graph: QuoteSearchGraph,
    search_value: str,
) -> None:
    result = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(search_term=search_value),
    )

    assert result.items == (quote_search_graph.quote,)
    assert result.total_count == 1


def test_quote_uuid_complete_and_partial_search(
    db_session,
    quote_search_graph: QuoteSearchGraph,
) -> None:
    complete = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(search_term=str(quote_search_graph.quote.id)),
    )
    partial = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(search_term=str(quote_search_graph.quote.id)[:10]),
    )

    assert complete.items == partial.items == (quote_search_graph.quote,)
    assert complete.total_count == partial.total_count == 1


def test_json_quote_with_multiple_matching_contacts_is_selected_once_without_distinct(
    db_session,
    quote_search_graph: QuoteSearchGraph,
) -> None:
    statements: list[str] = []

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", capture_statement)
    try:
        result = sales.quotes.query(
            db_session,
            sales.QuoteListQueryInput(search_term="multi match token"),
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture_statement)

    assert result.items == (quote_search_graph.quote,)
    assert result.total_count == 1
    quote_statements = [
        statement for statement in statements if "FROM quotes" in statement
    ]
    assert quote_statements
    assert all(
        "SELECT DISTINCT" not in statement.upper() for statement in quote_statements
    )


def test_inactive_contact_does_not_match_and_party_first_quote_does(
    db_session,
    quote_search_graph: QuoteSearchGraph,
) -> None:
    inactive = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(search_term="inactive.quote@example.com"),
    )
    by_lead = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(search_term="Party First Opportunity"),
    )
    by_party = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(search_term="Party First Beacon"),
    )
    by_contact = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(search_term="party-first@example.com"),
    )

    assert inactive.items == ()
    assert (
        by_lead.items
        == by_party.items
        == by_contact.items
        == (quote_search_graph.party_first_quote,)
    )


def test_status_lead_and_combined_search_filters_share_one_scope(
    db_session,
    quote_search_graph: QuoteSearchGraph,
) -> None:
    status_only = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(status="draft"),
    )
    lead_only = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(lead_id=str(quote_search_graph.lead.id)),
    )
    search_status = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(search_term="North Star", status="sent"),
    )
    search_lead = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(
            search_term="active.quote@example.com",
            lead_id=str(quote_search_graph.lead.id),
        ),
    )

    assert quote_search_graph.other_quote in status_only.items
    assert status_only.query.search_term is None
    assert lead_only.items == (quote_search_graph.quote,)
    assert lead_only.query.search_term is None
    assert search_status.items == search_lead.items == (quote_search_graph.quote,)
    assert search_status.total_count == search_lead.total_count == 1


def test_invalid_stale_empty_and_literal_like_search_are_canonicalized(
    db_session,
    quote_search_graph: QuoteSearchGraph,
) -> None:
    stale = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(
            status="retired-status",
            lead_id=str(uuid4()),
        ),
    )
    malformed = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(lead_id="not-a-uuid", search_term="   "),
    )
    unfiltered = sales.quotes.query(db_session, sales.QuoteListQueryInput())

    literal_party, literal_lead = _party_lead(
        db_session,
        party_name="Literal Search Party",
        lead_title=r"Literal 100%_\ Quote",
    )
    literal_quote = Quote(
        lead_id=literal_lead.id,
        status="draft",
        metadata_={"literal": True},
    )
    db_session.add(literal_quote)
    db_session.flush()
    literal = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(search_term="%_\\"),
    )

    assert stale.query.status is None
    assert stale.query.lead_id is None
    assert malformed.query.search_term is None
    assert malformed.query.lead_id is None
    assert malformed.items == unfiltered.items
    assert malformed.total_count == unfiltered.total_count
    assert literal.items == (literal_quote,)
    assert literal.total_count == 1
    assert literal_party.id == literal_lead.party_id


def test_pagination_is_stable_without_duplicates_or_missing_quotes(db_session) -> None:
    _, lead = _party_lead(
        db_session,
        party_name="Pagination Party",
        lead_title="Quote Pagination Cohort Token",
    )
    created_at = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    quotes = [
        Quote(
            id=UUID(int=10_000 + index),
            lead_id=lead.id,
            status="draft",
            created_at=created_at,
            metadata_={"page": index},
        )
        for index in range(12)
    ]
    db_session.add_all(reversed(quotes))
    db_session.flush()

    first = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(
            search_term="Quote Pagination Cohort Token",
            page=1,
            page_size=10,
        ),
    )
    second = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(
            search_term="Quote Pagination Cohort Token",
            page=2,
            page_size=10,
        ),
    )
    combined = first.items + second.items

    assert first.total_count == second.total_count == 12
    assert len(combined) == len({quote.id for quote in combined}) == 12
    assert [quote.id for quote in combined] == sorted(quote.id for quote in quotes)


def test_base_unfiltered_quote_list_continues_to_work(
    db_session,
    quote_search_graph: QuoteSearchGraph,
) -> None:
    result = sales.quotes.query(db_session, sales.QuoteListQueryInput())

    assert quote_search_graph.quote in result.items
    assert quote_search_graph.other_quote in result.items
    assert quote_search_graph.party_first_quote in result.items
    assert result.total_count >= len(result.items) >= 3
