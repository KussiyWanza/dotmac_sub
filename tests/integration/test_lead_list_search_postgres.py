"""PostgreSQL regression for Lead search over rows containing JSON metadata."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.party import Party, PartyContactPoint
from app.models.sales import Lead
from app.services import sales


def test_lead_search_does_not_distinct_complete_json_rows(db_session):
    party = Party(
        display_name="Postgres JSON Search Customer",
        party_type="person",
        status="active",
    )
    db_session.add(party)
    db_session.flush()
    db_session.add_all(
        [
            PartyContactPoint(
                party_id=party.id,
                channel_type="email",
                normalized_value="postgres-search@example.com",
                display_value="postgres-search@example.com",
                is_active=True,
            ),
            PartyContactPoint(
                party_id=party.id,
                channel_type="phone",
                normalized_value="+2348035550101",
                display_value="+234 (803) 555-0101",
                is_active=True,
            ),
        ]
    )
    lead = Lead(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest-postgresql",
        party_binding_reason="Reproduce the JSON DISTINCT Lead search failure",
        title="PostgreSQL JSON opportunity",
        metadata_={"campaign": {"source": "integration-regression"}},
    )
    db_session.add(lead)
    db_session.flush()

    result = sales.leads.query(
        db_session,
        sales.LeadListQueryInput(search_term="Postgres JSON Search"),
    )

    assert result.items == (lead,)
    assert result.total_count == 1
    assert result.summary.total_leads == 1
