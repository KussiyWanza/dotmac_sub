"""Tests for usage service."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.catalog import AccessCredential
from app.models.usage import UsageSource
from app.schemas.usage import (
    QuotaBucketCreate,
    QuotaBucketUpdate,
    RadiusAccountingSessionCreate,
    RadiusAccountingSessionUpdate,
    UsageRecordCreate,
)
from app.services import usage as usage_service


def test_create_quota_bucket(db_session, subscription):
    """Test creating a quota bucket."""
    bucket = usage_service.quota_buckets.create(
        db_session,
        QuotaBucketCreate(
            subscription_id=subscription.id,
            included_gb=Decimal("100"),
            period_start=datetime.now(UTC),
            period_end=datetime.now(UTC) + timedelta(days=30),
        ),
    )
    assert bucket.subscription_id == subscription.id
    assert bucket.included_gb == Decimal("100")


def test_list_quota_buckets_by_subscription(db_session, subscription):
    """Test listing quota buckets by subscription."""
    usage_service.quota_buckets.create(
        db_session,
        QuotaBucketCreate(
            subscription_id=subscription.id,
            included_gb=Decimal("50"),
            period_start=datetime.now(UTC),
            period_end=datetime.now(UTC) + timedelta(days=30),
        ),
    )

    buckets = usage_service.quota_buckets.list(
        db_session,
        subscription_id=str(subscription.id),
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(buckets) >= 1
    assert all(b.subscription_id == subscription.id for b in buckets)


def test_update_quota_bucket_usage(db_session, subscription):
    """Test updating quota bucket usage."""
    bucket = usage_service.quota_buckets.create(
        db_session,
        QuotaBucketCreate(
            subscription_id=subscription.id,
            included_gb=Decimal("100"),
            used_gb=Decimal("0"),
            period_start=datetime.now(UTC),
            period_end=datetime.now(UTC) + timedelta(days=30),
        ),
    )
    updated = usage_service.quota_buckets.update(
        db_session,
        str(bucket.id),
        QuotaBucketUpdate(used_gb=Decimal("25.5")),
    )
    assert updated.used_gb == Decimal("25.5")


def test_list_sessions_by_subscription(db_session, subscription):
    """Test listing RADIUS sessions by subscription."""
    # Note: Creating RADIUS sessions requires valid access_credential_id FK
    # This test just verifies the list method works
    sessions = usage_service.radius_accounting_sessions.list(
        db_session,
        subscription_id=str(subscription.id),
        access_credential_id=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    # Just verify the list call works (may return empty list)
    assert isinstance(sessions, list)


def test_radius_accounting_session_create_does_not_write_the_policy_mac(
    db_session, subscription
):
    """Accounting observes; it does not set device identity.

    `Subscription.mac_address` is consumed as identity -- `unmatched_radio_queue`
    matches a radio's MAC against it -- so letting the accounting feed write it
    made identity drift with whatever CPE last connected, and stop/backlog rows
    could overwrite the current value with a stale one. Same defect shape as the
    accounting dual-write into `ipv4_address`.
    """
    credential = AccessCredential(
        subscriber_id=subscription.subscriber_id,
        username="10005030",
        secret_hash="hashed-secret",
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    db_session.refresh(credential)

    session = usage_service.radius_accounting_sessions.create(
        db_session,
        RadiusAccountingSessionCreate(
            subscription_id=subscription.id,
            access_credential_id=credential.id,
            session_id="acct-start-1",
            status_type="start",
            session_start=datetime.now(UTC),
            calling_station_id="aabbccddeeff",
        ),
    )

    assert session.subscription_id == subscription.id
    db_session.refresh(subscription)
    assert subscription.mac_address is None


def test_radius_accounting_session_update_does_not_write_the_policy_mac(
    db_session, subscription
):
    credential = AccessCredential(
        subscriber_id=subscription.subscriber_id,
        username="10005030",
        secret_hash="hashed-secret",
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    db_session.refresh(credential)

    session = usage_service.radius_accounting_sessions.create(
        db_session,
        RadiusAccountingSessionCreate(
            subscription_id=subscription.id,
            access_credential_id=credential.id,
            session_id="acct-start-2",
            status_type="start",
            session_start=datetime.now(UTC),
        ),
    )

    usage_service.radius_accounting_sessions.update(
        db_session,
        str(session.id),
        RadiusAccountingSessionUpdate(
            calling_station_id="aa:bb:cc:dd:ee:11",
            output_octets=1024,
        ),
    )

    db_session.refresh(subscription)
    assert subscription.mac_address is None


def test_create_usage_record(db_session, subscription):
    """Test creating a usage record."""
    record = usage_service.usage_records.create(
        db_session,
        UsageRecordCreate(
            subscription_id=subscription.id,
            source=UsageSource.radius,
            input_gb=Decimal("5.25"),
            output_gb=Decimal("1.75"),
            total_gb=Decimal("7.00"),
            recorded_at=datetime.now(UTC),
        ),
    )
    assert record.subscription_id == subscription.id
    assert record.source == UsageSource.radius


def test_list_usage_records_by_subscription(db_session, subscription):
    """Test listing usage records by subscription."""
    usage_service.usage_records.create(
        db_session,
        UsageRecordCreate(
            subscription_id=subscription.id,
            source=UsageSource.radius,
            input_gb=Decimal("10.0"),
            recorded_at=datetime.now(UTC),
        ),
    )
    usage_service.usage_records.create(
        db_session,
        UsageRecordCreate(
            subscription_id=subscription.id,
            source=UsageSource.snmp,
            input_gb=Decimal("5.0"),
            recorded_at=datetime.now(UTC),
        ),
    )

    records = usage_service.usage_records.list(
        db_session,
        subscription_id=str(subscription.id),
        quota_bucket_id=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(records) >= 2
    assert all(r.subscription_id == subscription.id for r in records)


def test_usage_record_source_types(db_session, subscription):
    """Test usage records with different source types."""
    sources = [UsageSource.radius, UsageSource.snmp, UsageSource.api]
    for source in sources:
        record = usage_service.usage_records.create(
            db_session,
            UsageRecordCreate(
                subscription_id=subscription.id,
                source=source,
                input_gb=Decimal("1.0"),
                recorded_at=datetime.now(UTC),
            ),
        )
        assert record.source == source


def test_get_quota_bucket(db_session, subscription):
    """Test getting a quota bucket by ID."""
    bucket = usage_service.quota_buckets.create(
        db_session,
        QuotaBucketCreate(
            subscription_id=subscription.id,
            included_gb=Decimal("200"),
            period_start=datetime.now(UTC),
            period_end=datetime.now(UTC) + timedelta(days=30),
        ),
    )
    fetched = usage_service.quota_buckets.get(db_session, str(bucket.id))
    assert fetched is not None
    assert fetched.id == bucket.id
    assert fetched.included_gb == Decimal("200")


def test_delete_usage_record(db_session, subscription):
    """Test deleting a usage record."""
    import pytest
    from fastapi import HTTPException

    record = usage_service.usage_records.create(
        db_session,
        UsageRecordCreate(
            subscription_id=subscription.id,
            source=UsageSource.api,
            input_gb=Decimal("10.0"),
            recorded_at=datetime.now(UTC),
        ),
    )
    record_id = record.id
    usage_service.usage_records.delete(db_session, str(record_id))

    # Verify record is deleted
    with pytest.raises(HTTPException) as exc_info:
        usage_service.usage_records.get(db_session, str(record_id))
    assert exc_info.value.status_code == 404
