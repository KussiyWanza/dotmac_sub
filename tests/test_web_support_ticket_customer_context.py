from app.models.subscriber import Address, AddressType
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
