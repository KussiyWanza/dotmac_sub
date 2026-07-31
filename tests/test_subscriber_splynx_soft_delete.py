from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.subscriber import Subscriber, SubscriberStatus, UserType
from app.services import customer_account_visibility
from app.services import subscriber as subscriber_service


def test_is_splynx_deleted_import_detects_explicit_flag(db_session):
    subscriber = Subscriber(
        first_name="Deleted",
        last_name="Import",
        email="deleted-flag@example.com",
        status=SubscriberStatus.canceled,
        is_active=False,
        user_type=UserType.customer,
        splynx_customer_id=101,
        metadata_={"splynx_deleted": True, "splynx_status": "active"},
    )
    db_session.add(subscriber)
    db_session.commit()

    assert customer_account_visibility.is_splynx_deleted_import(subscriber) is True


def test_is_splynx_deleted_import_detects_legacy_rows_without_flag(db_session):
    subscriber = Subscriber(
        first_name="Legacy",
        last_name="Deleted",
        email="legacy-deleted@example.com",
        status=SubscriberStatus.canceled,
        is_active=False,
        user_type=UserType.customer,
        splynx_customer_id=202,
        metadata_={"splynx_status": "active"},
    )
    db_session.add(subscriber)
    db_session.commit()

    assert customer_account_visibility.is_splynx_deleted_import(subscriber) is True


def test_explicit_false_splynx_deleted_flag_overrides_legacy_fallback(db_session):
    retained = Subscriber(
        first_name="Retained",
        last_name="Canceled",
        email="retained-canceled@example.com",
        status=SubscriberStatus.canceled,
        is_active=False,
        user_type=UserType.customer,
        splynx_customer_id=203,
        metadata_={"splynx_deleted": False, "splynx_status": "blocked"},
    )
    legacy_deleted = Subscriber(
        first_name="Legacy",
        last_name="Fallback",
        email="legacy-fallback@example.com",
        status=SubscriberStatus.canceled,
        is_active=False,
        user_type=UserType.customer,
        splynx_customer_id=204,
        metadata_={"splynx_status": "blocked"},
    )
    db_session.add_all([retained, legacy_deleted])
    db_session.commit()

    assert customer_account_visibility.is_splynx_deleted_import(retained) is False
    assert customer_account_visibility.is_splynx_deleted_import(legacy_deleted) is True

    deleted_ids = set(
        db_session.scalars(
            select(Subscriber.id).where(
                customer_account_visibility.splynx_deleted_import_clause()
            )
        )
    )
    assert retained.id not in deleted_ids
    assert legacy_deleted.id in deleted_ids


def test_count_stats_excludes_splynx_deleted_imports(db_session):
    active = Subscriber(
        first_name="Active",
        last_name="User",
        email="active-user@example.com",
        status=SubscriberStatus.active,
        is_active=True,
        user_type=UserType.customer,
    )
    imported_deleted = Subscriber(
        first_name="Deleted",
        last_name="User",
        email="deleted-user@example.com",
        status=SubscriberStatus.canceled,
        is_active=False,
        user_type=UserType.customer,
        splynx_customer_id=303,
        metadata_={"splynx_status": "active"},
    )
    db_session.add_all([active, imported_deleted])
    db_session.commit()

    stats = subscriber_service.subscribers.count_stats(db_session)

    assert stats == {
        "total": 1,
        "active": 1,
        "persons": 1,
        "organizations": 0,
    }


def test_dashboard_stats_ignore_splynx_deleted_import_churn(db_session):
    now = datetime.now(UTC)
    active = Subscriber(
        first_name="Current",
        last_name="Active",
        email="current-active@example.com",
        status=SubscriberStatus.active,
        is_active=True,
        user_type=UserType.customer,
        created_at=now - timedelta(days=10),
        updated_at=now - timedelta(days=1),
    )
    canceled = Subscriber(
        first_name="Real",
        last_name="Canceled",
        email="real-canceled@example.com",
        status=SubscriberStatus.canceled,
        is_active=False,
        user_type=UserType.customer,
        created_at=now - timedelta(days=60),
        updated_at=now - timedelta(days=5),
    )
    imported_deleted = Subscriber(
        first_name="Imported",
        last_name="Deleted",
        email="imported-deleted@example.com",
        status=SubscriberStatus.canceled,
        is_active=False,
        user_type=UserType.customer,
        splynx_customer_id=404,
        metadata_={"splynx_status": "active"},
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=2),
    )
    db_session.add_all([active, canceled, imported_deleted])
    db_session.commit()

    stats = subscriber_service.subscribers.get_dashboard_stats(db_session)

    assert stats["total_count"] == 2
    assert stats["active_count"] == 1
    assert stats["new_this_month"] == 1
    assert stats["subscriber_status_chart"]["values"] == [1, 0, 1, 1]
    assert stats["churn_rate"] == 50.0
    assert all(
        row.email != "imported-deleted@example.com"
        for row in stats["recent_subscribers"]
    )


def test_effective_dates_prefer_splynx_source_metadata(db_session):
    subscriber = Subscriber(
        first_name="Source",
        last_name="Dates",
        email="source-dates@example.com",
        status=SubscriberStatus.active,
        is_active=True,
        user_type=UserType.customer,
        splynx_customer_id=505,
        account_start_date=datetime(2026, 1, 10, tzinfo=UTC),
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
        updated_at=datetime(2026, 3, 15, tzinfo=UTC),
        metadata_={
            "splynx_date_add": "2025-12-20T08:30:00+00:00",
            "splynx_last_update": "2026-02-14T09:45:00+00:00",
        },
    )
    db_session.add(subscriber)
    db_session.commit()

    assert subscriber_service.get_effective_created_at(subscriber) == datetime(
        2025, 12, 20, 8, 30, tzinfo=UTC
    )
    assert subscriber_service.get_effective_updated_at(subscriber) == datetime(
        2026, 2, 14, 9, 45, tzinfo=UTC
    )


def test_effective_created_at_falls_back_to_account_start_for_splynx_import(db_session):
    subscriber = Subscriber(
        first_name="Fallback",
        last_name="Date",
        email="fallback-date@example.com",
        status=SubscriberStatus.active,
        is_active=True,
        user_type=UserType.customer,
        splynx_customer_id=606,
        account_start_date=datetime(2025, 11, 5, tzinfo=UTC),
        created_at=datetime(2026, 3, 2, tzinfo=UTC),
    )
    db_session.add(subscriber)
    db_session.commit()

    assert subscriber_service.get_effective_created_at(subscriber) == datetime(
        2025, 11, 5, tzinfo=UTC
    )
