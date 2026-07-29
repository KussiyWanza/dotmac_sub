"""Workqueue principals, audience resolution and per-action authorization.

Sub's auth layer hands services a plain ``auth`` dict (principal_id, roles,
scopes). The workqueue turns that into a :class:`WorkqueuePrincipal` once, up
front, so providers and the aggregator never re-query the RBAC tables per item.

Permission keys are deliberately reused from the support domain (the workqueue
is a *view* over tickets/conversations/work orders — it grants no new access),
so no new RBAC seed rows are required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.common import coerce_uuid
from app.services.workqueue.types import AUDIENCE_RANK, WorkqueueAudience

#: Seeing the queue at all.
WORKQUEUE_VIEW_PERMISSION = "support:ticket:read"
#: Taking an inline action (snooze/claim/complete) on an item.
WORKQUEUE_ACT_PERMISSION = "support:ticket:update"

#: RBAC scopes that may widen audience only when operational team scope agrees.
AUDIENCE_TEAM_SCOPE = "workqueue:audience:team"
AUDIENCE_ORG_SCOPE = "workqueue:audience:org"

ADMIN_ROLES = frozenset({"admin", "superadmin"})


class WorkqueuePermissionError(PermissionError):
    """Raised when a principal may not view the queue or reach a scope."""


@dataclass(frozen=True)
class WorkqueuePrincipal:
    # Compatibility name: this is the authenticated SystemUser identifier, not
    # the staff Party identifier used by native service-team membership.
    person_id: UUID
    roles: frozenset[str]
    scopes: frozenset[str]
    can_view: bool
    can_act: bool

    @property
    def is_admin(self) -> bool:
        return bool(self.roles & ADMIN_ROLES)

    @property
    def system_user_id(self) -> UUID:
        """Return the explicit identity type represented by ``person_id``."""

        return self.person_id


def _normalize(values: Any) -> frozenset[str]:
    if not values:
        return frozenset()
    return frozenset(
        str(value).strip().lower() for value in values if str(value).strip()
    )


def principal_from_auth(db: Session, auth: dict) -> WorkqueuePrincipal:
    """Build a principal from sub's auth dict, resolving RBAC once."""
    # Imported lazily: auth_dependencies pulls in FastAPI's dependency stack,
    # and the workqueue service must stay importable from Celery tasks too.
    from app.services.auth_dependencies import has_permission

    person_id = coerce_uuid(auth.get("principal_id") or auth.get("person_id"))
    return WorkqueuePrincipal(
        person_id=person_id,
        roles=_normalize(auth.get("roles")),
        scopes=_normalize(auth.get("scopes")),
        can_view=has_permission(auth, db, WORKQUEUE_VIEW_PERMISSION),
        can_act=has_permission(auth, db, WORKQUEUE_ACT_PERMISSION),
    )


def has_workqueue_view(principal: WorkqueuePrincipal) -> bool:
    return principal.is_admin or principal.can_view


def require_workqueue_view(principal: WorkqueuePrincipal) -> None:
    if not has_workqueue_view(principal):
        raise WorkqueuePermissionError(
            "Missing permission: " + WORKQUEUE_VIEW_PERMISSION
        )


def natural_audience(
    principal: WorkqueuePrincipal, *, leads_team: bool
) -> WorkqueueAudience:
    """The widest audience a principal holds without asking for it."""
    if principal.is_admin or AUDIENCE_ORG_SCOPE in principal.scopes:
        return WorkqueueAudience.org
    # A responsibility narrows which team can be operated; it never grants the
    # RBAC scope. Conversely, RBAC cannot invent operational team ownership.
    if leads_team and AUDIENCE_TEAM_SCOPE in principal.scopes:
        return WorkqueueAudience.team
    return WorkqueueAudience.self_


def resolve_audience(
    principal: WorkqueuePrincipal,
    requested: str | WorkqueueAudience | None,
    *,
    leads_team: bool,
) -> WorkqueueAudience:
    """Resolve the requested audience, clamped to what the principal holds.

    Unlike CRM (which defaults to ``self``), an unspecified audience resolves to
    the principal's *natural* audience: sub's queue has always shown a team lead
    their team's unassigned work by default, and silently narrowing that would
    hide items operators rely on.
    """
    natural = natural_audience(principal, leads_team=leads_team)
    if requested is None:
        return natural
    try:
        wanted = WorkqueueAudience(requested)
    except ValueError:
        return natural
    if AUDIENCE_RANK[wanted] <= AUDIENCE_RANK[natural]:
        return wanted
    return natural


def can_act_on_item(
    principal: WorkqueuePrincipal,
    *,
    item_assignee_id: UUID | None,
    audience: WorkqueueAudience,
) -> bool:
    """Whether the principal may take an inline action on an item.

    ``self`` audience mixes items assigned to me with unassigned items from my
    teams (that is the point of a queue — you claim from it), so an unassigned
    item is actionable there; someone else's item is not.
    """
    if not (principal.can_act or principal.is_admin):
        return False
    if audience is WorkqueueAudience.self_:
        return item_assignee_id is None or item_assignee_id == principal.person_id
    return True
