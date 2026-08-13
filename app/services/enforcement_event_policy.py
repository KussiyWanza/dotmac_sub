"""Typed policy owner for event-driven access enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import settings_spec
from app.services.domain_errors import DomainError


class AccessEventPolicyError(DomainError):
    """Stable failures at the access event-policy boundary."""


class FupEnforcementAction(StrEnum):
    THROTTLE = "throttle"
    SUSPEND = "suspend"
    BLOCK = "block"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class GroupRoutingPolicy:
    enabled: bool


@dataclass(frozen=True, slots=True)
class SessionRefreshPolicy:
    enabled: bool


@dataclass(frozen=True, slots=True)
class ResolveFupEventPolicy:
    requested_action: FupEnforcementAction | None = None


@dataclass(frozen=True, slots=True)
class FupEventPolicyDecision:
    action: FupEnforcementAction
    throttle_profile_id: UUID | None
    refresh_sessions: bool

    def required_throttle_profile_id(self) -> UUID:
        if (
            self.action is not FupEnforcementAction.THROTTLE
            or self.throttle_profile_id is None
        ):
            raise _error(
                "invalid_throttle_decision",
                "The resolved FUP policy is not an executable throttle decision.",
            )
        return self.throttle_profile_id

    def throttle_fallback_profile_id(self) -> UUID | None:
        """The globally configured profile, which is a FALLBACK and may be unset.

        The throttle a subscriber actually gets is derived from their own rate
        (``app/services/fup_throttle_profile.py``); this global profile only
        stands in when that derivation cannot produce a rate. Requiring it up
        front — via ``required_throttle_profile_id`` — made the fallback a
        precondition for the primary path, so a deployment that had never set
        it silently never throttled anyone, however carefully its ladders were
        configured.
        """
        if self.action is not FupEnforcementAction.THROTTLE:
            raise _error(
                "invalid_throttle_decision",
                "The resolved FUP policy is not an executable throttle decision.",
            )
        return self.throttle_profile_id


def _error(suffix: str, message: str) -> AccessEventPolicyError:
    return AccessEventPolicyError(
        code=f"access.event_policy.{suffix}",
        message=message,
    )


def _boolean_setting(db: Session, domain: SettingDomain, key: str) -> bool:
    value = settings_spec.resolve_value(db, domain, key)
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower() if value is not None else ""
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise _error(
        "invalid_boolean_setting",
        f"The canonical {domain.value}.{key} setting is not boolean.",
    )


def parse_fup_action_override(value: object | None) -> FupEnforcementAction | None:
    """Validate optional event evidence before constructing the typed query."""
    if value is None:
        return None
    normalized = str(value).strip().lower() if isinstance(value, str) else ""
    if normalized == "reduce_speed":
        return FupEnforcementAction.THROTTLE
    try:
        return FupEnforcementAction(normalized)
    except ValueError as exc:
        raise _error(
            "invalid_requested_fup_action",
            "The requested FUP action is not part of the event-policy contract.",
        ) from exc


def _configured_fup_action(db: Session) -> FupEnforcementAction:
    value = settings_spec.resolve_value(db, SettingDomain.usage, "fup_action")
    normalized = str(value).strip().lower() if isinstance(value, str) else ""
    try:
        return FupEnforcementAction(normalized)
    except ValueError as exc:
        raise _error(
            "invalid_configured_fup_action",
            "The canonical usage.fup_action setting is invalid.",
        ) from exc


def resolve_group_routing_policy(db: Session) -> GroupRoutingPolicy:
    return GroupRoutingPolicy(
        enabled=_boolean_setting(
            db,
            SettingDomain.radius,
            "group_routing_enabled",
        )
    )


def resolve_session_refresh_policy(db: Session) -> SessionRefreshPolicy:
    return SessionRefreshPolicy(
        enabled=_boolean_setting(
            db,
            SettingDomain.radius,
            "refresh_sessions_on_profile_change",
        )
    )


def resolve_fup_event_policy(
    db: Session,
    query: ResolveFupEventPolicy,
) -> FupEventPolicyDecision:
    action = query.requested_action or _configured_fup_action(db)
    if action is not FupEnforcementAction.THROTTLE:
        return FupEventPolicyDecision(
            action=action,
            throttle_profile_id=None,
            refresh_sessions=False,
        )

    raw_profile_id = settings_spec.resolve_value(
        db,
        SettingDomain.usage,
        "fup_throttle_radius_profile_id",
    )
    if raw_profile_id is None or not str(raw_profile_id).strip():
        # Absent, not fatal. This setting is the FALLBACK profile; the throttle
        # a subscriber actually gets is derived from their own rate by
        # app/services/fup_throttle_profile.py. Refusing the whole decision here
        # meant a deployment that never set it enforced nothing at all. The
        # resolver raises if derivation fails AND this fallback is missing, so
        # the genuinely unthrottleable case still surfaces.
        return FupEventPolicyDecision(
            action=action,
            throttle_profile_id=None,
            refresh_sessions=resolve_session_refresh_policy(db).enabled,
        )
    try:
        throttle_profile_id = UUID(str(raw_profile_id).strip())
    except ValueError as exc:
        raise _error(
            "invalid_throttle_profile_id",
            "The canonical FUP throttle RADIUS profile identifier is invalid.",
        ) from exc

    return FupEventPolicyDecision(
        action=action,
        throttle_profile_id=throttle_profile_id,
        refresh_sessions=resolve_session_refresh_policy(db).enabled,
    )


__all__ = [
    "AccessEventPolicyError",
    "FupEnforcementAction",
    "FupEventPolicyDecision",
    "GroupRoutingPolicy",
    "ResolveFupEventPolicy",
    "SessionRefreshPolicy",
    "parse_fup_action_override",
    "resolve_fup_event_policy",
    "resolve_group_routing_policy",
    "resolve_session_refresh_policy",
]
