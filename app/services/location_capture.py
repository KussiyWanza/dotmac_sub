"""Location capture — the callers that drive the reconciler.

``geocode_reconciler.capture_location`` adjudicates a pin and writes ledger
rows for what reconciles cleanly. This module is where the three real surfaces
reach it — field GPS at install, the customer portal, and an agent — and it
owns the two things those surfaces share that the reconciler does not:

* **when to ask** — a subscriber whose location is already captured and fresh
  should not be prompted; ``subscriber_data_completeness`` answers that, and
  the portal prompt honours a snooze;
* **the feature gate** — the whole capture flow is behind ``loyalty.campaigns``
  and its per-surface sub-controls, default off.

It writes only the ledger (through ``capture_location``) and the prompt snooze
state. It never touches ``Subscriber`` columns — projecting a captured fact
onto the subscriber's own fields stays the subscriber owner's job. That
boundary is why declaring this owner does not make it a second writer of
customer profile data.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.location_capture_prompt import LocationCapturePromptState
from app.models.subscriber import Subscriber
from app.services import control_registry, settings_spec
from app.services import geocode_reconciler as reconciler
from app.services import service_address as service_address_service
from app.services import subscriber_data_completeness as completeness
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

# Feature gate (docs/designs/LOYALTY_AND_CAPTURE.md). All default OFF.
_CONTROL_CAMPAIGNS = "loyalty.campaigns"
_CONTROL_CAPTURE_PROMPT = "loyalty.capture_prompt"

SOURCE_FIELD_GPS = reconciler.SOURCE_FIELD_GPS
SOURCE_CUSTOMER_PORTAL = reconciler.SOURCE_CUSTOMER_PORTAL
SOURCE_AGENT = reconciler.SOURCE_AGENT
_SUPPORTED_SOURCES = frozenset({SOURCE_FIELD_GPS, SOURCE_CUSTOMER_PORTAL, SOURCE_AGENT})
_PROMPT_SOURCES = frozenset({SOURCE_CUSTOMER_PORTAL, SOURCE_AGENT})

_SNOOZE_DAYS_KEY = "loyalty_capture_prompt_snooze_days"
_DEFAULT_SNOOZE_DAYS = 30
_SERVICE_LOCATION_REQUIRED_KEY = "service_location_required"


class LocationCaptureDisabled(RuntimeError):
    """The requested location-capture surface is not enabled."""


def service_location_requirement_enabled(db: Session) -> bool:
    """Return the operator-controlled direct portal location requirement."""
    try:
        value = settings_spec.resolve_value(
            db, SettingDomain.subscriber, _SERVICE_LOCATION_REQUIRED_KEY
        )
    except Exception:
        logger.warning(
            "service location requirement setting could not be resolved", exc_info=True
        )
        return False
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def service_location_complete(db: Session, subscriber_id: str) -> bool:
    """Whether the canonical service Address has a usable map coordinate."""
    address = service_address_service.service_address(db, subscriber_id)
    if address is None or address.latitude is None or address.longitude is None:
        return False
    try:
        latitude = float(address.latitude)
        longitude = float(address.longitude)
    except (TypeError, ValueError):
        return False
    return -90 <= latitude <= 90 and -180 <= longitude <= 180


def requires_service_location_update(db: Session, subscriber_id: str) -> bool:
    """Single policy decision used by the portal gate and banner.

    A read failure fails open so a database/transient issue cannot lock every
    customer out of the portal. The exception is logged for operations.
    """
    if not service_location_requirement_enabled(db):
        return False
    try:
        return not service_location_complete(db, subscriber_id)
    except Exception:
        logger.exception(
            "service location completeness check failed for subscriber %s",
            subscriber_id,
        )
        return False


def prompt_enabled(db: Session) -> bool:
    """Whether portal/agent capture is enabled by both default-off controls."""
    return control_registry.is_enabled(
        db, _CONTROL_CAMPAIGNS
    ) and control_registry.is_enabled(db, _CONTROL_CAPTURE_PROMPT)


def _require_capture_enabled(db: Session, source: str) -> None:
    if source not in _SUPPORTED_SOURCES:
        raise ValueError(f"Unsupported location capture source: {source}")
    if not control_registry.is_enabled(db, _CONTROL_CAMPAIGNS):
        raise LocationCaptureDisabled("Location capture is disabled")
    if source in _PROMPT_SOURCES and not control_registry.is_enabled(
        db, _CONTROL_CAPTURE_PROMPT
    ):
        raise LocationCaptureDisabled("Prompted location capture is disabled")


def _setting_int(db: Session, key: str, default: int) -> int:
    from app.models.domain_settings import SettingDomain
    from app.services.settings_spec import resolve_value

    try:
        value = resolve_value(db, SettingDomain.subscriber, key)
    except Exception:  # pragma: no cover - best-effort settings resolution
        return default
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── capture ──────────────────────────────────────────────────────────────────


def capture(
    db: Session,
    subscriber_id: str,
    *,
    lat: float,
    lng: float,
    accuracy_m: float | None = None,
    source: str,
    actor_id: str | None = None,
    actor_name: str | None = None,
    claimed_state: str | None = None,
    claimed_lga: str | None = None,
    claimed_postcode: str | None = None,
) -> reconciler.CaptureResult:
    """Reconcile a pin and capture what agrees. A thin owner-boundary around
    ``geocode_reconciler.capture_location``: the reconciler decides what is
    honest to write; this is the declared entrypoint the surfaces call.

    The default-off controls are enforced here, at the owner boundary, so a
    direct route call cannot bypass rollout policy. The reconciler never
    raises for an unreachable geocoder; that simply yields ``unverifiable``.
    """
    _require_capture_enabled(db, source)
    return reconciler.capture_location(
        db,
        subscriber_id,
        lat=lat,
        lng=lng,
        accuracy_m=accuracy_m,
        source=source,
        actor_id=actor_id,
        actor_name=actor_name,
        claimed_state=claimed_state,
        claimed_lga=claimed_lga,
        claimed_postcode=claimed_postcode,
    )


def capture_from_field_arrival(
    db: Session,
    *,
    subscriber_id: str,
    lat: float | None,
    lng: float | None,
    accuracy_m: float | None,
    technician_actor_id: str | None,
    technician_name: str | None,
) -> reconciler.CaptureResult | None:
    """Capture when a technician arrives at a customer premises.

    The tech is physically at the service address, so the pin is the strongest
    evidence we ever get and nobody is asked anything. Best-effort: a capture
    failure must never break the field transition that triggered it. Returns
    None when disabled, ungated, or lacking a fix.
    """
    if lat is None or lng is None:
        return None
    try:
        # Keep a geocoder/ledger failure from invalidating the work-order
        # transaction that owns the arrival transition.
        with db.begin_nested():
            return capture(
                db,
                subscriber_id,
                lat=lat,
                lng=lng,
                accuracy_m=accuracy_m,
                source=SOURCE_FIELD_GPS,
                actor_id=technician_actor_id,
                actor_name=technician_name,
            )
    except LocationCaptureDisabled:
        return None
    except Exception:  # pragma: no cover - capture must not break the arrival
        logger.warning(
            "location capture from field arrival failed for subscriber %s",
            subscriber_id,
            exc_info=True,
        )
        return None


# ── the portal / agent prompt ────────────────────────────────────────────────


def should_prompt(
    db: Session,
    subscriber: Subscriber | None,
    *,
    ignore_snooze: bool = False,
    now: datetime | None = None,
) -> bool:
    """Whether to show the confirm-or-correct prompt for this subscriber.

    True only when the feature is on AND the subscriber's NCC location is
    absent or merely inferred (a captured, fresh location needs no prompt) AND
    — unless ``ignore_snooze`` — no active snooze is in effect. Payment passes
    ``ignore_snooze=True``: a snooze pauses browsing nags, not the one flow
    where we have the customer's attention.
    """
    if subscriber is None:
        return False
    if not prompt_enabled(db):
        return False
    states = completeness.state_of(db, subscriber, completeness.Purpose.ncc_filing)
    if not any(s.needs_revalidation for s in states):
        return False
    if ignore_snooze:
        return True
    moment = now or datetime.now(UTC)
    snooze = db.get(LocationCapturePromptState, coerce_uuid(str(subscriber.id)))
    if snooze is not None and snooze.snoozed_until is not None:
        until = snooze.snoozed_until
        until = until if until.tzinfo else until.replace(tzinfo=UTC)
        if until > moment:
            return False
    return True


def mark_prompted(
    db: Session, subscriber_id: str, *, now: datetime | None = None
) -> None:
    """Record that the prompt was shown (for cadence/telemetry)."""
    if not prompt_enabled(db):
        raise LocationCaptureDisabled("Prompted location capture is disabled")
    moment = now or datetime.now(UTC)
    state = _prompt_state(db, subscriber_id)
    state.last_prompted_at = moment


def snooze_prompt(
    db: Session, subscriber_id: str, *, now: datetime | None = None
) -> LocationCapturePromptState:
    """The customer clicked "remind me later". Hides the prompt for the
    configured window; payment still overrides it."""
    if not prompt_enabled(db):
        raise LocationCaptureDisabled("Prompted location capture is disabled")
    moment = now or datetime.now(UTC)
    days = _setting_int(db, _SNOOZE_DAYS_KEY, _DEFAULT_SNOOZE_DAYS)
    state = _prompt_state(db, subscriber_id)
    state.snoozed_until = moment + timedelta(days=max(days, 0))
    state.last_prompted_at = moment
    state.dismiss_count = (state.dismiss_count or 0) + 1
    db.flush()
    return state


def _prompt_state(db: Session, subscriber_id: str) -> LocationCapturePromptState:
    sub_uuid = coerce_uuid(str(subscriber_id))
    state = db.get(LocationCapturePromptState, sub_uuid)
    if state is None:
        state = LocationCapturePromptState(subscriber_id=sub_uuid)
        db.add(state)
        db.flush()
    return state


def prompt_context(
    db: Session,
    subscriber: Subscriber | None,
    *,
    ignore_snooze: bool = False,
) -> dict[str, object] | None:
    """What a template needs to render the prompt, or None if it should not
    show. Confirm-or-correct: the current (possibly inferred) field states are
    shown so the customer confirms rather than starts blank."""
    if not should_prompt(db, subscriber, ignore_snooze=ignore_snooze):
        return None
    assert subscriber is not None
    states = completeness.state_of(db, subscriber, completeness.Purpose.ncc_filing)
    return {
        "subscriber_id": str(subscriber.id),
        "fields": [
            {
                "key": s.key.value,
                "label": s.label,
                "value": s.value,
                "provenance": s.provenance.value,
                "needs_revalidation": s.needs_revalidation,
            }
            for s in states
        ],
        "claimed_state": service_address_service.address_parts(subscriber).region or "",
        "claimed_lga": service_address_service.address_parts(subscriber).lga or "",
        "claimed_postcode": (
            service_address_service.address_parts(subscriber).postal_code or ""
        ),
        "snooze_allowed": not ignore_snooze,
    }
