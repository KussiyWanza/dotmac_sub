from __future__ import annotations

from app.models.party import Party, PartyContactPoint, PartyType
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import team_inbox_projection


def _legacy_subscriber(
    db_session,
    *,
    name: str,
    phone: str,
    email: str,
    active: bool = True,
) -> Subscriber:
    subscriber = Subscriber(
        first_name=name.split()[0],
        last_name=" ".join(name.split()[1:]) or "Customer",
        display_name=name,
        email=email,
        phone=phone,
        status=(SubscriberStatus.active if active else SubscriberStatus.disabled),
        is_active=active,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def test_whatsapp_contact_search_includes_unbound_active_customer_by_name(db_session):
    subscriber = _legacy_subscriber(
        db_session,
        name="Shaheeda Musti",
        phone="09037423041",
        email="shaheeda@example.test",
    )

    result = team_inbox_projection.list_whatsapp_contacts(
        db_session, search="Shaheeda", limit=20
    )

    assert len(result) == 1
    assert result[0].id == f"subscriber:{subscriber.id}"
    assert result[0].party_id is None
    assert result[0].subscriber_id == subscriber.id
    assert result[0].whatsapp_address == "+2349037423041"


def test_whatsapp_contact_search_normalizes_international_number(db_session):
    subscriber = _legacy_subscriber(
        db_session,
        name="Number Search",
        phone="0903 742-3041",
        email="number-search@example.test",
    )

    result = team_inbox_projection.list_whatsapp_contacts(
        db_session, search="+2349037423041", limit=20
    )

    assert tuple(item.subscriber_id for item in result) == (subscriber.id,)


def test_whatsapp_contact_search_prefers_canonical_party_reachability(db_session):
    party = Party(
        party_type=PartyType.person.value,
        display_name="Canonical Customer",
    )
    db_session.add(party)
    db_session.flush()
    db_session.add(
        PartyContactPoint(
            party_id=party.id,
            channel_type="whatsapp",
            normalized_value="+2349037423041",
            display_value="09037423041",
            is_primary=True,
        )
    )
    _legacy_subscriber(
        db_session,
        name="Legacy Duplicate",
        phone="09037423041",
        email="legacy-duplicate@example.test",
    )

    result = team_inbox_projection.list_whatsapp_contacts(
        db_session, search="09037423041", limit=20
    )

    assert len(result) == 1
    assert result[0].party_id == party.id
    assert result[0].subscriber_id is None


def test_whatsapp_contact_search_omits_ambiguous_legacy_number(db_session):
    _legacy_subscriber(
        db_session,
        name="First Shared",
        phone="09037423041",
        email="first-shared@example.test",
    )
    _legacy_subscriber(
        db_session,
        name="Second Shared",
        phone="09037423041",
        email="second-shared@example.test",
    )

    result = team_inbox_projection.list_whatsapp_contacts(
        db_session, search="09037423041", limit=20
    )

    assert result == ()


def test_whatsapp_contact_search_excludes_inactive_legacy_customer(db_session):
    _legacy_subscriber(
        db_session,
        name="Inactive Customer",
        phone="09037423041",
        email="inactive@example.test",
        active=False,
    )

    result = team_inbox_projection.list_whatsapp_contacts(
        db_session, search="Inactive", limit=20
    )

    assert result == ()
