from datetime import UTC, datetime, timedelta

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.services import web_catalog_subscriptions
from app.services.subscription_ipv4_projection import (
    ServiceIPv4Source,
    resolve_subscription_service_ipv4,
)


def test_subscription_ipv4_projection_uses_exact_primary_and_ignores_older_rows(
    db_session,
    subscription,
):
    sibling = Subscription(
        subscriber_id=subscription.subscriber_id,
        offer_id=subscription.offer_id,
        status=SubscriptionStatus.canceled,
    )
    legacy_address = IPv4Address(address="172.16.135.21")
    sibling_address = IPv4Address(address="172.16.135.22")
    current_address = IPv4Address(address="172.16.135.233")
    db_session.add_all([sibling, legacy_address, sibling_address, current_address])
    db_session.flush()

    now = datetime.now(UTC)
    legacy_assignment = IPAssignment(
        subscriber_id=subscription.subscriber_id,
        subscription_id=None,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=legacy_address.id,
        is_active=True,
        created_at=now - timedelta(days=30),
    )
    sibling_assignment = IPAssignment(
        subscriber_id=subscription.subscriber_id,
        subscription_id=sibling.id,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=sibling_address.id,
        is_active=True,
        is_primary=True,
        created_at=now - timedelta(days=20),
    )
    current_assignment = IPAssignment(
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=current_address.id,
        is_active=True,
        is_primary=True,
        created_at=now,
    )
    subscription.ipv4_address = "172.16.135.233"
    db_session.add_all([legacy_assignment, sibling_assignment, current_assignment])
    db_session.commit()

    selection = resolve_subscription_service_ipv4(
        db_session,
        subscription_id=subscription.id,
    )
    form_data = web_catalog_subscriptions.edit_form_data(db_session, subscription)
    detail = web_catalog_subscriptions.subscription_detail_context(
        db_session,
        subscription,
    )

    assert selection.address == "172.16.135.233"
    assert selection.assignment_id == current_assignment.id
    assert selection.source is ServiceIPv4Source.exact_primary_assignment
    assert form_data["ipv4_addresses"] == ["172.16.135.233"]
    assert "172.16.135.21" not in form_data["ipv4_addresses"]
    assert "172.16.135.22" not in form_data["ipv4_addresses"]
    assert detail["service_ipv4"] == selection

    db_session.refresh(legacy_assignment)
    db_session.refresh(sibling_assignment)
    assert legacy_assignment.is_active is True
    assert sibling_assignment.is_active is True


def test_subscription_ipv4_projection_uses_served_copy_only_without_exact_ipam(
    db_session,
    subscription,
):
    subscription.ipv4_address = "172.16.140.10"
    db_session.commit()

    selection = resolve_subscription_service_ipv4(
        db_session,
        subscription_id=subscription.id,
    )

    assert selection.address == "172.16.140.10"
    assert selection.source is ServiceIPv4Source.served_projection
    assert selection.is_exact_assignment is False


def test_subscription_ipv4_projection_refuses_unmarked_exact_ambiguity(
    db_session,
    subscription,
):
    first = IPv4Address(address="172.16.141.10")
    second = IPv4Address(address="172.16.141.11")
    db_session.add_all([first, second])
    db_session.flush()
    db_session.add_all(
        [
            IPAssignment(
                subscriber_id=subscription.subscriber_id,
                subscription_id=subscription.id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=first.id,
                is_active=True,
            ),
            IPAssignment(
                subscriber_id=subscription.subscriber_id,
                subscription_id=subscription.id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=second.id,
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    selection = resolve_subscription_service_ipv4(
        db_session,
        subscription_id=subscription.id,
    )

    assert selection.address is None
    assert selection.source is ServiceIPv4Source.ambiguous_exact_assignments
