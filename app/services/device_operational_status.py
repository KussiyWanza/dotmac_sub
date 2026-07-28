"""Binary device-operation resolver owned by ``network.device_state``.

Collectors write timestamped observations. Their age is an internal lifecycle
input that makes another verification due; it is not a third device state and
must never leak into UI filters, badges, or KPIs.

The public projection therefore has exactly two values:

``working``
    Current verification proves operation, including operation with a separate
    impairment/alarm.

``not_working``
    Verification proves failure or the permanent verification lifecycle cannot
    currently confirm operation. ``reason`` distinguishes physical negative
    evidence from a verifier/path failure without inventing a freshness state.

Administrative maintenance/decommissioning remains separate source state. It
prevents alarms, but the operational answer is still ``not_working``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


# Derived UI buckets (keep small — reasons live in `reason`, not extra pills).
class DeviceOperationalState(StrEnum):
    """Complete public vocabulary for the NOC-facing projection."""

    working = "working"
    not_working = "not_working"


DEVICE_OPERATIONAL_STATE_VALUES = tuple(state.value for state in DeviceOperationalState)

WORKING = DeviceOperationalState.working.value
NOT_WORKING = DeviceOperationalState.not_working.value

_VERIFICATION_FAILURE_REASONS = frozenset(
    {
        "verification_error",
        "verification_expired",
        "verification_inconclusive",
        "verification_not_configured",
        "verification_not_started",
        "verification_path_unavailable",
    }
)
_IMPAIRMENT_REASON_PREFIXES = ("active_trigger", "health_degraded", "poll_")
_REASON_LABELS = {
    "active_trigger": "Working with an active alarm",
    "health_degraded": "Working with an impairment",
    "health_unhealthy": "Health verification confirmed failure",
    "observed_not_working": "Verification confirmed failure",
    "observed_working": "Verification confirmed operation",
    "ping_failed": "Ping verification confirmed failure",
    "verification_error": "Unable to verify — verifier error",
    "verification_expired": "Unable to verify — confirmation expired",
    "verification_inconclusive": "Unable to verify — inconclusive result",
    "verification_not_configured": "Unable to verify — verifier not configured",
    "verification_not_started": "Unable to verify — verification not completed",
    "verification_path_unavailable": "Unable to verify — verification path unavailable",
}

# Reuse the customer-facing warmer staleness threshold (selfcare uses the same).
_WARM_STALE_SECONDS = 600


@dataclass(frozen=True)
class OperationalStatus:
    status: str  # one of the UI buckets above
    reason: str  # machine reason, surfaced in the tooltip / used as a filter
    admin_status: str | None  # the raw lifecycle intent, for the secondary text
    mismatch: bool  # admin intent conflicts with observed reality
    mismatch_reason: str | None

    @property
    def presentation(self):
        """Return the shared cross-client semantic presentation contract."""
        from app.services.status_presentation import (
            device_operational_status_presentation,
        )

        return device_operational_status_presentation(self)

    @property
    def label(self) -> str:
        """Compatibility accessor; semantic labels are owned by presentation."""
        return self.presentation.label

    @property
    def alarming(self) -> bool:
        """Only confirmed device-negative evidence opens a device alarm."""

        return (
            self.status == NOT_WORKING
            and not self.verification_failed
            and not self.reason.startswith("admin_")
        )

    @property
    def verification_failed(self) -> bool:
        """Internal verifier outcome; never a public device-state branch."""

        return self.reason in _VERIFICATION_FAILURE_REASONS

    @property
    def impaired(self) -> bool:
        """Working with a separate impairment that may require attention."""

        return self.status == WORKING and self.reason.startswith(
            _IMPAIRMENT_REASON_PREFIXES
        )

    @property
    def reason_label(self) -> str:
        """Human explanation without creating another public state."""

        exact = _REASON_LABELS.get(self.reason)
        if exact:
            return exact
        if self.reason.startswith("admin_"):
            lifecycle = self.reason.removeprefix("admin_").replace("_", " ")
            return f"Administrative lifecycle: {lifecycle}"
        if self.reason.startswith("poll_"):
            poll = self.reason.removeprefix("poll_").replace("_", " ")
            return f"Working; secondary poll {poll}"
        if self.reason.endswith("_linked"):
            base = self.reason.removesuffix("_linked")
            return _REASON_LABELS.get(base, base.replace("_", " ").capitalize())
        return self.reason.replace("_", " ").capitalize()


def warmer_is_stale(now: datetime | None = None) -> bool:
    """Internal verification-due input derived from the warmer heartbeat."""
    try:
        from app.services.app_cache import get_json
        from app.services.topology.live_status import WARM_HEARTBEAT_KEY

        raw = get_json(WARM_HEARTBEAT_KEY)
    except Exception:
        return True
    if not raw:
        return True
    try:
        warmed_at = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return True
    if warmed_at.tzinfo is None:
        warmed_at = warmed_at.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return (now - warmed_at) > timedelta(seconds=_WARM_STALE_SECONDS)


def _enum_value(v) -> str | None:
    if v is None:
        return None
    return getattr(v, "value", v)


def _is_fresh(ts, now: datetime, seconds: int) -> bool:
    """True if timestamp ``ts`` is within ``seconds`` of ``now`` (tz-safe)."""
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts) <= timedelta(seconds=seconds)


# ── Per-type derivers (Phase 2b) ─────────────────────────────────────────────
# OLT and ONT carry their own native liveness fields; unlike NetworkDevice they
# are NOT warmed into `live_status`. Today both tabs render an admin-ish
# value (OLT `runtime_status` = linked device admin status; ONT badge = is_active),
# so deriving from the real telemetry is an accuracy fix, not a re-skin.

# An OLT poll/ping older than this is no longer trustworthy.
_OLT_FRESH_SECONDS = 3600  # 1 hour


def _live_status_to_operational(live: str | None, reason_suffix: str):
    """Map an internal warmer observation to the binary public projection."""
    if live == "up":
        return OperationalStatus(
            WORKING, f"observed_working{reason_suffix}", None, False, None
        )
    if live == "down":
        return OperationalStatus(
            NOT_WORKING, f"observed_not_working{reason_suffix}", None, False, None
        )
    if live == "problem":
        return OperationalStatus(
            WORKING, f"active_trigger{reason_suffix}", None, False, None
        )
    return None


def derive_olt_operational_status(
    olt,
    *,
    linked_live_status: str | None = None,
    warm_stale: bool = False,
    now: datetime | None = None,
):
    """Operational status for an OLT — direct ping/poll telemetry first, then a
    fall-back to the linked monitored device's live_status (reachable if any
    source).

    working = current positive ping or linked positive reachability
    not_working = current negative evidence or verification failure

    The fall-back matters in practice: OLT direct polling can be stale/dead while
    the OLT is still observed via its linked NetworkDevice.
    """
    now = now or datetime.now(UTC)
    last_ping_ok = getattr(olt, "last_ping_ok", None)
    last_ping_at = getattr(olt, "last_ping_at", None)
    poll = _enum_value(getattr(olt, "last_poll_status", None))

    direct_fresh = _is_fresh(last_ping_at, now, _OLT_FRESH_SECONDS)
    if direct_fresh and last_ping_ok is True:
        if poll == "success":
            return OperationalStatus(WORKING, "observed_working", None, False, None)
        return OperationalStatus(WORKING, f"poll_{poll or 'none'}", None, False, None)
    if direct_fresh and last_ping_ok is False:
        return OperationalStatus(NOT_WORKING, "ping_failed", None, False, None)

    # Direct telemetry missing/stale — fall back to the linked observation.
    if linked_live_status and not warm_stale:
        mapped = _live_status_to_operational(linked_live_status, "_linked")
        if mapped is not None:
            return mapped

    if last_ping_ok is None and last_ping_at is None:
        return OperationalStatus(
            NOT_WORKING, "verification_not_started", None, False, None
        )
    return OperationalStatus(NOT_WORKING, "verification_expired", None, False, None)


def derive_ont_operational_status(ont, *, now: datetime | None = None):
    """Operational status for an ONT, reconciling the OLT-reported state with
    ACS informs and last-seen — reachable if *any* source confirms recently.

    This closes the real gap: an ONT the OLT last reported offline but which
    informed ACS minutes ago is working. A due/inconclusive verification is
    not working rather than a third public state.
    """
    from app.services.network.ont_status import resolve_effective_ont_status

    effective = resolve_effective_ont_status(ont, now=now)
    if effective.retry_pending:
        reason = (
            "verification_not_started"
            if effective.reason == "never_seen_retry_pending"
            else "verification_expired"
        )
        return OperationalStatus(
            NOT_WORKING,
            reason,
            None,
            False,
            None,
        )
    return OperationalStatus(
        WORKING if effective.is_online else NOT_WORKING,
        effective.reason,
        None,
        False,
        None,
    )


# Admin lifecycle states that are intentional and must override observation
# (we don't alarm on a device we deliberately took out of service).
_LIFECYCLE_OVERRIDE = {"maintenance", "decommissioned", "retired"}


_ROUTER_STATE_MAP = {
    "online": WORKING,
    "degraded": WORKING,
    "offline": NOT_WORKING,
    "unreachable": NOT_WORKING,
    "maintenance": NOT_WORKING,
}


def derive_nas_operational_status(
    nas, *, linked_device=None, warm_stale: bool = False
) -> OperationalStatus:
    """Derive a NAS/BNG operational status into the binary vocabulary.

    Ownership: when the NAS links a monitored ``NetworkDevice`` (``network_device_id``),
    that device's real liveness is authoritative and we delegate to
    ``derive_operational_status``. Otherwise the NAS admin lifecycle + health
    fields are the only signal. One owner per derived field (SoT).
    """
    if linked_device is not None:
        return derive_operational_status(linked_device, warm_stale=warm_stale)
    admin = _enum_value(getattr(nas, "status", None))
    if admin in ("maintenance", "decommissioned"):
        return OperationalStatus(NOT_WORKING, f"admin_{admin}", admin, False, None)
    if admin == "offline":
        return OperationalStatus(NOT_WORKING, "admin_offline", admin, False, None)
    health = _enum_value(getattr(nas, "health_status", None))
    if health == "unhealthy":
        return OperationalStatus(NOT_WORKING, "health_unhealthy", admin, False, None)
    if health == "degraded":
        return OperationalStatus(WORKING, "health_degraded", admin, False, None)
    if admin == "active":
        return OperationalStatus(
            NOT_WORKING, "verification_not_configured", admin, False, None
        )
    return OperationalStatus(
        NOT_WORKING, "verification_inconclusive", admin, False, None
    )


def derive_router_operational_status(
    router, *, now: datetime | None = None
) -> OperationalStatus:
    """Derive a MikroTik router status from the synced ``RouterStatus`` field."""
    admin = _enum_value(getattr(router, "status", None))
    last_seen = getattr(router, "last_seen_at", None)
    current = now or datetime.now(UTC)
    if admin in {"online", "degraded"} and not _is_fresh(
        last_seen, current, _WARM_STALE_SECONDS
    ):
        return OperationalStatus(
            NOT_WORKING,
            "verification_expired",
            admin,
            False,
            None,
        )
    state = _ROUTER_STATE_MAP.get(admin or "", NOT_WORKING)
    reason = f"router_{admin}" if admin else "verification_not_started"
    return OperationalStatus(state, reason, admin, False, None)


def derive_operational_status(
    device,
    *,
    warm_stale: bool,
    coverage=None,
    now: datetime | None = None,
) -> OperationalStatus:
    """Derive the operational status for one device-like object.

    ``device`` needs ``status`` / ``live_status`` (and ``mgmt_ip`` when coverage
    is supplied), read defensively. ``warm_stale`` is computed once per request.
    ``coverage`` is an internal verification-path observation. When loaded, a
    device whose management IP has no reachable path is not working because the
    lifecycle cannot confirm operation.
    """
    admin = _enum_value(getattr(device, "status", None))
    live = _enum_value(getattr(device, "live_status", None))

    # 1. Lifecycle intent wins — intentional states never become alarms.
    if admin in _LIFECYCLE_OVERRIDE:
        return OperationalStatus(NOT_WORKING, f"admin_{admin}", admin, False, None)

    # 2. No monitoring path (and not positively observed up) -> blind spot, not
    # an outage. Positive 'up' wins (it proves a path), so only gate non-up.
    if (
        coverage is not None
        and getattr(coverage, "loaded", False)
        and live != "up"
        and not coverage.covers(getattr(device, "mgmt_ip", None))
    ):
        return _maybe_mismatch(NOT_WORKING, "verification_path_unavailable", admin)

    # 3/4/5. Native observation time decides whether verification has expired.
    # live_status_at is the status-transition/dwell timestamp, not freshness
    # evidence. A current per-device collector timestamp is stronger than a
    # missing global heartbeat; legacy rows without one fall back to the warmer
    # heartbeat.
    if live is None:
        return _maybe_mismatch(NOT_WORKING, "verification_not_started", admin)
    current = now or datetime.now(UTC)
    from app.services.topology.live_status import live_status_observed_at

    observation_at = live_status_observed_at(device, now=current)
    verification_expired = (
        not _is_fresh(observation_at, current, _WARM_STALE_SECONDS)
        if observation_at is not None
        else warm_stale
    )
    if verification_expired:
        return _maybe_mismatch(NOT_WORKING, "verification_expired", admin)
    if live == "unknown":
        return _maybe_mismatch(NOT_WORKING, "verification_inconclusive", admin)

    # 5. Live observation maps to the UI bucket. "problem" is kept for legacy
    # cached rows from the older warmer that folded active triggers into status.
    if live == "problem":
        return _maybe_mismatch(WORKING, "active_trigger", admin)
    if live == "down":
        return _maybe_mismatch(NOT_WORKING, "observed_not_working", admin)
    if live == "up":
        return _maybe_mismatch(WORKING, "observed_working", admin)
    return _maybe_mismatch(NOT_WORKING, "verification_inconclusive", admin)


def _maybe_mismatch(status: str, reason: str, admin: str | None) -> OperationalStatus:
    """Flag inventory-hygiene conflicts between admin intent and observation."""
    mismatch = False
    mreason: str | None = None
    if admin == "online" and status == NOT_WORKING:
        mismatch, mreason = True, "admin_online_not_working"
    elif admin == "offline" and status == WORKING:
        mismatch, mreason = True, "admin_offline_working"
    return OperationalStatus(status, reason, admin, mismatch, mreason)


# Mismatch reason -> (operator-facing label, owning team). The worklist groups
# by this so inventory-hygiene conflicts route to whoever can fix them.
_MISMATCH_OWNERS = {
    "admin_online_not_working": (
        "Admin says online, device is not working",
        "Field ops",
    ),
    "admin_offline_working": (
        "Admin says offline, device is working",
        "Inventory hygiene",
    ),
}


def mismatch_worklist(
    db,
    *,
    reason: str | None = None,
    search: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict:
    """Devices whose admin intent conflicts with observed reality, grouped by
    reason and routed to an owning team. The operational hygiene queue.

    Read-only; derives operational status on the fly (no persisted state)."""
    from app.models.network_monitoring import NetworkDevice

    devices = list(
        db.query(NetworkDevice).filter(NetworkDevice.is_active.is_(True)).all()
    )
    annotate_operational_status(devices)

    search_filter = (search or "").strip()
    search_match = search_filter.lower()
    groups: dict[str, dict] = {}
    for d in devices:
        op = getattr(d, "operational", None)
        if not op or not op.mismatch or not op.mismatch_reason:
            continue
        if reason and op.mismatch_reason != reason:
            continue
        if search_match and search_match not in " ".join(
            str(value or "").lower()
            for value in (
                d.name,
                getattr(d, "mgmt_ip", None),
                op.admin_status,
                op.status,
                op.label,
                op.mismatch_reason,
            )
        ):
            continue
        label, owner = _MISMATCH_OWNERS.get(
            op.mismatch_reason, (op.mismatch_reason, "Unassigned")
        )
        g = groups.setdefault(
            op.mismatch_reason,
            {"reason": op.mismatch_reason, "label": label, "owner": owner, "rows": []},
        )
        g["rows"].append(
            {
                "id": d.id,
                "name": d.name,
                "mgmt_ip": getattr(d, "mgmt_ip", None),
                "admin": op.admin_status,
                "operational": op.status,
                "status_presentation": op.presentation,
            }
        )

    ordered = sorted(groups.values(), key=lambda g: len(g["rows"]), reverse=True)
    for g in ordered:
        g["count"] = len(g["rows"])
        g["rows"].sort(key=lambda r: (r["name"] or "").lower())

    total = sum(g["count"] for g in ordered)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    end = start + per_page
    flattened = [(group, row) for group in ordered for row in group["rows"]]
    page_groups: list[dict] = []
    page_group_by_reason: dict[str, dict] = {}
    for source_group, row in flattened[start:end]:
        page_group = page_group_by_reason.get(source_group["reason"])
        if page_group is None:
            page_group = {
                key: value for key, value in source_group.items() if key != "rows"
            }
            page_group["rows"] = []
            page_group_by_reason[source_group["reason"]] = page_group
            page_groups.append(page_group)
        page_group["rows"].append(row)

    return {
        "groups": page_groups,
        "total": total,
        "reason_filter": reason,
        "search": search_filter,
        "reasons": list(_MISMATCH_OWNERS),
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        },
    }


def annotate_operational_status(devices, *, now: datetime | None = None) -> None:
    """Attach the transient operational and presentation projections.

    Computes warmer staleness + monitoring coverage once for the whole batch.
    Safe on ORM instances and on stub objects (attributes read defensively).
    """
    warm_stale = warmer_is_stale(now)
    try:
        from app.services.monitoring_coverage import get_coverage

        coverage = get_coverage()
    except Exception:
        coverage = None
    for device in devices:
        try:
            operational = derive_operational_status(
                device,
                warm_stale=warm_stale,
                coverage=coverage,
                now=now,
            )
        except Exception:
            # Never let status derivation break a page render.
            operational = OperationalStatus(
                NOT_WORKING,
                "verification_error",
                _enum_value(getattr(device, "status", None)),
                False,
                None,
            )
        _attach_operational_projection(device, operational)


def _attach_operational_projection(device, operational: OperationalStatus) -> None:
    """Expose one derived state consistently to Jinja and API serializers."""
    device.operational = operational
    device.operational_status = operational.status
    device.operational_reason = operational.reason
    device.status_presentation = operational.presentation
