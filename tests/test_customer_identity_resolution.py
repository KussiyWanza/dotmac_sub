from __future__ import annotations

from uuid import uuid4

from app.models.comms import CustomerNotificationEvent
from app.models.communication_log import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationLog,
)
from app.models.customer_identity import CustomerIdentityIndex
from app.models.subscriber import (
    ChannelType,
    Subscriber,
    SubscriberChannel,
    SubscriberContact,
)
from app.services.customer_identity_normalization import (
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.customer_identity_resolution import (
    MATCH_CONFIDENCE_HIGH,
    MATCH_CONFIDENCE_LOW,
    MATCH_CONFIDENCE_MEDIUM,
    MATCH_VIA_HISTORICAL_PARTICIPANT,
    MATCH_VIA_SUBSCRIBER,
    MATCH_VIA_SUBSCRIBER_CHANNEL,
    MATCH_VIA_SUBSCRIBER_CONTACT,
    CustomerIdentityResolution,
    identity_resolution_allows_sensitive_automation,
    identity_resolution_requires_manual_review,
    rebuild_identity_index_for_subscriber,
    resolve_customer_identity,
)


def _subscriber(**overrides) -> Subscriber:
    return Subscriber(
        first_name=overrides.pop("first_name", "Test"),
        last_name=overrides.pop("last_name", "User"),
        email=overrides.pop("email", f"{uuid4().hex}@example.com"),
        phone=overrides.pop("phone", None),
        **overrides,
    )


def test_normalization_behaviour():
    assert (
        normalize_email_identifier("  Mixed.Case@Example.COM  ")
        == "mixed.case@example.com"
    )
    assert normalize_phone_identifier("(0801) 234-5678") == "+2348012345678"
    assert normalize_phone_identifier("whatsapp: 0808 111 2222") == "+2348081112222"
    assert normalize_phone_identifier("+1 (415) 555-0100") == "+14155550100"
    assert normalize_phone_identifier("+2348012345678") == "+2348012345678"
    assert normalize_phone_identifier("08012345678") == "+2348012345678"
    assert normalize_phone_identifier("2348012345678") == "+2348012345678"


def test_resolve_exact_email_prefers_direct_subscriber_over_same_subscriber_contact(
    db_session,
):
    subscriber = _subscriber(email="direct@example.com")
    db_session.add(subscriber)
    db_session.flush()
    db_session.add(
        SubscriberContact(
            subscriber_id=subscriber.id,
            email="direct@example.com",
            contact_type="general",
        )
    )
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, subscriber.id)

    result = resolve_customer_identity(db_session, "  DIRECT@example.com  ")

    assert result.matched is True
    assert result.ambiguous is False
    assert result.subscriber_id == subscriber.id
    assert result.matched_via == MATCH_VIA_SUBSCRIBER
    assert result.matched_field == "email"
    assert result.match_confidence == MATCH_CONFIDENCE_HIGH


def test_resolve_phone_and_whatsapp_matches_contact(db_session):
    subscriber = _subscriber()
    db_session.add(subscriber)
    db_session.flush()
    contact = SubscriberContact(
        subscriber_id=subscriber.id,
        phone="(0801) 234-5678",
        whatsapp="0808 111 2222",
        contact_type="general",
    )
    db_session.add(contact)
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, subscriber.id)

    phone_result = resolve_customer_identity(
        db_session, "+2348012345678", channel_hint="phone"
    )
    whatsapp_result = resolve_customer_identity(
        db_session,
        "whatsapp: 0808 111 2222",
        channel_hint="whatsapp",
    )

    assert phone_result.matched is True
    assert phone_result.subscriber_id == subscriber.id
    assert phone_result.matched_via == MATCH_VIA_SUBSCRIBER_CONTACT
    assert phone_result.matched_field == "phone"
    assert phone_result.matched_contact_id == contact.id
    assert phone_result.match_confidence == MATCH_CONFIDENCE_MEDIUM

    assert whatsapp_result.matched is True
    assert whatsapp_result.subscriber_id == subscriber.id
    assert whatsapp_result.matched_via == MATCH_VIA_SUBSCRIBER_CONTACT
    assert whatsapp_result.matched_field == "whatsapp"
    assert whatsapp_result.matched_contact_id == contact.id
    assert whatsapp_result.match_confidence == MATCH_CONFIDENCE_MEDIUM


def test_sensitive_automation_threshold_can_require_high_confidence(
    monkeypatch,
    db_session,
):
    monkeypatch.setattr(
        "app.services.customer_identity_resolution.resolve_value",
        lambda db, domain, key: "HIGH",
    )
    resolution = CustomerIdentityResolution(
        raw_identifier="08012345678",
        normalized_identifier="+2348012345678",
        identity_type="phone",
        inbound_channel="sms",
        matched=True,
        ambiguous=False,
        match_confidence=MATCH_CONFIDENCE_MEDIUM,
    )

    assert identity_resolution_allows_sensitive_automation(resolution) is True
    assert (
        identity_resolution_allows_sensitive_automation(resolution, db_session) is False
    )


def test_resolve_matches_verified_subscriber_channel_with_high_confidence(db_session):
    subscriber = _subscriber()
    db_session.add(subscriber)
    db_session.flush()
    channel = SubscriberChannel(
        subscriber_id=subscriber.id,
        channel_type=ChannelType.sms,
        address="08070000000",
        is_primary=False,
        is_verified=True,
    )
    db_session.add(channel)
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, subscriber.id)

    result = resolve_customer_identity(db_session, "+2348070000000", channel_hint="sms")

    assert result.matched is True
    assert result.subscriber_id == subscriber.id
    assert result.matched_via == MATCH_VIA_SUBSCRIBER_CHANNEL
    assert result.matched_field == "sms"
    assert result.matched_channel_id == channel.id
    assert result.match_confidence == MATCH_CONFIDENCE_HIGH


def test_historical_match_is_last_priority_after_direct_match(db_session):
    authoritative = _subscriber(phone="08099999999")
    historical_only = _subscriber()
    db_session.add_all([authoritative, historical_only])
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, authoritative.id)
    db_session.add(
        CommunicationLog(
            subscriber_id=historical_only.id,
            channel=CommunicationChannel.sms,
            sender="+2348099999999",
            recipient="+2348000000000",
            direction=CommunicationDirection.inbound,
            body="Old conversation",
        )
    )
    db_session.commit()

    result = resolve_customer_identity(db_session, "08099999999", channel_hint="sms")

    assert result.matched is True
    assert result.subscriber_id == authoritative.id
    assert result.matched_via == MATCH_VIA_SUBSCRIBER
    assert result.match_confidence == MATCH_CONFIDENCE_HIGH


def test_historical_match_is_last_priority_after_contact_match(db_session):
    contact_owner = _subscriber()
    historical_only = _subscriber()
    db_session.add_all([contact_owner, historical_only])
    db_session.flush()
    db_session.add(
        SubscriberContact(
            subscriber_id=contact_owner.id,
            phone="08088888888",
            contact_type="general",
        )
    )
    db_session.add(
        CustomerNotificationEvent(
            entity_type="support_ticket",
            entity_id=historical_only.id,
            subscriber_id=historical_only.id,
            channel="sms",
            recipient="+2348088888888",
            message="Previous notification",
        )
    )
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, contact_owner.id)
    db_session.commit()

    result = resolve_customer_identity(db_session, "08088888888", channel_hint="sms")

    assert result.matched is True
    assert result.subscriber_id == contact_owner.id
    assert result.matched_via == MATCH_VIA_SUBSCRIBER_CONTACT
    assert result.match_confidence == MATCH_CONFIDENCE_MEDIUM


def test_resolve_uses_historical_participant_linkage_with_low_confidence_when_no_direct_identity(
    db_session,
):
    subscriber = _subscriber()
    db_session.add(subscriber)
    db_session.flush()
    db_session.add(
        CustomerNotificationEvent(
            entity_type="support_ticket",
            entity_id=subscriber.id,
            subscriber_id=subscriber.id,
            channel="sms",
            recipient="+2348099999999",
            message="Previous notification",
        )
    )
    db_session.commit()

    result = resolve_customer_identity(db_session, "08099999999", channel_hint="sms")

    assert result.matched is True
    assert result.subscriber_id == subscriber.id
    assert result.matched_via == MATCH_VIA_HISTORICAL_PARTICIPANT
    assert result.match_confidence == MATCH_CONFIDENCE_LOW
    assert identity_resolution_requires_manual_review(result) is True


def test_resolve_marks_duplicate_identifier_ambiguous_without_logging_pii(
    db_session, caplog
):
    left = _subscriber()
    right = _subscriber()
    db_session.add_all([left, right])
    db_session.flush()
    db_session.add_all(
        [
            SubscriberContact(
                subscriber_id=left.id,
                phone="08012345678",
                contact_type="general",
            ),
            SubscriberContact(
                subscriber_id=right.id,
                phone="+2348012345678",
                contact_type="general",
            ),
        ]
    )
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, left.id)
    rebuild_identity_index_for_subscriber(db_session, right.id)

    with caplog.at_level("WARNING"):
        result = resolve_customer_identity(
            db_session, "(0801) 234-5678", channel_hint="phone"
        )

    assert result.matched is False
    assert result.ambiguous is True
    assert result.subscriber_id is None
    assert result.ambiguity_count == 2
    assert identity_resolution_requires_manual_review(result) is True
    assert "customer_identity_ambiguous_identifier" in caplog.text
    assert "08012345678" not in caplog.text
    assert "+2348012345678" not in caplog.text


def test_rebuild_identity_index_cleans_stale_contact_identities(db_session):
    subscriber = _subscriber()
    db_session.add(subscriber)
    db_session.flush()
    contact = SubscriberContact(
        subscriber_id=subscriber.id,
        phone="08011111111",
        contact_type="general",
    )
    db_session.add(contact)
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, subscriber.id)

    old_normalized = normalize_phone_identifier("08011111111")
    assert (
        db_session.query(CustomerIdentityIndex)
        .filter(CustomerIdentityIndex.normalized_value == old_normalized)
        .count()
        == 1
    )

    contact.phone = "08022222222"
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, subscriber.id)

    new_normalized = normalize_phone_identifier("08022222222")
    assert (
        db_session.query(CustomerIdentityIndex)
        .filter(CustomerIdentityIndex.normalized_value == old_normalized)
        .count()
        == 0
    )
    assert (
        db_session.query(CustomerIdentityIndex)
        .filter(CustomerIdentityIndex.normalized_value == new_normalized)
        .count()
        == 1
    )

    db_session.delete(contact)
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, subscriber.id)

    assert (
        db_session.query(CustomerIdentityIndex)
        .filter(CustomerIdentityIndex.normalized_value == new_normalized)
        .count()
        == 0
    )


def test_resolve_logs_match_details_with_confidence(db_session, caplog):
    subscriber = _subscriber()
    db_session.add(subscriber)
    db_session.flush()
    contact = SubscriberContact(
        subscriber_id=subscriber.id,
        email="linked@example.com",
        contact_type="general",
    )
    db_session.add(contact)
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, subscriber.id)

    with caplog.at_level("INFO"):
        result = resolve_customer_identity(
            db_session, "linked@example.com", channel_hint="email"
        )

    assert result.matched is True
    assert "customer_identity_resolved" in caplog.text
    assert "matched_via=subscriber_contact" in caplog.text
    assert "confidence=MEDIUM" in caplog.text
    assert "linked@example.com" not in caplog.text
    assert str(subscriber.id) not in caplog.text
    assert str(contact.id) not in caplog.text
