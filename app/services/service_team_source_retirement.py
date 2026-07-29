"""Retire legacy service-team sources without adopting CRM membership identity.

This owner exists only for the pre-426 cutover. It preserves the five native
team pointers, verifies their referential integrity, retires the two workflow
setting sources, clears the non-authoritative scalar manager pointer, and
removes only membership rows whose identity resolves to neither a Party nor a
SystemUser or would otherwise fail migration 426's reviewed Party-binding
rules. It never reads CRM, matches people, creates Party identity, creates
membership, or grants RBAC.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import column, delete, func, select, table
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.party import Party, PartyType
from app.models.service_team import ServiceTeam, ServiceTeamMember
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

OWNER = "operations.service_team_source_retirement"
CONCERN = "legacy service-team source retirement"
_RETIRE = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="retire_legacy_service_team_sources",
)

LEGACY_SETTING_KEYS = (
    "support_service_teams",
    "support_service_team_members",
)

# The five native decision pointers retained from #1634. The gate proves that
# source retirement cannot orphan any current consumer.
TEAM_POINTERS = (
    ("support_tickets", "service_team_id"),
    ("projects", "service_team_id"),
    ("dispatch_rules", "service_team_id"),
    ("team_inbox_email_routes", "service_team_id"),
    ("inbox_conversation_teams", "service_team_id"),
)


class ServiceTeamSourceRetirementError(DomainError):
    """Stable refusal for an unsafe pre-426 source retirement."""


@dataclass(frozen=True)
class RetireLegacyServiceTeamSources:
    context: CommandContext


@dataclass(frozen=True)
class ServiceTeamSourceRetirementAudit:
    dangling_pointer_count: int
    duplicate_name_count: int
    active_legacy_source_count: int
    manager_pointer_count: int
    migration_blocking_membership_count: int

    @property
    def ready(self) -> bool:
        return (
            self.dangling_pointer_count == 0
            and self.duplicate_name_count == 0
            and self.active_legacy_source_count == 0
            and self.manager_pointer_count == 0
            and self.migration_blocking_membership_count == 0
        )

    def summary(self) -> dict[str, int | bool]:
        return {
            "ready": self.ready,
            "pointer_contract_count": len(TEAM_POINTERS),
            "dangling_pointer_count": self.dangling_pointer_count,
            "duplicate_name_count": self.duplicate_name_count,
            "active_legacy_source_count": self.active_legacy_source_count,
            "manager_pointer_count": self.manager_pointer_count,
            "migration_blocking_membership_count": (
                self.migration_blocking_membership_count
            ),
        }


@dataclass(frozen=True)
class ServiceTeamSourceRetirementOutcome:
    retired_source_count: int
    cleared_manager_pointer_count: int
    removed_migration_blocking_membership_count: int
    replayed: bool
    reactivated_native_membership_count: int = 0
    abandoned_legacy_member_entry_count: int = 0


def _error(
    code: str, message: str, **details: object
) -> ServiceTeamSourceRetirementError:
    return ServiceTeamSourceRetirementError(
        code=code,
        message=message,
        details=details,
    )


def _dangling_pointer_count(db: Session) -> int:
    total = 0
    for table_name, column_name in TEAM_POINTERS:
        pointer = table(table_name, column(column_name))
        pointer_column = pointer.c[column_name]
        total += int(
            db.scalar(
                select(func.count())
                .select_from(
                    pointer.outerjoin(
                        ServiceTeam.__table__,
                        ServiceTeam.id == pointer_column,
                    )
                )
                .where(
                    pointer_column.is_not(None),
                    ServiceTeam.id.is_(None),
                )
            )
            or 0
        )
    return total


@dataclass(frozen=True)
class _MigrationBlockingMemberResolution:
    blocker_ids: tuple[UUID, ...]
    #: Inactive native rows whose active duplicate compat row is being removed;
    #: restoring them preserves the person's effective membership.
    reactivate_native_ids: tuple[UUID, ...]


def _migration_blocking_member_ids(
    db: Session,
    *,
    lock_identities: bool = False,
) -> tuple[UUID, ...]:
    return _resolve_migration_blocking_members(
        db, lock_identities=lock_identities
    ).blocker_ids


def _resolve_migration_blocking_members(
    db: Session,
    *,
    lock_identities: bool = False,
) -> _MigrationBlockingMemberResolution:
    """Return exactly the compatibility rows that migration 426 would reject."""

    members = db.scalars(
        select(ServiceTeamMember).order_by(
            ServiceTeamMember.team_id.asc(),
            ServiceTeamMember.id.asc(),
        )
    ).all()
    stored_ids = {member.person_id for member in members}
    party_statement = select(Party).where(Party.id.in_(stored_ids))
    if lock_identities:
        party_statement = party_statement.with_for_update()
    parties = {party.id: party for party in db.scalars(party_statement).all()}
    user_statement = select(SystemUser).where(SystemUser.id.in_(stored_ids))
    if lock_identities:
        user_statement = user_statement.with_for_update()
    users_by_id = {user.id: user for user in db.scalars(user_statement).all()}
    target_party_ids = {
        user.person_party_id
        for user in users_by_id.values()
        if user.person_party_id is not None
    }
    target_party_statement = select(Party).where(Party.id.in_(target_party_ids))
    if lock_identities:
        target_party_statement = target_party_statement.with_for_update()
    target_parties = {
        party.id: party for party in db.scalars(target_party_statement).all()
    }
    principal_statement = select(SystemUser).where(
        SystemUser.person_party_id.in_(stored_ids | target_party_ids)
    )
    if lock_identities:
        principal_statement = principal_statement.with_for_update()
    active_principal_counts: dict[UUID, int] = {}
    for principal in db.scalars(principal_statement).all():
        if principal.is_active and principal.person_party_id is not None:
            active_principal_counts[principal.person_party_id] = (
                active_principal_counts.get(principal.person_party_id, 0) + 1
            )

    blockers: set[UUID] = set()
    targets: dict[UUID, UUID] = {}
    for member in members:
        stored_party = parties.get(member.person_id)
        compatibility_user = users_by_id.get(member.person_id)
        if (
            stored_party is not None
            and stored_party.party_type == PartyType.person.value
        ):
            if (
                compatibility_user is not None
                and compatibility_user.person_party_id is not None
                and compatibility_user.person_party_id != stored_party.id
            ):
                blockers.add(member.id)
                continue
            if member.is_active and (
                stored_party.status != "active"
                or active_principal_counts.get(stored_party.id, 0) != 1
            ):
                blockers.add(member.id)
                continue
            targets[member.id] = stored_party.id
            continue

        if compatibility_user is None or compatibility_user.person_party_id is None:
            blockers.add(member.id)
            continue
        target_party = target_parties.get(compatibility_user.person_party_id)
        if (
            target_party is None
            or target_party.party_type != PartyType.person.value
            or (
                member.is_active
                and (
                    not compatibility_user.is_active or target_party.status != "active"
                )
            )
        ):
            blockers.add(member.id)
            continue
        targets[member.id] = target_party.id

    target_groups: dict[tuple[UUID, UUID], list[ServiceTeamMember]] = {}
    for member in members:
        if member.id in blockers or member.id not in targets:
            continue
        target_groups.setdefault(
            (member.team_id, targets[member.id]),
            [],
        ).append(member)
    reactivate_native: set[UUID] = set()
    for (_team_id, target_party_id), group in target_groups.items():
        if len(group) <= 1:
            continue
        native = [member for member in group if member.person_id == target_party_id]
        if len(native) == 1:
            duplicates = [member for member in group if member.id != native[0].id]
            blockers.update(member.id for member in duplicates)
            if not native[0].is_active and any(
                member.is_active for member in duplicates
            ):
                # Deleting the person's only active row would silently end
                # their membership; the native row is restored instead.
                reactivate_native.add(native[0].id)
        else:
            # Several compatibility principals resolving to one Party are
            # conflicting observations. Do not choose a membership winner.
            blockers.update(member.id for member in group)
    return _MigrationBlockingMemberResolution(
        blocker_ids=tuple(sorted(blockers, key=str)),
        reactivate_native_ids=tuple(sorted(reactivate_native, key=str)),
    )


def audit_legacy_service_team_sources(
    db: Session,
) -> ServiceTeamSourceRetirementAudit:
    duplicate_names = db.execute(
        select(func.lower(ServiceTeam.name))
        .group_by(func.lower(ServiceTeam.name))
        .having(func.count(ServiceTeam.id) > 1)
    ).all()
    source_count = db.scalar(
        select(func.count(DomainSetting.id)).where(
            DomainSetting.domain == SettingDomain.workflow,
            DomainSetting.key.in_(LEGACY_SETTING_KEYS),
            DomainSetting.is_active.is_(True),
        )
    )
    manager_count = db.scalar(
        select(func.count(ServiceTeam.id)).where(
            ServiceTeam.manager_person_id.is_not(None)
        )
    )
    return ServiceTeamSourceRetirementAudit(
        dangling_pointer_count=_dangling_pointer_count(db),
        duplicate_name_count=len(duplicate_names),
        active_legacy_source_count=int(source_count or 0),
        manager_pointer_count=int(manager_count or 0),
        migration_blocking_membership_count=len(_migration_blocking_member_ids(db)),
    )


def retire_legacy_service_team_sources(
    db: Session,
    command: RetireLegacyServiceTeamSources,
) -> ServiceTeamSourceRetirementOutcome:
    """Perform the bounded retirement after an operator reviews the audit."""

    def apply() -> ServiceTeamSourceRetirementOutcome:
        teams = db.scalars(
            select(ServiceTeam).order_by(ServiceTeam.id.asc()).with_for_update()
        ).all()
        members = db.scalars(
            select(ServiceTeamMember)
            .order_by(ServiceTeamMember.id.asc())
            .with_for_update()
        ).all()
        settings = db.scalars(
            select(DomainSetting)
            .where(
                DomainSetting.domain == SettingDomain.workflow,
                DomainSetting.key.in_(LEGACY_SETTING_KEYS),
            )
            .order_by(DomainSetting.key.asc())
            .with_for_update()
        ).all()
        dangling = _dangling_pointer_count(db)
        if dangling:
            raise _error(
                "service_team_source_retirement_dangling_pointer",
                "A native service-team pointer does not resolve.",
                dangling_pointer_count=dangling,
                pointer_contract_count=len(TEAM_POINTERS),
            )
        duplicate_names = db.execute(
            select(func.lower(ServiceTeam.name))
            .group_by(func.lower(ServiceTeam.name))
            .having(func.count(ServiceTeam.id) > 1)
        ).all()
        if duplicate_names:
            raise _error(
                "service_team_source_retirement_duplicate_name",
                "Duplicate case-insensitive service-team names require adjudication.",
                duplicate_name_count=len(duplicate_names),
            )

        by_id = {str(team.id): team for team in teams}
        team_source = next(
            (
                setting
                for setting in settings
                if setting.key == "support_service_teams" and setting.is_active
            ),
            None,
        )
        if team_source is not None:
            payload = team_source.value_json
            if not isinstance(payload, list):
                raise _error(
                    "service_team_source_retirement_malformed",
                    "The legacy team source is not a list.",
                )
            for item in payload:
                if not isinstance(item, dict):
                    raise _error(
                        "service_team_source_retirement_malformed",
                        "The legacy team source contains a malformed entry.",
                    )
                team = by_id.get(str(item.get("id")))
                label = " ".join(str(item.get("label") or "").split())
                if team is None or team.name != label:
                    raise _error(
                        "service_team_source_retirement_team_mismatch",
                        "Legacy and native team identity disagree.",
                    )

        resolution = _resolve_migration_blocking_members(db, lock_identities=True)
        migration_blockers = set(resolution.blocker_ids)
        removed_members = len(migration_blockers)
        reactivated_members = 0
        for member in members:
            if member.id in resolution.reactivate_native_ids and not member.is_active:
                # The person's only active row is a duplicate compat row being
                # removed; keeping their effective membership means restoring
                # the native row, not inventing a new one.
                member.is_active = True
                reactivated_members += 1
        if migration_blockers:
            # Core deletion is intentional: this command runs before migration
            # 440, so ORM relationship cascades must not query the not-yet-created
            # responsibility table. After 440 the FK itself cascades safely.
            db.execute(
                delete(ServiceTeamMember).where(
                    ServiceTeamMember.id.in_(migration_blockers)
                )
            )
        cleared_managers = 0
        for team in teams:
            if team.manager_person_id is not None:
                team.manager_person_id = None
                cleared_managers += 1
        retired_sources = 0
        abandoned_member_entries = 0
        for setting in settings:
            if setting.is_active:
                if setting.key == "support_service_team_members" and isinstance(
                    setting.value_json, list
                ):
                    # Deliberately not imported; counted so the operator record
                    # shows how many legacy member entries were left behind.
                    abandoned_member_entries = len(setting.value_json)
                setting.is_active = False
                retired_sources += 1

        if not (retired_sources or cleared_managers or removed_members):
            return ServiceTeamSourceRetirementOutcome(0, 0, 0, replayed=True)
        db.flush()
        actor_value = str(command.context.actor or "").rsplit(":", maxsplit=1)[-1]
        try:
            actor_id = str(UUID(actor_value))
            actor_type = AuditActorType.user
        except (TypeError, ValueError):
            actor_id = None
            actor_type = AuditActorType.system
        evidence = {
            "schema_version": 1,
            "owner": OWNER,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "pointer_contract_count": len(TEAM_POINTERS),
            "retired_source_count": retired_sources,
            "cleared_manager_pointer_count": cleared_managers,
            "removed_migration_blocking_membership_count": removed_members,
            "reactivated_native_membership_count": reactivated_members,
            "abandoned_legacy_member_entry_count": abandoned_member_entries,
        }
        stage_audit_event(
            db,
            action="service_team.legacy_sources_retired",
            entity_type="service_team_source_retirement",
            entity_id=str(command.context.command_id),
            actor_type=actor_type,
            actor_id=actor_id,
            metadata=evidence,
        )
        emit_event(
            db,
            EventType.service_team_changed,
            {
                **evidence,
                "aggregate_type": "service_team_source_retirement",
                "aggregate_id": str(command.context.command_id),
                "aggregate_version": str(command.context.command_id),
            },
            actor=command.context.actor,
        )
        return ServiceTeamSourceRetirementOutcome(
            retired_source_count=retired_sources,
            cleared_manager_pointer_count=cleared_managers,
            removed_migration_blocking_membership_count=removed_members,
            replayed=False,
            reactivated_native_membership_count=reactivated_members,
            abandoned_legacy_member_entry_count=abandoned_member_entries,
        )

    return execute_owner_command(
        db,
        definition=_RETIRE,
        context=command.context,
        operation=apply,
    )
