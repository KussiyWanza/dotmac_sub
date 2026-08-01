from __future__ import annotations

import builtins
import logging
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import and_, bindparam, create_engine, func, or_, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session, selectinload

from app.models.billing import Invoice, InvoiceStatus, TaxApplication
from app.models.catalog import (
    AccessCredential,
    AddOn,
    CatalogOffer,
    OfferVersion,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
    UsageAllowance,
)
from app.models.domain_settings import SettingDomain
from app.models.radius import RadiusClient, RadiusUser
from app.models.subscriber import Subscriber
from app.models.usage import (
    AccountingStatus,
    QuotaBucket,
    RadiusAccountingSession,
    UsageCharge,
    UsageChargeStatus,
    UsageRatingRun,
    UsageRatingRunStatus,
    UsageRecord,
)
from app.schemas.billing import InvoiceCreate, SystemInvoiceLineCreate
from app.schemas.usage import (
    QuotaBucketCreate,
    QuotaBucketUpdate,
    RadiusAccountingSessionCreate,
    RadiusAccountingSessionUpdate,
    UsageChargePostBatchRequest,
    UsageChargePostRequest,
    UsageRatingRunRequest,
    UsageRatingRunResponse,
    UsageRecordCreate,
    UsageRecordUpdate,
)
from app.services import control_registry, settings_spec
from app.services import domain_settings as domain_settings_service
from app.services.billing.invoices import InvoiceLines, Invoices
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    validate_enum,
)
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.radius_accounting_health import assess_freshness, stale_after_seconds
from app.services.response import ListResponseMixin, list_response

logger = logging.getLogger(__name__)

_MAC_HEX_RE = re.compile(r"[^0-9A-Fa-f]")

# Long-lived workers run the importer every minute; a fresh Engine per run
# leaked its connection pool (never disposed) until the prefork child hit
# max-tasks-per-child — one of the two leaks behind the 2026-06-10 ingestion
# worker OOM loop. Cache per URL; NullPool so no idle connections linger
# between runs.
_RADACCT_ENGINES: dict[str, object] = {}


def _radacct_engine(db_url: str):
    engine = _RADACCT_ENGINES.get(db_url)
    if engine is None:
        from sqlalchemy.pool import NullPool

        engine = create_engine(db_url, poolclass=NullPool)
        _RADACCT_ENGINES[db_url] = engine
    return engine


_RADIUS_ACCOUNTING_CURSOR_KEY = "radius_accounting_last_radacctid"
# terminate_cause stamped on sessions the reaper closes synthetically (no Stop
# packet ever arrived). Distinguishable from any real RADIUS terminate cause.
_REAPED_TERMINATE_CAUSE = "reaped"
# Per import run, how many locally-open sessions get re-read from radacct.
_RADIUS_REFRESH_BATCH = 500

# Bandwidth samples derived from RADIUS interim-update deltas are written to
# the same Redis stream consumed by app.tasks.bandwidth.process_bandwidth_stream
# so they flow into VictoriaMetrics + the hot BandwidthSample table alongside
# samples from the active poller.
_BANDWIDTH_REDIS_STREAM = os.getenv("BANDWIDTH_REDIS_STREAM", "bandwidth:samples")
_BANDWIDTH_STREAM_MAXLEN = 100000
# Octet rollover ceiling: ignore deltas implying > 100 Gbps (clearly a counter
# reset, not real traffic). Mikrotik RADIUS uses 32-bit counters that wrap.
_BANDWIDTH_SANITY_BPS_CEILING = 100 * 1_000_000_000


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _round_gb(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _round_bucket_gb(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _period_bounds(payload: UsageRatingRunRequest) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    if payload.period_start and payload.period_end:
        return payload.period_start, payload.period_end
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
    return payload.period_start or start, payload.period_end or end


def _resolve_allowance(subscription: Subscription) -> UsageAllowance | None:
    if subscription.offer_version and subscription.offer_version.usage_allowance_id:
        return cast(UsageAllowance | None, subscription.offer_version.usage_allowance)
    if subscription.offer and subscription.offer.usage_allowance_id:
        return cast(UsageAllowance | None, subscription.offer.usage_allowance)
    return None


def _period_bounds_for_record(recorded_at: datetime) -> tuple[datetime, datetime]:
    start = datetime(recorded_at.year, recorded_at.month, 1, tzinfo=UTC)
    if recorded_at.month == 12:
        end = datetime(recorded_at.year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(recorded_at.year, recorded_at.month + 1, 1, tzinfo=UTC)
    return start, end


def _normalize_mac_address(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    compact = _MAC_HEX_RE.sub("", raw)
    if len(compact) != 12:
        return None
    return ":".join(compact[i : i + 2] for i in range(0, 12, 2)).upper()


def _write_subscription_mac_from_accounting(
    db: Session,
    subscription_id,
    calling_station_id: str | None,
) -> None:
    """Deliberately does nothing. Retained as the documented boundary.

    This used to write ``Subscription.mac_address`` from the accounting
    ``Calling-Station-Id``, which conflated an OBSERVATION with a POLICY field.
    Two things went wrong with that:

    * the accounting feed carries stop and backlog rows, so a stale observation
      could overwrite the current value; and
    * ``Subscription.mac_address`` is consumed as device IDENTITY -- see
      ``unmatched_radio_queue``, which matches a radio's MAC against it -- so
      identity matching drifted with whatever CPE last connected.

    This is the same defect shape as the accounting dual-write that put observed
    framed IPs into ``ipv4_address`` and produced duplicate ``Framed-IP-Address``
    projections: an observer writing a field an owner is responsible for. See
    ``_write_subscription_ips_from_accounting`` for how the IP side was split.

    The observed MAC already has a home -- ``RadiusActiveSession``
    ``calling_station_id`` for the live view and external ``radacct`` for
    history -- so nothing is lost by not writing here.

    ``Subscription.mac_address`` is now LEGACY AMBIGUOUS state: some values were
    operator-entered, some were written by this function, and the two are no
    longer distinguishable. It must not be treated as an enforceable device
    binding. An owner-managed binding at exact subscription/credential grain,
    projected as a ``Calling-Station-Id`` CHECK item and shadow-first, is a
    separate slice to be decided after measurement.
    """
    return


def _write_subscription_ips_from_accounting(
    db: Session,
    subscription_id,
    *,
    ipv4: str | None,
    ipv6: str | None,
) -> None:
    """Record the OBSERVED framed address from a live accounting row.

    The observed live IP goes to ``last_seen_framed_ipv4/ipv6`` (display /
    diagnostics, never enforcement). It is separate from
    ``ipv4_address``/``ipv6_address`` — the DESIRED/served IP owned by the IP
    assignment and connectivity owners. Accounting observations never overwrite
    the desired projection."""
    if not subscription_id or not (ipv4 or ipv6):
        return
    subscription = db.get(Subscription, subscription_id)
    if not subscription:
        return
    # OBSERVED → last_seen_framed_* (any status; observational, safe).
    if ipv4 and subscription.last_seen_framed_ipv4 != ipv4:
        subscription.last_seen_framed_ipv4 = ipv4
    if ipv6 and subscription.last_seen_framed_ipv6 != ipv6:
        subscription.last_seen_framed_ipv6 = ipv6


def _radius_accounting_target(db: Session) -> dict[str, Any] | None:
    """Resolve the singular DB-configured accounting authority and schema."""
    from app.services.external_radius_targets import authoritative_accounting_target

    return authoritative_accounting_target(db)


def _radius_accounting_db_url(db: Session) -> str | None:
    """Compatibility reader for callers that only need the configured URL."""
    target = _radius_accounting_target(db)
    return str(target["db_url"]) if target else None


def _get_radius_accounting_cursor(db: Session) -> int:
    try:
        setting = domain_settings_service.usage_settings.get_by_key(
            db, _RADIUS_ACCOUNTING_CURSOR_KEY
        )
    except Exception:
        return 0
    try:
        return int(str(setting.value_text or "0").strip() or "0")
    except ValueError:
        return 0


def _set_radius_accounting_cursor(db: Session, radacctid: int) -> None:
    from app.models.subscription_engine import SettingValueType
    from app.schemas.settings import DomainSettingUpdate

    domain_settings_service.usage_settings.upsert_by_key(
        db,
        _RADIUS_ACCOUNTING_CURSOR_KEY,
        DomainSettingUpdate(
            value_type=SettingValueType.integer,
            value_text=str(int(radacctid)),
            is_active=True,
        ),
    )


def _resolve_subscription_for_credential(
    db: Session, credential: AccessCredential
) -> Subscription | None:
    radius_user = (
        db.query(RadiusUser)
        .filter(RadiusUser.access_credential_id == credential.id)
        .filter(RadiusUser.is_active.is_(True))
        .first()
    )
    if radius_user and radius_user.subscription_id:
        subscription = db.get(Subscription, radius_user.subscription_id)
        if subscription:
            return subscription
    return (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == credential.subscriber_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .order_by(
            Subscription.start_at.desc().nullslast(),
            Subscription.created_at.desc(),
        )
        .first()
    )


def _status_from_radacct(row: dict[str, object]) -> AccountingStatus:
    if row.get("acctstoptime") is not None:
        return AccountingStatus.stop
    if row.get("acctupdatetime") and row.get("acctupdatetime") != row.get(
        "acctstarttime"
    ):
        return AccountingStatus.interim
    return AccountingStatus.start


# (url, client) cache: the emit runs per interim upsert, every importer cycle.
# Building a fresh client (and its connection pool) each call leaked sockets
# for the life of the prefork child — the second leak behind the 2026-06-10
# ingestion worker OOM loop. The leak was dormant until the open-session
# refresh pass made interim re-reads routine.
_BANDWIDTH_REDIS: tuple[str, object] | None = None


def _bandwidth_redis_client(redis_sync, redis_url: str):
    global _BANDWIDTH_REDIS
    if _BANDWIDTH_REDIS is None or _BANDWIDTH_REDIS[0] != redis_url:
        _BANDWIDTH_REDIS = (
            redis_url,
            redis_sync.from_url(redis_url, decode_responses=True),
        )
    return _BANDWIDTH_REDIS[1]


def _emit_bandwidth_sample_from_radius_delta(
    *,
    subscription_id: uuid.UUID,
    nas_device_id: uuid.UUID,
    session_id: str,
    prev_input_octets: int | None,
    prev_output_octets: int | None,
    prev_update_at: datetime | None,
    new_input_octets: int | None,
    new_output_octets: int | None,
    new_update_at: datetime | None,
) -> None:
    """Convert an interim-update octet delta into a bandwidth sample.

    The poller-side ingest worker (app.tasks.bandwidth.process_bandwidth_stream)
    consumes the same Redis stream regardless of source, so RADIUS-derived
    samples flow into VictoriaMetrics + the hot BandwidthSample table without
    any additional plumbing. Previous (octets, timestamp) per session lives in
    a short-lived Redis hash so we don't have to migrate the accounting model.
    """
    if new_input_octets is None or new_output_octets is None or new_update_at is None:
        return

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return

    try:
        import redis as redis_sync  # imported lazily — keeps usage.py optional
    except ImportError:
        return

    state_key = f"radius_bandwidth_state:{session_id}"
    client = _bandwidth_redis_client(redis_sync, redis_url)
    try:
        prev = client.hgetall(state_key)
    except Exception as exc:
        logger.debug("radius_bandwidth_state read failed: %s", exc)
        prev = {}

    # Establish the "previous" anchor from cache first, then from the row's
    # prior in-memory state, then fall through to no-emit on the very first
    # interim of a session.
    prev_in = _safe_int(prev.get("in")) if prev else prev_input_octets
    prev_out = _safe_int(prev.get("out")) if prev else prev_output_octets
    prev_ts = _parse_iso_ts(prev.get("ts")) if prev else prev_update_at

    new_state = {
        "in": str(int(new_input_octets)),
        "out": str(int(new_output_octets)),
        "ts": new_update_at.isoformat(),
    }
    try:
        # Two-interim TTL: long enough to bridge a missed/late update but
        # short enough that a torn-down session doesn't keep a stale anchor.
        client.hset(state_key, mapping=new_state)
        client.expire(state_key, 600)
    except Exception as exc:
        logger.debug("radius_bandwidth_state write failed: %s", exc)

    if prev_in is None or prev_out is None or prev_ts is None:
        return

    delta_seconds = (new_update_at - prev_ts).total_seconds()
    if delta_seconds <= 0:
        return

    delta_in = int(new_input_octets) - int(prev_in)
    delta_out = int(new_output_octets) - int(prev_out)
    # Counter wrap / session restart: skip rather than emit a phantom spike.
    if delta_in < 0 or delta_out < 0:
        return

    rx_bps = int((delta_in * 8) / delta_seconds)
    tx_bps = int((delta_out * 8) / delta_seconds)
    if rx_bps > _BANDWIDTH_SANITY_BPS_CEILING or tx_bps > _BANDWIDTH_SANITY_BPS_CEILING:
        return

    sample_payload: dict[bytes | str | int | float, bytes | str | int | float] = {
        "subscription_id": str(subscription_id),
        "nas_device_id": str(nas_device_id),
        "queue_name": f"radius:{session_id}",
        "rx_bps": str(rx_bps),
        "tx_bps": str(tx_bps),
        "sample_at": new_update_at.isoformat(),
    }
    try:
        client.xadd(
            _BANDWIDTH_REDIS_STREAM, sample_payload, maxlen=_BANDWIDTH_STREAM_MAXLEN
        )
    except Exception as exc:
        logger.debug("radius bandwidth xadd failed: %s", exc)


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _parse_iso_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize for comparison: radacct (Postgres) hands back aware datetimes
    while SQLite-backed local rows can be naive — treat naive as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _coerce_radacct_ts(value: object) -> datetime | None:
    """radacct timestamps arrive as datetime from Postgres but as ISO strings
    from drivers without type info on raw SELECTs (SQLite in tests, some MySQL
    setups)."""
    if value is None or isinstance(value, datetime):
        return value
    return _parse_iso_ts(str(value))


def _coerce_radacct_ip(value: object) -> str | None:
    """radacct inet columns arrive as ipaddress objects from psycopg or as
    strings elsewhere. Host addresses lose their redundant /32 (v4) or /128
    (v6) suffix; genuine prefixes (e.g. a /56 delegation) keep theirs."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("/32") or raw.endswith("/128"):
        raw = raw.rsplit("/", 1)[0]
    return raw


def _upsert_accounting_row(db: Session, row: dict[str, object]) -> bool:
    username = str(row.get("username") or "").strip()
    session_id = str(row.get("acctsessionid") or "").strip()
    if not username or not session_id:
        return False

    credential = (
        db.query(AccessCredential).filter(AccessCredential.username == username).first()
    )
    if not credential:
        return False

    subscription = _resolve_subscription_for_credential(db, credential)
    nas_ip = str(row.get("nasipaddress") or "").strip()
    radius_client = None
    if nas_ip:
        radius_client = (
            db.query(RadiusClient)
            .filter(RadiusClient.client_ip == nas_ip)
            .filter(RadiusClient.is_active.is_(True))
            .first()
        )

    status_type = _status_from_radacct(row)
    existing = (
        db.query(RadiusAccountingSession)
        .filter(RadiusAccountingSession.access_credential_id == credential.id)
        .filter(RadiusAccountingSession.session_id == session_id)
        .first()
    )
    target = existing or RadiusAccountingSession(
        access_credential_id=credential.id,
        session_id=session_id,
    )
    if not existing:
        db.add(target)

    prev_input_octets = target.input_octets
    prev_output_octets = target.output_octets
    prev_update_at = target.last_update_at
    new_update_at = _coerce_radacct_ts(row.get("acctupdatetime"))
    new_stop_at = _coerce_radacct_ts(row.get("acctstoptime"))
    new_start_at = _coerce_radacct_ts(row.get("acctstarttime"))
    # Most recent accounting observation for this row: stop beats interim
    # beats start.
    observed_at = new_stop_at or new_update_at or new_start_at

    # A session we reaped can show up again via the open-session refresh while
    # its radacct row is unchanged (still no stop, no fresher update). Without
    # this guard the upsert would reopen it (session_end back to NULL) and the
    # reaper would close it again next run — a permanent flap. Only let a
    # reaped session be revived by genuinely new information: a real stop or a
    # fresher acctupdatetime.
    if (
        existing is not None
        and existing.terminate_cause == _REAPED_TERMINATE_CAUSE
        and new_stop_at is None
    ):
        observed_utc = _as_utc(observed_at)
        reaped_seen_utc = _as_utc(existing.last_update_at)
        if observed_utc is None or (
            reaped_seen_utc is not None and observed_utc <= reaped_seen_utc
        ):
            return False

    target.subscription_id = subscription.id if subscription else None
    target.radius_client_id = radius_client.id if radius_client else None
    target.nas_device_id = radius_client.nas_device_id if radius_client else None
    target.status_type = status_type
    target.session_start = new_start_at
    target.session_end = new_stop_at
    if observed_at is not None:
        target.last_update_at = observed_at
    target.input_octets = cast(int | None, row.get("acctinputoctets"))
    target.output_octets = cast(int | None, row.get("acctoutputoctets"))
    target.terminate_cause = cast(str | None, row.get("acctterminatecause"))
    framed_ipv4 = _coerce_radacct_ip(row.get("framedipaddress"))
    framed_ipv6_prefix = _coerce_radacct_ip(row.get("framedipv6prefix"))
    delegated_ipv6_prefix = _coerce_radacct_ip(row.get("delegatedipv6prefix"))
    nas_port_id = str(row.get("nasportid") or "").strip() or None
    called_station_id = str(row.get("calledstationid") or "").strip() or None
    # Keep the last known value when a later row omits it (or the column is
    # absent from this radacct schema entirely).
    if framed_ipv4:
        target.framed_ip_address = framed_ipv4
    if framed_ipv6_prefix:
        target.framed_ipv6_prefix = framed_ipv6_prefix
    if delegated_ipv6_prefix:
        target.delegated_ipv6_prefix = delegated_ipv6_prefix
    if nas_port_id:
        target.nas_port_id = nas_port_id
    if called_station_id:
        target.called_station_id = called_station_id

    if (
        status_type == AccountingStatus.interim
        and subscription is not None
        and target.nas_device_id is not None
    ):
        _emit_bandwidth_sample_from_radius_delta(
            subscription_id=subscription.id,
            nas_device_id=target.nas_device_id,
            session_id=session_id,
            prev_input_octets=prev_input_octets,
            prev_output_octets=prev_output_octets,
            prev_update_at=prev_update_at,
            new_input_octets=target.input_octets,
            new_output_octets=target.output_octets,
            new_update_at=new_update_at,
        )
    _write_subscription_mac_from_accounting(
        db,
        target.subscription_id,
        cast(str | None, row.get("callingstationid")),
    )
    # Only live rows update the subscription's current address — a Stop (or a
    # backlog of historical rows) shouldn't overwrite it.
    if new_stop_at is None:
        _write_subscription_ips_from_accounting(
            db,
            target.subscription_id,
            ipv4=framed_ipv4,
            ipv6=delegated_ipv6_prefix or framed_ipv6_prefix,
        )
    if status_type in {AccountingStatus.start, AccountingStatus.interim}:
        credential.last_auth_at = datetime.now(UTC)
    return True


_RADACCT_BASE_COLUMNS = (
    "radacctid",
    "acctsessionid",
    "username",
    "nasipaddress",
    "acctstarttime",
    "acctupdatetime",
    "acctstoptime",
    "acctinputoctets",
    "acctoutputoctets",
    "acctterminatecause",
    "callingstationid",
)
# Present in the standard FreeRADIUS schema but historically not selected
# here; can be absent on older radacct deployments, and the v6 ones only
# carry data if the NAS sends them AND queries.conf writes them. Probed per
# run — missing columns are simply skipped, never an error.
_RADACCT_OPTIONAL_COLUMNS = (
    "framedipaddress",
    "framedipv6prefix",
    "delegatedipv6prefix",
    "nasportid",
    "calledstationid",
)


def _release_postgres_read_transaction(db: Session) -> None:
    bind = db.get_bind()
    if bind.dialect.name.startswith("postgres"):
        db.rollback()


def _quoted_table_identifier(conn, table_name: str) -> str:
    from app.services.external_radius_targets import sanitize_table_identifier

    safe_name = sanitize_table_identifier(table_name, "radacct")
    quote = conn.dialect.identifier_preparer.quote
    return ".".join(quote(part) for part in safe_name.split("."))


def _radacct_select_list(conn, table_name: str) -> str:
    from app.services.external_radius_targets import sanitize_table_identifier

    safe_name = sanitize_table_identifier(table_name, "radacct")
    parts = safe_name.split(".")
    schema = ".".join(parts[:-1]) or None
    try:
        available = {
            c["name"] for c in sa_inspect(conn).get_columns(parts[-1], schema=schema)
        }
    except Exception:
        available = set()
    extras = [c for c in _RADACCT_OPTIONAL_COLUMNS if c in available]
    return ", ".join((*_RADACCT_BASE_COLUMNS, *extras))


def _refresh_open_sessions_from_radacct(
    db: Session,
    conn,
    select_list: str,
    table_sql: str,
    *,
    batch: int = _RADIUS_REFRESH_BATCH,
) -> tuple[int, int]:
    """Re-read radacct for sessions we still hold open.

    FreeRADIUS UPDATEs the radacct row in place on Interim-Update/Stop (same
    radacctid), so the forward-only cursor never sees a session again after
    first ingesting it — without this pass an open session's Stop would be
    lost forever and it would look "active" indefinitely. Recently reaped
    sessions are included too, so one the reaper guessed wrong on (or whose
    real Stop arrived late) gets corrected with the real close.

    Round-robin by refresh_attempted_at (least-recently-attempted first), so
    open sessions whose radacct rows never change — ghosts the reaper hasn't
    closed yet — can't pin the window and starve live sessions of refreshes.
    Returns (candidates_checked, rows_updated).
    """
    now = datetime.now(UTC)
    candidates = (
        db.query(
            RadiusAccountingSession.id,
            RadiusAccountingSession.session_id,
            AccessCredential.username,
        )
        .join(
            AccessCredential,
            RadiusAccountingSession.access_credential_id == AccessCredential.id,
        )
        .filter(
            or_(
                RadiusAccountingSession.session_end.is_(None),
                and_(
                    RadiusAccountingSession.terminate_cause == _REAPED_TERMINATE_CAUSE,
                    RadiusAccountingSession.session_end >= now - timedelta(hours=24),
                ),
            )
        )
        .order_by(
            RadiusAccountingSession.refresh_attempted_at.asc().nullsfirst(),
            RadiusAccountingSession.last_update_at.asc().nullsfirst(),
        )
        .limit(batch)
        .all()
    )
    if not candidates:
        return 0, 0

    candidate_ids = [row_id for row_id, _, _ in candidates]
    session_ids = sorted({sid for _, sid, _ in candidates})
    usernames = sorted({username for _, _, username in candidates if username})
    _release_postgres_read_transaction(db)
    if not usernames:
        db.query(RadiusAccountingSession).filter(
            RadiusAccountingSession.id.in_(candidate_ids)
        ).update({"refresh_attempted_at": now}, synchronize_session=False)
        return len(candidates), 0
    stmt = text(
        f"""
                SELECT {select_list}
                FROM {table_sql}
                WHERE acctsessionid IN :session_ids
                  AND username IN :usernames
                ORDER BY radacctid ASC
        """  # nosec B608  # noqa: S608 — fixed column names; values are bind params
    ).bindparams(
        bindparam("session_ids", expanding=True),
        bindparam("usernames", expanding=True),
    )
    result = conn.execute(stmt, {"session_ids": session_ids, "usernames": usernames})
    rows = [dict(row._mapping) for row in result]
    db.query(RadiusAccountingSession).filter(
        RadiusAccountingSession.id.in_(candidate_ids)
    ).update({"refresh_attempted_at": now}, synchronize_session=False)
    updated = 0
    for row in rows:
        if _upsert_accounting_row(db, row):
            updated += 1
    return len(candidates), updated


def import_radius_accounting(
    db: Session,
    *,
    limit: int | None = None,
) -> dict[str, int | bool | str | None]:
    target = _radius_accounting_target(db)
    if not target:
        return {
            "ok": False,
            "processed": 0,
            "created_or_updated": 0,
            "cursor": 0,
            "source_status": "unconfigured",
            "source_latest_at": None,
            "source_age_seconds": None,
        }

    db_url = str(target["db_url"])
    radacct_table = str(target["radacct_table"])
    batch_size = max(limit or 500, 1)
    last_radacctid = _get_radius_accounting_cursor(db)
    _release_postgres_read_transaction(db)
    processed = 0
    created_or_updated = 0
    refreshed = 0
    cursor = last_radacctid
    engine = _radacct_engine(db_url)
    with engine.begin() as conn:
        table_sql = _quoted_table_identifier(conn, radacct_table)
        select_list = _radacct_select_list(conn, radacct_table)
        result = conn.execute(
            text(
                f"""
                SELECT {select_list}
                FROM {table_sql}
                WHERE radacctid > :cursor
                ORDER BY radacctid ASC
                LIMIT :limit
                """  # nosec B608  # noqa: S608 — fixed column names; values are bind params
            ),
            {"cursor": last_radacctid, "limit": batch_size},
        )
        rows = [dict(row._mapping) for row in result]
        for row in rows:
            processed += 1
            if _upsert_accounting_row(db, row):
                created_or_updated += 1
            cursor = max(cursor, int(row.get("radacctid") or 0))
        if cursor > last_radacctid:
            _set_radius_accounting_cursor(db, cursor)
        db.commit()
        # New rows first (a fresh Stop may close a session and shrink the
        # refresh set), then re-read radacct for whatever is still open.
        refresh_checked, refreshed = _refresh_open_sessions_from_radacct(
            db,
            conn,
            select_list,
            table_sql,
            batch=_RADIUS_REFRESH_BATCH,
        )
        source_latest_at = _coerce_radacct_ts(
            conn.execute(
                text(
                    "SELECT MAX(COALESCE(acctstoptime, acctupdatetime, "  # nosec B608  # noqa: S608 -- sanitized identifier
                    f"acctstarttime)) FROM {table_sql}"
                )
            ).scalar()
        )

    db.commit()
    if refreshed:
        logger.info(
            "radius accounting refresh: %d/%d open sessions updated from radacct",
            refreshed,
            refresh_checked,
        )
    freshness = assess_freshness(
        source_latest_at,
        stale_seconds=stale_after_seconds(db),
    )
    return {
        "ok": freshness.fresh,
        "processed": processed,
        "created_or_updated": created_or_updated,
        "refreshed": refreshed,
        "cursor": cursor,
        "source_status": freshness.state.value,
        "source_latest_at": (
            freshness.observed_at.isoformat()
            if freshness.observed_at is not None
            else None
        ),
        "source_age_seconds": freshness.age_seconds,
        "source_stale_after_seconds": freshness.stale_after_seconds,
    }


_RADIUS_REAP_STALE_DEFAULT_SECONDS = 3600
_RADIUS_REAP_STALE_FLOOR_SECONDS = 300


def reap_stale_radius_sessions(
    db: Session,
    *,
    stale_after_seconds: int | None = None,
    batch: int = 1000,
) -> dict[str, int]:
    """Close open accounting sessions whose accounting feed went silent.

    A NAS reboot, crash or lost Stop packet leaves a session open forever —
    FreeRADIUS never writes acctstoptime, so session_end stays NULL and the
    session renders as "active" indefinitely. A genuinely live session keeps
    advancing last_update_at via interim updates (kept fresh by the
    open-session refresh in import_radius_accounting), so "open but no
    observation since the cutoff" reliably means dead.

    The synthetic session_end is the last time we actually saw the session,
    not the reap time — closer to the truth for usage display. If the session
    turns out to be alive after all, the refresh pass revives it (the upsert
    only honors a reaped close while radacct shows nothing new).
    """
    if stale_after_seconds is None:
        raw = settings_spec.resolve_value(
            db, SettingDomain.usage, "radius_session_reap_stale_seconds"
        )
        try:
            stale_after_seconds = (
                int(str(raw)) if raw is not None else _RADIUS_REAP_STALE_DEFAULT_SECONDS
            )
        except ValueError:
            stale_after_seconds = _RADIUS_REAP_STALE_DEFAULT_SECONDS
    stale_after_seconds = max(stale_after_seconds, _RADIUS_REAP_STALE_FLOOR_SECONDS)

    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=stale_after_seconds)
    last_seen = func.coalesce(
        RadiusAccountingSession.last_update_at,
        RadiusAccountingSession.session_start,
        RadiusAccountingSession.created_at,
    )
    stale = (
        db.query(RadiusAccountingSession)
        .filter(RadiusAccountingSession.session_end.is_(None))
        .filter(last_seen < cutoff)
        .order_by(last_seen.asc())
        .limit(batch)
        .all()
    )
    reaped = 0
    for sess in stale:
        sess.session_end = sess.last_update_at or sess.session_start or sess.created_at
        sess.status_type = AccountingStatus.stop
        sess.terminate_cause = _REAPED_TERMINATE_CAUSE
        reaped += 1
    db.commit()
    if reaped:
        logger.info(
            "reaped %d stale radius sessions (no accounting update in %ds)",
            reaped,
            stale_after_seconds,
        )
    return {"reaped": reaped, "stale_after_seconds": stale_after_seconds}


# Single source of truth for the FUP warn ratio fallback (matches the first
# value of the usage_warning_thresholds spec default "0.8,0.9").
DEFAULT_FUP_WARN_RATIO = 0.8


def _parse_warning_thresholds(value: str | None) -> list[Decimal]:
    if not value:
        return []
    thresholds: list[Decimal] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            threshold = Decimal(part)
        except ArithmeticError:
            continue
        if Decimal("0") < threshold < Decimal("1.5"):
            thresholds.append(threshold)
    return sorted(set(thresholds))


def _resolve_or_create_quota_bucket(
    db: Session, subscription: Subscription, recorded_at: datetime
) -> QuotaBucket:
    period_start, period_end = _period_bounds_for_record(recorded_at)
    bucket = (
        db.query(QuotaBucket)
        .filter(QuotaBucket.subscription_id == subscription.id)
        .filter(QuotaBucket.period_start == period_start)
        .filter(QuotaBucket.period_end == period_end)
        .first()
    )
    if bucket:
        return bucket
    allowance = _resolve_allowance(subscription)
    included_gb, _ = _prorate_allowance(
        allowance, subscription, period_start, period_end
    )
    rounded_included = _round_bucket_gb(included_gb)
    rollover_gb = _carry_forward_rollover(
        db, subscription, allowance, period_start, rounded_included
    )
    bucket = QuotaBucket(
        subscription_id=subscription.id,
        period_start=period_start,
        period_end=period_end,
        included_gb=rounded_included,
        used_gb=Decimal("0.00"),
        rollover_gb=rollover_gb,
        overage_gb=Decimal("0.00"),
    )
    db.add(bucket)
    db.flush()
    return bucket


def _carry_forward_rollover(
    db: Session,
    subscription: Subscription,
    allowance,
    period_start: datetime,
    included_gb: Decimal,
) -> Decimal:
    """Unused allowance from the immediately-preceding period, when the plan has
    rollover. Capped at one period's included_gb so it can't accumulate forever."""
    if allowance is None or not getattr(allowance, "rollover_enabled", False):
        return Decimal("0.00")
    prev = (
        db.query(QuotaBucket)
        .filter(QuotaBucket.subscription_id == subscription.id)
        .filter(QuotaBucket.period_end == period_start)
        .first()
    )
    if prev is None:
        return Decimal("0.00")
    available = (
        Decimal(str(prev.included_gb or 0))
        + Decimal(str(prev.rollover_gb or 0))
        - Decimal(str(prev.used_gb or 0))
    )
    if available <= 0:
        return Decimal("0.00")
    capped = min(available, included_gb) if included_gb > 0 else available
    return _round_bucket_gb(capped)


_GB_BYTES = 1024**3


def meter_usage_into_quota(db: Session, now: datetime | None = None) -> dict:
    """Populate the current period's ``QuotaBucket.used_gb`` from RADIUS
    accounting octets, for every active *capped* subscription.

    This is the missing link between imported ``RadiusAccountingSession`` traffic
    and the quota/FUP machinery — without it ``used_gb`` stays 0 and nothing ever
    triggers. Idempotent: ``used_gb`` is recomputed absolutely from the period's
    sessions each run (never incremented). Uncapped/unlimited plans have no
    allowance, so they get no bucket and are skipped — so an unlimited plan never
    looks "exhausted".
    """
    now = now or datetime.now(UTC)
    subs = (
        db.query(Subscription)
        .join(CatalogOffer, Subscription.offer_id == CatalogOffer.id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(CatalogOffer.usage_allowance_id.isnot(None))
        .all()
    )
    metered = 0
    changed_subscription_ids: list[str] = []
    for sub in subs:
        if _resolve_allowance(sub) is None:
            continue
        bucket = _resolve_or_create_quota_bucket(db, sub, now)
        octets = (
            db.query(
                func.coalesce(func.sum(RadiusAccountingSession.input_octets), 0)
                + func.coalesce(func.sum(RadiusAccountingSession.output_octets), 0)
            )
            .filter(RadiusAccountingSession.subscription_id == sub.id)
            .filter(RadiusAccountingSession.session_start >= bucket.period_start)
            .filter(RadiusAccountingSession.session_start < bucket.period_end)
            .scalar()
        ) or 0
        previous_used_gb = Decimal(str(bucket.used_gb or 0))
        previous_topup_gb = Decimal(str(bucket.topup_gb or 0))
        previous_overage_gb = Decimal(str(bucket.overage_gb or 0))
        used_gb = _round_bucket_gb(Decimal(int(octets)) / Decimal(_GB_BYTES))
        bucket.used_gb = used_gb
        # Refresh top-up from still-valid purchases so expired ones drop out.
        bucket.topup_gb = _round_bucket_gb(_active_topup_gb(db, sub, now))
        allowed = (
            Decimal(str(bucket.included_gb or 0))
            + Decimal(str(bucket.rollover_gb or 0))
            + Decimal(str(bucket.topup_gb or 0))
        )
        overage = used_gb - allowed
        bucket.overage_gb = (
            _round_bucket_gb(overage) if overage > Decimal("0") else Decimal("0.00")
        )
        if (
            previous_used_gb != Decimal(str(bucket.used_gb or 0))
            or previous_topup_gb != Decimal(str(bucket.topup_gb or 0))
            or previous_overage_gb != Decimal(str(bucket.overage_gb or 0))
        ):
            changed_subscription_ids.append(str(sub.id))
        metered += 1
    logger.info(
        "usage_metered_into_quota",
        extra={"metered": metered, "changed": len(changed_subscription_ids)},
    )
    return {"metered": metered, "changed_subscription_ids": changed_subscription_ids}


def _active_topup_gb(db: Session, subscription: Subscription, now: datetime) -> Decimal:
    """Total GB from the subscription's data-top-up purchases that are currently
    in their validity window (started, not yet expired)."""
    rows = (
        db.query(SubscriptionAddOn, AddOn)
        .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
        .filter(SubscriptionAddOn.subscription_id == subscription.id)
        .filter(AddOn.grant_gb.isnot(None))
        .filter(
            (SubscriptionAddOn.start_at.is_(None)) | (SubscriptionAddOn.start_at <= now)
        )
        .filter((SubscriptionAddOn.end_at.is_(None)) | (SubscriptionAddOn.end_at > now))
        .all()
    )
    total = Decimal("0")
    for sub_addon, add_on in rows:
        total += Decimal(str(add_on.grant_gb or 0)) * Decimal(
            str(sub_addon.quantity or 1)
        )
    return total


def grant_data_topup(
    db: Session,
    subscription: Subscription,
    sub_add_on: SubscriptionAddOn,
    add_on: AddOn,
    now: datetime | None = None,
) -> QuotaBucket:
    """Apply a data-top-up purchase: stamp its validity window on the purchase
    record (end_at), then refresh the current quota bucket's topup_gb from all
    still-valid top-ups. A top-up with no validity_days expires at period end."""
    now = now or datetime.now(UTC)
    bucket = _resolve_or_create_quota_bucket(db, subscription, now)
    if add_on.validity_days:
        sub_add_on.end_at = now + timedelta(days=int(add_on.validity_days))
    else:
        sub_add_on.end_at = bucket.period_end
    db.flush()
    bucket.topup_gb = _round_bucket_gb(_active_topup_gb(db, subscription, now))
    db.flush()
    return bucket


def _emit_usage_events(
    db: Session,
    subscription: Subscription,
    bucket: QuotaBucket,
    previous_used: Decimal,
    new_used: Decimal,
) -> None:
    if not control_registry.is_enabled(db, "usage.warnings"):
        return
    included = Decimal(str(bucket.included_gb or 0))
    if included <= 0:
        return
    thresholds_raw = settings_spec.resolve_value(
        db, SettingDomain.usage, "usage_warning_thresholds"
    )
    thresholds = _parse_warning_thresholds(
        str(thresholds_raw) if thresholds_raw is not None else None
    )
    if thresholds:
        previous_ratio = previous_used / included if included else Decimal("0")
        new_ratio = new_used / included if included else Decimal("0")
        for threshold in thresholds:
            if previous_ratio < threshold <= new_ratio:
                emit_event(
                    db,
                    EventType.usage_warning,
                    {
                        "subscription_id": str(subscription.id),
                        "account_id": str(subscription.subscriber_id),
                        "used_gb": str(_round_gb(new_used)),
                        "included_gb": str(_round_gb(included)),
                        "threshold": str(threshold),
                    },
                    subscription_id=subscription.id,
                    account_id=subscription.subscriber_id,
                )
    if previous_used < included <= new_used:
        emit_event(
            db,
            EventType.usage_exhausted,
            {
                "subscription_id": str(subscription.id),
                "account_id": str(subscription.subscriber_id),
                "used_gb": str(_round_gb(new_used)),
                "included_gb": str(_round_gb(included)),
                # When the cap resets — lets enforcement store cap_resets_at so
                # the throttle/block auto-lifts at the period boundary.
                "cap_resets_at": (
                    bucket.period_end.isoformat() if bucket.period_end else None
                ),
            },
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )


def _prorate_allowance(
    allowance: UsageAllowance | None,
    subscription: Subscription,
    period_start: datetime,
    period_end: datetime,
) -> tuple[Decimal, Decimal | None]:
    if not allowance:
        return Decimal("0.0000"), None
    included = Decimal(str(allowance.included_gb or 0))
    cap = Decimal(str(allowance.overage_cap_gb)) if allowance.overage_cap_gb else None
    if not subscription.start_at and not subscription.end_at:
        return included, cap
    active_start = max(subscription.start_at or period_start, period_start)
    active_end = min(subscription.end_at or period_end, period_end)
    period_seconds = max((period_end - period_start).total_seconds(), 1)
    active_seconds = max((active_end - active_start).total_seconds(), 0)
    ratio = Decimal(str(active_seconds / period_seconds))
    if ratio <= 0:
        return Decimal("0.0000"), Decimal("0.0000") if cap else None
    prorated_included = _round_gb(included * ratio)
    prorated_cap = _round_gb(cap * ratio) if cap is not None else None
    return prorated_included, prorated_cap


def _resolve_or_create_invoice(
    db: Session,
    account_id: str,
    period_start: datetime,
    period_end: datetime,
    currency: str,
) -> Invoice:
    invoice = (
        db.query(Invoice)
        .filter(Invoice.account_id == account_id)
        .filter(Invoice.billing_period_start == period_start)
        .filter(Invoice.billing_period_end == period_end)
        .filter(Invoice.is_active.is_(True))
        .first()
    )
    if invoice:
        if invoice.currency != currency:
            raise HTTPException(status_code=400, detail="Invoice currency mismatch")
        return invoice
    default_status = settings_spec.resolve_value(
        db, SettingDomain.billing, "default_invoice_status"
    )
    status_value = (
        validate_enum(default_status, InvoiceStatus, "status")
        if default_status
        else InvoiceStatus.draft
    )
    invoice = Invoices.stage_system_invoice(
        db,
        InvoiceCreate(
            account_id=coerce_uuid(account_id),
            status=status_value,
            currency=currency,
            subtotal=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("0.00"),
            balance_due=Decimal("0.00"),
            billing_period_start=period_start,
            billing_period_end=period_end,
        ),
        reason="usage_charge_posting",
    )
    return invoice


class QuotaBuckets(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: QuotaBucketCreate):
        bucket = QuotaBucket(**payload.model_dump())
        db.add(bucket)
        db.commit()
        db.refresh(bucket)
        return bucket

    @staticmethod
    def get(db: Session, bucket_id: str):
        bucket = db.get(QuotaBucket, bucket_id)
        if not bucket:
            raise HTTPException(status_code=404, detail="Quota bucket not found")
        return bucket

    @staticmethod
    def list(
        db: Session,
        subscription_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(QuotaBucket)
        if subscription_id:
            query = query.filter(QuotaBucket.subscription_id == subscription_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": QuotaBucket.created_at,
                "period_start": QuotaBucket.period_start,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_for_subscriber(
        db: Session, subscriber_id: str, limit: int, offset: int
    ) -> builtins.list:
        """Quota buckets across every subscription owned by ``subscriber_id``."""
        sub_ids = [
            row[0]
            for row in db.query(Subscription.id)
            .filter(Subscription.subscriber_id == subscriber_id)
            .all()
        ]
        if not sub_ids:
            return []
        query = (
            db.query(QuotaBucket)
            .filter(QuotaBucket.subscription_id.in_(sub_ids))
            .order_by(QuotaBucket.period_start.desc())
        )
        buckets = apply_pagination(query, limit, offset).all()
        # Customer-facing overage cost: overage_gb is metered continuously but
        # only priced at rating time — surface the running amount so the app
        # can warn "in overage — ₦X so far" instead of a surprise invoice.
        for bucket in buckets:
            amount = None
            if (bucket.overage_gb or Decimal("0")) > Decimal("0"):
                subscription = db.get(Subscription, bucket.subscription_id)
                allowance = _resolve_allowance(subscription) if subscription else None
                if allowance is not None and allowance.overage_rate is not None:
                    amount = _round_money(
                        Decimal(str(bucket.overage_gb))
                        * Decimal(str(allowance.overage_rate))
                    )
            bucket.overage_amount = amount  # transient, picked up by the schema
        return buckets

    @classmethod
    def list_response_for_subscriber(
        cls, db: Session, subscriber_id: str, limit: int, offset: int
    ) -> dict:
        items = cls.list_for_subscriber(db, subscriber_id, limit, offset)
        return list_response(items, limit, offset)

    @staticmethod
    def update(db: Session, bucket_id: str, payload: QuotaBucketUpdate):
        bucket = db.get(QuotaBucket, bucket_id)
        if not bucket:
            raise HTTPException(status_code=404, detail="Quota bucket not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(bucket, key, value)
        db.commit()
        db.refresh(bucket)
        return bucket

    @staticmethod
    def delete(db: Session, bucket_id: str):
        bucket = db.get(QuotaBucket, bucket_id)
        if not bucket:
            raise HTTPException(status_code=404, detail="Quota bucket not found")
        db.delete(bucket)
        db.commit()


class RadiusAccountingSessions(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: RadiusAccountingSessionCreate):
        session = RadiusAccountingSession(
            **payload.model_dump(exclude={"calling_station_id"})
        )
        db.add(session)
        _write_subscription_mac_from_accounting(
            db,
            session.subscription_id,
            payload.calling_station_id,
        )
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def get(db: Session, session_id: str):
        session = db.get(RadiusAccountingSession, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Accounting session not found")
        return session

    @staticmethod
    def list(
        db: Session,
        subscription_id: str | None,
        access_credential_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(RadiusAccountingSession)
        if subscription_id:
            query = query.filter(
                RadiusAccountingSession.subscription_id == subscription_id
            )
        if access_credential_id:
            query = query.filter(
                RadiusAccountingSession.access_credential_id == access_credential_id
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": RadiusAccountingSession.created_at,
                "session_start": RadiusAccountingSession.session_start,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def find_by_ip(
        db: Session,
        ip: str,
        *,
        at: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list:
        """Reverse lookup: which sessions held this address (v4, or exact v6
        prefix), optionally narrowed to ones live at a point in time. This is
        the abuse-desk / DMCA / lawful-request question — "who had IP X at
        time T" — answerable only because the importer stores the framed
        address against the session window."""
        ip = ip.strip()
        query = db.query(RadiusAccountingSession).filter(
            or_(
                RadiusAccountingSession.framed_ip_address == ip,
                RadiusAccountingSession.framed_ipv6_prefix == ip,
                RadiusAccountingSession.delegated_ipv6_prefix == ip,
            )
        )
        if at is not None:
            query = query.filter(
                RadiusAccountingSession.session_start <= at,
                or_(
                    RadiusAccountingSession.session_end.is_(None),
                    RadiusAccountingSession.session_end >= at,
                ),
            )
        query = query.order_by(RadiusAccountingSession.session_start.desc().nullslast())
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def find_by_ip_response(
        cls,
        db: Session,
        ip: str,
        *,
        at: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        items = cls.find_by_ip(db, ip, at=at, limit=limit, offset=offset)
        return list_response(items, limit, offset)

    @staticmethod
    def list_for_subscriber(
        db: Session, subscriber_id: str, limit: int, offset: int
    ) -> builtins.list:
        """Accounting sessions across every subscription owned by the caller."""
        sub_ids = [
            row[0]
            for row in db.query(Subscription.id)
            .filter(Subscription.subscriber_id == subscriber_id)
            .all()
        ]
        if not sub_ids:
            return []
        query = (
            db.query(RadiusAccountingSession)
            .filter(RadiusAccountingSession.subscription_id.in_(sub_ids))
            .order_by(RadiusAccountingSession.session_start.desc().nullslast())
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def list_response_for_subscriber(
        cls, db: Session, subscriber_id: str, limit: int, offset: int
    ) -> dict:
        items = cls.list_for_subscriber(db, subscriber_id, limit, offset)
        return list_response(items, limit, offset)

    @staticmethod
    def update(db: Session, session_id: str, payload: RadiusAccountingSessionUpdate):
        session = db.get(RadiusAccountingSession, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Accounting session not found")
        for key, value in payload.model_dump(
            exclude_unset=True,
            exclude={"calling_station_id"},
        ).items():
            setattr(session, key, value)
        _write_subscription_mac_from_accounting(
            db,
            session.subscription_id,
            payload.calling_station_id,
        )
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def delete(db: Session, session_id: str):
        session = db.get(RadiusAccountingSession, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Accounting session not found")
        db.delete(session)
        db.commit()


class UsageRecords(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: UsageRecordCreate):
        record = UsageRecord(**payload.model_dump())
        db.add(record)
        subscription = db.get(Subscription, record.subscription_id)
        bucket = None
        previous_used = Decimal("0.00")
        new_used = Decimal("0.00")
        if subscription:
            if record.quota_bucket_id:
                bucket = db.get(QuotaBucket, record.quota_bucket_id)
            if not bucket:
                bucket = _resolve_or_create_quota_bucket(
                    db, subscription, record.recorded_at
                )
                record.quota_bucket_id = bucket.id
            previous_used = Decimal(str(bucket.used_gb or 0))
            increment = Decimal(str(record.total_gb or 0))
            new_used = previous_used + increment
            bucket.used_gb = _round_bucket_gb(new_used)
            bucket.overage_gb = _round_bucket_gb(
                max(new_used - Decimal(str(bucket.included_gb or 0)), Decimal("0"))
            )
        db.commit()
        db.refresh(record)
        if subscription and bucket:
            _emit_usage_events(db, subscription, bucket, previous_used, new_used)
        return record

    @staticmethod
    def get(db: Session, record_id: str):
        record = db.get(UsageRecord, record_id)
        if not record:
            raise HTTPException(status_code=404, detail="Usage record not found")
        return record

    @staticmethod
    def list(
        db: Session,
        subscription_id: str | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
        quota_bucket_id: str | None = None,
    ):
        query = db.query(UsageRecord)
        if subscription_id:
            query = query.filter(UsageRecord.subscription_id == subscription_id)
        if quota_bucket_id:
            query = query.filter(UsageRecord.quota_bucket_id == quota_bucket_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": UsageRecord.created_at,
                "recorded_at": UsageRecord.recorded_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, record_id: str, payload: UsageRecordUpdate):
        record = db.get(UsageRecord, record_id)
        if not record:
            raise HTTPException(status_code=404, detail="Usage record not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(record, key, value)
        db.commit()
        db.refresh(record)
        return record

    @staticmethod
    def delete(db: Session, record_id: str):
        record = db.get(UsageRecord, record_id)
        if not record:
            raise HTTPException(status_code=404, detail="Usage record not found")
        db.delete(record)
        db.commit()


class UsageCharges(ListResponseMixin):
    @staticmethod
    def get(db: Session, charge_id: str):
        charge = db.get(UsageCharge, charge_id)
        if not charge:
            raise HTTPException(status_code=404, detail="Usage charge not found")
        return charge

    @staticmethod
    def list(
        db: Session,
        subscription_id: str | None = None,
        is_posted: bool | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
        subscriber_id: str | None = None,
        status: str | None = None,
        period_start: object | None = None,
        period_end: object | None = None,
    ):
        query = db.query(UsageCharge)
        if subscription_id:
            query = query.filter(UsageCharge.subscription_id == subscription_id)
        if subscriber_id:
            query = query.filter(UsageCharge.subscriber_id == subscriber_id)
        if is_posted is not None:
            if is_posted:
                query = query.filter(UsageCharge.status == UsageChargeStatus.posted)
            else:
                query = query.filter(UsageCharge.status != UsageChargeStatus.posted)
        if status:
            query = query.filter(
                UsageCharge.status == validate_enum(status, UsageChargeStatus, "status")
            )
        if period_start is not None:
            query = query.filter(UsageCharge.period_start == period_start)
        if period_end is not None:
            query = query.filter(UsageCharge.period_end == period_end)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": UsageCharge.created_at,
                "period_start": UsageCharge.period_start,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def post(
        db: Session,
        charge_id: str,
        payload: UsageChargePostRequest,
        commit: bool = True,
    ):
        charge = db.get(UsageCharge, charge_id)
        if not charge:
            raise HTTPException(status_code=404, detail="Usage charge not found")
        if charge.status == UsageChargeStatus.posted:
            return charge
        if charge.status == UsageChargeStatus.needs_review:
            raise HTTPException(status_code=400, detail="Charge requires review")
        subscriber = db.get(Subscriber, charge.subscriber_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        if payload.invoice_id:
            invoice = db.get(Invoice, payload.invoice_id)
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if str(invoice.account_id) != str(charge.subscriber_id):
                raise HTTPException(status_code=400, detail="Invoice not for account")
        else:
            invoice = _resolve_or_create_invoice(
                db,
                str(charge.subscriber_id),
                charge.period_start,
                charge.period_end,
                charge.currency,
            )
        line = InvoiceLines.stage_system_line(
            db,
            SystemInvoiceLineCreate(
                invoice_id=invoice.id,
                subscription_id=charge.subscription_id,
                description="Usage overage",
                quantity=Decimal("1.000"),
                unit_price=_round_money(charge.amount),
                amount=_round_money(charge.amount),
                tax_application=TaxApplication.exclusive,
            ),
            reason="usage_charge_posting",
        )
        charge.invoice_line_id = line.id
        charge.status = UsageChargeStatus.posted
        db.flush()
        from app.services import billing as billing_service

        billing_service._recalculate_invoice_totals(db, invoice)
        if commit:
            db.commit()
            db.refresh(charge)
        return charge

    @staticmethod
    def post_charge(db: Session, charge_id: str, payload: UsageChargePostRequest):
        return UsageCharges.post(db, charge_id, payload)

    @staticmethod
    def post_batch(db: Session, payload: UsageChargePostBatchRequest) -> int:
        query = (
            db.query(UsageCharge)
            .filter(UsageCharge.period_start == payload.period_start)
            .filter(UsageCharge.period_end == payload.period_end)
            .filter(UsageCharge.status == UsageChargeStatus.staged)
        )
        if payload.account_id:
            query = query.filter(UsageCharge.subscriber_id == payload.account_id)
        charges = query.all()
        posted = 0
        for charge in charges:
            UsageCharges.post(
                db,
                str(charge.id),
                UsageChargePostRequest(),
                commit=False,
            )
            posted += 1
        if posted:
            db.commit()
        return posted

    @staticmethod
    def bulk_post_by_ids(db: Session, charge_ids: builtins.list[str]) -> int:
        """Post multiple staged charges by their IDs."""
        posted = 0
        for charge_id in charge_ids:
            try:
                UsageCharges.post(
                    db,
                    charge_id,
                    UsageChargePostRequest(),
                    commit=False,
                )
                posted += 1
            except Exception:
                logger.warning(
                    "Failed to post usage charge %s",
                    charge_id,
                    exc_info=True,
                )
        if posted:
            db.commit()
        return posted


class UsageRatingRuns(ListResponseMixin):
    @staticmethod
    def get(db: Session, run_id: str):
        run = db.get(UsageRatingRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Usage rating run not found")
        return run

    @staticmethod
    def list(
        db: Session,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(UsageRatingRun)
        if status:
            query = query.filter(
                UsageRatingRun.status
                == validate_enum(status, UsageRatingRunStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": UsageRatingRun.created_at, "run_at": UsageRatingRun.run_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_runs_response(
        db: Session,
        started_by: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        # Usage rating runs are currently system-generated; `started_by` is
        # accepted for API compatibility but not yet used for filtering.
        _ = started_by
        return UsageRatingRuns.list_response(
            db, None, order_by, order_dir, limit, offset
        )

    @staticmethod
    def get_run(db: Session, run_id: str):
        return UsageRatingRuns.get(db, run_id)

    @staticmethod
    def run(db: Session, payload: UsageRatingRunRequest) -> UsageRatingRunResponse:
        period_start, period_end = _period_bounds(payload)
        run_at = datetime.now(UTC)
        default_run_status = settings_spec.resolve_value(
            db, SettingDomain.usage, "default_rating_run_status"
        )
        run_status = (
            validate_enum(default_run_status, UsageRatingRunStatus, "status")
            if default_run_status
            else UsageRatingRunStatus.running
        )
        run = UsageRatingRun(
            run_at=run_at,
            period_start=period_start,
            period_end=period_end,
            status=run_status,
        )
        if not payload.dry_run:
            db.add(run)
            db.flush()
        try:
            query = db.query(Subscription).options(
                selectinload(Subscription.offer).selectinload(
                    CatalogOffer.usage_allowance
                ),
                selectinload(Subscription.offer_version).selectinload(
                    OfferVersion.usage_allowance
                ),
            )
            if payload.subscription_id:
                query = query.filter(Subscription.id == payload.subscription_id)
            subscriptions = query.all()
            charges_created = 0
            skipped = 0
            for subscription in subscriptions:
                existing = (
                    db.query(UsageCharge)
                    .filter(UsageCharge.subscription_id == subscription.id)
                    .filter(UsageCharge.period_start == period_start)
                    .filter(UsageCharge.period_end == period_end)
                    .first()
                )
                if existing:
                    skipped += 1
                    continue
                total_gb = (
                    db.query(func.coalesce(func.sum(UsageRecord.total_gb), 0))
                    .filter(UsageRecord.subscription_id == subscription.id)
                    .filter(UsageRecord.recorded_at >= period_start)
                    .filter(UsageRecord.recorded_at < period_end)
                    .scalar()
                )
                total_gb = _round_gb(Decimal(str(total_gb)))
                allowance = _resolve_allowance(subscription)
                included_gb, cap_gb = _prorate_allowance(
                    allowance, subscription, period_start, period_end
                )
                included_gb = _round_gb(included_gb)
                billable_gb = total_gb - included_gb
                if billable_gb < 0:
                    billable_gb = Decimal("0.0000")
                if cap_gb is not None and billable_gb > cap_gb:
                    billable_gb = cap_gb
                rate = Decimal("0.0000")
                default_currency = settings_spec.resolve_value(
                    db, SettingDomain.billing, "default_currency"
                )
                currency = default_currency or "NGN"
                default_status = settings_spec.resolve_value(
                    db, SettingDomain.usage, "default_charge_status"
                )
                status = (
                    validate_enum(default_status, UsageChargeStatus, "status")
                    if default_status
                    else UsageChargeStatus.staged
                )
                notes = None
                if allowance and allowance.overage_rate is not None:
                    rate = Decimal(str(allowance.overage_rate))
                else:
                    status = UsageChargeStatus.needs_review
                    notes = "Missing usage allowance or overage rate"
                amount = _round_money(billable_gb * rate)
                if billable_gb == 0:
                    amount = Decimal("0.00")
                    status = UsageChargeStatus.skipped
                if not subscription.subscriber_id:
                    status = UsageChargeStatus.needs_review
                    notes = "Subscription missing account"
                charge = UsageCharge(
                    subscription_id=subscription.id,
                    subscriber_id=subscription.subscriber_id,
                    period_start=period_start,
                    period_end=period_end,
                    total_gb=total_gb,
                    included_gb=included_gb,
                    billable_gb=_round_gb(billable_gb),
                    unit_price=rate,
                    amount=amount,
                    currency=currency,
                    status=status,
                    notes=notes,
                    rated_at=run_at,
                )
                if not payload.dry_run:
                    db.add(charge)
                charges_created += 1
            if not payload.dry_run:
                run.subscriptions_scanned = len(subscriptions)
                run.charges_created = charges_created
                run.skipped = skipped
                run.status = UsageRatingRunStatus.success
                db.commit()
            return UsageRatingRunResponse(
                run_id=run.id if not payload.dry_run else None,
                run_at=run_at,
                period_start=period_start,
                period_end=period_end,
                subscriptions_scanned=len(subscriptions),
                charges_created=charges_created,
                skipped=skipped,
            )
        except Exception as exc:
            if not payload.dry_run:
                run.status = UsageRatingRunStatus.failed
                run.error = str(exc)
                db.commit()
            raise


quota_buckets = QuotaBuckets()
radius_accounting_sessions = RadiusAccountingSessions()
usage_records = UsageRecords()
usage_charges = UsageCharges()
usage_rating_runs = UsageRatingRuns()
usage_ratings = usage_rating_runs
