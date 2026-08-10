"""Named accessors for the provisioning settings. Not a settings subsystem.

Every timeout, retry count and interval that affects OLT/ONT provisioning is a
registered `SettingSpec` in `app/services/settings_spec.py`, resolved through
`settings_spec.resolve_value` like every other setting. What lives here is the
naming: `get_tr069_bootstrap_timeout()` instead of a string key at each call
site, and the two accessors that normalise an enum-ish value.

It used to be more than that, and the difference is the point. A frozen
`ProvisioningDefaults` dataclass held the defaults — a second authority, drifting
from nothing because nothing else knew them. `_get_setting_from_db` queried
`DomainSetting` directly with no tenant filter and no spec, so a stored value
was returned raw: uncoerced, unchecked against bounds, never degraded to a
default it failed. `_get_setting_from_cache` read the unscoped
`settings:{domain}:{key}`, the keyspace that served one organization's settings
to every other in `dotmac_erp`.

Consequences of registering them, beyond closing that: they appear on the admin
settings screen like any other setting, and an operator can see and change them
without a deploy.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain

logger = logging.getLogger(__name__)


#: The sixteen keys this module owns, for anything that needs to enumerate
#: them. NOT a source of defaults — those live on the registered `SettingSpec`s
#: in `app/services/settings_spec.py`, which is what `get_setting` reads.
#:
#: `ProvisioningDefaults` used to hold the defaults here, which made this module
#: a second authority on them: a value changed in one place and not the other
#: drifted silently, and nothing compared the two. It is gone rather than kept
#: in sync.
SETTING_KEYS: tuple[str, ...] = (
    "tr069_bootstrap_timeout_sec",
    "tr069_bootstrap_poll_interval_sec",
    "tr069_task_ready_timeout_sec",
    "tr069_task_ready_poll_interval_sec",
    "pppoe_push_max_attempts",
    "pppoe_push_retry_delay_sec",
    "stale_runtime_hours",
    "olt_write_mode_enabled",
    "pppoe_provisioning_method",
    "verification_interval_sec",
    "verification_staleness_minutes",
    "drift_handling_mode",
    "circuit_breaker_failure_threshold",
    "circuit_breaker_backoff_sec",
    "service_port_pool_min_index",
    "service_port_pool_max_index",
)


def _spec(key: str) -> Any:
    """The registered spec for one of this module's keys, or None."""

    from app.services import settings_spec

    return settings_spec.get_spec(SettingDomain.provisioning, key)


def default_for(key: str) -> Any:
    """The registered default for one of this module's keys.

    The replacement for `DEFAULTS.<name>`. That was a frozen dataclass holding a
    second copy of every default; this reads the one the spec declares, so there
    is nothing to keep in sync.
    """

    spec = _spec(key)
    return spec.default if spec is not None else None


def get_setting(db: Session | None, key: str, default: Any = None) -> Any:
    """One provisioning setting, through the one resolver.

    This module used to be a parallel settings subsystem: a frozen
    `ProvisioningDefaults` dataclass as a second source of defaults, its own
    `DomainSetting` query with NO tenant filter, and its own cache under the
    unscoped `settings:{domain}:{key}` — the keyspace that served one tenant
    another's value in `dotmac_erp`.

    Its sixteen keys are now registered `SettingSpec`s, with the dataclass's
    values copied across exactly, so `resolve_value` answers and the kernel's
    scoped cache fronts it. What that buys beyond the defect: these appear on
    the admin settings screen like every other setting, and a stored value is
    coerced and range-checked by its spec rather than returned raw.

    `db is None` is still supported — several callers resolve a value with no
    session in hand — and now returns the SPEC's default rather than a second
    copy of it. An explicit `default` argument still wins, for a caller that
    wants its own.
    """

    from app.services import settings_spec

    if db is None:
        if default is not None:
            return default
        spec = settings_spec.get_spec(SettingDomain.provisioning, key)
        return spec.default if spec is not None else None

    value = settings_spec.resolve_value(db, SettingDomain.provisioning, key)
    if value is not None:
        return value
    return default


def get_int_setting(db: Session | None, key: str, default: int | None = None) -> int:
    """Get an integer provisioning setting."""
    value = get_setting(db, key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        # The spec's default, not a second copy of it. `SETTING_KEYS` is a
        # tuple of names now — it stopped being a defaults map when the specs
        # took that job.
        if default is not None:
            return default
        spec = _spec(key)
        return int(str(spec.default)) if spec is not None else 0


def get_float_setting(
    db: Session | None, key: str, default: float | None = None
) -> float:
    """Get a float provisioning setting."""
    value = get_setting(db, key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        if default is not None:
            return default
        spec = _spec(key)
        return float(str(spec.default)) if spec is not None else 0.0


def get_bool_setting(db: Session | None, key: str, default: bool | None = None) -> bool:
    """Get a boolean provisioning setting."""
    value = get_setting(db, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


# Convenience functions for specific settings
def get_tr069_bootstrap_timeout(db: Session | None = None) -> int:
    """Get TR-069 bootstrap polling timeout in seconds."""
    return get_int_setting(db, "tr069_bootstrap_timeout_sec")


def get_tr069_bootstrap_poll_interval(db: Session | None = None) -> int:
    """Get TR-069 bootstrap poll interval in seconds."""
    return get_int_setting(db, "tr069_bootstrap_poll_interval_sec")


def get_pppoe_push_max_attempts(db: Session | None = None) -> int:
    """Get maximum PPPoE push retry attempts."""
    return get_int_setting(db, "pppoe_push_max_attempts")


def get_pppoe_push_retry_delay(db: Session | None = None) -> int:
    """Get PPPoE push retry delay in seconds."""
    return get_int_setting(db, "pppoe_push_retry_delay_sec")


def get_stale_runtime_hours(db: Session | None = None) -> int:
    """Get stale runtime data threshold in hours."""
    return get_int_setting(db, "stale_runtime_hours")


def get_olt_write_mode_enabled(db: Session | None = None) -> bool:
    """Return whether provisioning may execute OLT write commands."""
    return get_bool_setting(db, "olt_write_mode_enabled")


def get_pppoe_provisioning_method(db: Session | None = None) -> str:
    """Get PPPoE provisioning method preference.

    Returns one of:
    - "auto": Try OMCI first, fall back to TR-069 on failure (default)
    - "omci": Only use OLT OMCI commands
    - "tr069": Only use TR-069/GenieACS, skip OMCI entirely
    """
    value = get_setting(db, "pppoe_provisioning_method")
    normalized = str(value).strip().lower()
    if normalized in {"omci", "tr069"}:
        return normalized
    return "auto"


# Async verification settings
def get_verification_interval(db: Session | None = None) -> int:
    """Get verification interval in seconds (default 300 = 5 minutes)."""
    return get_int_setting(db, "verification_interval_sec")


def get_verification_staleness_minutes(db: Session | None = None) -> int:
    """Get verification staleness threshold in minutes."""
    return get_int_setting(db, "verification_staleness_minutes")


def get_drift_handling_mode(db: Session | None = None) -> str:
    """Get drift handling mode: 'alert_only' or 'auto_repair'."""
    value = get_setting(db, "drift_handling_mode")
    normalized = str(value).strip().lower()
    if normalized in {"auto_repair"}:
        return normalized
    return "alert_only"


# Circuit-breaker settings
def get_circuit_breaker_threshold(db: Session | None = None) -> int:
    """Get number of failures before circuit opens."""
    return get_int_setting(db, "circuit_breaker_failure_threshold")


def get_circuit_breaker_backoff(db: Session | None = None) -> int:
    """Get circuit breaker backoff period in seconds."""
    return get_int_setting(db, "circuit_breaker_backoff_sec")


# Service-port allocator settings
def get_service_port_pool_range(db: Session | None = None) -> tuple[int, int]:
    """Get service-port pool index range (min, max)."""
    min_idx = get_int_setting(db, "service_port_pool_min_index")
    max_idx = get_int_setting(db, "service_port_pool_max_index")
    return (min_idx, max_idx)
