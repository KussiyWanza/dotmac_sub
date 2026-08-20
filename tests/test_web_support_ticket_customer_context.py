from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.subscriber import Address, AddressType, SubscriberCategory
from app.services import web_support_tickets


def test_ticket_customer_context_includes_contact_and_service_address(
    db_session, subscriber
):
    subscriber.first_name = "Adaeze"
    subscriber.last_name = "Nwosu"
    subscriber.email = "adaeze@example.com"
    subscriber.phone = "+2348012345678"
    db_session.add(
        Address(
            subscriber_id=subscriber.id,
            address_type=AddressType.service,
            is_primary=True,
            address_line1="31 Customer Road",
            city="Abuja",
        )
    )
    db_session.commit()

    context = web_support_tickets._ticket_customer_context(db_session, subscriber.id)

    assert context is not None
    assert context.name == "Adaeze Nwosu"
    assert context.email == "adaeze@example.com"
    assert context.phone == "+2348012345678"
    assert context.service_address == "31 Customer Road, Abuja"


@pytest.mark.parametrize(
    ("category", "customer_type"),
    (
        (SubscriberCategory.residential, "person"),
        (SubscriberCategory.business, "business"),
    ),
)
def test_ticket_customer_context_owns_customer_detail_route(
    db_session, subscriber, category, customer_type
):
    subscriber.category = category
    db_session.commit()

    context = web_support_tickets._ticket_customer_context(db_session, subscriber.id)

    assert context is not None
    assert context.detail_url == f"/admin/customers/{customer_type}/{subscriber.id}"


@pytest.mark.parametrize(
    "field_name",
    ("subscriber_id", "customer_account_id", "customer_person_id"),
)
def test_ticket_customer_id_resolves_each_supported_customer_link(field_name):
    customer_id = uuid4()
    values = {
        "subscriber_id": None,
        "customer_account_id": None,
        "customer_person_id": None,
    }
    values[field_name] = customer_id

    assert (
        web_support_tickets._ticket_customer_id(SimpleNamespace(**values))
        == customer_id
    )
