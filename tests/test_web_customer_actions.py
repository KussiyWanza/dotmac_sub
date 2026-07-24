from decimal import Decimal

import pytest

from app.models.auth import UserCredential
from app.models.billing import TaxRate
from app.models.catalog import (
    AccessCredential,
    AccessType,
    BillingMode,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.collections import DunningCase, DunningCaseStatus
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Subscriber, SubscriberCategory, SubscriberStatus
from app.services import web_customer_actions as actions
from app.services.account_lifecycle import has_active_lock
from app.services.radius import (
    ConnectivityProjectionDisposition,
    SubscriptionConnectivityOutcome,
)


def test_update_person_customer_persists_billing_overrides(db_session, subscriber):
    tax_rate = TaxRate(name="VAT", rate=Decimal("0.075"))
    db_session.add(tax_rate)
    db_session.commit()

    actions.update_person_customer(
        db=db_session,
        customer_id=str(subscriber.id),
        first_name="Test",
        last_name="User",
        display_name=None,
        avatar_url=None,
        email=subscriber.email,
        email_verified="false",
        phone=None,
        nin=None,
        date_of_birth=None,
        gender="unknown",
        preferred_contact_method=None,
        locale=None,
        timezone_value=None,
        address_line1=None,
        address_line2=None,
        city=None,
        region=None,
        postal_code=None,
        country_code=None,
        status="active",
        is_active="true",
        marketing_opt_in="false",
        notes=None,
        account_start_date=None,
        billing_enabled_override="false",
        billing_day="8",
        payment_due_days="5",
        grace_period_days="2",
        min_balance="125.50",
        captive_redirect_enabled="false",
        tax_rate_id=str(tax_rate.id),
        withholding_tax_enabled=None,
        payment_method="transfer",
        metadata_json=None,
    )

    updated = db_session.get(Subscriber, subscriber.id)
    assert updated is not None
    assert updated.billing_enabled is False
    assert updated.billing_day == 8
    assert updated.payment_due_days == 5
    assert updated.grace_period_days == 2
    assert updated.min_balance == Decimal("125.50")
    assert updated.tax_rate_id == tax_rate.id
    assert updated.payment_method == "transfer"


def test_update_business_customer_applies_billing_overrides_to_linked_subscribers(
    db_session,
):
    tax_rate = TaxRate(name="Reduced VAT", rate=Decimal("0.050"))
    organization = Subscriber(
        first_name="Acme",
        last_name="Business",
        email="billing@acme.example.com",
        company_name="Acme Corp",
    )
    organization.category = SubscriberCategory.business
    db_session.add_all([tax_rate, organization])
    db_session.flush()
    db_session.commit()

    actions.update_business_customer(
        db=db_session,
        customer_id=str(organization.id),
        name="Acme Corp",
        legal_name=None,
        tax_id=None,
        domain=None,
        website=None,
        org_notes=None,
        org_account_start_date=None,
        billing_enabled_override="true",
        billing_day="12",
        payment_due_days="9",
        grace_period_days="4",
        min_balance="75.00",
        captive_redirect_enabled="false",
        tax_rate_id=str(tax_rate.id),
        withholding_tax_enabled=None,
        payment_method="cash",
    )

    refreshed = db_session.get(Subscriber, organization.id)
    assert refreshed is not None
    assert refreshed.company_name == "Acme Corp"
    assert refreshed.billing_enabled is True
    assert refreshed.billing_day == 12
    assert refreshed.payment_due_days == 9
    assert refreshed.grace_period_days == 4
    assert refreshed.min_balance == Decimal("75.00")
    assert refreshed.tax_rate_id == tax_rate.id
    assert refreshed.payment_method == "cash"


def test_repair_customer_access_state_restores_stale_active_projection(
    db_session, monkeypatch, subscriber, catalog_offer
):
    subscriber.status = SubscriberStatus.blocked
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        access_state="suspended",
        login="repair-active-1",
    )
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="repair-active-1",
        is_active=True,
    )
    db_session.add_all([subscription, credential])
    db_session.commit()

    reconcile_calls = []
    monkeypatch.setattr(
        actions.radius_service,
        "unblock_external_radius_credentials",
        lambda db, account_id: 1,
    )
    monkeypatch.setattr(
        actions.radius_service,
        "reconcile_subscription_connectivity",
        lambda db, subscription_id: (
            reconcile_calls.append(subscription_id)
            or SubscriptionConnectivityOutcome(
                subscription_id=str(subscription_id),
                disposition=ConnectivityProjectionDisposition.projected,
                radius_users_changed=1,
                external_credentials_synced=1,
                requested_logins=1,
                projected_logins=1,
                projection_targets=1,
            )
        ),
    )

    result = actions.repair_customer_access_state(db_session, str(subscriber.id))

    db_session.refresh(subscriber)
    assert subscriber.status == SubscriberStatus.active
    assert result["status_before"] == "blocked"
    assert result["status_after"] == "active"
    assert result["reject_rows_removed"] == 1
    assert subscription.access_state == "active"
    assert reconcile_calls == [str(subscription.id)]


def test_repair_customer_access_state_skips_active_dunning_case(
    db_session, subscriber, catalog_offer
):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        login="repair-dunning-1",
    )
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="repair-dunning-1",
        is_active=True,
    )
    case = DunningCase(account_id=subscriber.id, status=DunningCaseStatus.open)
    db_session.add_all([subscription, credential, case])
    db_session.commit()

    with pytest.raises(ValueError, match="active dunning case"):
        actions.repair_customer_access_state(db_session, str(subscriber.id))


def test_repair_customer_access_state_skips_active_enforcement_lock(
    db_session, subscriber, catalog_offer
):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        login="repair-lock-1",
    )
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="repair-lock-1",
        is_active=True,
    )
    db_session.add_all([subscription, credential])
    db_session.flush()
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscriber.id,
        reason=EnforcementReason.overdue,
        source="test",
        is_active=True,
    )
    db_session.add(lock)
    db_session.commit()

    with pytest.raises(ValueError, match="active suspension lock"):
        actions.repair_customer_access_state(db_session, str(subscriber.id))


def test_deactivate_business_customer_suspends_member_subscriptions(db_session):
    offer = CatalogOffer(
        name="Business Fiber",
        service_type=ServiceType.business,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()

    subscriber = Subscriber(
        first_name="Acme",
        last_name="Business",
        email="org.member@example.com",
        company_name="Acme Corp",
        status=SubscriberStatus.active,
    )
    subscriber.category = SubscriberCategory.business
    db_session.add(subscriber)
    db_session.flush()

    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(subscription)
    db_session.add(
        UserCredential(
            subscriber_id=subscriber.id,
            username="org.member@example.com",
            password_hash="hash",
        )
    )
    db_session.commit()

    actions.deactivate_business_customer(db_session, str(subscriber.id))
    db_session.refresh(subscriber)
    db_session.refresh(subscription)

    assert subscriber.is_active is False
    assert subscriber.status == SubscriberStatus.suspended
    assert subscription.status == SubscriptionStatus.suspended
    assert has_active_lock(db_session, str(subscription.id), EnforcementReason.admin)


def test_bulk_activate_business_restores_admin_locked_member(db_session):
    offer = CatalogOffer(
        name="Business Fiber",
        service_type=ServiceType.business,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()

    subscriber = Subscriber(
        first_name="Beta",
        last_name="Business",
        email="beta.member@example.com",
        company_name="Beta Corp",
        status=SubscriberStatus.active,
        is_active=True,
    )
    subscriber.category = SubscriberCategory.business
    db_session.add(subscriber)
    db_session.flush()

    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(subscription)
    db_session.commit()

    actions.deactivate_business_customer(db_session, str(subscriber.id))
    result = actions.bulk_update_customer_status(
        db_session, [{"id": str(subscriber.id), "type": "business"}], True
    )

    db_session.refresh(subscriber)
    db_session.refresh(subscription)

    assert result["errors"] == []
    assert subscriber.is_active is True
    assert subscriber.status == SubscriberStatus.active
    assert subscription.status == SubscriptionStatus.active
    assert not has_active_lock(
        db_session, str(subscription.id), EnforcementReason.admin
    )


def _person_form_data(**overrides):
    data = {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"}
    data.update(overrides)
    return data


_EMPTY_CONTACTS = {
    "first_name": [],
    "last_name": [],
    "title": [],
    "role": [],
    "email": [],
    "phone": [],
    "is_primary": [],
}


def test_create_person_rejects_whitespace_only_names(db_session):
    with pytest.raises(ValueError, match="First name is required"):
        actions.create_customer_from_form(
            db=db_session,
            customer_type="person",
            form_data=_person_form_data(
                first_name="   ", email="ws-create@example.com"
            ),
            contact_columns=_EMPTY_CONTACTS,
        )


def test_create_person_rejects_overlong_name(db_session):
    with pytest.raises(ValueError, match="80 characters or fewer"):
        actions.create_customer_from_form(
            db=db_session,
            customer_type="person",
            form_data=_person_form_data(
                first_name="A" * 81, email="long-create@example.com"
            ),
            contact_columns=_EMPTY_CONTACTS,
        )


def test_create_person_trims_surrounding_whitespace(db_session):
    _, created_id = actions.create_customer_from_form(
        db=db_session,
        customer_type="person",
        form_data=_person_form_data(
            first_name="  John  ",
            last_name="  Smith ",
            email="trim-create@example.com",
        ),
        contact_columns=_EMPTY_CONTACTS,
    )
    created = db_session.get(Subscriber, created_id)
    assert created.first_name == "John"
    assert created.last_name == "Smith"


def test_create_business_rejects_blank_name(db_session):
    with pytest.raises(ValueError, match="Business name is required"):
        actions.create_customer_from_form(
            db=db_session,
            customer_type="business",
            form_data={"name": "   "},
            contact_columns=_EMPTY_CONTACTS,
        )


def test_update_person_rejects_blank_name(db_session, subscriber):
    with pytest.raises(ValueError, match="Last name is required"):
        actions.update_person_customer(
            db=db_session,
            customer_id=str(subscriber.id),
            first_name="Test",
            last_name="   ",
            display_name=None,
            avatar_url=None,
            email=subscriber.email,
            email_verified="false",
            phone=None,
            nin=None,
            date_of_birth=None,
            gender="unknown",
            preferred_contact_method=None,
            locale=None,
            timezone_value=None,
            address_line1=None,
            address_line2=None,
            city=None,
            region=None,
            postal_code=None,
            country_code=None,
            status="active",
            is_active="true",
            marketing_opt_in="false",
            notes=None,
            account_start_date=None,
            billing_enabled_override="false",
            billing_day=None,
            payment_due_days=None,
            grace_period_days=None,
            min_balance=None,
            captive_redirect_enabled="false",
            tax_rate_id=None,
            withholding_tax_enabled=None,
            payment_method=None,
            metadata_json=None,
        )


def _update_person(db, subscriber, **overrides):
    kwargs = dict(
        first_name="Test",
        last_name="User",
        display_name=None,
        avatar_url=None,
        email=subscriber.email,
        email_verified="false",
        phone=None,
        nin=None,
        date_of_birth=None,
        gender="unknown",
        preferred_contact_method=None,
        locale=None,
        timezone_value=None,
        address_line1=None,
        address_line2=None,
        city=None,
        region=None,
        postal_code=None,
        country_code=None,
        status="active",
        is_active="true",
        marketing_opt_in="false",
        notes=None,
        account_start_date=None,
        billing_enabled_override="false",
        billing_day=None,
        payment_due_days=None,
        grace_period_days=None,
        min_balance=None,
        captive_redirect_enabled="false",
        tax_rate_id=None,
        withholding_tax_enabled=None,
        payment_method=None,
        metadata_json=None,
    )
    kwargs.update(overrides)
    return actions.update_person_customer(
        db=db, customer_id=str(subscriber.id), **kwargs
    )


def test_update_person_allows_shared_email(db_session, subscriber):
    # Email is non-unique contact info: editing a customer to use an address
    # another customer already has is allowed (customers under one reseller
    # often share a contact email), not a collision error.
    other = Subscriber(
        first_name="Other", last_name="Person", email="taken@example.com"
    )
    db_session.add(other)
    db_session.commit()

    _update_person(db_session, subscriber, email="taken@example.com")
    refreshed = db_session.get(Subscriber, subscriber.id)
    assert refreshed.email == "taken@example.com"


def test_update_person_allows_keeping_own_email(db_session, subscriber):
    # Re-saving the same email (self) must not be treated as a collision.
    _update_person(db_session, subscriber, email=subscriber.email, first_name="Renamed")
    refreshed = db_session.get(Subscriber, subscriber.id)
    assert refreshed.first_name == "Renamed"


def test_update_person_preserves_existing_customer_category(db_session, subscriber):
    subscriber.category = SubscriberCategory.government
    db_session.commit()

    _update_person(
        db_session,
        subscriber,
        first_name="Renamed",
        metadata_json={"subscriber_category": "business", "source": "form"},
    )

    refreshed = db_session.get(Subscriber, subscriber.id)
    assert refreshed.category == SubscriberCategory.government
    assert refreshed.company_name is None
    assert refreshed.metadata_["source"] == "form"


def test_convert_person_to_business_customer_changes_account_type(
    db_session, subscriber
):
    actions.convert_person_to_business_customer(
        db=db_session,
        customer_id=str(subscriber.id),
        company_name="  Acme Corporate  ",
        legal_name=" Acme Corporate Limited ",
        tax_id=" TIN-123 ",
        domain=" acme.example ",
        website=" https://acme.example ",
    )

    refreshed = db_session.get(Subscriber, subscriber.id)
    assert refreshed.category == SubscriberCategory.business
    assert refreshed.company_name == "Acme Corporate"
    assert refreshed.display_name == "Acme Corporate"
    assert refreshed.legal_name == "Acme Corporate Limited"
    assert refreshed.tax_id == "TIN-123"
    assert refreshed.domain == "acme.example"
    assert refreshed.website == "https://acme.example"
    assert refreshed.first_name == subscriber.first_name
    assert refreshed.last_name == subscriber.last_name


def test_convert_person_to_business_customer_rejects_existing_business(
    db_session, subscriber
):
    subscriber.category = SubscriberCategory.business
    db_session.commit()

    with pytest.raises(ValueError, match="already a business customer"):
        actions.convert_person_to_business_customer(
            db=db_session,
            customer_id=str(subscriber.id),
            company_name="Acme Corporate",
            legal_name=None,
            tax_id=None,
            domain=None,
            website=None,
        )


def test_update_person_normalizes_nin(db_session, subscriber):
    _update_person(db_session, subscriber, nin="123-456-78901")
    refreshed = db_session.get(Subscriber, subscriber.id)
    assert refreshed.nin == "12345678901"


def test_update_person_keeps_verified_nin_locked(db_session, subscriber):
    subscriber.nin = "12345678901"
    subscriber.metadata_ = {"nin_verified": True}
    db_session.commit()

    _update_person(db_session, subscriber, nin="99999999999")
    refreshed = db_session.get(Subscriber, subscriber.id)
    assert refreshed.nin == "12345678901"


def test_update_person_rejects_invalid_nin(db_session, subscriber):
    with pytest.raises(ValueError, match="11 digits"):
        _update_person(db_session, subscriber, nin="12345")


def test_create_customer_contact_bad_account_id_raises_value_error(db_session):
    # The create_contact web handler maps ValueError -> 400 (not a 500 page).
    # A non-UUID account_id must raise ValueError so that mapping engages.
    with pytest.raises(ValueError):
        actions.create_customer_contact(
            db=db_session,
            account_id="not-a-uuid",
            first_name="Jo",
            last_name="Lead",
            role="primary",
            title=None,
            email=None,
            phone=None,
            is_primary="false",
        )
