"""Admin leads list is routed through list_query (Carbon/WCAG list standard)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.models.party import Party, PartyContactPoint
from app.models.sales import Lead, Pipeline, PipelineStage
from app.models.subscriber import Subscriber
from app.services import sales, web_sales


def _party_lead(
    db_session,
    *,
    name: str,
    title: str,
    status: str = "new",
    pipeline_id=None,
    stage_id=None,
    owner_agent_id=None,
    lead_source: str | None = None,
    metadata: dict[str, object] | None = None,
    contacts: tuple[tuple[str, str, str], ...] = (),
) -> Lead:
    party = Party(display_name=name, party_type="person", status="active")
    db_session.add(party)
    db_session.flush()
    for channel_type, normalized_value, display_value in contacts:
        db_session.add(
            PartyContactPoint(
                party_id=party.id,
                channel_type=channel_type,
                normalized_value=normalized_value,
                display_value=display_value,
                is_active=True,
            )
        )
    lead = Lead(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Lead list query regression fixture",
        title=title,
        status=status,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        owner_agent_id=owner_agent_id,
        lead_source=lead_source,
        metadata_=metadata,
    )
    db_session.add(lead)
    db_session.commit()
    return lead


def _subscriber_lead(
    db_session,
    *,
    first_name: str,
    last_name: str,
    display_name: str | None = None,
    email: str = "subscriber@example.com",
    phone: str | None = None,
    title: str = "Subscriber opportunity",
) -> Lead:
    subscriber = Subscriber(
        first_name=first_name,
        last_name=last_name,
        display_name=display_name,
        email=f"{uuid4().hex[:8]}-{email}",
        phone=phone,
    )
    db_session.add(subscriber)
    db_session.flush()
    lead = Lead(subscriber_id=subscriber.id, title=title, metadata_={"fixture": True})
    db_session.add(lead)
    db_session.commit()
    return lead


def _query(db_session, **overrides):
    return sales.leads.query(db_session, sales.LeadListQueryInput(**overrides))


def test_lead_list_definition_declares_its_capabilities():
    definition = web_sales.LEAD_LIST_DEFINITION
    # Sortable keys mirror the leads.list order_by whitelist.
    assert set(definition.sortable_keys) == {"created_at", "updated_at"}
    assert set(definition.filterable_keys) == {
        "status",
        "lead_source",
        "pipeline_id",
        "stage_id",
        "owner_agent_id",
    }
    assert definition.default_sort == "created_at"


def test_build_leads_list_context_exposes_list_query_and_page_meta(db_session):
    ctx = web_sales.build_leads_list_context(
        db_session,
        status=None,
        pipeline_id=None,
        stage_id=None,
        lead_source=None,
        search=None,
        page=1,
        per_page=25,
    )
    assert "list_query" in ctx
    assert "page_meta" in ctx
    assert ctx["page"] == ctx["page_meta"].page
    assert ctx["total"] == ctx["page_meta"].total_items
    assert ctx["list_query"].page == ctx["page_meta"].page


def test_build_leads_list_context_normalizes_stale_params(db_session):
    ctx = web_sales.build_leads_list_context(
        db_session,
        status="not-a-status",
        pipeline_id="not-a-uuid",
        stage_id="also-not-a-uuid",
        lead_source="not-a-source",
        search=None,
        sort_by="status",  # filterable, not sortable → falls back
        sort_dir="sideways",
        page=1,
        per_page=999,
    )
    query = ctx["list_query"]
    assert query.sort_by == "created_at"
    assert query.sort_dir == "desc"
    assert query.per_page == 25
    # Unknown filter values are cleared, not applied.
    assert query.filter_value("status") is None
    assert query.filter_value("lead_source") is None
    assert query.filter_value("pipeline_id") is None
    assert query.filter_value("stage_id") is None
    assert query.filter_value("owner_agent_id") is None
    assert ctx["canonicalization_needed"] is True


def test_lead_list_has_a_stable_id_tie_breaker_across_pages(db_session, subscriber):
    created = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    rows = [
        Lead(
            id=UUID(int=value),
            subscriber_id=subscriber.id,
            title=f"Lead {value}",
            created_at=created,
        )
        for value in (4, 1, 3, 2)
    ]
    db_session.add_all(rows)
    db_session.commit()

    first = _query(db_session, page=1, page_size=10).items[:2]
    second = _query(db_session, page=1, page_size=10).items[2:4]

    assert [row.id.int for row in first + second] == [1, 2, 3, 4]


def test_lead_context_clamps_stale_page_into_canonical_query(db_session):
    ctx = web_sales.build_leads_list_context(
        db_session,
        status=None,
        pipeline_id=None,
        stage_id=None,
        lead_source=None,
        search=None,
        page=999,
        per_page=25,
    )
    assert ctx["list_query"].page == ctx["page_meta"].page == 1
    assert ctx["canonicalization_needed"] is True


def test_search_matches_lead_title_and_party_display_name(db_session):
    title_match = _party_lead(
        db_session,
        name="Unrelated Contact",
        title="Metro Fibre Expansion",
    )
    party_match = _party_lead(
        db_session,
        name="Ada Lovelace Ventures",
        title="Generic opportunity",
    )

    assert _query(db_session, search_term="fibre").items == (title_match,)
    assert _query(db_session, search_term="lovelace").items == (party_match,)


@pytest.mark.parametrize("term", ["Chinwe", "Okafor", "Chinwe Okafor", "inWE oka"])
def test_search_matches_subscriber_first_last_full_and_partial_name(db_session, term):
    lead = _subscriber_lead(
        db_session,
        first_name="Chinwe",
        last_name="Okafor",
        display_name="Chinwe Okafor",
    )

    result = _query(db_session, search_term=term)

    assert result.items == (lead,)
    assert result.total_count == 1


def test_search_collapses_leading_and_repeated_whitespace(db_session):
    lead = _subscriber_lead(
        db_session,
        first_name="John",
        last_name="Mensah",
    )

    result = _query(db_session, search_term="   John     Mensah   ")

    assert result.query.search_term == "John Mensah"
    assert result.items == (lead,)


@pytest.mark.parametrize(
    "term",
    ["contact@example.com", "+234 (803) 123-4567", "2348031234567"],
)
def test_search_matches_active_email_and_formatted_or_normalized_phone(
    db_session, term
):
    lead = _party_lead(
        db_session,
        name="Reachable Contact",
        title="Contact search",
        contacts=(
            ("email", "contact@example.com", "Contact@Example.com"),
            ("phone", "+2348031234567", "+234 (803) 123-4567"),
        ),
    )

    result = _query(db_session, search_term=term)

    assert result.items == (lead,)
    assert result.total_count == 1


def test_subscriber_phone_search_matches_normalized_digits(db_session):
    lead = _subscriber_lead(
        db_session,
        first_name="Phone",
        last_name="Only",
        phone="+234 (809) 765-4321",
    )

    assert _query(db_session, search_term="2348097654321").items == (lead,)


def test_party_first_lead_with_json_metadata_and_multiple_contacts_is_unique(
    db_session,
):
    lead = _party_lead(
        db_session,
        name="Multiple Contact John",
        title="JSON opportunity",
        metadata={"campaign": {"name": "August"}},
        contacts=(
            ("email", "john.one@example.com", "john.one@example.com"),
            ("email", "john.two@example.com", "john.two@example.com"),
            ("phone", "+2348011111111", "+234 801 111 1111"),
        ),
    )

    result = _query(db_session, search_term="john")

    assert result.items == (lead,)
    assert result.total_count == 1
    assert result.summary.total_leads == 1


def test_each_filter_and_search_filter_composition(db_session):
    pipeline = Pipeline(name="Enterprise")
    db_session.add(pipeline)
    db_session.flush()
    stage = PipelineStage(pipeline_id=pipeline.id, name="Qualified")
    db_session.add(stage)
    db_session.flush()
    owner_id = uuid4()
    expected = _party_lead(
        db_session,
        name="John Qualified",
        title="Target opportunity",
        status="qualified",
        pipeline_id=pipeline.id,
        stage_id=stage.id,
        owner_agent_id=owner_id,
        lead_source="Website",
    )
    _party_lead(
        db_session,
        name="Other Person",
        title="Distractor",
        status="new",
        lead_source="Email",
    )
    filters = {
        "status": "qualified",
        "pipeline_id": str(pipeline.id),
        "stage_id": str(stage.id),
        "owner_agent_id": str(owner_id),
        "lead_source": "Website",
    }

    for key, value in filters.items():
        assert _query(db_session, **{key: value}).items == (expected,)
        assert _query(db_session, search_term="John", **{key: value}).items == (
            expected,
        )
    assert _query(db_session, search_term="John", **filters).items == (expected,)


def test_filtered_count_summary_and_pagination_share_the_same_scope(db_session):
    matches = [
        _party_lead(
            db_session,
            name=f"Paged John {index}",
            title=f"Page target {index}",
            status="qualified",
        )
        for index in range(12)
    ]
    _party_lead(db_session, name="Not a match", title="Other", status="new")

    first = _query(
        db_session,
        search_term="Paged John",
        status="qualified",
        page=1,
        page_size=10,
    )
    second = _query(
        db_session,
        search_term="Paged John",
        status="qualified",
        page=2,
        page_size=10,
    )

    assert first.total_count == second.total_count == 12
    assert first.summary.total_leads == second.summary.total_leads == 12
    assert len(first.items) == 10
    assert len(second.items) == 2
    assert set(first.items + second.items) == set(matches)


def test_empty_no_match_and_invalid_queries_follow_canonical_policy(db_session):
    lead = _party_lead(db_session, name="Normal Lead", title="Ordinary")

    empty = _query(db_session, search_term="  \t  ")
    no_match = _query(db_session, search_term="does-not-exist")
    invalid = _query(
        db_session,
        status="invalid",
        pipeline_id="invalid",
        stage_id="invalid",
        owner_agent_id="invalid",
        lead_source="invalid",
        sort_field="invalid",
        sort_direction="invalid",
        page=-4,
        page_size=999,
    )

    assert empty.query.search_term is None
    assert empty.items == (lead,)
    assert no_match.items == ()
    assert no_match.total_count == 0
    assert invalid.query.status is None
    assert invalid.query.pipeline_id is None
    assert invalid.query.stage_id is None
    assert invalid.query.owner_agent_id is None
    assert invalid.query.lead_source is None
    assert invalid.query.sort_field is sales.LeadListSortField.CREATED_AT
    assert invalid.query.sort_direction is sales.LeadListSortDirection.DESC
    assert invalid.query.page == 1
    assert invalid.query.page_size == 25
