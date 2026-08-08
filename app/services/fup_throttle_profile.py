"""Owner of *how hard* a FUP throttle bites.

``fup_rules.speed_reduction_percent`` is the source of truth for throttle depth
(docs/PLAN_FAMILY_ARCHITECTURE.md §1, §12). Until this module existed the field
was written on every throttle rule and read arithmetically by nothing: the
enforcement handler moved every throttled subscriber onto one globally
configured RADIUS profile, ``usage.fup_throttle_radius_profile_id``. In
production that profile is *FUP Throttle 1Mbps*, so a 6 Mbps Homeflex Starter
and a 50 Mbps Premium were both cut to 1 Mbps — an 83% cut for one and a 98%
cut for the other, from a field claiming 90% for both.

The design requires the throttle to be **relative to the plan**: 50% of the
subscriber's own rate. This module computes that rate and resolves the RADIUS
profile expressing it.

Why derive from the *effective profile* rather than the offer's Mbps columns:
the effective profile is what the subscriber is actually running at, honouring
credential- and subscription-level overrides (``_resolve_effective_profile``).
Throttling to half of a nominal offer speed the subscriber was never on would
be a rate they never had. It also keeps the arithmetic entirely inside kbps —
no Mbps-to-kbps conversion, and so no exposure to the 1000-vs-1024 ambiguity
that already produced one rate-limit defect in this codebase.

**This module is the only writer of ``fup-throttle-*`` RADIUS profiles.** They
are a derived projection, not catalog data: keyed deterministically on the rate
pair, rebuildable by deleting them, and never edited by hand. Anything that
wants a different throttle changes ``speed_reduction_percent`` on the rule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import NasVendor, RadiusProfile, Subscription
from app.models.fup import FupRule
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event

logger = logging.getLogger(__name__)

# A throttle is a penalty, not a disconnection. Below roughly this rate a
# connection stops being usable for the things that make a customer notice they
# still have service at all, so a very deep reduction on an already-slow plan
# is floored rather than honoured exactly.
MIN_THROTTLE_KBPS = 512


class FupThrottleRateError(DomainError):
    """The rule's throttle depth cannot produce a rate (adapter: HTTP 400)."""


def _error(suffix: str, message: str, **details) -> FupThrottleRateError:
    return FupThrottleRateError(
        code=f"access.fup_throttle_rate.{suffix}",
        message=message,
        details=details or None,
    )


@dataclass(frozen=True, slots=True)
class ThrottleProfileDecision:
    """Which profile to apply, and how it was arrived at.

    ``derived`` is False when the rule or the subscriber's profile could not
    supply a rate and the globally configured fallback was used — the caller
    logs that, because a fallback silently standing in for a proportional
    throttle is the defect this module exists to remove.
    """

    profile_id: UUID
    derived: bool
    reason: str
    download_kbps: int | None = None
    upload_kbps: int | None = None


def reduced_kbps(full_kbps: int, reduction_percent: float) -> int:
    """Apply a percentage *reduction* to a rate, floored at a usable minimum.

    ``speed_reduction_percent`` is a reduction, not a target: 90 means "cut by
    90%", leaving a tenth. The design's "throttle to 50%" is therefore
    ``speed_reduction_percent = 50``, where the two readings happen to coincide
    — which is exactly why the distinction has to be stated somewhere rather
    than left to a reader's guess.

    Refuses a percentage outside ``0 < pct < 100``. The rule engine validates
    that range on write, but an imported or hand-edited row can hold 0 (a
    no-op dressed as enforcement) or 100 (a disconnection dressed as a
    throttle). Neither should quietly become a RADIUS profile.
    """
    pct = float(reduction_percent)
    if not 0 < pct < 100:
        raise _error(
            "invalid_reduction_percent",
            "A FUP throttle reduction must be between 1 and 99 percent.",
            speed_reduction_percent=pct,
        )
    if full_kbps <= 0:
        raise _error(
            "invalid_full_rate",
            "Cannot reduce a non-positive rate.",
            full_kbps=full_kbps,
        )
    remaining = full_kbps * (100.0 - pct) / 100.0
    return max(int(round(remaining)), MIN_THROTTLE_KBPS)


def _profile_code(download_kbps: int, upload_kbps: int) -> str:
    return f"fup-throttle-{download_kbps}k-{upload_kbps}k"


def _profile_name(download_kbps: int, upload_kbps: int) -> str:
    return f"FUP Throttle {download_kbps}k/{upload_kbps}k"


def _rate_limit(download_kbps: int, upload_kbps: int) -> str:
    # MikroTik Rate-Limit is rx/tx, and rx is the subscriber's UPLOAD as seen
    # at the NAS. Upload first. Getting this backwards silently swaps a
    # customer's up and down rates.
    return f"{upload_kbps}k/{download_kbps}k"


def _repair_derived_profile(
    profile: RadiusProfile, download_kbps: int, upload_kbps: int
) -> list[str]:
    """Force a reused derived profile back to what its code says it is.

    Returns the names of the fields that had drifted, so the caller can record
    that a repair happened rather than let it pass silently.
    """
    expected = {
        "download_speed": download_kbps,
        "upload_speed": upload_kbps,
        "mikrotik_rate_limit": _rate_limit(download_kbps, upload_kbps),
        "is_active": True,
    }
    drifted = [
        field
        for field, value in expected.items()
        if getattr(profile, field, None) != value
    ]
    for field in drifted:
        setattr(profile, field, expected[field])
    return drifted


def resolve_or_create_profile(
    db: Session, *, download_kbps: int, upload_kbps: int
) -> RadiusProfile:
    """The profile expressing this exact throttled rate pair, creating it once.

    Keyed on ``code``, which carries a unique constraint, so concurrent
    enforcement of two subscribers landing on the same rate cannot fork the
    projection.
    """
    code = _profile_code(download_kbps, upload_kbps)
    existing = db.query(RadiusProfile).filter(RadiusProfile.code == code).one_or_none()
    if existing is not None:
        # These rows are a derived projection and this module is their only
        # writer, so a row that no longer expresses its own code has drifted —
        # hand-edited, or deactivated. Returning it unchecked made the cache the
        # only copy of truth: a profile named for 5000k could be projecting
        # anything at all to the NAS, for every subscriber that rate throttles.
        # Repairing here is the reconciler this projection was missing.
        drifted = _repair_derived_profile(existing, download_kbps, upload_kbps)
        if drifted:
            logger.warning(
                "Repaired drifted derived FUP throttle profile %s (%s): %s",
                code,
                existing.id,
                ", ".join(drifted),
            )
        return existing

    profile = RadiusProfile(
        name=_profile_name(download_kbps, upload_kbps),
        code=code,
        vendor=NasVendor.mikrotik,
        description=(
            "Derived FUP throttle profile. Written only by "
            "app/services/fup_throttle_profile.py — edit the rule's "
            "speed_reduction_percent instead of this row."
        ),
        download_speed=download_kbps,
        upload_speed=upload_kbps,
        mikrotik_rate_limit=_rate_limit(download_kbps, upload_kbps),
        is_active=True,
    )
    db.add(profile)
    db.flush()
    # Creating a RADIUS profile is a material act — a new row that will be
    # projected to the NAS. Reuse is a read and emits nothing; only the first
    # appearance of a rate is evidence worth keeping.
    emit_event(
        db,
        EventType.fup_throttle_profile_derived,
        {
            "profile_id": str(profile.id),
            "code": code,
            "download_kbps": download_kbps,
            "upload_kbps": upload_kbps,
        },
    )
    return profile


def resolve_fup_throttle_profile(
    db: Session,
    *,
    subscription: Subscription,
    rule_id: str | UUID | None,
    fallback_profile_id: UUID | None,
) -> ThrottleProfileDecision:
    """Which RADIUS profile expresses ``rule_id``'s throttle for this subscriber.

    Derivation from the subscriber's own rate is the primary path. The globally
    configured profile is consulted only when derivation cannot produce a rate,
    and it may legitimately be unset — deriving a proportional throttle does not
    need one. When derivation fails AND no fallback exists there is no throttle
    to apply, and that is raised rather than papered over: silently returning
    "no profile" would leave a breaching subscriber at full speed while the
    sweep counted the enforcement as done.
    """
    from app.services.enforcement import _resolve_effective_profile

    def _fallback(reason: str) -> ThrottleProfileDecision:
        if fallback_profile_id is None:
            raise _error(
                "no_throttle_profile_available",
                (
                    "This FUP throttle could not be derived from the "
                    "subscriber's rate and no global fallback profile is "
                    "configured."
                ),
                reason=reason,
                subscription_id=str(subscription.id),
            )
        return ThrottleProfileDecision(
            profile_id=fallback_profile_id,
            derived=False,
            reason=reason,
        )

    if rule_id is None:
        return _fallback("no rule on the enforcement event")

    rule = db.get(FupRule, UUID(str(rule_id)))
    if rule is None or rule.speed_reduction_percent is None:
        return _fallback("rule carries no speed_reduction_percent")

    full = _resolve_effective_profile(db, subscription)
    if full is None or not full.download_speed or not full.upload_speed:
        return _fallback("subscriber has no full-speed profile to reduce")

    # A rule holding an out-of-range percentage is a data defect, not a missing
    # value. Enforcement still has to throttle, so it degrades to the global
    # profile with the defect named — refusing here would leave the subscriber
    # at full speed, which is the one outcome worse than a blunt throttle.
    try:
        download = reduced_kbps(full.download_speed, rule.speed_reduction_percent)
        upload = reduced_kbps(full.upload_speed, rule.speed_reduction_percent)
    except FupThrottleRateError as exc:
        return _fallback(f"{exc.code}: {exc.message}")

    # MIN_THROTTLE_KBPS is a floor on how hard a throttle may bite, not a
    # licence to RAISE a rate. On an already-slow plan the floor can land above
    # the subscriber's actual rate in ONE direction — a 256k uplink handed a
    # 512k one — and the both-directions test below is an `and`, so that passed
    # through as a "throttle" that made the customer faster. Cap each direction
    # at what they already have; enforcement may only take away.
    download = min(download, full.download_speed)
    upload = min(upload, full.upload_speed)

    # A "reduction" that leaves the subscriber at their current rate in BOTH
    # directions is not a throttle. This happens when the floor bites on an
    # already-slow plan, and applying it would be a no-op dressed up as
    # enforcement.
    if download >= full.download_speed and upload >= full.upload_speed:
        return _fallback(
            f"{rule.speed_reduction_percent:g}% of "
            f"{full.download_speed}k floors above the current rate"
        )

    profile = resolve_or_create_profile(db, download_kbps=download, upload_kbps=upload)
    return ThrottleProfileDecision(
        profile_id=profile.id,
        derived=True,
        reason=(
            f"{rule.speed_reduction_percent:g}% reduction of "
            f"{full.download_speed}k/{full.upload_speed}k"
        ),
        download_kbps=download,
        upload_kbps=upload,
    )
