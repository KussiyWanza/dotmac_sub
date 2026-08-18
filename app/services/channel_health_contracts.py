"""Authoritative monitoring contracts for sensitive inbound channels.

The registry declares operational intent; inbox rows remain authoritative facts.
Every supported external channel is present exactly once and is either enabled
with an enforceable health policy or explicitly disabled with a reason. Runtime
callers resolve the registry through ``settings_spec`` so environment variables,
Prometheus rules, and individual collectors cannot become parallel policy owners.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.timezone import APP_TIMEZONE_NAME

logger = logging.getLogger(__name__)

CHANNEL_HEALTH_CONTRACTS_SETTING = "channel_health_contracts"
SUPPORTED_EXTERNAL_CHANNELS = (
    "email",
    "website_fiber",
    "whatsapp",
    "facebook_messenger",
    "facebook_comment",
    "instagram_dm",
    "instagram_comment",
    "chat_widget",
)
MONITORING_MODES = frozenset({"natural", "synthetic", "hybrid"})
ALERT_SEVERITIES = frozenset({"warning", "critical"})


@dataclass(frozen=True, slots=True)
class ChannelHealthContractReconciliation:
    rows_updated: int
    channels_added: tuple[str, ...]


DEFAULT_CHANNEL_HEALTH_CONTRACTS: dict[str, Any] = {
    "version": 1,
    "channels": [
        {
            "channel": "email",
            "owner": "communications.team_inbox",
            "enabled": False,
            "disabled_reason": "SMTP intake is not activated in this environment",
            "monitoring_mode": "synthetic",
            "max_quiet_seconds": 14_400,
            "synthetic_max_age_seconds": 1_800,
            "active_days": [1, 2, 3, 4, 5, 6, 7],
            "active_start": "00:00",
            "active_end": "00:00",
            "timezone": APP_TIMEZONE_NAME,
            "severity": "critical",
            "runbook": "docs/designs/CHANNEL_OBSERVABILITY.md#email",
        },
        {
            "channel": "website_fiber",
            "owner": "communications.team_inbox",
            "enabled": False,
            "disabled_reason": "Fiber website inquiry intake is not activated in this environment",
            "monitoring_mode": "natural",
            "max_quiet_seconds": 14_400,
            "active_days": [1, 2, 3, 4, 5, 6, 7],
            "active_start": "07:00",
            "active_end": "23:00",
            "timezone": APP_TIMEZONE_NAME,
            "severity": "warning",
            "runbook": "docs/designs/CHANNEL_OBSERVABILITY.md#fiber-website",
        },
        {
            "channel": "whatsapp",
            "owner": "communications.team_inbox",
            "enabled": False,
            "disabled_reason": "WhatsApp intake is not activated in this environment",
            "monitoring_mode": "natural",
            "max_quiet_seconds": 1_800,
            "active_days": [1, 2, 3, 4, 5, 6, 7],
            "active_start": "07:00",
            "active_end": "23:00",
            "timezone": APP_TIMEZONE_NAME,
            "severity": "critical",
            "runbook": "docs/designs/CHANNEL_OBSERVABILITY.md#whatsapp",
        },
        {
            "channel": "facebook_messenger",
            "owner": "communications.team_inbox",
            "enabled": False,
            "disabled_reason": "Messenger intake is not activated in this environment",
            "monitoring_mode": "natural",
            "max_quiet_seconds": 14_400,
            "active_days": [1, 2, 3, 4, 5, 6, 7],
            "active_start": "07:00",
            "active_end": "23:00",
            "timezone": APP_TIMEZONE_NAME,
            "severity": "warning",
            "runbook": "docs/designs/CHANNEL_OBSERVABILITY.md#meta-social",
        },
        {
            "channel": "facebook_comment",
            "owner": "communications.team_inbox",
            "enabled": False,
            "disabled_reason": "Facebook comment intake is not activated in this environment",
            "monitoring_mode": "natural",
            "max_quiet_seconds": 14_400,
            "active_days": [1, 2, 3, 4, 5, 6, 7],
            "active_start": "07:00",
            "active_end": "23:00",
            "timezone": APP_TIMEZONE_NAME,
            "severity": "warning",
            "runbook": "docs/designs/CHANNEL_OBSERVABILITY.md#meta-social",
        },
        {
            "channel": "instagram_dm",
            "owner": "communications.team_inbox",
            "enabled": False,
            "disabled_reason": "Instagram intake is not activated in this environment",
            "monitoring_mode": "natural",
            "max_quiet_seconds": 14_400,
            "active_days": [1, 2, 3, 4, 5, 6, 7],
            "active_start": "07:00",
            "active_end": "23:00",
            "timezone": APP_TIMEZONE_NAME,
            "severity": "warning",
            "runbook": "docs/designs/CHANNEL_OBSERVABILITY.md#meta-social",
        },
        {
            "channel": "instagram_comment",
            "owner": "communications.team_inbox",
            "enabled": False,
            "disabled_reason": "Instagram comment intake is not activated in this environment",
            "monitoring_mode": "natural",
            "max_quiet_seconds": 14_400,
            "active_days": [1, 2, 3, 4, 5, 6, 7],
            "active_start": "07:00",
            "active_end": "23:00",
            "timezone": APP_TIMEZONE_NAME,
            "severity": "warning",
            "runbook": "docs/designs/CHANNEL_OBSERVABILITY.md#meta-social",
        },
        {
            "channel": "chat_widget",
            "owner": "communications.team_inbox",
            "enabled": False,
            "disabled_reason": "Live chat intake is not activated in this environment",
            "monitoring_mode": "natural",
            "max_quiet_seconds": 3_600,
            "active_days": [1, 2, 3, 4, 5, 6, 7],
            "active_start": "07:00",
            "active_end": "23:00",
            "timezone": APP_TIMEZONE_NAME,
            "severity": "critical",
            "runbook": "docs/designs/CHANNEL_OBSERVABILITY.md#chat-widget",
        },
    ],
}


class ChannelHealthContractError(ValueError):
    """Raised when the authoritative registry cannot be enforced safely."""


@dataclass(frozen=True)
class ChannelHealthContract:
    channel: str
    owner: str
    enabled: bool
    disabled_reason: str | None
    monitoring_mode: str
    max_quiet_seconds: int
    synthetic_max_age_seconds: int | None
    active_days: frozenset[int]
    active_start: time
    active_end: time
    timezone: str
    severity: str
    runbook: str

    @property
    def requires_natural_traffic(self) -> bool:
        return self.monitoring_mode in {"natural", "hybrid"}

    @property
    def requires_synthetic_probe(self) -> bool:
        return self.monitoring_mode in {"synthetic", "hybrid"}

    @property
    def is_continuous(self) -> bool:
        return (
            self.active_days == frozenset(range(1, 8))
            and self.active_start == self.active_end
        )


def _required_text(item: dict[str, Any], key: str) -> str:
    value = str(item.get(key) or "").strip()
    if not value:
        raise ChannelHealthContractError(f"Channel contract requires {key}")
    return value


def _bounded_seconds(item: dict[str, Any], key: str) -> int:
    try:
        value = int(str(item.get(key)))
    except (TypeError, ValueError):
        raise ChannelHealthContractError(
            f"Channel contract {key} must be an integer"
        ) from None
    if value < 300 or value > 7 * 86_400:
        raise ChannelHealthContractError(
            f"Channel contract {key} must be between 300 and 604800"
        )
    return value


def _clock(value: object, *, key: str) -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError:
        raise ChannelHealthContractError(
            f"Channel contract {key} must be HH:MM"
        ) from None


def _parse_contract(item: object) -> ChannelHealthContract:
    if not isinstance(item, dict):
        raise ChannelHealthContractError("Each channel contract must be an object")
    channel = _required_text(item, "channel")
    if channel not in SUPPORTED_EXTERNAL_CHANNELS:
        raise ChannelHealthContractError(f"Unsupported channel contract: {channel}")
    owner = _required_text(item, "owner")
    if owner != "communications.team_inbox":
        raise ChannelHealthContractError(
            f"Unsupported channel fact owner for {channel}: {owner}"
        )
    enabled = item.get("enabled")
    if not isinstance(enabled, bool):
        raise ChannelHealthContractError(
            f"Channel contract enabled must be boolean: {channel}"
        )
    disabled_reason = str(item.get("disabled_reason") or "").strip() or None
    if not enabled and not disabled_reason:
        raise ChannelHealthContractError(
            f"Disabled channel contract requires a reason: {channel}"
        )
    monitoring_mode = _required_text(item, "monitoring_mode")
    if monitoring_mode not in MONITORING_MODES:
        raise ChannelHealthContractError(
            f"Unsupported monitoring mode for {channel}: {monitoring_mode}"
        )
    max_quiet_seconds = _bounded_seconds(item, "max_quiet_seconds")
    synthetic_max_age_seconds = None
    if monitoring_mode in {"synthetic", "hybrid"}:
        synthetic_max_age_seconds = _bounded_seconds(item, "synthetic_max_age_seconds")
    raw_days = item.get("active_days")
    if not isinstance(raw_days, list) or not raw_days:
        raise ChannelHealthContractError(
            f"Channel contract active_days must be a non-empty list: {channel}"
        )
    try:
        if any(isinstance(day, bool) for day in raw_days):
            raise ValueError
        active_days = frozenset(int(day) for day in raw_days)
    except (TypeError, ValueError):
        raise ChannelHealthContractError(
            f"Channel contract active_days must contain integers: {channel}"
        ) from None
    if not active_days.issubset(range(1, 8)):
        raise ChannelHealthContractError(
            f"Channel contract active_days must use ISO weekdays 1-7: {channel}"
        )
    timezone = _required_text(item, "timezone")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise ChannelHealthContractError(
            f"Unknown channel contract timezone for {channel}: {timezone}"
        ) from None
    severity = _required_text(item, "severity")
    if severity not in ALERT_SEVERITIES:
        raise ChannelHealthContractError(
            f"Unsupported channel alert severity for {channel}: {severity}"
        )
    return ChannelHealthContract(
        channel=channel,
        owner=owner,
        enabled=enabled,
        disabled_reason=disabled_reason,
        monitoring_mode=monitoring_mode,
        max_quiet_seconds=max_quiet_seconds,
        synthetic_max_age_seconds=synthetic_max_age_seconds,
        active_days=active_days,
        active_start=_clock(item.get("active_start"), key="active_start"),
        active_end=_clock(item.get("active_end"), key="active_end"),
        timezone=timezone,
        severity=severity,
        runbook=_required_text(item, "runbook"),
    )


def parse_channel_health_contracts(raw: object) -> tuple[ChannelHealthContract, ...]:
    if (
        not isinstance(raw, dict)
        or isinstance(raw.get("version"), bool)
        or raw.get("version") != 1
    ):
        raise ChannelHealthContractError(
            "Channel health registry must be a version 1 object"
        )
    items = raw.get("channels")
    if not isinstance(items, list):
        raise ChannelHealthContractError("Channel health registry requires channels")
    contracts = tuple(_parse_contract(item) for item in items)
    channels = [contract.channel for contract in contracts]
    duplicates = sorted(
        {channel for channel in channels if channels.count(channel) > 1}
    )
    if duplicates:
        raise ChannelHealthContractError(
            f"Duplicate channel health contracts: {', '.join(duplicates)}"
        )
    missing = sorted(set(SUPPORTED_EXTERNAL_CHANNELS) - set(channels))
    if missing:
        raise ChannelHealthContractError(
            f"Missing channel health contracts: {', '.join(missing)}"
        )
    return tuple(sorted(contracts, key=lambda contract: contract.channel))


def _backfilled_registry(raw: object) -> tuple[object, tuple[str, ...]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("channels"), list):
        return raw, ()
    items = raw["channels"]
    present = {item.get("channel") for item in items if isinstance(item, dict)}
    missing = tuple(
        channel for channel in SUPPORTED_EXTERNAL_CHANNELS if channel not in present
    )
    if not missing:
        return raw, ()
    defaults = {
        item["channel"]: item for item in DEFAULT_CHANNEL_HEALTH_CONTRACTS["channels"]
    }
    return (
        {
            **raw,
            "channels": [*items, *(deepcopy(defaults[channel]) for channel in missing)],
        },
        missing,
    )


@lru_cache(maxsize=16)
def _warn_backfilled_defaults_once(missing: tuple[str, ...]) -> None:
    logger.warning(
        "channel_health_contracts_backfilled_defaults: %s", ", ".join(missing)
    )


def backfill_missing_supported_channels(raw: object) -> object:
    """Fill in channels added to the supported set after a registry was stored.

    A stored registry that predates a newly supported channel would otherwise
    fail closed and take the whole channel-observability domain to ``error``.
    Each missing channel inherits its shipped default contract, which is
    explicitly disabled with a reason — operational intent stays declared and
    the operator can enable it deliberately. Anything structurally malformed
    passes through untouched so parsing still rejects it.
    """
    backfilled, missing = _backfilled_registry(raw)
    if missing:
        _warn_backfilled_defaults_once(missing)
    return backfilled


def reconcile_persisted_channel_health_contracts(
    db: Session,
) -> ChannelHealthContractReconciliation:
    """Persist disabled defaults missing from otherwise valid stored registries."""
    from app.models.domain_settings import DomainSetting
    from app.services.setting_history import (
        SettingChangeContext,
        reset_change_context,
        set_change_context,
    )

    rows = (
        db.query(DomainSetting)
        .filter(
            DomainSetting.domain == SettingDomain.network_monitoring,
            DomainSetting.key == CHANNEL_HEALTH_CONTRACTS_SETTING,
            DomainSetting.is_active.is_(True),
        )
        .all()
    )
    rows_updated = 0
    channels_added: set[str] = set()
    for row in rows:
        backfilled, missing = _backfilled_registry(row.value_json)
        if not missing:
            continue
        if not isinstance(backfilled, dict):
            raise ChannelHealthContractError(
                "Backfilled channel health registry must be an object"
            )
        try:
            parse_channel_health_contracts(backfilled)
        except ChannelHealthContractError:
            # Malformed operator input must continue to fail closed rather than
            # being silently rewritten by startup reconciliation.
            logger.warning(
                "channel_health_contract_reconciliation_skipped_invalid",
                extra={"setting_id": str(row.id)},
            )
            continue
        row.value_json = backfilled
        rows_updated += 1
        channels_added.update(missing)
    if rows_updated:
        token = set_change_context(
            SettingChangeContext(
                reason="Append disabled defaults for newly supported inbox channels"
            )
        )
        try:
            db.commit()
        finally:
            reset_change_context(token)
        logger.info(
            "channel_health_contracts_reconciled",
            extra={
                "rows_updated": rows_updated,
                "channels_added": sorted(channels_added),
            },
        )
    return ChannelHealthContractReconciliation(
        rows_updated=rows_updated,
        channels_added=tuple(sorted(channels_added)),
    )


def load_channel_health_contracts(db: Session) -> tuple[ChannelHealthContract, ...]:
    from app.services.settings_spec import resolve_value

    raw = resolve_value(
        db,
        SettingDomain.network_monitoring,
        CHANNEL_HEALTH_CONTRACTS_SETTING,
    )
    return parse_channel_health_contracts(backfill_missing_supported_channels(raw))


def _at_local_date(local: datetime, clock: time, *, days: int = 0) -> datetime:
    target_date = local.date() + timedelta(days=days)
    return datetime.combine(target_date, clock, tzinfo=local.tzinfo)


def active_window_elapsed_seconds(
    contract: ChannelHealthContract,
    *,
    now: datetime | None = None,
) -> tuple[bool, float | None]:
    """Return whether policy is active and elapsed seconds in this window.

    ``None`` elapsed means a true 24x7 contract: there is no daily boundary at
    which a long-running silence should be forgiven.
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    local = moment.astimezone(ZoneInfo(contract.timezone))
    if contract.is_continuous:
        return True, None

    weekday = local.isoweekday()
    clock = local.timetz().replace(tzinfo=None)
    start = contract.active_start
    end = contract.active_end
    if start < end:
        if weekday not in contract.active_days or not (start <= clock < end):
            return False, 0.0
        window_start = _at_local_date(local, start)
    else:
        if clock >= start and weekday in contract.active_days:
            window_start = _at_local_date(local, start)
        else:
            previous_weekday = 7 if weekday == 1 else weekday - 1
            if clock >= end or previous_weekday not in contract.active_days:
                return False, 0.0
            window_start = _at_local_date(local, start, days=-1)
    return True, max(0.0, (local - window_start).total_seconds())


def effective_age_seconds(
    contract: ChannelHealthContract,
    *,
    observed_at: datetime | None,
    now: datetime,
    max_age_seconds: int | None = None,
) -> float:
    """Return age without charging a channel for scheduled inactive time."""
    active, window_elapsed = active_window_elapsed_seconds(contract, now=now)
    if not active:
        return 0.0
    if observed_at is None:
        if window_elapsed is None:
            threshold = max_age_seconds or contract.max_quiet_seconds
            return float(threshold + 1)
        return window_elapsed
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    age = max(0.0, (now - observed_at).total_seconds())
    return age if window_elapsed is None else min(age, window_elapsed)
