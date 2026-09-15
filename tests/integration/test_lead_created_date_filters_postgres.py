"""Lead creation-date parity on the real Alembic-migrated PostgreSQL schema."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.party import Party, PartyContactPoint
from app.models.sales import Lead
from app.services import sales


def test_date_scope_keeps_unique_rows_counts_summaries_and_pages(db_session):
    start = datetime(2026, 9, 1, tzinfo=UTC)
    next_day = start + timedelta(days=1)
    expected = []
    moments = [
        start - timedelta(microseconds=1),
        start,
        *[start + timedelta(hours=12)] * 10,
        next_day - timedelta(microseconds=1),
        next_day,
    ]
    for index, created in enumerate(moments):
        # The migrated schema permits one open Lead per Party and pipeline.
        # Use independent prospects instead of violating that business invariant.
        party = Party(
            display_name=f"Dated PostgreSQL contact {index}",
            party_type="person",
            status="active",
        )
        db_session.add(party)
        db_session.flush()
        for channel, value in [
            ("email", f"date-{index}@example.com"),
            ("phone", f"+234803555{index:04d}"),
        ]:
            db_session.add(
                PartyContactPoint(
                    party_id=party.id,
                    channel_type=channel,
                    normalized_value=value,
                    display_value=value,
                    is_active=True,
                )
            )
        lead = Lead(
            party_id=party.id,
            party_bound_at=start,
            party_binding_source="pytest-postgresql",
            party_binding_reason="Date-scope parity",
            title=f"Date scope {index}",
            status="new",
            estimated_value=Decimal("1000"),
            currency="NGN",
            created_at=created,
            metadata_={"date_test": {"index": index}},
        )
        db_session.add(lead)
        db_session.flush()
        if start <= created < next_day:
            expected.append(lead.id)
    pages = [
        sales.leads.query(
            db_session,
            sales.LeadListQueryInput(
                search_term="Dated PostgreSQL contact",
                status="new",
                date_preset="custom",
                date_from="2026-09-01",
                date_to="2026-09-01",
                page=page,
                page_size=10,
            ),
        )
        for page in (1, 2)
    ]
    combined = [lead.id for page in pages for lead in page.items]
    assert len(combined) == len(set(combined)) == 12
    assert set(combined) == set(expected)
    assert [len(page.items) for page in pages] == [10, 2]
    for page in pages:
        assert (
            page.total_count
            == page.summary.total_leads
            == page.summary.open_leads
            == 12
        )
        assert page.summary.won_leads == 0
        assert page.summary.pipeline_value == Decimal("12000")
