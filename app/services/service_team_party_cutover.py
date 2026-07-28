"""Guarded pre-migration adoption for legacy CRM service-team identities.

Migration 426 can only cut service teams over after every referenced legacy
person has a canonical Party identity.  This coordinator consumes one private,
reviewed, digest-bound plan before Alembic reaches that migration.  It creates
only the predetermined Person Parties, records CRM references, binds explicitly
selected SystemUsers, imports exact legacy memberships, and stages PII-free
audit/event evidence in one transaction.

The read-only audit deliberately reports aggregate counts only.  Neither the
audit nor an approved plan changes credentials, RBAC, login state, team
lifecycle, manager selection, or any non-service-team identity.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Self
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.party import (
    Party,
    PartyDataClassification,
    PartyExternalReference,
    PartyIdentityStatus,
    PartyType,
)
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamMember,
    ServiceTeamMemberRole,
)
from app.models.system_user import SystemUser
from app.services import party as party_registry
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.settings_spec import (
    resolve_legacy_service_team_cutover_settings,
)

OWNER = "operations.service_team_party_cutover"
READINESS_CONCERN = "service-team Party cutover readiness"
ADOPTION_CONCERN = "approved service-team Party cutover adoption"
SOURCE_SYSTEM = "dotmac_crm"
SOURCE_ENTITY_TYPE = "person"
PLAN_SCHEMA_VERSION = 1
APPROVAL_SCHEMA_VERSION = 1
MAX_IDENTITIES_PER_EXECUTION = 500
MAX_MEMBERSHIPS_PER_EXECUTION = 2500
MAX_APPROVAL_WINDOW = timedelta(hours=24)
MAX_FUTURE_APPROVAL_SKEW = timedelta(minutes=5)
RECEIPT_ACTION = "service_team.party_cutover_adopted"
RECEIPT_ENTITY_TYPE = "service_team_party_cutover"

_ADOPT = OwnerCommandDefinition(
    owner=OWNER,
    concern=ADOPTION_CONCERN,
    name="adopt_service_team_party_cutover",
)


class ServiceTeamPartyCutoverError(DomainError):
    """Stable, transport-neutral rejection of an unsafe cutover operation."""


class IdentityDecisionKind(StrEnum):
    """Allowed reviewed outcomes for one referenced CRM Person."""

    bind = "bind"
    identity_only = "identity_only"


@dataclass(frozen=True)
class ServiceTeamPartyCutoverAudit:
    """PII-free aggregate readiness for migration 426."""

    team_count: int
    active_team_count: int
    duplicate_casefolded_team_name_count: int
    manager_reference_count: int
    manager_ready_count: int
    manager_blocked_count: int
    membership_count: int
    active_membership_count: int
    membership_ready_count: int
    membership_blocked_count: int
    workflow_setting_team_count: int
    workflow_setting_team_blocked_count: int
    workflow_setting_member_count: int
    workflow_setting_member_ready_count: int
    workflow_setting_member_blocked_count: int
    workflow_setting_member_team_blocked_count: int
    workflow_setting_malformed_entry_count: int

    @property
    def blocker_count(self) -> int:
        return (
            self.duplicate_casefolded_team_name_count
            + self.manager_blocked_count
            + self.membership_blocked_count
            + self.workflow_setting_team_blocked_count
            + self.workflow_setting_member_blocked_count
            + self.workflow_setting_member_team_blocked_count
            + self.workflow_setting_malformed_entry_count
        )

    @property
    def ready(self) -> bool:
        return self.blocker_count == 0

    def summary(self) -> dict[str, int | bool]:
        return {
            "ready": self.ready,
            "blocker_count": self.blocker_count,
            "team_count": self.team_count,
            "active_team_count": self.active_team_count,
            "duplicate_casefolded_team_name_count": (
                self.duplicate_casefolded_team_name_count
            ),
            "manager_reference_count": self.manager_reference_count,
            "manager_ready_count": self.manager_ready_count,
            "manager_blocked_count": self.manager_blocked_count,
            "membership_count": self.membership_count,
            "active_membership_count": self.active_membership_count,
            "membership_ready_count": self.membership_ready_count,
            "membership_blocked_count": self.membership_blocked_count,
            "workflow_setting_team_count": self.workflow_setting_team_count,
            "workflow_setting_team_blocked_count": (
                self.workflow_setting_team_blocked_count
            ),
            "workflow_setting_member_count": self.workflow_setting_member_count,
            "workflow_setting_member_ready_count": (
                self.workflow_setting_member_ready_count
            ),
            "workflow_setting_member_blocked_count": (
                self.workflow_setting_member_blocked_count
            ),
            "workflow_setting_member_team_blocked_count": (
                self.workflow_setting_member_team_blocked_count
            ),
            "workflow_setting_malformed_entry_count": (
                self.workflow_setting_malformed_entry_count
            ),
        }


@dataclass(frozen=True)
class PlannedStaffIdentity:
    """One reviewed CRM Person identity and optional staff-principal binding."""

    legacy_person_id: UUID
    display_name: str
    decision: IdentityDecisionKind
    decision_id: UUID
    reason_sha256: str
    system_user_id: UUID | None = None

    def digest_payload(self) -> dict[str, object]:
        return {
            "legacy_person_id": str(self.legacy_person_id),
            "display_name": self.display_name,
            "decision": self.decision.value,
            "decision_id": str(self.decision_id),
            "reason_sha256": self.reason_sha256,
            "system_user_id": (
                str(self.system_user_id) if self.system_user_id is not None else None
            ),
        }

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        item = _object_payload(payload, "identity")
        try:
            return cls(
                legacy_person_id=UUID(str(item["legacy_person_id"])),
                display_name=str(item["display_name"]),
                decision=IdentityDecisionKind(str(item["decision"])),
                decision_id=UUID(str(item["decision_id"])),
                reason_sha256=str(item["reason_sha256"]),
                system_user_id=(
                    UUID(str(item["system_user_id"]))
                    if item.get("system_user_id") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _error(
                "invalid_plan",
                "The cutover plan contains an invalid identity decision.",
            ) from exc


@dataclass(frozen=True)
class PlannedServiceTeamMembership:
    """One exact CRM membership row to adopt into native service-team state."""

    membership_id: UUID
    team_id: UUID
    legacy_person_id: UUID
    role: ServiceTeamMemberRole
    is_active: bool
    created_at: datetime

    def digest_payload(self) -> dict[str, object]:
        return {
            "membership_id": str(self.membership_id),
            "team_id": str(self.team_id),
            "legacy_person_id": str(self.legacy_person_id),
            "role": self.role.value,
            "is_active": self.is_active,
            "created_at": _utc(self.created_at).isoformat(),
        }

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        item = _object_payload(payload, "membership")
        try:
            created_at = datetime.fromisoformat(
                str(item["created_at"]).replace("Z", "+00:00")
            )
            return cls(
                membership_id=UUID(str(item["membership_id"])),
                team_id=UUID(str(item["team_id"])),
                legacy_person_id=UUID(str(item["legacy_person_id"])),
                role=ServiceTeamMemberRole(str(item["role"])),
                is_active=_strict_bool(item["is_active"], "membership.is_active"),
                created_at=created_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _error(
                "invalid_plan",
                "The cutover plan contains an invalid membership.",
            ) from exc


@dataclass(frozen=True)
class ServiceTeamPartyCutoverPlan:
    """Private, immutable source snapshot and reviewed adoption decisions."""

    source_snapshot_sha256: str
    decision_file_sha256: str
    planned_at: datetime
    identities: tuple[PlannedStaffIdentity, ...]
    memberships: tuple[PlannedServiceTeamMembership, ...]
    source_system: str = SOURCE_SYSTEM
    schema_version: int = PLAN_SCHEMA_VERSION

    def digest_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_system": self.source_system,
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "decision_file_sha256": self.decision_file_sha256,
            "planned_at": _utc(self.planned_at).isoformat(),
            "identities": [
                item.digest_payload()
                for item in sorted(
                    self.identities, key=lambda row: str(row.legacy_person_id)
                )
            ],
            "memberships": [
                item.digest_payload()
                for item in sorted(
                    self.memberships, key=lambda row: str(row.membership_id)
                )
            ],
        }

    @property
    def plan_digest(self) -> str:
        return _payload_digest(self.digest_payload())

    def file_payload(self) -> dict[str, object]:
        return {**self.digest_payload(), "plan_digest": self.plan_digest}

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        item = _object_payload(payload, "plan")
        try:
            planned_at = datetime.fromisoformat(
                str(item["planned_at"]).replace("Z", "+00:00")
            )
            identities_payload = item["identities"]
            memberships_payload = item["memberships"]
            if not isinstance(identities_payload, list) or not isinstance(
                memberships_payload, list
            ):
                raise TypeError
            plan = cls(
                schema_version=int(str(item["schema_version"])),
                source_system=str(item["source_system"]),
                source_snapshot_sha256=str(item["source_snapshot_sha256"]),
                decision_file_sha256=str(item["decision_file_sha256"]),
                planned_at=planned_at,
                identities=tuple(
                    PlannedStaffIdentity.from_payload(row) for row in identities_payload
                ),
                memberships=tuple(
                    PlannedServiceTeamMembership.from_payload(row)
                    for row in memberships_payload
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ServiceTeamPartyCutoverError):
                raise
            raise _error(
                "invalid_plan",
                "The service-team cutover plan is malformed.",
            ) from exc
        supplied_digest = str(item.get("plan_digest") or "")
        _validate_plan(plan)
        if supplied_digest != plan.plan_digest:
            raise _error(
                "invalid_plan",
                "The service-team cutover plan digest does not match its content.",
            )
        return plan


@dataclass(frozen=True)
class ServiceTeamPartyCutoverApproval:
    """Separate, expiring approval for one exact private plan artifact."""

    plan_digest: str
    plan_file_sha256: str
    decision_file_sha256: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    reason: str
    maximum_identities: int
    maximum_memberships: int
    schema_version: int = APPROVAL_SCHEMA_VERSION

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        item = _object_payload(payload, "approval")
        try:
            return cls(
                schema_version=int(str(item["schema_version"])),
                plan_digest=str(item["plan_digest"]),
                plan_file_sha256=str(item["plan_file_sha256"]),
                decision_file_sha256=str(item["decision_file_sha256"]),
                approved_by=str(item["approved_by"]),
                approved_at=datetime.fromisoformat(
                    str(item["approved_at"]).replace("Z", "+00:00")
                ),
                expires_at=datetime.fromisoformat(
                    str(item["expires_at"]).replace("Z", "+00:00")
                ),
                reason=str(item["reason"]),
                maximum_identities=int(str(item["maximum_identities"])),
                maximum_memberships=int(str(item["maximum_memberships"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _error(
                "approval_invalid",
                "The service-team cutover approval is malformed.",
            ) from exc


@dataclass(frozen=True)
class AdoptServiceTeamPartyCutover:
    """Command for one exact, separately approved cutover plan."""

    context: CommandContext
    plan: ServiceTeamPartyCutoverPlan
    approval: ServiceTeamPartyCutoverApproval
    plan_file_sha256: str
    approval_file_sha256: str


@dataclass(frozen=True)
class ServiceTeamPartyCutoverOutcome:
    plan_digest: str
    parties_created: int
    principals_bound: int
    memberships_created: int
    replayed: bool


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> ServiceTeamPartyCutoverError:
    return ServiceTeamPartyCutoverError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _object_payload(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _error("invalid_plan", f"The cutover {label} must be an object.")
    return value


def _strict_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise _error(
            "invalid_plan",
            "The cutover plan contains an invalid boolean.",
            field=field,
        )
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise _error(
            "invalid_plan",
            "Cutover timestamps must include a timezone offset.",
        )
    return value.astimezone(UTC)


def _payload_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_sha256(value: str, field: str) -> str:
    cleaned = value.strip()
    if len(cleaned) != 64 or cleaned != cleaned.lower():
        raise _error(
            "invalid_plan",
            "The cutover plan contains an invalid SHA-256 digest.",
            field=field,
        )
    try:
        int(cleaned, 16)
    except ValueError as exc:
        raise _error(
            "invalid_plan",
            "The cutover plan contains an invalid SHA-256 digest.",
            field=field,
        ) from exc
    return cleaned


def _clean_display_name(value: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > 200:
        raise _error(
            "invalid_plan",
            "Every planned identity needs a display name of at most 200 characters.",
        )
    return cleaned


def _validate_plan(plan: ServiceTeamPartyCutoverPlan) -> None:
    errors: list[str] = []
    if plan.schema_version != PLAN_SCHEMA_VERSION:
        errors.append("unsupported plan schema version")
    if plan.source_system != SOURCE_SYSTEM:
        errors.append("unsupported source system")
    try:
        _require_sha256(plan.source_snapshot_sha256, "source_snapshot_sha256")
        _require_sha256(plan.decision_file_sha256, "decision_file_sha256")
        _utc(plan.planned_at)
    except ServiceTeamPartyCutoverError as exc:
        errors.append(exc.message)
    if not plan.identities:
        errors.append("plan has no identities")
    if len(plan.identities) > MAX_IDENTITIES_PER_EXECUTION:
        errors.append("plan exceeds the identity execution limit")
    if len(plan.memberships) > MAX_MEMBERSHIPS_PER_EXECUTION:
        errors.append("plan exceeds the membership execution limit")

    identity_ids: set[UUID] = set()
    system_user_ids: set[UUID] = set()
    for identity in plan.identities:
        if identity.legacy_person_id in identity_ids:
            errors.append("plan repeats a legacy Person identity")
        identity_ids.add(identity.legacy_person_id)
        try:
            _clean_display_name(identity.display_name)
            _require_sha256(identity.reason_sha256, "identity.reason_sha256")
        except ServiceTeamPartyCutoverError as exc:
            errors.append(exc.message)
        if identity.decision is IdentityDecisionKind.bind:
            if identity.system_user_id is None:
                errors.append("bind decision has no SystemUser")
        elif identity.system_user_id is not None:
            errors.append("identity-only decision unexpectedly names a SystemUser")
        if identity.system_user_id is not None:
            if identity.system_user_id in system_user_ids:
                errors.append("plan binds one SystemUser to multiple identities")
            system_user_ids.add(identity.system_user_id)

    membership_ids: set[UUID] = set()
    membership_keys: set[tuple[UUID, UUID]] = set()
    active_identity_ids: set[UUID] = set()
    for membership in plan.memberships:
        if membership.membership_id in membership_ids:
            errors.append("plan repeats a membership identifier")
        membership_ids.add(membership.membership_id)
        key = (membership.team_id, membership.legacy_person_id)
        if key in membership_keys:
            errors.append("plan repeats a team/person membership")
        membership_keys.add(key)
        if membership.legacy_person_id not in identity_ids:
            errors.append("membership references an identity absent from the plan")
        if membership.is_active:
            active_identity_ids.add(membership.legacy_person_id)
        try:
            _utc(membership.created_at)
        except ServiceTeamPartyCutoverError as exc:
            errors.append(exc.message)

    bindings = {
        item.legacy_person_id
        for item in plan.identities
        if item.system_user_id is not None
    }
    if active_identity_ids - bindings:
        errors.append("active memberships require reviewed SystemUser bindings")
    if errors:
        raise _error(
            "invalid_plan",
            "The service-team cutover plan failed validation.",
            error_count=len(errors),
            errors=tuple(sorted(set(errors))),
        )


def _resolved_person_party(
    *,
    stored_id: UUID,
    parties: dict[UUID, Party],
    users_by_id: dict[UUID, SystemUser],
) -> Party | None:
    party = parties.get(stored_id)
    user = users_by_id.get(stored_id)
    if party is not None and party.party_type == PartyType.person.value:
        if (
            user is not None
            and user.person_party_id is not None
            and user.person_party_id != stored_id
        ):
            return None
        return party
    if user is None or user.person_party_id is None:
        return None
    target = parties.get(user.person_party_id)
    if target is None or target.party_type != PartyType.person.value:
        return None
    return target


def _active_principal_for_party(
    *,
    party_id: UUID,
    users_by_party: dict[UUID, tuple[SystemUser, ...]],
) -> bool:
    return sum(user.is_active for user in users_by_party.get(party_id, ())) == 1


def audit_service_team_party_cutover(
    db: Session,
) -> ServiceTeamPartyCutoverAudit:
    """Return aggregate migration-426 readiness without identity values."""

    teams = db.scalars(select(ServiceTeam)).all()
    members = db.scalars(select(ServiceTeamMember)).all()
    parties = {row.id: row for row in db.scalars(select(Party)).all()}
    users = db.scalars(select(SystemUser)).all()
    users_by_id = {row.id: row for row in users}
    mutable_users_by_party: dict[UUID, list[SystemUser]] = {}
    for user in users:
        if user.person_party_id is not None:
            mutable_users_by_party.setdefault(user.person_party_id, []).append(user)
    users_by_party = {
        party_id: tuple(rows) for party_id, rows in mutable_users_by_party.items()
    }

    manager_ready = 0
    manager_blocked = 0
    for team in teams:
        if team.manager_person_id is None:
            continue
        party = _resolved_person_party(
            stored_id=team.manager_person_id,
            parties=parties,
            users_by_id=users_by_id,
        )
        if (
            party is not None
            and party.status == PartyIdentityStatus.active.value
            and _active_principal_for_party(
                party_id=party.id,
                users_by_party=users_by_party,
            )
        ):
            manager_ready += 1
        else:
            manager_blocked += 1

    member_ready = 0
    member_blocked = 0
    for member in members:
        party = _resolved_person_party(
            stored_id=member.person_id,
            parties=parties,
            users_by_id=users_by_id,
        )
        if party is None:
            ready = False
        elif member.is_active:
            ready = bool(
                party.status == PartyIdentityStatus.active.value
                and _active_principal_for_party(
                    party_id=party.id,
                    users_by_party=users_by_party,
                )
            )
        else:
            ready = True
        if ready:
            member_ready += 1
        else:
            member_blocked += 1

    legacy_settings = resolve_legacy_service_team_cutover_settings(db)
    projected_team_names = {team.id: team.name for team in teams}
    projected_name_ids = {team.name.casefold(): team.id for team in teams}
    setting_team_blocked = 0
    for setting_team in legacy_settings.teams:
        existing_name = projected_team_names.get(setting_team.team_id)
        if existing_name is not None:
            if existing_name != setting_team.label:
                setting_team_blocked += 1
            continue
        conflicting_id = projected_name_ids.get(setting_team.label.casefold())
        if conflicting_id is not None and conflicting_id != setting_team.team_id:
            setting_team_blocked += 1
            continue
        projected_team_names[setting_team.team_id] = setting_team.label
        projected_name_ids[setting_team.label.casefold()] = setting_team.team_id

    setting_member_ready = 0
    setting_member_blocked = 0
    setting_member_team_blocked = 0
    for setting_member in legacy_settings.members:
        if setting_member.team_id not in projected_team_names:
            setting_member_team_blocked += 1
        setting_user = users_by_id.get(setting_member.system_user_id)
        party = (
            parties.get(setting_user.person_party_id)
            if setting_user is not None and setting_user.person_party_id is not None
            else None
        )
        if (
            setting_user is not None
            and setting_user.is_active
            and party is not None
            and party.party_type == PartyType.person.value
            and party.status == PartyIdentityStatus.active.value
        ):
            setting_member_ready += 1
        else:
            setting_member_blocked += 1

    duplicate_names = int(
        db.scalar(
            select(func.count()).select_from(
                select(func.lower(ServiceTeam.name))
                .group_by(func.lower(ServiceTeam.name))
                .having(func.count(ServiceTeam.id) > 1)
                .subquery()
            )
        )
        or 0
    )
    manager_count = sum(team.manager_person_id is not None for team in teams)
    return ServiceTeamPartyCutoverAudit(
        team_count=len(teams),
        active_team_count=sum(team.is_active for team in teams),
        duplicate_casefolded_team_name_count=duplicate_names,
        manager_reference_count=manager_count,
        manager_ready_count=manager_ready,
        manager_blocked_count=manager_blocked,
        membership_count=len(members),
        active_membership_count=sum(member.is_active for member in members),
        membership_ready_count=member_ready,
        membership_blocked_count=member_blocked,
        workflow_setting_team_count=len(legacy_settings.teams),
        workflow_setting_team_blocked_count=setting_team_blocked,
        workflow_setting_member_count=len(legacy_settings.members),
        workflow_setting_member_ready_count=setting_member_ready,
        workflow_setting_member_blocked_count=setting_member_blocked,
        workflow_setting_member_team_blocked_count=setting_member_team_blocked,
        workflow_setting_malformed_entry_count=(legacy_settings.malformed_entry_count),
    )


def _validate_approval(
    command: AdoptServiceTeamPartyCutover,
    *,
    executed_at: datetime,
) -> tuple[str, str]:
    approval = command.approval
    plan = command.plan
    errors: list[str] = []
    if approval.schema_version != APPROVAL_SCHEMA_VERSION:
        errors.append("unsupported approval schema version")
    for value, field in (
        (approval.plan_digest, "approval.plan_digest"),
        (approval.plan_file_sha256, "approval.plan_file_sha256"),
        (approval.decision_file_sha256, "approval.decision_file_sha256"),
        (command.plan_file_sha256, "plan_file_sha256"),
        (command.approval_file_sha256, "approval_file_sha256"),
    ):
        try:
            _require_sha256(value, field)
        except ServiceTeamPartyCutoverError:
            errors.append(f"{field} is not a SHA-256 digest")
    approved_by = approval.approved_by.strip()
    reason = approval.reason.strip()
    if not approved_by:
        errors.append("approval.approved_by is required")
    if not reason:
        errors.append("approval.reason is required")
    try:
        approved_at = _utc(approval.approved_at)
        expires_at = _utc(approval.expires_at)
        now = _utc(executed_at)
    except ServiceTeamPartyCutoverError:
        errors.append("approval timestamps must be timezone-aware")
    else:
        if expires_at < approved_at:
            errors.append("approval expires before it was approved")
        if expires_at - approved_at > MAX_APPROVAL_WINDOW:
            errors.append("approval window exceeds 24 hours")
        if approved_at > now + MAX_FUTURE_APPROVAL_SKEW:
            errors.append("approval time is in the future")
        if now > expires_at:
            errors.append("approval has expired")
    if approval.plan_digest != plan.plan_digest:
        errors.append("approval does not match the plan digest")
    if approval.plan_file_sha256 != command.plan_file_sha256:
        errors.append("plan file does not match the approved SHA-256")
    if approval.decision_file_sha256 != plan.decision_file_sha256:
        errors.append("approval does not match the decision file SHA-256")
    if approval.maximum_identities < len(plan.identities):
        errors.append("plan identity count exceeds the approved maximum")
    if approval.maximum_memberships < len(plan.memberships):
        errors.append("plan membership count exceeds the approved maximum")
    if approval.maximum_identities > MAX_IDENTITIES_PER_EXECUTION:
        errors.append("approved identity maximum exceeds the execution limit")
    if approval.maximum_memberships > MAX_MEMBERSHIPS_PER_EXECUTION:
        errors.append("approved membership maximum exceeds the execution limit")
    if errors:
        raise _error(
            "approval_invalid",
            "The service-team cutover approval is invalid or stale.",
            error_count=len(errors),
            errors=tuple(sorted(set(errors))),
        )
    return approved_by, reason


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()


def _receipt_evidence(
    command: AdoptServiceTeamPartyCutover,
    *,
    approved_by: str,
    approval_reason: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "owner": OWNER,
        "plan_digest": command.plan.plan_digest,
        "source_snapshot_sha256": command.plan.source_snapshot_sha256,
        "decision_file_sha256": command.plan.decision_file_sha256,
        "plan_file_sha256": command.plan_file_sha256,
        "approval_file_sha256": command.approval_file_sha256,
        "approved_by_sha256": _text_digest(approved_by),
        "approval_reason_sha256": _text_digest(approval_reason),
        "identity_count": len(command.plan.identities),
        "membership_count": len(command.plan.memberships),
        "command_id": str(command.context.command_id),
        "correlation_id": str(command.context.correlation_id),
    }


def _actor(context: CommandContext) -> tuple[AuditActorType, str | None]:
    raw_actor = str(context.actor or "")
    actor_type_value, separator, actor_id = raw_actor.partition(":")
    try:
        actor_type = AuditActorType(actor_type_value)
    except ValueError:
        return AuditActorType.system, None
    return actor_type, actor_id if separator and actor_id else None


def _lock_execution(db: Session, plan_digest: str) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    lock_key = int.from_bytes(
        hashlib.sha256(f"service-team-cutover:{plan_digest}".encode()).digest()[:8],
        byteorder="big",
        signed=True,
    )
    db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})


def _set_serializable(db: Session) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ WRITE"))


def _receipt(
    db: Session,
    plan_digest: str,
) -> AuditEvent | None:
    return db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.action == RECEIPT_ACTION,
            AuditEvent.entity_type == RECEIPT_ENTITY_TYPE,
            AuditEvent.entity_id == plan_digest,
            AuditEvent.is_success.is_(True),
        )
        .with_for_update()
    )


def _verify_receipt(
    receipt: AuditEvent,
    expected: dict[str, object],
) -> None:
    actual = receipt.metadata_ or {}
    replay_keys = set(expected) - {"command_id", "correlation_id"}
    mismatches = sorted(key for key in replay_keys if actual.get(key) != expected[key])
    if mismatches:
        raise _error(
            "identity_conflict",
            "The existing cutover receipt conflicts with this approved execution.",
            mismatch_count=len(mismatches),
        )


def _lock_teams(
    db: Session,
    plan: ServiceTeamPartyCutoverPlan,
) -> dict[UUID, ServiceTeam]:
    plan_team_ids = {item.team_id for item in plan.memberships}
    teams = db.scalars(
        select(ServiceTeam).order_by(ServiceTeam.id).with_for_update()
    ).all()
    by_id = {team.id: team for team in teams}
    missing = plan_team_ids - set(by_id)
    if missing:
        raise _error(
            "stale_source",
            "The approved plan references service teams absent from Sub.",
            missing_team_count=len(missing),
        )
    return by_id


def _lock_users(
    db: Session,
    plan: ServiceTeamPartyCutoverPlan,
) -> dict[UUID, SystemUser]:
    user_ids = {
        item.system_user_id
        for item in plan.identities
        if item.system_user_id is not None
    }
    if not user_ids:
        return {}
    users = db.scalars(
        select(SystemUser)
        .where(SystemUser.id.in_(user_ids))
        .order_by(SystemUser.id)
        .with_for_update()
    ).all()
    by_id = {user.id: user for user in users}
    missing = user_ids - set(by_id)
    if missing:
        raise _error(
            "stale_source",
            "A reviewed SystemUser disappeared before cutover execution.",
            missing_system_user_count=len(missing),
        )
    return by_id


def _ensure_external_reference(
    db: Session,
    *,
    party_id: UUID,
    legacy_person_id: UUID,
    plan_digest: str,
) -> None:
    by_external = db.scalar(
        select(PartyExternalReference)
        .where(
            PartyExternalReference.source_system == SOURCE_SYSTEM,
            PartyExternalReference.entity_type == SOURCE_ENTITY_TYPE,
            PartyExternalReference.external_id == str(legacy_person_id),
        )
        .with_for_update()
    )
    if by_external is not None:
        if by_external.party_id != party_id or not by_external.is_active:
            raise _error(
                "identity_conflict",
                "A CRM Person reference is already bound to another Party.",
            )
        return
    by_party = db.scalar(
        select(PartyExternalReference)
        .where(
            PartyExternalReference.party_id == party_id,
            PartyExternalReference.source_system == SOURCE_SYSTEM,
            PartyExternalReference.entity_type == SOURCE_ENTITY_TYPE,
        )
        .with_for_update()
    )
    if by_party is not None:
        raise _error(
            "identity_conflict",
            "The selected Party already carries a different CRM Person reference.",
        )
    party_registry.add_external_reference(
        db,
        party_id=party_id,
        source_system=SOURCE_SYSTEM,
        entity_type=SOURCE_ENTITY_TYPE,
        external_id=str(legacy_person_id),
        metadata={
            "service_team_cutover": {
                "schema_version": 1,
                "plan_digest": plan_digest,
            }
        },
    )


def _adopt_identities(
    db: Session,
    *,
    plan: ServiceTeamPartyCutoverPlan,
    users: dict[UUID, SystemUser],
) -> tuple[int, int]:
    parties_created = 0
    principals_bound = 0
    for identity in sorted(plan.identities, key=lambda row: str(row.legacy_person_id)):
        party = db.scalar(
            select(Party).where(Party.id == identity.legacy_person_id).with_for_update()
        )
        if party is None:
            party = party_registry.create_party(
                db,
                party_id=identity.legacy_person_id,
                party_type=PartyType.person,
                display_name=_clean_display_name(identity.display_name),
                data_classification=PartyDataClassification.production,
                metadata={
                    "service_team_cutover": {
                        "schema_version": 1,
                        "plan_digest": plan.plan_digest,
                    }
                },
            )
            parties_created += 1
        elif (
            party.party_type != PartyType.person.value
            or party.status != PartyIdentityStatus.active.value
        ):
            raise _error(
                "identity_conflict",
                "A planned Person Party identifier already has incompatible state.",
            )
        _ensure_external_reference(
            db,
            party_id=party.id,
            legacy_person_id=identity.legacy_person_id,
            plan_digest=plan.plan_digest,
        )
        if identity.system_user_id is None:
            continue
        user = users[identity.system_user_id]
        was_unbound = user.person_party_id is None
        try:
            party_registry.bind_system_user_principal(
                db,
                system_user_id=user.id,
                person_party_id=party.id,
                source=f"service_team_cutover:{plan.plan_digest[:32]}",
                reason=(
                    f"decision={identity.decision_id};"
                    f"reason_sha256={identity.reason_sha256}"
                ),
            )
        except ValueError as exc:
            raise _error(
                "identity_conflict",
                "A reviewed staff principal cannot be bound to the planned Party.",
            ) from exc
        if was_unbound:
            principals_bound += 1
    return parties_created, principals_bound


def _require_active_identity(
    *,
    legacy_person_id: UUID,
    identities: dict[UUID, PlannedStaffIdentity],
    users: dict[UUID, SystemUser],
) -> None:
    identity = identities.get(legacy_person_id)
    if identity is None or identity.system_user_id is None:
        raise _error(
            "stale_source",
            "An active service-team identity is absent from the reviewed bindings.",
        )
    user = users[identity.system_user_id]
    if not user.is_active or user.person_party_id != legacy_person_id:
        raise _error(
            "stale_source",
            "An active service-team identity no longer has an active reviewed principal.",
        )


def _validate_manager_identities(
    db: Session,
    *,
    teams: dict[UUID, ServiceTeam],
    plan: ServiceTeamPartyCutoverPlan,
    users: dict[UUID, SystemUser],
) -> None:
    identities = {item.legacy_person_id: item for item in plan.identities}
    parties = {
        row.id: row
        for row in db.scalars(
            select(Party)
            .where(
                Party.id.in_(
                    {
                        team.manager_person_id
                        for team in teams.values()
                        if team.manager_person_id is not None
                    }
                )
            )
            .with_for_update()
        ).all()
    }
    users_by_party = {
        row.person_party_id: row
        for row in db.scalars(
            select(SystemUser)
            .where(SystemUser.person_party_id.is_not(None))
            .order_by(SystemUser.id)
            .with_for_update()
        ).all()
        if row.person_party_id is not None
    }
    for team in teams.values():
        manager_id = team.manager_person_id
        if manager_id is None:
            continue
        party = parties.get(manager_id)
        principal = users_by_party.get(manager_id)
        if (
            party is not None
            and party.party_type == PartyType.person.value
            and party.status == PartyIdentityStatus.active.value
            and principal is not None
            and principal.is_active
        ):
            continue
        _require_active_identity(
            legacy_person_id=manager_id,
            identities=identities,
            users=users,
        )


def _same_instant(left: datetime, right: datetime) -> bool:
    left_utc = left.replace(tzinfo=UTC) if left.tzinfo is None else left.astimezone(UTC)
    right_utc = (
        right.replace(tzinfo=UTC) if right.tzinfo is None else right.astimezone(UTC)
    )
    return left_utc == right_utc


def _adopt_memberships(
    db: Session,
    *,
    plan: ServiceTeamPartyCutoverPlan,
    users: dict[UUID, SystemUser],
) -> int:
    identities = {item.legacy_person_id: item for item in plan.identities}
    created = 0
    for item in sorted(plan.memberships, key=lambda row: str(row.membership_id)):
        if item.is_active:
            _require_active_identity(
                legacy_person_id=item.legacy_person_id,
                identities=identities,
                users=users,
            )
        by_id = db.scalar(
            select(ServiceTeamMember)
            .where(ServiceTeamMember.id == item.membership_id)
            .with_for_update()
        )
        if by_id is not None:
            exact = (
                by_id.team_id == item.team_id
                and by_id.person_id == item.legacy_person_id
                and by_id.role == item.role.value
                and by_id.is_active is item.is_active
                and _same_instant(by_id.created_at, item.created_at)
            )
            if not exact:
                raise _error(
                    "membership_conflict",
                    "A planned membership identifier has incompatible native state.",
                )
            continue
        by_identity = db.scalar(
            select(ServiceTeamMember)
            .where(
                ServiceTeamMember.team_id == item.team_id,
                ServiceTeamMember.person_id == item.legacy_person_id,
            )
            .with_for_update()
        )
        if by_identity is not None:
            raise _error(
                "membership_conflict",
                "A planned team/person membership has a different native identifier.",
            )
        db.add(
            ServiceTeamMember(
                id=item.membership_id,
                team_id=item.team_id,
                person_id=item.legacy_person_id,
                role=item.role.value,
                is_active=item.is_active,
                created_at=_utc(item.created_at),
            )
        )
        db.flush()
        created += 1
    return created


def _verify_applied_plan(
    db: Session,
    *,
    plan: ServiceTeamPartyCutoverPlan,
) -> None:
    errors = 0
    for identity in plan.identities:
        party = db.get(Party, identity.legacy_person_id)
        if (
            party is None
            or party.party_type != PartyType.person.value
            or party.status != PartyIdentityStatus.active.value
        ):
            errors += 1
            continue
        reference = db.scalar(
            select(PartyExternalReference).where(
                PartyExternalReference.party_id == party.id,
                PartyExternalReference.source_system == SOURCE_SYSTEM,
                PartyExternalReference.entity_type == SOURCE_ENTITY_TYPE,
                PartyExternalReference.external_id == str(identity.legacy_person_id),
                PartyExternalReference.is_active.is_(True),
            )
        )
        if reference is None:
            errors += 1
        if identity.system_user_id is not None:
            user = db.get(SystemUser, identity.system_user_id)
            if user is None or user.person_party_id != party.id:
                errors += 1
    for item in plan.memberships:
        member = db.get(ServiceTeamMember, item.membership_id)
        if (
            member is None
            or member.team_id != item.team_id
            or member.person_id != item.legacy_person_id
            or member.role != item.role.value
            or member.is_active is not item.is_active
            or not _same_instant(member.created_at, item.created_at)
        ):
            errors += 1
    if errors:
        raise _error(
            "identity_conflict",
            "Applied service-team cutover state drifted from its receipt.",
            drift_count=errors,
        )


def adopt_service_team_party_cutover(
    db: Session,
    command: AdoptServiceTeamPartyCutover,
    *,
    executed_at: datetime | None = None,
) -> ServiceTeamPartyCutoverOutcome:
    """Atomically apply or verify one exact, separately approved cutover plan."""

    _validate_plan(command.plan)
    now = executed_at or datetime.now(UTC)
    approved_by, approval_reason = _validate_approval(command, executed_at=now)
    expected_receipt = _receipt_evidence(
        command,
        approved_by=approved_by,
        approval_reason=approval_reason,
    )

    def apply() -> ServiceTeamPartyCutoverOutcome:
        _set_serializable(db)
        _lock_execution(db, command.plan.plan_digest)
        existing_receipt = _receipt(db, command.plan.plan_digest)
        if existing_receipt is not None:
            _verify_receipt(existing_receipt, expected_receipt)
            _verify_applied_plan(db, plan=command.plan)
            return ServiceTeamPartyCutoverOutcome(
                plan_digest=command.plan.plan_digest,
                parties_created=0,
                principals_bound=0,
                memberships_created=0,
                replayed=True,
            )

        teams = _lock_teams(db, command.plan)
        users = _lock_users(db, command.plan)
        parties_created, principals_bound = _adopt_identities(
            db,
            plan=command.plan,
            users=users,
        )
        _validate_manager_identities(
            db,
            teams=teams,
            plan=command.plan,
            users=users,
        )
        memberships_created = _adopt_memberships(
            db,
            plan=command.plan,
            users=users,
        )
        readiness = audit_service_team_party_cutover(db)
        if not readiness.ready:
            raise _error(
                "not_ready",
                "The approved adoption does not satisfy migration 426 readiness.",
                blocker_count=readiness.blocker_count,
            )
        actor_type, actor_id = _actor(command.context)
        stage_audit_event(
            db,
            action=RECEIPT_ACTION,
            entity_type=RECEIPT_ENTITY_TYPE,
            entity_id=command.plan.plan_digest,
            actor_type=actor_type,
            actor_id=actor_id,
            request_id=str(command.context.correlation_id),
            metadata=expected_receipt,
        )
        emit_event(
            db,
            EventType.service_team_party_cutover_adopted,
            {
                **expected_receipt,
                "aggregate_type": RECEIPT_ENTITY_TYPE,
                "aggregate_id": command.plan.plan_digest,
                "aggregate_version": str(command.context.command_id),
            },
            actor=command.context.actor,
        )
        return ServiceTeamPartyCutoverOutcome(
            plan_digest=command.plan.plan_digest,
            parties_created=parties_created,
            principals_bound=principals_bound,
            memberships_created=memberships_created,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_ADOPT,
        context=command.context,
        operation=apply,
    )
