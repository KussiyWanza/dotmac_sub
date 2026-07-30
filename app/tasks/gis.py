import logging
import time
from datetime import UTC, datetime

from app.celery_app import celery_app
from app.models.domain_settings import SettingDomain
from app.services import gis_sync as gis_sync_service
from app.services import web_system_geocode_tool as web_system_geocode_tool_service
from app.services.db_session_adapter import db_session_adapter
from app.services.observability import record_task_run
from app.services.settings_spec import resolve_boolean

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.gis.sync_gis_sources")
def sync_gis_sources():
    start = time.monotonic()
    started_at = datetime.now(UTC)
    status = "success"
    # record_last_sync_run persists this as an opaque JSON payload, so the
    # value type stays open rather than narrowing to the per-source counters.
    results: dict[str, object] = {}
    sync_pops = False
    sync_addresses = False
    deactivate_missing = False
    session = SessionLocal()
    try:
        sync_pops = resolve_boolean(
            session,
            SettingDomain.gis,
            "sync_pop_sites",
        )
        sync_addresses = resolve_boolean(
            session,
            SettingDomain.gis,
            "sync_addresses",
        )
        deactivate_missing = resolve_boolean(
            session,
            SettingDomain.gis,
            "sync_deactivate_missing",
        )
        logger.info(
            "GIS sync start pops=%s addresses=%s deactivate_missing=%s",
            sync_pops,
            sync_addresses,
            deactivate_missing,
        )
        if sync_pops:
            result = gis_sync_service.geo_sync.sync_pop_sites(
                session, deactivate_missing=deactivate_missing
            )
            results["pop_sites"] = gis_sync_service._sync_result_payload(result)
            logger.info(
                "GIS sync pop sites created=%s updated=%s skipped=%s",
                result.created,
                result.updated,
                result.skipped,
            )
        if sync_addresses:
            result = gis_sync_service.geo_sync.sync_addresses(
                session, deactivate_missing=deactivate_missing
            )
            results["addresses"] = gis_sync_service._sync_result_payload(result)
            logger.info(
                "GIS sync addresses created=%s updated=%s skipped=%s",
                result.created,
                result.updated,
                result.skipped,
            )
        session.commit()
        gis_sync_service.record_last_sync_run(
            session,
            status="success",
            started_at=started_at,
            sync_pops=sync_pops,
            sync_addresses=sync_addresses,
            deactivate_missing=deactivate_missing,
            results=results,
        )
    except Exception as exc:
        status = "error"
        session.rollback()
        gis_sync_service.record_last_sync_run(
            session,
            status="error",
            started_at=started_at,
            sync_pops=sync_pops,
            sync_addresses=sync_addresses,
            deactivate_missing=deactivate_missing,
            results=results,
            error=str(exc),
        )
        logger.exception("GIS sync failed.")
        raise
    finally:
        session.close()
        duration = time.monotonic() - start
        record_task_run("gis_sync", status=status, duration_seconds=duration)


@celery_app.task(name="app.tasks.gis.run_batch_geocode_job")
def run_batch_geocode_job(*, job_id: str):
    """Execute a batch geocoding job from system geocode tool."""
    session = SessionLocal()
    try:
        result = web_system_geocode_tool_service.execute_job(session, job_id=job_id)
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
