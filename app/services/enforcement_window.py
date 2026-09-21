"""Shared time-of-day gating for billing/dunning enforcement and notifications.

Shared wall-clock decision helper for customer-impacting billing actions
(suspension, throttling, dunning comms) without each path re-implementing
timezone math.

Pure decision helper (``window_block_reason``) + thin settings resolvers. The
celery cadence must fire often enough (hourly) for a window to be effective — a
daily 01:00-UTC run with a 09:00 window would never act. See
``docs/FINANCIAL_ACCESS_ENFORCEMENT.md``.

Timezone note: celery beat fires on the celery *app* timezone (UTC today); the
window comparisons here use the ``scheduler.timezone`` setting (local TZ).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import settings_spec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnforcementWindowDecision:
    inside_window: bool
    block_reason: str | None

    @property
    def should_defer(self) -> bool:
        return not self.inside_window


def resolve_timezone_name(db: Session) -> str:
    """The configured local timezone for enforcement/notification decisions."""
    value = settings_spec.resolve_value(db, SettingDomain.scheduler, "timezone")
    return str(value) if value else "UTC"


def to_local(db: Session, run_at: datetime) -> datetime:
    """Convert ``run_at`` to the configured local timezone (falls back to as-is)."""
    try:
        return run_at.astimezone(ZoneInfo(resolve_timezone_name(db)))
    except Exception:
        return run_at


def parse_time(value: str | None) -> time | None:
    """Parse an ``HH:MM`` / ``HH:MM:SS`` setting value into a ``time``."""
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return None


def window_block_reason(
    local_run_at: datetime,
    *,
    start_time: time | None = None,
    end_time: time | None = None,
) -> str | None:
    """Why an action should be skipped at ``local_run_at``, or ``None`` to proceed.

    ``local_run_at`` must already be in the target local timezone (see
    ``to_local``). Window semantics:

    * ``start_time`` + ``end_time``, start <= end: act only when
      ``start_time <= now < end_time`` (e.g. 09:00–18:00).
    * ``start_time`` + ``end_time``, start > end: window wraps midnight, act when
      ``now >= start_time`` OR ``now < end_time`` (e.g. 22:00–06:00).
    * ``start_time`` only: act only at/after it (matches the legacy prepaid
      ``blocking_time`` gate).
    * neither: no time gate.

    Calendar-day exclusions are deliberately unsupported. Financial lifecycle
    enforcement uses the same time-of-day policy every logical day.
    """
    now_t = local_run_at.time()
    if start_time is not None and end_time is not None:
        if start_time <= end_time:
            if not (start_time <= now_t < end_time):
                return "outside_window"
        else:  # wraps midnight
            if not (now_t >= start_time or now_t < end_time):
                return "outside_window"
    elif start_time is not None:
        if now_t < start_time:
            return "before_window"

    return None


def within_send_window(db: Session, run_at: datetime) -> bool:
    """Whether billing/dunning notifications may be sent at ``run_at``.

    Gated by ``collections.billing_notif_send_hour`` (0-23) evaluated in
    ``scheduler.timezone``: sends are allowed only during the
    ``[send_hour, send_hour+1)`` local hour, so an hourly notifications runner
    emits once per day at the configured hour. Returns ``True`` (no gate) when
    the hour is unset. A malformed (non-integer) configured value fails
    CLOSED — treated as "not in window" — rather than silently degrading to
    "always send"; a clear warning is logged so an operator notices the
    misconfiguration.
    """
    hour_value = settings_spec.resolve_value(
        db, SettingDomain.collections, "billing_notif_send_hour"
    )
    if hour_value is None:
        return True
    try:
        hour = int(str(hour_value))
    except (TypeError, ValueError):
        logger.warning(
            "billing_notif_send_hour is not a valid integer (%r); "
            "failing closed (treating as outside the send window)",
            hour_value,
        )
        return False
    if not (0 <= hour <= 23):
        logger.warning(
            "billing_notif_send_hour %r is out of range 0-23; "
            "failing closed (treating as outside the send window)",
            hour,
        )
        return False
    local_run_at = to_local(db, run_at)
    return (
        window_block_reason(
            local_run_at,
            start_time=time(hour, 0),
            end_time=time((hour + 1) % 24, 0),
        )
        is None
    )


def resolve_enforcement_window_decision(
    db: Session, run_at: datetime | None = None
) -> EnforcementWindowDecision:
    """Resolve the configured enforcement window.

    Gated by ``collections.enforcement_window_start`` / ``enforcement_window_end``
    ("HH:MM", local ``scheduler.timezone``) plus
    The window applies every logical day.
    """
    start_raw = settings_spec.resolve_value(
        db, SettingDomain.collections, "enforcement_window_start"
    )
    end_raw = settings_spec.resolve_value(
        db, SettingDomain.collections, "enforcement_window_end"
    )
    start = parse_time(str(start_raw) if start_raw is not None else None)
    end = parse_time(str(end_raw) if end_raw is not None else None)
    local_run_at = to_local(db, run_at or datetime.now(UTC))
    block_reason = window_block_reason(
        local_run_at,
        start_time=start,
        end_time=end,
    )
    return EnforcementWindowDecision(
        inside_window=block_reason is None,
        block_reason=block_reason,
    )


def within_enforcement_window(db: Session, run_at: datetime | None = None) -> bool:
    """Whether the configured local enforcement window is currently open."""
    return resolve_enforcement_window_decision(db, run_at).inside_window
