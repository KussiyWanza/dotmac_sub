"""Web helpers for generic integration sync profiles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.integration import (
    IntegrationJob,
    IntegrationJobType,
    IntegrationRecord,
    IntegrationRun,
    IntegrationRunStatus,
    IntegrationScheduleType,
)
from app.models.support import Ticket, TicketComment, canonical_ticket_status_value
from app.schemas.status_presentation import StatusTone
from app.services.common import coerce_uuid
from app.services.integrations.connectors.dotmac_crm import (
    CRM_TICKET_OBSERVATION_CAPABILITY,
)
from app.services.ui_contracts import Action, Kpi, StateValue
from app.services.web_integrations import _parse_json

# A sync profile whose newest run is older than this multiple of its scheduled
# interval is reported stale — the freshness signal an integration owner needs
# to spot a wedged puller before the failure count climbs.
_STALE_INTERVAL_MULTIPLE = 2


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _job_runs(db: Session, job_ids: list) -> dict[str, list[IntegrationRun]]:
    if not job_ids:
        return {}
    rows = (
        db.query(IntegrationRun)
        .filter(IntegrationRun.job_id.in_(job_ids))
        .order_by(IntegrationRun.started_at.desc())
        .limit(200)
        .all()
    )
    grouped: dict[str, list[IntegrationRun]] = {}
    for run in rows:
        grouped.setdefault(str(run.job_id), []).append(run)
    return grouped


def _daily_counts(db: Session, days: int = 14) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        db.query(
            func.date(IntegrationRun.started_at).label("day"),
            IntegrationJob.direction,
            IntegrationRun.status,
            func.count(IntegrationRun.id),
        )
        .join(IntegrationJob, IntegrationJob.id == IntegrationRun.job_id)
        .filter(IntegrationRun.started_at >= since)
        .group_by("day", IntegrationJob.direction, IntegrationRun.status)
        .order_by("day")
        .all()
    )
    return [
        {
            "day": str(day),
            "direction": direction or "sync",
            "status": _enum_value(status),
            "count": count,
        }
        for day, direction, status, count in rows
    ]


def _syncs_cohort_url(
    *, direction: str | None = None, active: bool | None = None
) -> str:
    """Drill-down to the sync list filtered to exactly the cohort a KPI counts.

    ``direction`` and ``active`` are the same filters the index route honours, so
    a headline count and the profiles it summarises never diverge (KPI-parity).
    """
    params = {
        "direction": direction,
        "active": "1" if active else None,
    }
    query = urlencode({key: value for key, value in params.items() if value})
    return "/admin/integrations/syncs" + (f"?{query}" if query else "")


def _last_run_state(job: IntegrationJob, runs: list[IntegrationRun]) -> StateValue:
    """Freshness of a sync profile's newest run as a State contract value.

    Never-run profiles resolve to ``unknown`` (no run instant exists yet) so the
    template shows an explicit absence rather than a zero/date stand-in; an
    interval profile whose newest run is past its staleness window resolves to
    ``stale`` so a wedged puller is visible before failures accrue.
    """
    latest = runs[0] if runs else None
    started = getattr(latest, "started_at", None) if latest else None
    if started is None:
        return StateValue.unknown()
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    interval = (
        job.interval_minutes
        if job.schedule_type == IntegrationScheduleType.interval
        else None
    )
    if interval and datetime.now(UTC) - started > timedelta(
        minutes=interval * _STALE_INTERVAL_MULTIPLE
    ):
        return StateValue.stale(started, as_of=started)
    return StateValue.present(started, as_of=started)


def _run_action(job: IntegrationJob) -> Action:
    """Manual-run eligibility owned here, mirroring the route's disabled guard
    (``sync_run`` refuses inactive jobs) so the button is never offered on a
    profile the backend would reject."""
    active = bool(job.is_active)
    return Action(
        key="run",
        label="Run",
        allowed=active,
        reason=None if active else "Profile is disabled",
        permission="system:settings:write",
        tone=StatusTone.info if active else StatusTone.neutral,
    )


def _sync_row(job: IntegrationJob, runs: list[IntegrationRun]) -> dict[str, Any]:
    latest = runs[0] if runs else None
    return {
        "job": job,
        "last_run": _last_run_state(job, runs),
        "status_val": (latest.status.value if latest and latest.status else "never"),
        "run_action": _run_action(job),
    }


def build_syncs_index_data(
    db: Session,
    *,
    direction: str | None = None,
    active: bool | None = None,
) -> dict[str, Any]:
    sync_jobs = (
        db.query(IntegrationJob)
        .filter(IntegrationJob.job_type == IntegrationJobType.sync)
        .order_by(IntegrationJob.name.asc())
        .all()
    )
    runs_by_job = _job_runs(db, [job.id for job in sync_jobs])
    failed_records = (
        db.query(IntegrationRecord)
        .filter(IntegrationRecord.status == "failed")
        .order_by(IntegrationRecord.created_at.desc())
        .limit(10)
        .all()
    )
    # KPIs summarise the whole profile set and each drills into its own cohort;
    # the displayed table is the drill-down, filtered to the active view so a
    # tile's count always matches the rows its link produces.
    want_direction = (direction or "").strip().lower() or None
    displayed_jobs = [
        job
        for job in sync_jobs
        if (want_direction is None or job.direction == want_direction)
        and (active is not True or job.is_active)
    ]
    sync_rows = [
        _sync_row(job, runs_by_job.get(str(job.id), [])) for job in displayed_jobs
    ]
    sync_kpis = {
        "total": Kpi(
            label="Profiles",
            value=StateValue.present(len(sync_jobs)),
            cohort_url=_syncs_cohort_url(),
        ),
        "active": Kpi(
            label="Active",
            value=StateValue.present(sum(1 for job in sync_jobs if job.is_active)),
            cohort_url=_syncs_cohort_url(active=True),
            tone=StatusTone.positive,
        ),
        "pull": Kpi(
            label="Pull",
            value=StateValue.present(
                sum(1 for job in sync_jobs if job.direction == "pull")
            ),
            cohort_url=_syncs_cohort_url(direction="pull"),
            tone=StatusTone.info,
        ),
        "push": Kpi(
            label="Push",
            value=StateValue.present(
                sum(1 for job in sync_jobs if job.direction == "push")
            ),
            cohort_url=_syncs_cohort_url(direction="push"),
            tone=StatusTone.warning,
        ),
    }
    return {
        "sync_jobs": displayed_jobs,
        "sync_rows": sync_rows,
        "runs_by_job": runs_by_job,
        "daily_counts": _daily_counts(db),
        "failed_records": failed_records,
        "sync_kpis": sync_kpis,
        "direction_filter": want_direction,
        "active_only": active is True,
    }


def build_sync_detail_data(db: Session, job_id: str) -> dict[str, Any]:
    job = db.get(IntegrationJob, coerce_uuid(job_id))
    if job is None:
        raise ValueError("Sync profile not found")
    runs = (
        db.query(IntegrationRun)
        .filter(IntegrationRun.job_id == job.id)
        .order_by(IntegrationRun.started_at.desc())
        .limit(50)
        .all()
    )
    run_ids = [run.id for run in runs[:5]]
    records = []
    if run_ids:
        records = (
            db.query(IntegrationRecord)
            .filter(IntegrationRecord.run_id.in_(run_ids))
            .order_by(IntegrationRecord.created_at.desc())
            .limit(100)
            .all()
        )
    return {"job": job, "runs": runs, "records": records}


def update_sync_profile(
    db: Session,
    job_id: str,
    *,
    schedule_type: str,
    interval_minutes: str | None,
    trigger_mode: str | None,
    mapping_config: str | None,
    filter_config: str | None,
    page_size: str | None = None,
    max_pages: str | None = None,
    sync_comments: bool | None = None,
    mapping_primary: str | None = None,
    mapping_fallback: str | None = None,
    mapping_ambiguous: str | None = None,
    conflict_policy: str | None,
    is_active: bool,
) -> IntegrationJob:
    job = db.get(IntegrationJob, coerce_uuid(job_id))
    if job is None:
        raise ValueError("Sync profile not found")
    interval_value = int(interval_minutes) if interval_minutes else None
    if schedule_type == "interval" and not interval_value:
        raise ValueError("interval_minutes is required for interval schedules")
    job.schedule_type = IntegrationScheduleType(schedule_type)
    job.interval_minutes = interval_value
    job.trigger_mode = (trigger_mode or "").strip() or None
    if (
        job.capability_binding is not None
        and job.capability_binding.capability_id == CRM_TICKET_OBSERVATION_CAPABILITY
    ):
        job.filter_config = {
            "page_size": int(page_size or 200),
            "max_pages": int(max_pages or 50),
            "sync_comments": bool(sync_comments),
        }
        job.mapping_config = None
        job.conflict_policy = None
    else:
        job.mapping_config = _parse_json(mapping_config, "mapping_config")
        job.filter_config = _parse_json(filter_config, "filter_config")
        job.conflict_policy = (conflict_policy or "").strip() or None
    job.is_active = is_active
    db.commit()
    db.refresh(job)
    return job


def backfill_crm_ticket_import_history(db: Session, job_id: str) -> IntegrationRun:
    job = db.get(IntegrationJob, coerce_uuid(job_id))
    if job is None:
        raise ValueError("Sync profile not found")

    existing = (
        db.query(IntegrationRun)
        .filter(IntegrationRun.job_id == job.id)
        .filter(IntegrationRun.trigger == "backfill")
        .first()
    )
    if existing:
        return existing

    crm_ticket_filter = Ticket.metadata_["sync_source"].as_string() == "crm"
    crm_comment_filter = TicketComment.metadata_["sync_source"].as_string() == "crm"
    tickets = (
        db.query(Ticket)
        .filter(crm_ticket_filter)
        .order_by(Ticket.created_at.asc())
        .all()
    )
    comments_count = (
        db.query(func.count(TicketComment.id)).filter(crm_comment_filter).scalar() or 0
    )

    started_at = min(
        (ticket.created_at for ticket in tickets if ticket.created_at),
        default=datetime.now(UTC),
    )
    run = IntegrationRun(
        job_id=job.id,
        status=IntegrationRunStatus.success,
        trigger="backfill",
        requested_by="system",
        started_at=started_at,
        finished_at=datetime.now(UTC),
        metrics={
            "fetched": len(tickets),
            "created": len(tickets),
            "updated": 0,
            "skipped_unmapped_subscribers": 0,
            "comments_created": comments_count,
            "errors": [],
            "source": "backfilled_from_existing_crm_ticket_import",
        },
    )
    db.add(run)
    db.flush()

    for ticket in tickets:
        metadata = ticket.metadata_ or {}
        db.add(
            IntegrationRecord(
                run_id=run.id,
                entity_type=job.entity_type or "ticket",
                direction=job.direction or "pull",
                local_id=str(ticket.id),
                remote_id=str(metadata.get("crm_ticket_id") or "") or None,
                remote_number=ticket.number,
                action="created",
                status="success",
                reason="Backfilled from existing CRM ticket import.",
                payload_snapshot={
                    "number": ticket.number,
                    "title": ticket.title,
                    "status": canonical_ticket_status_value(ticket.status),
                    "priority": ticket.priority,
                    "subscriber_id": (
                        str(ticket.subscriber_id) if ticket.subscriber_id else None
                    ),
                    "crm_ticket_id": metadata.get("crm_ticket_id"),
                    "crm_ticket_number": metadata.get("crm_ticket_number"),
                },
                created_at=ticket.created_at or datetime.now(UTC),
            )
        )

    job.last_run_at = run.finished_at
    db.commit()
    db.refresh(run)
    return run


def trigger_sync_job(job_id: str) -> None:
    from app.tasks.integrations import run_integration_job

    run_integration_job.delay(job_id, "manual")
