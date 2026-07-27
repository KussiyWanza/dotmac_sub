"""Authoritative native service-team lifecycle and administration projections.

Ticket configuration, Inbox, workqueue, outage, project, and field-work callers
consume service teams. They do not own team identity or membership lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.party import Party, PartyIdentityStatus, PartyType
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamMember,
    ServiceTeamMemberRole,
    ServiceTeamType,
)
from app.models.system_user import SystemUser
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "operations.service_team_lifecycle"
LIFECYCLE_CONCERN = "service-team lifecycle"
MEMBERSHIP_CONCERN = "service-team membership lifecycle"

_CREATE = OwnerCommandDefinition(
    owner=OWNER,
    concern=LIFECYCLE_CONCERN,
    name="create_service_team",
)
_UPDATE = OwnerCommandDefinition(
    owner=OWNER,
    concern=LIFECYCLE_CONCERN,
    name="update_service_team",
)
_SET_ACTIVE = OwnerCommandDefinition(
    owner=OWNER,
    concern=LIFECYCLE_CONCERN,
    name="set_service_team_active",
)
_ADD_MEMBER = OwnerCommandDefinition(
    owner=OWNER,
    concern=MEMBERSHIP_CONCERN,
    name="add_service_team_member",
)
_UPDATE_MEMBER = OwnerCommandDefinition(
    owner=OWNER,
    concern=MEMBERSHIP_CONCERN,
    name="update_service_team_member",
)
_REMOVE_MEMBER = OwnerCommandDefinition(
    owner=OWNER,
    concern=MEMBERSHIP_CONCERN,
    name="remove_service_team_member",
)


class ServiceTeamLifecycleError(DomainError):
    """Stable, transport-neutral service-team command rejection."""


@dataclass(frozen=True)
class CreateServiceTeam:
    context: CommandContext
    team_id: UUID
    name: str
    team_type: ServiceTeamType
    region: str | None = None
    manager_system_user_id: UUID | None = None


@dataclass(frozen=True)
class UpdateServiceTeam:
    context: CommandContext
    team_id: UUID
    expected_updated_at: datetime
    name: str
    team_type: ServiceTeamType
    region: str | None = None
    manager_system_user_id: UUID | None = None


@dataclass(frozen=True)
class SetServiceTeamActive:
    context: CommandContext
    team_id: UUID
    expected_updated_at: datetime
    is_active: bool
    reason: str


@dataclass(frozen=True)
class AddServiceTeamMember:
    context: CommandContext
    team_id: UUID
    system_user_id: UUID
    role: ServiceTeamMemberRole


@dataclass(frozen=True)
class UpdateServiceTeamMember:
    context: CommandContext
    team_id: UUID
    member_id: UUID
    role: ServiceTeamMemberRole


@dataclass(frozen=True)
class RemoveServiceTeamMember:
    context: CommandContext
    team_id: UUID
    member_id: UUID
    reason: str


@dataclass(frozen=True)
class ServiceTeamMutation:
    team_id: UUID
    member_id: UUID | None
    operation: str
    replayed: bool


@dataclass(frozen=True)
class StaffOption:
    system_user_id: UUID
    person_party_id: UUID
    label: str
    email: str


@dataclass(frozen=True)
class ServiceTeamMemberView:
    member_id: UUID
    person_id: UUID
    system_user_id: UUID | None
    person_label: str
    person_email: str
    role: ServiceTeamMemberRole
    is_active: bool
    staff_identity_active: bool
    created_at: datetime


@dataclass(frozen=True)
class ServiceTeamView:
    team_id: UUID
    name: str
    team_type: ServiceTeamType
    region: str | None
    manager_person_id: UUID | None
    manager_system_user_id: UUID | None
    manager_label: str | None
    manager_identity_active: bool | None
    is_active: bool
    active_member_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ServiceTeamDetail:
    team: ServiceTeamView
    members: tuple[ServiceTeamMemberView, ...]
    available_staff: tuple[StaffOption, ...]
    actions: ServiceTeamActionEligibility


@dataclass(frozen=True)
class ServiceTeamActionEligibility:
    can_edit: bool
    can_add_member: bool
    can_activate: bool
    can_deactivate: bool
    lifecycle_block_reason: str | None


class ServiceTeamResolutionKind(str, Enum):
    resolved = "resolved"
    identity_unavailable = "identity_unavailable"
    no_membership = "no_membership"
    ambiguous = "ambiguous"


@dataclass(frozen=True)
class ServiceTeamResolution:
    system_user_id: UUID
    person_party_id: UUID | None
    kind: ServiceTeamResolutionKind
    team_id: UUID | None
    candidate_team_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class StaffServiceTeamScope:
    """Active native team scope for one authenticated staff principal."""

    system_user_id: UUID
    person_party_id: UUID | None
    identity_available: bool
    member_team_ids: tuple[UUID, ...]
    lead_team_ids: tuple[UUID, ...]
    managed_team_ids: tuple[UUID, ...]

    @property
    def accessible_team_ids(self) -> tuple[UUID, ...]:
        return tuple(
            sorted(
                {
                    *self.member_team_ids,
                    *self.managed_team_ids,
                },
                key=str,
            )
        )

    @property
    def leads_team(self) -> bool:
        return bool(self.lead_team_ids or self.managed_team_ids)


@dataclass(frozen=True)
class ServiceTeamRoleRegionMember:
    person_id: UUID
    system_user_id: UUID
    label: str
    email: str
    team_names: tuple[str, ...]


@dataclass(frozen=True)
class ServiceTeamRoleRegionGroup:
    group_key: str
    region: str
    role: ServiceTeamMemberRole
    members: tuple[ServiceTeamRoleRegionMember, ...]


@dataclass
class _RoleRegionAccumulator:
    region: str
    role: ServiceTeamMemberRole
    members: dict[UUID, tuple[SystemUser, set[str]]]


@dataclass(frozen=True)
class ServiceTeamList:
    items: tuple[ServiceTeamView, ...]
    total: int
    active_count: int
    inactive_count: int
    search: str
    active_filter: bool | None
    offset: int
    limit: int


def _error(code: str, message: str, **details: object) -> ServiceTeamLifecycleError:
    return ServiceTeamLifecycleError(code=code, message=message, details=details)


def _clean_name(value: str) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise _error("service_team_invalid", "Team name is required.", field="name")
    if len(name) > 160:
        raise _error(
            "service_team_invalid",
            "Team name must be 160 characters or fewer.",
            field="name",
        )
    return name


def _clean_region(value: str | None) -> str | None:
    region = " ".join(str(value or "").split()) or None
    if region is not None and len(region) > 80:
        raise _error(
            "service_team_invalid",
            "Region must be 80 characters or fewer.",
            field="region",
        )
    return region


def _clean_reason(value: str) -> str:
    reason = " ".join(str(value or "").split())
    if not reason:
        raise _error(
            "service_team_reason_required",
            "A reason is required for this lifecycle change.",
        )
    if len(reason) > 500:
        raise _error(
            "service_team_invalid",
            "Reason must be 500 characters or fewer.",
            field="reason",
        )
    return reason


def _actor(context: CommandContext) -> tuple[AuditActorType, str | None]:
    raw_actor = str(context.actor or "")
    candidate = raw_actor.rsplit(":", maxsplit=1)[-1]
    try:
        UUID(candidate)
    except (TypeError, ValueError):
        return AuditActorType.system, None
    return AuditActorType.user, candidate


def _staff_label(user: SystemUser) -> str:
    return (
        str(user.display_name or "").strip()
        or f"{user.first_name} {user.last_name}".strip()
        or user.email
    )


def _staff_option(user: SystemUser) -> StaffOption:
    if user.person_party_id is None:
        raise _error(
            "service_team_staff_identity_unbound",
            "The selected staff member has no reviewed Person Party binding.",
            system_user_id=str(user.id),
        )
    return StaffOption(
        system_user_id=user.id,
        person_party_id=user.person_party_id,
        label=_staff_label(user),
        email=user.email,
    )


def _active_staff_identity(
    db: Session,
    system_user_id: UUID,
) -> tuple[SystemUser, UUID]:
    user = db.scalar(
        select(SystemUser).where(SystemUser.id == system_user_id).with_for_update()
    )
    if user is None or not user.is_active:
        raise _error(
            "service_team_staff_not_found",
            "The selected staff member is not active.",
            system_user_id=str(system_user_id),
        )
    if user.person_party_id is None:
        raise _error(
            "service_team_staff_identity_unbound",
            "The selected staff member has no reviewed Person Party binding.",
            system_user_id=str(system_user_id),
        )
    person = db.scalar(
        select(Party).where(Party.id == user.person_party_id).with_for_update()
    )
    if (
        person is None
        or person.party_type != PartyType.person.value
        or person.status != PartyIdentityStatus.active.value
    ):
        raise _error(
            "service_team_staff_identity_invalid",
            "The selected staff member's Person Party binding is not active.",
            system_user_id=str(system_user_id),
            person_party_id=str(user.person_party_id),
        )
    return user, user.person_party_id


def _active_staff_for_person_party(
    db: Session,
    person_party_id: UUID,
) -> SystemUser:
    user = db.scalar(
        select(SystemUser)
        .where(SystemUser.person_party_id == person_party_id)
        .with_for_update()
    )
    if user is None:
        raise _error(
            "service_team_staff_identity_unbound",
            "The service-team identity has no reviewed staff principal binding.",
            person_party_id=str(person_party_id),
        )
    validated, _person_party_id = _active_staff_identity(db, user.id)
    return validated


def _locked_team(db: Session, team_id: UUID) -> ServiceTeam:
    team = db.scalar(
        select(ServiceTeam).where(ServiceTeam.id == team_id).with_for_update()
    )
    if team is None:
        raise _error(
            "service_team_not_found",
            "Service team was not found.",
            team_id=str(team_id),
        )
    return team


def _locked_member(
    db: Session,
    *,
    team_id: UUID,
    member_id: UUID,
) -> ServiceTeamMember:
    member = db.scalar(
        select(ServiceTeamMember)
        .where(
            ServiceTeamMember.id == member_id,
            ServiceTeamMember.team_id == team_id,
        )
        .with_for_update()
    )
    if member is None:
        raise _error(
            "service_team_member_not_found",
            "Service-team member was not found.",
            team_id=str(team_id),
            member_id=str(member_id),
        )
    return member


def _ensure_name_available(
    db: Session,
    *,
    name: str,
    excluding_team_id: UUID | None = None,
) -> None:
    statement = select(ServiceTeam.id).where(
        func.lower(ServiceTeam.name) == name.lower()
    )
    if excluding_team_id is not None:
        statement = statement.where(ServiceTeam.id != excluding_team_id)
    if db.scalar(statement) is not None:
        raise _error(
            "service_team_name_conflict",
            "Another service team already uses this name.",
            name=name,
        )


def _utc_instant(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _same_timestamp(left: datetime, right: datetime) -> bool:
    return _utc_instant(left) == _utc_instant(right)


def _audit_and_event(
    db: Session,
    *,
    context: CommandContext,
    action: str,
    team: ServiceTeam,
    member: ServiceTeamMember | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    actor_type, actor_id = _actor(context)
    evidence = {
        "schema_version": 1,
        "owner": OWNER,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
        "operation": action,
        "team_id": str(team.id),
        "team_type": str(team.team_type),
        "member_id": str(member.id) if member is not None else None,
        "person_id": str(member.person_id) if member is not None else None,
        **(metadata or {}),
    }
    stage_audit_event(
        db,
        action=f"service_team.{action}",
        entity_type="service_team",
        entity_id=str(team.id),
        actor_type=actor_type,
        actor_id=actor_id,
        metadata=evidence,
    )
    emit_event(
        db,
        (
            EventType.service_team_membership_changed
            if member is not None
            else EventType.service_team_changed
        ),
        {
            **evidence,
            "aggregate_type": "service_team",
            "aggregate_id": str(team.id),
            "aggregate_version": str(context.command_id),
        },
        actor=context.actor,
    )


def create_team(db: Session, command: CreateServiceTeam) -> ServiceTeamMutation:
    """Create one native team; exact caller-supplied team-id replays are stable."""

    name = _clean_name(command.name)
    region = _clean_region(command.region)

    def apply() -> ServiceTeamMutation:
        existing = db.scalar(
            select(ServiceTeam)
            .where(ServiceTeam.id == command.team_id)
            .with_for_update()
        )
        manager_person_id = None
        if command.manager_system_user_id is not None:
            _, manager_person_id = _active_staff_identity(
                db, command.manager_system_user_id
            )
        if existing is not None:
            if (
                existing.name == name
                and existing.team_type == command.team_type.value
                and existing.region == region
                and existing.manager_person_id == manager_person_id
                and existing.is_active
            ):
                return ServiceTeamMutation(existing.id, None, "created", replayed=True)
            raise _error(
                "service_team_identity_collision",
                "The service-team identifier is already bound to different values.",
                team_id=str(command.team_id),
            )
        _ensure_name_available(db, name=name)
        team = ServiceTeam(
            id=command.team_id,
            name=name,
            team_type=command.team_type.value,
            region=region,
            manager_person_id=manager_person_id,
            is_active=True,
        )
        db.add(team)
        db.flush()
        _audit_and_event(db, context=command.context, action="created", team=team)
        return ServiceTeamMutation(team.id, None, "created", replayed=False)

    return execute_owner_command(
        db,
        definition=_CREATE,
        context=command.context,
        operation=apply,
    )


def update_team(db: Session, command: UpdateServiceTeam) -> ServiceTeamMutation:
    """Update descriptive team fields with desired-state replay and stale guard."""

    name = _clean_name(command.name)
    region = _clean_region(command.region)

    def apply() -> ServiceTeamMutation:
        team = _locked_team(db, command.team_id)
        manager_person_id = None
        if command.manager_system_user_id is not None:
            _, manager_person_id = _active_staff_identity(
                db, command.manager_system_user_id
            )
        desired = (
            name,
            command.team_type.value,
            region,
            manager_person_id,
        )
        current = (
            team.name,
            team.team_type,
            team.region,
            team.manager_person_id,
        )
        if current == desired:
            return ServiceTeamMutation(team.id, None, "updated", replayed=True)
        if not _same_timestamp(team.updated_at, command.expected_updated_at):
            raise _error(
                "service_team_stale",
                "The service team changed after this form was loaded.",
                team_id=str(team.id),
            )
        _ensure_name_available(db, name=name, excluding_team_id=team.id)
        previous: dict[str, object] = {
            "previous_name": team.name,
            "previous_team_type": team.team_type,
            "previous_region": team.region,
            "previous_manager_person_id": (
                str(team.manager_person_id) if team.manager_person_id else None
            ),
        }
        team.name = name
        team.team_type = command.team_type.value
        team.region = region
        team.manager_person_id = manager_person_id
        db.flush()
        _audit_and_event(
            db,
            context=command.context,
            action="updated",
            team=team,
            metadata=previous,
        )
        return ServiceTeamMutation(team.id, None, "updated", replayed=False)

    return execute_owner_command(
        db,
        definition=_UPDATE,
        context=command.context,
        operation=apply,
    )


def set_team_active(
    db: Session,
    command: SetServiceTeamActive,
) -> ServiceTeamMutation:
    """Activate or deactivate a team without deleting historical identity."""

    reason = _clean_reason(command.reason)

    def apply() -> ServiceTeamMutation:
        team = _locked_team(db, command.team_id)
        operation = "activated" if command.is_active else "deactivated"
        if team.is_active is command.is_active:
            return ServiceTeamMutation(team.id, None, operation, replayed=True)
        if not _same_timestamp(team.updated_at, command.expected_updated_at):
            raise _error(
                "service_team_stale",
                "The service team changed after this action was loaded.",
                team_id=str(team.id),
            )
        if not command.is_active:
            active_members = db.scalar(
                select(func.count(ServiceTeamMember.id)).where(
                    ServiceTeamMember.team_id == team.id,
                    ServiceTeamMember.is_active.is_(True),
                )
            )
            if active_members:
                raise _error(
                    "service_team_has_active_members",
                    "Remove or transfer every active member before deactivating the team.",
                    team_id=str(team.id),
                    active_member_count=int(active_members),
                )
        elif team.manager_person_id is not None:
            _active_staff_for_person_party(db, team.manager_person_id)
        team.is_active = command.is_active
        db.flush()
        _audit_and_event(
            db,
            context=command.context,
            action=operation,
            team=team,
            metadata={"reason": reason},
        )
        return ServiceTeamMutation(team.id, None, operation, replayed=False)

    return execute_owner_command(
        db,
        definition=_SET_ACTIVE,
        context=command.context,
        operation=apply,
    )


def add_member(
    db: Session,
    command: AddServiceTeamMember,
) -> ServiceTeamMutation:
    """Add or reactivate one active staff member in one active team."""

    def apply() -> ServiceTeamMutation:
        team = _locked_team(db, command.team_id)
        if not team.is_active:
            raise _error(
                "service_team_inactive",
                "Members cannot be added to an inactive service team.",
                team_id=str(team.id),
            )
        _, person_party_id = _active_staff_identity(db, command.system_user_id)
        member = db.scalar(
            select(ServiceTeamMember)
            .where(
                ServiceTeamMember.team_id == team.id,
                ServiceTeamMember.person_id == person_party_id,
            )
            .with_for_update()
        )
        if (
            member is not None
            and member.is_active
            and member.role == command.role.value
        ):
            return ServiceTeamMutation(
                team.id, member.id, "member_added", replayed=True
            )
        if member is None:
            member = ServiceTeamMember(
                team_id=team.id,
                person_id=person_party_id,
                role=command.role.value,
                is_active=True,
            )
            db.add(member)
        else:
            member.role = command.role.value
            member.is_active = True
        db.flush()
        _audit_and_event(
            db,
            context=command.context,
            action="member_added",
            team=team,
            member=member,
            metadata={"role": member.role},
        )
        return ServiceTeamMutation(team.id, member.id, "member_added", replayed=False)

    return execute_owner_command(
        db,
        definition=_ADD_MEMBER,
        context=command.context,
        operation=apply,
    )


def update_member(
    db: Session,
    command: UpdateServiceTeamMember,
) -> ServiceTeamMutation:
    """Change the role of one active membership."""

    def apply() -> ServiceTeamMutation:
        team = _locked_team(db, command.team_id)
        member = _locked_member(
            db,
            team_id=team.id,
            member_id=command.member_id,
        )
        if not member.is_active:
            raise _error(
                "service_team_member_inactive",
                "Reactivate the member before changing their role.",
                member_id=str(member.id),
            )
        _active_staff_for_person_party(db, member.person_id)
        if member.role == command.role.value:
            return ServiceTeamMutation(
                team.id, member.id, "member_updated", replayed=True
            )
        previous_role = member.role
        member.role = command.role.value
        db.flush()
        _audit_and_event(
            db,
            context=command.context,
            action="member_updated",
            team=team,
            member=member,
            metadata={"previous_role": previous_role, "role": member.role},
        )
        return ServiceTeamMutation(team.id, member.id, "member_updated", replayed=False)

    return execute_owner_command(
        db,
        definition=_UPDATE_MEMBER,
        context=command.context,
        operation=apply,
    )


def remove_member(
    db: Session,
    command: RemoveServiceTeamMember,
) -> ServiceTeamMutation:
    """Deactivate one membership while retaining historical identity."""

    reason = _clean_reason(command.reason)

    def apply() -> ServiceTeamMutation:
        team = _locked_team(db, command.team_id)
        member = _locked_member(
            db,
            team_id=team.id,
            member_id=command.member_id,
        )
        if not member.is_active:
            return ServiceTeamMutation(
                team.id, member.id, "member_removed", replayed=True
            )
        member.is_active = False
        db.flush()
        _audit_and_event(
            db,
            context=command.context,
            action="member_removed",
            team=team,
            member=member,
            metadata={"reason": reason, "role": member.role},
        )
        return ServiceTeamMutation(team.id, member.id, "member_removed", replayed=False)

    return execute_owner_command(
        db,
        definition=_REMOVE_MEMBER,
        context=command.context,
        operation=apply,
    )


def _team_views(db: Session, teams: list[ServiceTeam]) -> tuple[ServiceTeamView, ...]:
    team_ids = [team.id for team in teams]
    manager_ids = {
        team.manager_person_id for team in teams if team.manager_person_id is not None
    }
    manager_users = (
        {
            user.person_party_id: user
            for user in db.scalars(
                select(SystemUser).where(SystemUser.person_party_id.in_(manager_ids))
            ).all()
        }
        if manager_ids
        else {}
    )
    active_manager_party_ids = (
        set(
            db.scalars(
                select(Party.id).where(
                    Party.id.in_(manager_ids),
                    Party.party_type == PartyType.person.value,
                    Party.status == PartyIdentityStatus.active.value,
                )
            ).all()
        )
        if manager_ids
        else set()
    )
    member_counts = (
        {
            team_id: int(count)
            for team_id, count in db.execute(
                select(ServiceTeamMember.team_id, func.count(ServiceTeamMember.id))
                .where(
                    ServiceTeamMember.team_id.in_(team_ids),
                    ServiceTeamMember.is_active.is_(True),
                )
                .group_by(ServiceTeamMember.team_id)
            ).all()
        }
        if team_ids
        else {}
    )
    return tuple(
        ServiceTeamView(
            team_id=team.id,
            name=team.name,
            team_type=ServiceTeamType(team.team_type),
            region=team.region,
            manager_person_id=team.manager_person_id,
            manager_system_user_id=(
                manager_users[team.manager_person_id].id
                if team.manager_person_id in manager_users
                else None
            ),
            manager_label=(
                _staff_label(manager_users[team.manager_person_id])
                if team.manager_person_id in manager_users
                else None
            ),
            manager_identity_active=(
                None
                if team.manager_person_id is None
                else (
                    team.manager_person_id in manager_users
                    and manager_users[team.manager_person_id].is_active
                    and team.manager_person_id in active_manager_party_ids
                )
            ),
            is_active=team.is_active,
            active_member_count=member_counts.get(team.id, 0),
            created_at=team.created_at,
            updated_at=team.updated_at,
        )
        for team in teams
    )


def list_teams(
    db: Session,
    *,
    search: str | None = None,
    is_active: bool | None = None,
    offset: int = 0,
    limit: int = 100,
) -> ServiceTeamList:
    """Return one bounded operator list from native records."""

    normalized_search = " ".join(str(search or "").split())
    normalized_offset = max(int(offset), 0)
    normalized_limit = min(max(int(limit), 1), 200)
    filters = []
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                ServiceTeam.name.ilike(pattern),
                ServiceTeam.region.ilike(pattern),
                ServiceTeam.team_type.ilike(pattern),
            )
        )
    if is_active is not None:
        filters.append(ServiceTeam.is_active.is_(is_active))
    statement = select(ServiceTeam)
    count_statement = select(func.count(ServiceTeam.id))
    if filters:
        statement = statement.where(*filters)
        count_statement = count_statement.where(*filters)
    teams = db.scalars(
        statement.order_by(ServiceTeam.is_active.desc(), ServiceTeam.name.asc())
        .offset(normalized_offset)
        .limit(normalized_limit)
    ).all()
    active_count, inactive_count = db.execute(
        select(
            func.count(ServiceTeam.id).filter(ServiceTeam.is_active.is_(True)),
            func.count(ServiceTeam.id).filter(ServiceTeam.is_active.is_(False)),
        )
    ).one()
    return ServiceTeamList(
        items=_team_views(db, list(teams)),
        total=int(db.scalar(count_statement) or 0),
        active_count=int(active_count),
        inactive_count=int(inactive_count),
        search=normalized_search,
        active_filter=is_active,
        offset=normalized_offset,
        limit=normalized_limit,
    )


def get_team(db: Session, team_id: UUID) -> ServiceTeamDetail:
    """Return one team, memberships, and eligible staff from native records."""

    team = db.get(ServiceTeam, team_id)
    if team is None:
        raise _error(
            "service_team_not_found",
            "Service team was not found.",
            team_id=str(team_id),
        )
    member_rows = db.scalars(
        select(ServiceTeamMember)
        .where(ServiceTeamMember.team_id == team.id)
        .order_by(
            ServiceTeamMember.is_active.desc(),
            ServiceTeamMember.created_at.asc(),
        )
    ).all()
    person_ids = {member.person_id for member in member_rows}
    if team.manager_person_id is not None:
        person_ids.add(team.manager_person_id)
    staff = (
        {
            user.person_party_id: user
            for user in db.scalars(
                select(SystemUser).where(SystemUser.person_party_id.in_(person_ids))
            ).all()
        }
        if person_ids
        else {}
    )
    active_party_ids = (
        set(
            db.scalars(
                select(Party.id).where(
                    Party.id.in_(person_ids),
                    Party.party_type == PartyType.person.value,
                    Party.status == PartyIdentityStatus.active.value,
                )
            ).all()
        )
        if person_ids
        else set()
    )
    active_staff_person_ids = {
        person_party_id
        for person_party_id, user in staff.items()
        if user.is_active and person_party_id in active_party_ids
    }
    members = tuple(
        ServiceTeamMemberView(
            member_id=member.id,
            person_id=member.person_id,
            system_user_id=(
                staff[member.person_id].id if member.person_id in staff else None
            ),
            person_label=(
                _staff_label(staff[member.person_id])
                if member.person_id in staff
                else "Unknown staff"
            ),
            person_email=(
                staff[member.person_id].email if member.person_id in staff else ""
            ),
            role=ServiceTeamMemberRole(member.role),
            is_active=member.is_active,
            staff_identity_active=member.person_id in active_staff_person_ids,
            created_at=member.created_at,
        )
        for member in member_rows
    )
    active_member_ids = {member.person_id for member in member_rows if member.is_active}
    available_staff = tuple(
        _staff_option(user)
        for user in db.scalars(
            select(SystemUser)
            .join(Party, Party.id == SystemUser.person_party_id)
            .where(
                SystemUser.is_active.is_(True),
                SystemUser.person_party_id.is_not(None),
                SystemUser.person_party_id.not_in(active_member_ids),
                Party.party_type == PartyType.person.value,
                Party.status == PartyIdentityStatus.active.value,
            )
            .order_by(
                SystemUser.first_name.asc(),
                SystemUser.last_name.asc(),
                SystemUser.email.asc(),
            )
        ).all()
    )
    team_view = _team_views(db, [team])[0]
    if team.is_active:
        can_deactivate = team_view.active_member_count == 0
        actions = ServiceTeamActionEligibility(
            can_edit=True,
            can_add_member=True,
            can_activate=False,
            can_deactivate=can_deactivate,
            lifecycle_block_reason=(
                None
                if can_deactivate
                else "Remove or transfer every active member before deactivation."
            ),
        )
    else:
        manager_identity_active = (
            team.manager_person_id is None or team_view.manager_identity_active is True
        )
        actions = ServiceTeamActionEligibility(
            can_edit=True,
            can_add_member=False,
            can_activate=manager_identity_active,
            can_deactivate=False,
            lifecycle_block_reason=(
                None
                if manager_identity_active
                else "Assign an active, Party-bound manager before activation."
            ),
        )
    return ServiceTeamDetail(
        team=team_view,
        members=members,
        available_staff=available_staff,
        actions=actions,
    )


def list_staff_options(db: Session) -> tuple[StaffOption, ...]:
    """Return active staff principals available to manage or join teams."""

    return tuple(
        _staff_option(user)
        for user in db.scalars(
            select(SystemUser)
            .join(Party, Party.id == SystemUser.person_party_id)
            .where(
                SystemUser.is_active.is_(True),
                SystemUser.person_party_id.is_not(None),
                Party.party_type == PartyType.person.value,
                Party.status == PartyIdentityStatus.active.value,
            )
            .order_by(
                SystemUser.first_name.asc(),
                SystemUser.last_name.asc(),
                SystemUser.email.asc(),
            )
        ).all()
    )


def resolve_staff_service_team(
    db: Session,
    system_user_id: UUID,
    *,
    team_type: ServiceTeamType | None = None,
) -> ServiceTeamResolution:
    """Resolve one active staff principal to one active native team.

    System-user identifiers are adapter-facing identity. Membership rows are
    Party-backed, so callers must use this owner query instead of comparing the
    two identifier domains. Multiple matching memberships are deliberately
    ambiguous; the owner never invents precedence from row creation order.
    """

    user = db.get(SystemUser, system_user_id)
    if user is None or not user.is_active or user.person_party_id is None:
        return ServiceTeamResolution(
            system_user_id=system_user_id,
            person_party_id=(user.person_party_id if user is not None else None),
            kind=ServiceTeamResolutionKind.identity_unavailable,
            team_id=None,
            candidate_team_ids=(),
        )
    person_party_id = user.person_party_id
    party_is_active = db.scalar(
        select(Party.id).where(
            Party.id == person_party_id,
            Party.party_type == PartyType.person.value,
            Party.status == PartyIdentityStatus.active.value,
        )
    )
    if party_is_active is None:
        return ServiceTeamResolution(
            system_user_id=system_user_id,
            person_party_id=person_party_id,
            kind=ServiceTeamResolutionKind.identity_unavailable,
            team_id=None,
            candidate_team_ids=(),
        )
    statement = (
        select(ServiceTeam.id)
        .join(ServiceTeamMember, ServiceTeamMember.team_id == ServiceTeam.id)
        .where(
            ServiceTeamMember.person_id == person_party_id,
            ServiceTeamMember.is_active.is_(True),
            ServiceTeam.is_active.is_(True),
        )
    )
    if team_type is not None:
        statement = statement.where(ServiceTeam.team_type == team_type.value)
    candidate_team_ids = tuple(
        db.scalars(statement.order_by(ServiceTeam.id.asc())).all()
    )
    if not candidate_team_ids:
        return ServiceTeamResolution(
            system_user_id=system_user_id,
            person_party_id=person_party_id,
            kind=ServiceTeamResolutionKind.no_membership,
            team_id=None,
            candidate_team_ids=(),
        )
    if len(candidate_team_ids) > 1:
        return ServiceTeamResolution(
            system_user_id=system_user_id,
            person_party_id=person_party_id,
            kind=ServiceTeamResolutionKind.ambiguous,
            team_id=None,
            candidate_team_ids=candidate_team_ids,
        )
    return ServiceTeamResolution(
        system_user_id=system_user_id,
        person_party_id=person_party_id,
        kind=ServiceTeamResolutionKind.resolved,
        team_id=candidate_team_ids[0],
        candidate_team_ids=candidate_team_ids,
    )


def resolve_staff_team_scope(
    db: Session,
    system_user_id: UUID,
) -> StaffServiceTeamScope:
    """Resolve every active native team role for one staff principal.

    Workqueue, Inbox, ticket, and dispatch callers consume this query instead of
    comparing adapter-facing ``SystemUser.id`` values with Party-backed
    ``ServiceTeamMember.person_id`` values themselves.
    """

    user = db.get(SystemUser, system_user_id)
    if user is None or not user.is_active or user.person_party_id is None:
        return StaffServiceTeamScope(
            system_user_id=system_user_id,
            person_party_id=(user.person_party_id if user is not None else None),
            identity_available=False,
            member_team_ids=(),
            lead_team_ids=(),
            managed_team_ids=(),
        )
    person_party_id = user.person_party_id
    party_is_active = db.scalar(
        select(Party.id).where(
            Party.id == person_party_id,
            Party.party_type == PartyType.person.value,
            Party.status == PartyIdentityStatus.active.value,
        )
    )
    if party_is_active is None:
        return StaffServiceTeamScope(
            system_user_id=system_user_id,
            person_party_id=person_party_id,
            identity_available=False,
            member_team_ids=(),
            lead_team_ids=(),
            managed_team_ids=(),
        )

    memberships = db.execute(
        select(ServiceTeam.id, ServiceTeamMember.role)
        .join(ServiceTeamMember, ServiceTeamMember.team_id == ServiceTeam.id)
        .where(
            ServiceTeam.is_active.is_(True),
            ServiceTeamMember.is_active.is_(True),
            ServiceTeamMember.person_id == person_party_id,
        )
        .order_by(ServiceTeam.id.asc())
    ).all()
    member_team_ids = tuple(row[0] for row in memberships)
    lead_roles = {
        ServiceTeamMemberRole.lead.value,
        ServiceTeamMemberRole.manager.value,
    }
    lead_team_ids = tuple(row[0] for row in memberships if row[1] in lead_roles)
    managed_team_ids = tuple(
        db.scalars(
            select(ServiceTeam.id)
            .where(
                ServiceTeam.is_active.is_(True),
                ServiceTeam.manager_person_id == person_party_id,
            )
            .order_by(ServiceTeam.id.asc())
        ).all()
    )
    return StaffServiceTeamScope(
        system_user_id=system_user_id,
        person_party_id=person_party_id,
        identity_available=True,
        member_team_ids=member_team_ids,
        lead_team_ids=lead_team_ids,
        managed_team_ids=managed_team_ids,
    )


def list_active_team_member_system_user_ids(
    db: Session,
    team_ids: frozenset[UUID] | set[UUID] | tuple[UUID, ...],
) -> tuple[UUID, ...]:
    """Return active authenticated staff IDs for active native teams."""

    normalized_team_ids = tuple(sorted(set(team_ids), key=str))
    if not normalized_team_ids:
        return ()
    return tuple(
        db.scalars(
            select(SystemUser.id)
            .join(
                ServiceTeamMember,
                ServiceTeamMember.person_id == SystemUser.person_party_id,
            )
            .join(ServiceTeam, ServiceTeam.id == ServiceTeamMember.team_id)
            .join(Party, Party.id == ServiceTeamMember.person_id)
            .where(
                ServiceTeam.id.in_(normalized_team_ids),
                ServiceTeam.is_active.is_(True),
                ServiceTeamMember.is_active.is_(True),
                SystemUser.is_active.is_(True),
                Party.party_type == PartyType.person.value,
                Party.status == PartyIdentityStatus.active.value,
            )
            .distinct()
            .order_by(SystemUser.id.asc())
        ).all()
    )


def list_active_team_options(db: Session) -> tuple[tuple[UUID, str], ...]:
    """Return the shared active-team selector projection."""

    return tuple(
        (team.id, team.name)
        for team in db.scalars(
            select(ServiceTeam)
            .where(ServiceTeam.is_active.is_(True))
            .order_by(ServiceTeam.name.asc())
        ).all()
    )


def list_role_region_groups(
    db: Session,
    *,
    search: str | None = None,
    limit: int = 500,
) -> tuple[ServiceTeamRoleRegionGroup, ...]:
    """Group active, Party-backed members by native team region and member role."""

    normalized_search = " ".join(str(search or "").split())
    normalized_limit = min(max(int(limit), 1), 1000)
    statement = (
        select(ServiceTeam, ServiceTeamMember, SystemUser)
        .join(ServiceTeamMember, ServiceTeamMember.team_id == ServiceTeam.id)
        .join(
            SystemUser,
            SystemUser.person_party_id == ServiceTeamMember.person_id,
        )
        .join(Party, Party.id == ServiceTeamMember.person_id)
        .where(
            ServiceTeam.is_active.is_(True),
            ServiceTeamMember.is_active.is_(True),
            SystemUser.is_active.is_(True),
            Party.party_type == PartyType.person.value,
            Party.status == PartyIdentityStatus.active.value,
        )
    )
    if normalized_search:
        pattern = f"%{normalized_search}%"
        statement = statement.where(
            or_(
                ServiceTeam.name.ilike(pattern),
                ServiceTeam.region.ilike(pattern),
                ServiceTeamMember.role.ilike(pattern),
                SystemUser.display_name.ilike(pattern),
                SystemUser.first_name.ilike(pattern),
                SystemUser.last_name.ilike(pattern),
                SystemUser.email.ilike(pattern),
            )
        )
    rows = db.execute(
        statement.order_by(
            ServiceTeam.region.asc(),
            ServiceTeamMember.role.asc(),
            SystemUser.first_name.asc(),
            SystemUser.last_name.asc(),
            SystemUser.email.asc(),
            ServiceTeam.name.asc(),
        ).limit(normalized_limit)
    ).all()
    grouped: dict[tuple[str, ServiceTeamMemberRole], _RoleRegionAccumulator] = {}
    for team, member, user in rows:
        region = str(team.region or "").strip() or "unassigned"
        role = ServiceTeamMemberRole(member.role)
        key = (region.casefold(), role)
        accumulator = grouped.setdefault(
            key,
            _RoleRegionAccumulator(region=region, role=role, members={}),
        )
        current = accumulator.members.get(member.person_id)
        if current is None:
            accumulator.members[member.person_id] = (user, {team.name})
        else:
            current[1].add(team.name)

    return tuple(
        ServiceTeamRoleRegionGroup(
            group_key=f"{region_key}_{role.value}",
            region=accumulator.region,
            role=role,
            members=tuple(
                ServiceTeamRoleRegionMember(
                    person_id=person_id,
                    system_user_id=user.id,
                    label=_staff_label(user),
                    email=user.email,
                    team_names=tuple(sorted(team_names, key=str.casefold)),
                )
                for person_id, (user, team_names) in sorted(
                    accumulator.members.items(),
                    key=lambda item: (
                        _staff_label(item[1][0]).casefold(),
                        item[1][0].email.casefold(),
                        str(item[0]),
                    ),
                )
            ),
        )
        for (region_key, role), accumulator in sorted(
            grouped.items(),
            key=lambda item: (item[0][0], item[0][1].value),
        )
    )
