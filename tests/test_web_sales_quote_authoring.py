"""Focused contract tests for Lead-backed admin Quote authoring."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from app.models.billing import TaxRate
from app.models.field_material import FieldInventoryItem
from app.models.party import Party, PartyIdentityStatus, PartyType
from app.models.project import ProjectType
from app.models.sales import Lead, LeadStatus, Quote, QuoteLineItem, QuoteStatus
from app.models.system_user import SystemUser
from app.services import web_sales
from app.services.db_session_adapter import db_session_adapter
from app.services.sales import quote_authoring


@pytest.fixture
def feasible():
    """Keep location coverage data out of the Quote authoring unit boundary."""

    with patch.object(
        quote_authoring,
        "compute_feasibility",
        return_value={"coverage": "green", "feasible": True, "distance_meters": 120},
    ) as stub:
        yield stub


def _identity(db_session) -> tuple[SystemUser, Lead, Party]:
    party = Party(
        party_type=PartyType.person.value,
        display_name="Amina Bello",
        status=PartyIdentityStatus.active.value,
    )
    actor = SystemUser(
        first_name="Sales",
        last_name="Author",
        email=f"quote-author-{uuid4().hex}@example.com",
        is_active=True,
    )
    db_session.add_all([party, actor])
    db_session.flush()
    lead = Lead(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Quote authoring test identity",
        title="Fiber deployment",
        status=LeadStatus.new.value,
        is_active=True,
    )
    db_session.add(lead)
    db_session.commit()
    return actor, lead, party


def _create(db_session, actor: SystemUser, lead: Lead, **overrides: object) -> UUID:
    fields: dict[str, object] = {
        "actor_system_user_id": str(actor.id),
        "submission_id": str(uuid4()),
        "lead_id": str(lead.id),
        "status": QuoteStatus.draft.value,
        "currency": "NGN",
        "project_type": ProjectType.fiber_optics_installation.value,
        "tax_rate_id": None,
        "manual_tax_total": "0.00",
        "expires_at": None,
        "is_active": True,
        "notes": None,
        "latitude": None,
        "longitude": None,
        "address": None,
        "region": None,
        "descriptions": [""],
        "quantities": [""],
        "unit_prices": [""],
        "discount_percents": [""],
        "sub_offer_ids": [""],
        "inventory_item_ids": [""],
    }
    fields.update(overrides)
    return UUID(web_sales.create_quote_from_form(db_session, **fields))  # type: ignore[arg-type]


def test_new_quote_template_has_required_lead_as_its_only_identity_selector():
    template = Path("templates/admin/sales/quotes/form.html").read_text(
        encoding="utf-8"
    )

    assert 'name="lead_id" required' in template
    assert "Select a Lead" in template
    assert 'name="person_id"' not in template
    assert 'name="subscriber_id"' not in template
    assert 'name="customer_id"' not in template
    assert 'name="account_id"' not in template
    assert 'name="owner_person_id"' not in template
    assert 'name="project_type" required' in template


def test_new_quote_template_retains_responsive_dark_install_and_line_contracts():
    template = Path("templates/admin/sales/quotes/form.html").read_text(
        encoding="utf-8"
    )

    assert "max-w-3xl" in template
    assert "Install Location" in template
    assert "dark:" in template
    assert "sm:grid-cols-2" in template
    assert "Add Item" in template
    assert "removeItem(index)" in template
    assert "if (this.rows.length === 1)" in template
    assert 'loading_label="Submitting..."' in template


def test_new_quote_context_renders_defaults_and_one_empty_line(db_session):
    _actor, lead, _party = _identity(db_session)
    context = web_sales.build_quote_new_context(db_session, lead_id=str(lead.id))

    assert context["quote_form"]["lead_id"] == str(lead.id)
    assert context["quote_form"]["status"] == QuoteStatus.draft.value
    assert context["quote_form"]["currency"] == "NGN"
    assert context["quote_form"]["is_active"] is True
    assert len(context["quote_form"]["items"]) == 1
    assert {item["value"] for item in context["project_types"]} == {
        item.value for item in ProjectType
    }


def test_missing_and_nonexistent_leads_fail_server_side(db_session):
    actor, lead, _party = _identity(db_session)
    with pytest.raises(ValueError, match="Lead is required"):
        _create(db_session, actor, lead, lead_id=None)

    with pytest.raises(quote_authoring.QuoteAuthoringError, match="valid Lead"):
        _create(db_session, actor, lead, lead_id=str(uuid4()))


def test_inactive_and_partyless_leads_fail_closed(db_session, subscriber):
    actor, lead, _party = _identity(db_session)
    lead.is_active = False
    partyless = Lead(
        subscriber_id=subscriber.id,
        title="Legacy account-only opportunity",
        status=LeadStatus.new.value,
        is_active=True,
    )
    db_session.add(partyless)
    db_session.commit()

    with pytest.raises(quote_authoring.QuoteAuthoringError, match="no longer eligible"):
        _create(db_session, actor, lead)
    with pytest.raises(quote_authoring.QuoteAuthoringError, match="linked Person"):
        _create(db_session, actor, partyless)


def test_lead_resolves_authoritative_person_and_authenticated_owner(db_session):
    actor, lead, party = _identity(db_session)
    quote_id = _create(db_session, actor, lead)
    quote = db_session.get(Quote, quote_id)

    assert quote.lead_id == lead.id
    assert quote.person_id == party.id
    assert quote.subscriber_id is None
    assert quote.owner_person_id == actor.id
    assert quote.metadata_["quote_name"] == party.display_name
    assert quote.metadata_["authoring_actor_system_user_id"] == str(actor.id)


@pytest.mark.parametrize("project_type", list(ProjectType))
def test_project_type_is_stored_on_quote_and_projected_in_metadata(
    db_session, project_type
):
    actor, lead, _party = _identity(db_session)
    quote_id = _create(db_session, actor, lead, project_type=project_type.value)

    quote = db_session.get(Quote, quote_id)
    metadata = quote.metadata_
    assert quote.project_type == project_type.value
    assert metadata["project_type"] == project_type.value
    assert metadata["source"] == "admin"


def test_project_type_is_required_server_side(db_session):
    actor, lead, _party = _identity(db_session)

    with pytest.raises(ValueError, match="Project Type is required"):
        _create(db_session, actor, lead, project_type=None)


def test_currency_is_normalized_and_exactly_three_letters(db_session):
    actor, lead, _party = _identity(db_session)
    quote_id = _create(db_session, actor, lead, currency="ngn")
    assert db_session.get(Quote, quote_id).currency == "NGN"

    with pytest.raises(quote_authoring.QuoteAuthoringError, match="three alphabetic"):
        _create(db_session, actor, lead, currency="N1")


def test_empty_rows_are_ignored_and_zero_value_quotes_remain_supported(db_session):
    actor, lead, _party = _identity(db_session)
    quote_id = _create(db_session, actor, lead)
    quote = db_session.get(Quote, quote_id)

    assert quote.line_items == []
    assert quote.subtotal == Decimal("0.00")
    assert quote.tax_total == Decimal("0.00")
    assert quote.total == Decimal("0.00")


def test_custom_discounted_lines_and_manual_tax_are_server_calculated(db_session):
    actor, lead, _party = _identity(db_session)
    quote_id = _create(
        db_session,
        actor,
        lead,
        descriptions=["Custom fiber design"],
        quantities=["1.333"],
        unit_prices=["100.00"],
        discount_percents=["10"],
        sub_offer_ids=[""],
        inventory_item_ids=[""],
        manual_tax_total="5.55",
    )
    quote = db_session.get(Quote, quote_id)
    line = db_session.query(QuoteLineItem).filter_by(quote_id=quote_id).one()

    assert line.amount == Decimal("119.97")
    assert quote.subtotal == Decimal("119.97")
    assert quote.tax_total == Decimal("5.55")
    assert quote.total == Decimal("125.52")


@pytest.mark.parametrize(
    ("quantity", "price", "discount", "message"),
    [
        ("0", "10", "0", "greater than zero"),
        ("1", "-0.01", "0", "cannot be negative"),
        ("1", "10", "100.01", "between 0 and 100"),
    ],
)
def test_invalid_line_numbers_fail_closed(
    db_session, quantity, price, discount, message
):
    actor, lead, _party = _identity(db_session)
    with pytest.raises(quote_authoring.QuoteAuthoringError, match=message):
        _create(
            db_session,
            actor,
            lead,
            descriptions=["Custom line"],
            quantities=[quantity],
            unit_prices=[price],
            discount_percents=[discount],
        )


def test_configured_tax_is_authoritative_and_inactive_tax_fails(db_session):
    actor, lead, _party = _identity(db_session)
    active_tax = TaxRate(name="VAT", rate=Decimal("7.5000"), is_active=True)
    inactive_tax = TaxRate(name="Retired", rate=Decimal("5.0000"), is_active=False)
    db_session.add_all([active_tax, inactive_tax])
    db_session.commit()

    quote_id = _create(
        db_session,
        actor,
        lead,
        tax_rate_id=str(active_tax.id),
        manual_tax_total="999999.00",
        descriptions=["Installation"],
        quantities=["2"],
        unit_prices=["100.00"],
        discount_percents=["0"],
    )
    quote = db_session.get(Quote, quote_id)
    assert quote.subtotal == Decimal("200.00")
    assert quote.tax_total == Decimal("15.00")
    assert quote.total == Decimal("215.00")

    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(quote_authoring.QuoteAuthoringError, match="active configured"):
        _create(db_session, actor, lead, tax_rate_id=str(inactive_tax.id))


def test_inventory_identifier_must_be_active_and_match_description(db_session):
    actor, lead, _party = _identity(db_session)
    item = FieldInventoryItem(name="Drop cable", sku="CBL-1", is_active=True)
    db_session.add(item)
    db_session.commit()

    quote_id = _create(
        db_session,
        actor,
        lead,
        descriptions=["Drop cable — CBL-1"],
        quantities=["1"],
        unit_prices=["0"],
        discount_percents=["0"],
        inventory_item_ids=[str(item.id)],
    )
    line = db_session.query(QuoteLineItem).filter_by(quote_id=quote_id).one()
    assert line.inventory_item_id == item.id

    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(quote_authoring.QuoteAuthoringError, match="no longer matches"):
        _create(
            db_session,
            actor,
            lead,
            descriptions=["Unrelated custom text"],
            quantities=["1"],
            unit_prices=["0"],
            discount_percents=["0"],
            inventory_item_ids=[str(item.id)],
        )


def test_install_location_preserves_selfcare_metadata_contract(db_session, feasible):
    actor, lead, _party = _identity(db_session)
    quote_id = _create(
        db_session,
        actor,
        lead,
        latitude="9.057000",
        longitude="7.495000",
        address="12 Aminu Kano Cres",
        region="Abuja",
    )
    quote = db_session.get(Quote, quote_id)

    assert quote.metadata_["install"] == {
        "latitude": 9.057,
        "longitude": 7.495,
        "address": "12 Aminu Kano Cres",
        "region": "Abuja",
    }
    feasible.assert_called_once_with(db_session, 9.057, 7.495)


def test_sent_is_the_only_non_draft_initial_status(db_session):
    actor, sent_lead, _party = _identity(db_session)
    sent_id = _create(
        db_session,
        actor,
        sent_lead,
        status=QuoteStatus.sent.value,
        descriptions=["Survey"],
        quantities=["1"],
        unit_prices=["0"],
        discount_percents=["0"],
    )
    assert db_session.get(Quote, sent_id).sent_at is not None

    for forbidden in (
        QuoteStatus.accepted,
        QuoteStatus.rejected,
        QuoteStatus.expired,
    ):
        actor, lead, _party = _identity(db_session)
        with pytest.raises(ValueError, match="valid Quote status"):
            _create(db_session, actor, lead, status=forbidden.value)
        assert db_session.get(Lead, lead.id).status == LeadStatus.new.value


def test_exact_submission_replays_without_duplicate_quote(db_session):
    actor, lead, _party = _identity(db_session)
    submission_id = str(uuid4())
    first = _create(db_session, actor, lead, submission_id=submission_id)
    db_session_adapter.release_read_transaction(db_session)
    second = _create(db_session, actor, lead, submission_id=submission_id)

    assert second == first
    assert db_session.query(Quote).filter_by(id=first).count() == 1
