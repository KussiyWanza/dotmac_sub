"""Per-entity scoping — which items a principal is allowed to see.

Scope is resolved once per request from service-team membership, then handed to
every provider. Providers must express their visibility filter in terms of the
scope (never re-derive it), so "who can see what" has exactly one definition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.services import service_team_lifecycle
from app.services.workqueue.permissions import (
    WorkqueuePermissionError,
    WorkqueuePrincipal,
    require_workqueue_view,
    resolve_audience,
)
from app.services.workqueue.types import WorkqueueAudience

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkqueueScope:
    principal: WorkqueuePrincipal
    audience: WorkqueueAudience
    #: Teams the principal belongs to (any role).
    member_service_team_ids: frozenset[UUID]
    #: Teams whose work is visible at this audience.
    accessible_service_team_ids: frozenset[UUID]
    #: People whose assigned work is visible at this audience.
    accessible_person_ids: frozenset[UUID]
    #: Optional caller-supplied narrowing filter (already authorized).
    service_team_filter: UUID | None
    #: True for org audience: no team/person restriction at all.
    is_org_wide: bool

    @property
    def person_id(self) -> UUID:
        return self.principal.person_id

    @property
    def is_self_audience(self) -> bool:
        return self.audience is WorkqueueAudience.self_

    def team_ids_for_query(self) -> frozenset[UUID]:
        """Teams a provider should filter on (honours the caller's filter)."""
        if self.service_team_filter is not None:
            return frozenset({self.service_team_filter})
        return self.accessible_service_team_ids

    def allows_team(self, team_id: UUID | None) -> bool:
        if self.service_team_filter is not None:
            return team_id == self.service_team_filter
        if self.is_org_wide:
            return True
        return team_id is not None and team_id in self.accessible_service_team_ids

    def allows_person(self, person_id: UUID | None) -> bool:
        """Unassigned work (``None``) is visible to everyone in scope."""
        if person_id is None:
            return True
        if self.is_org_wide:
            return True
        return person_id in self.accessible_person_ids

    @property
    def applied_filters(self) -> dict[str, object]:
        return {
            "audience": self.audience.value,
            "org_wide": self.is_org_wide,
            "team_count": len(self.accessible_service_team_ids),
            "person_count": len(self.accessible_person_ids),
            "team_filter": str(self.service_team_filter)
            if self.service_team_filter
            else None,
        }


def get_workqueue_scope(
    db: Session,
    principal: WorkqueuePrincipal,
    *,
    requested_audience: str | WorkqueueAudience | None = None,
    service_team_id: UUID | None = None,
) -> WorkqueueScope:
    """Resolve audience + visibility for a principal.

    Raises ``WorkqueuePermissionError`` if the principal cannot view the queue
    at all, or asks to filter on a team outside their scope.
    """
    require_workqueue_view(principal)

    team_scope = service_team_lifecycle.resolve_staff_team_scope(
        db,
        principal.person_id,
    )
    member_team_ids = frozenset(team_scope.member_team_ids)
    managed_team_ids = frozenset(team_scope.managed_team_ids)

    audience = resolve_audience(
        principal,
        requested_audience,
        leads_team=team_scope.leads_team,
    )
    is_org_wide = audience is WorkqueueAudience.org

    # Even at `self` audience a principal sees unassigned work sitting in their
    # own teams — that is the queue they are expected to pull from.
    accessible_team_ids = member_team_ids | frozenset(managed_team_ids)

    query_team_ids = (
        frozenset({service_team_id})
        if service_team_id is not None
        else accessible_team_ids
    )

    if is_org_wide:
        accessible_person_ids = (
            frozenset(
                service_team_lifecycle.list_active_team_member_system_user_ids(
                    db,
                    query_team_ids,
                )
            )
            if service_team_id is not None
            else frozenset()
        )
    elif audience is WorkqueueAudience.team:
        accessible_person_ids = frozenset(
            {
                *service_team_lifecycle.list_active_team_member_system_user_ids(
                    db,
                    query_team_ids,
                ),
                principal.person_id,
            }
        )
    else:
        accessible_person_ids = frozenset({principal.person_id})

    if service_team_id is not None and not (
        is_org_wide or service_team_id in accessible_team_ids
    ):
        raise WorkqueuePermissionError(
            f"Service team {service_team_id} is outside your workqueue scope"
        )

    scope = WorkqueueScope(
        principal=principal,
        audience=audience,
        member_service_team_ids=member_team_ids,
        accessible_service_team_ids=accessible_team_ids,
        accessible_person_ids=accessible_person_ids,
        service_team_filter=service_team_id,
        is_org_wide=is_org_wide,
    )
    logger.debug(
        "workqueue_scope_resolved person_id=%s filters=%s",
        principal.person_id,
        scope.applied_filters,
    )
    return scope
