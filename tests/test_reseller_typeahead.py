from app.models.subscriber import Reseller
from app.services import typeahead


def test_reseller_typeahead_searches_email_and_excludes_house(db_session):
    house = Reseller(
        name="House",
        code="HOUSE",
        contact_email="house-match@example.com",
        is_active=True,
        is_house=True,
    )
    partner = Reseller(
        name="North Partner",
        code="NORTH",
        contact_email="reseller-match@example.com",
        is_active=True,
        is_house=False,
    )
    inactive = Reseller(
        name="Inactive Partner",
        contact_email="reseller-match@example.com",
        is_active=False,
        is_house=False,
    )
    db_session.add_all([house, partner, inactive])
    db_session.commit()

    rows = typeahead.resellers(db_session, "match@example.com", 20)

    assert rows == [
        {
            "id": partner.id,
            "label": "North Partner (reseller-match@example.com)",
        }
    ]
