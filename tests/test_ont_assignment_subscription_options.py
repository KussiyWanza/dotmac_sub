"""Focused coverage for subscriber-scoped ONT subscription choices."""

from pathlib import Path

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber
from app.services import web_network_ont_assignments
from app.web.templates import templates


def test_assignment_options_show_all_statuses_for_exact_subscriber(
    db_session, subscriber, subscription, catalog_offer
):
    subscription.status = SubscriptionStatus.active
    subscription.login = "active-service"
    inactive = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.suspended,
        login="suspended-service",
    )
    other_subscriber = Subscriber(
        first_name="Other",
        last_name="Customer",
        email="other-assignment-options@example.com",
    )
    db_session.add(other_subscriber)
    db_session.flush()
    db_session.add_all(
        [
            inactive,
            Subscription(
                subscriber_id=other_subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.active,
                login="other-customer-service",
            ),
        ]
    )
    db_session.commit()

    options = web_network_ont_assignments.assignment_subscription_options(
        db_session, subscriber_id=subscriber.id
    )

    assert [option.id for option in options] == [subscription.id, inactive.id]
    assert options[0].status_indicator == "Active"
    assert options[1].status_indicator == "Inactive (Suspended)"
    assert {option.service_login for option in options} == {
        "active-service",
        "suspended-service",
    }


def test_assignment_subscription_partial_labels_status_and_preserves_selection(
    db_session, subscriber, subscription, catalog_offer
):
    subscription.status = SubscriptionStatus.active
    subscription.login = "customer-service"
    db_session.commit()
    options = web_network_ont_assignments.assignment_subscription_options(
        db_session, subscriber_id=subscriber.id
    )

    rendered = templates.env.get_template(
        "admin/network/onts/_assignment_subscription_options.html"
    ).render(
        assignment_account_id=str(subscriber.id),
        assignment_subscription_options=options,
        selected_assignment_subscription_id=str(subscription.id),
    )

    assert "Standard Internet · customer-service — Active" in rendered
    assert f'value="{subscription.id}" selected' in rendered
    assert "All subscriptions for this subscriber are shown." in rendered


def test_assign_subscriber_modal_loads_options_from_selected_subscriber():
    source = Path(
        "templates/admin/network/onts/_assign_subscriber_modal.html"
    ).read_text(encoding="utf-8")

    assert 'id="modal_account_id"' in source
    assert 'hx-get="/admin/network/ont-assignment/subscriptions"' in source
    assert 'hx-trigger="change"' in source
    assert 'hx-target="#modal_subscription_field"' in source
    assert 'data-typeahead-url="/api/v1/search/subscriptions"' not in source
