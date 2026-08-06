import pytest
from fastapi import HTTPException

from app.services import web_provisioning_migration as migration_service


def test_service_migration_jobs_are_scoped_to_actor_id(db_session):
    filters = migration_service.MigrationFilters(
        reseller_id=None,
        pop_site_id=None,
        subscriber_status=None,
        current_offer_id=None,
        current_nas_device_id=None,
        query=None,
    )
    targets = migration_service.MigrationTargets(
        offer_id="offer-1",
        nas_device_id=None,
        ip_pool_id=None,
        pon_port_id=None,
        scheduled_at=None,
    )

    first = migration_service.create_job(
        db_session,
        filters=filters,
        targets=targets,
        selected_ids=["subscriber-1"],
        actor_id="system-user-1",
    )
    second = migration_service.create_job(
        db_session,
        filters=filters,
        targets=targets,
        selected_ids=["subscriber-2"],
        actor_id="system-user-2",
    )

    jobs = migration_service.list_jobs(db_session, actor_id="system-user-1")

    assert [item["job_id"] for item in jobs] == [first["job_id"]]
    assert (
        migration_service.get_job(db_session, first["job_id"], actor_id="system-user-1")
        is not None
    )
    assert (
        migration_service.get_job(
            db_session, second["job_id"], actor_id="system-user-1"
        )
        is None
    )


def test_service_migration_page_options_scope_jobs_to_actor_id(db_session):
    filters = migration_service.MigrationFilters(
        reseller_id=None,
        pop_site_id=None,
        subscriber_status=None,
        current_offer_id=None,
        current_nas_device_id=None,
        query=None,
    )
    targets = migration_service.MigrationTargets(
        offer_id="offer-1",
        nas_device_id=None,
        ip_pool_id=None,
        pon_port_id=None,
        scheduled_at=None,
    )
    migration_service.create_job(
        db_session,
        filters=filters,
        targets=targets,
        selected_ids=["subscriber-1"],
        actor_id="system-user-visible",
    )
    migration_service.create_job(
        db_session,
        filters=filters,
        targets=targets,
        selected_ids=["subscriber-2"],
        actor_id="system-user-hidden",
    )

    state = migration_service.page_options(db_session, actor_id="system-user-visible")

    assert [item["actor_id"] for item in state["jobs"]] == ["system-user-visible"]


def test_service_migration_rejects_bulk_pon_identity_target(db_session):
    filters = migration_service.MigrationFilters(
        reseller_id=None,
        pop_site_id=None,
        subscriber_status=None,
        current_offer_id=None,
        current_nas_device_id=None,
        query=None,
    )
    targets = migration_service.MigrationTargets(
        offer_id=None,
        nas_device_id=None,
        ip_pool_id=None,
        pon_port_id="00000000-0000-0000-0000-000000000001",
        scheduled_at=None,
    )

    with pytest.raises(HTTPException) as exc_info:
        migration_service.create_job(
            db_session,
            filters=filters,
            targets=targets,
            selected_ids=["00000000-0000-0000-0000-000000000002"],
            actor_id="system-user",
        )

    assert exc_info.value.status_code == 410
    assert "reviewed ONT identity workflow" in exc_info.value.detail


@pytest.mark.parametrize(
    ("nas_device_id", "ip_pool_id"),
    [
        ("00000000-0000-0000-0000-000000000001", None),
        (None, "00000000-0000-0000-0000-000000000002"),
    ],
)
def test_service_migration_rejects_bulk_router_or_pool_target(
    db_session,
    nas_device_id,
    ip_pool_id,
):
    filters = migration_service.MigrationFilters(
        reseller_id=None,
        pop_site_id=None,
        subscriber_status=None,
        current_offer_id=None,
        current_nas_device_id=None,
        query=None,
    )
    targets = migration_service.MigrationTargets(
        offer_id=None,
        nas_device_id=nas_device_id,
        ip_pool_id=ip_pool_id,
        pon_port_id=None,
        scheduled_at=None,
    )

    with pytest.raises(HTTPException) as exc_info:
        migration_service.create_job(
            db_session,
            filters=filters,
            targets=targets,
            selected_ids=["00000000-0000-0000-0000-000000000003"],
            actor_id="system-user",
        )

    assert exc_info.value.status_code == 410
    assert "Move service access" in exc_info.value.detail
