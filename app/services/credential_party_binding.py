"""Canonical credential-to-Party authentication projection owner.

Migration 525 is additive: legacy principal foreign keys remain authoritative
for login until a later reader cutover. This service is the only runtime writer
of the new projection. It binds a credential to a Person Party, one installed
verifier binding, Sub's operator tenant, and reviewed evidence in one command.

The person-only rule is scoped to ``UserCredential.party_id``. A Subscriber may
correctly belong to an Organization Party; its credential instead binds to a
reviewed human administrator. This command never rewrites the Subscriber owner
to satisfy authentication.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import NoReturn
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.models.audit import AuditActorType
from app.models.auth import AuthenticationBinding, UserCredential
from app.models.party import Party, PartyIdentityStatus, PartyType
from app.services.audit_adapter import stage_audit_event
from app.services.authentication_mechanism_registry import (
    UndeclaredAuthenticationMechanismError,
    declared_authentication_mechanisms,
    require_declared_mechanism,
)
from app.services.domain_errors import DomainError
from app.services.operator_tenant import operator_tenant, operator_tenant_id
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

_OWNER = "party.credential_authentication_projection"
_COMMAND_SCOPE = "party:credential_authentication_projection"
_BIND_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="credential Party authentication projection",
    name="bind_credential_party",
)


class CredentialBindingError(DomainError):
    """Stable, transport-neutral credential projection failure."""


class CredentialPrincipalKind(StrEnum):
    """Open-owner principal variants accepted by the typed projection command."""

    subscriber = "subscriber"
    system_user = "system_user"
    reseller_user = "reseller_user"


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise CredentialBindingError(
        code=f"{_OWNER}.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class CredentialPartyBinding:
    """One reviewed, exact credential projection command."""

    context: CommandContext
    credential_id: UUID
    expected_principal_kind: CredentialPrincipalKind
    expected_principal_id: UUID
    party_id: UUID
    authentication_binding_id: UUID
    tenant_id: UUID
    binding_source: str
    binding_reason: str


@dataclass(frozen=True, slots=True)
class CredentialPartyBindingOutcome:
    credential_id: UUID
    party_id: UUID
    authentication_binding_id: UUID
    tenant_id: UUID
    bound_at: datetime
    replayed: bool


def _required_text(value: str | None, field: str) -> str:
    text = (value or "").strip()
    if not text:
        _error("invalid_command", f"{field} is required.", field=field)
    return text


def _provider_code(credential: UserCredential) -> str:
    provider = credential.provider
    return str(provider.value if hasattr(provider, "value") else provider)


def _as_utc(value: datetime) -> datetime:
    """Return a stable aware instant across PostgreSQL and SQLite tests."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _locked_credential(db: Session, credential_id: UUID) -> UserCredential:
    credential = db.scalar(
        select(UserCredential)
        .where(UserCredential.id == credential_id)
        .with_for_update()
    )
    if credential is None:
        _error("credential_missing", "The credential was not found.")
    return credential


def _locked_person_party(db: Session, party_id: UUID) -> Party:
    party = db.scalar(select(Party).where(Party.id == party_id).with_for_update())
    if party is None:
        _error("party_missing", "The Party was not found.")
    if party.party_type != PartyType.person.value:
        _error(
            "person_required",
            "A credential may bind only to a Person Party; an organization "
            "uses a reviewed human administrator and membership context.",
        )
    if party.status not in {
        PartyIdentityStatus.active.value,
        PartyIdentityStatus.quarantined.value,
    }:
        _error(
            "party_unavailable",
            "The Person Party is not in a bindable identity state.",
            party_status=party.status,
        )
    return party


def _locked_binding(db: Session, binding_id: UUID) -> AuthenticationBinding:
    binding = db.scalar(
        select(AuthenticationBinding)
        .where(AuthenticationBinding.id == binding_id)
        .with_for_update()
    )
    if binding is None:
        _error(
            "authentication_binding_missing",
            "The authentication binding was not found.",
        )
    if not binding.is_active:
        _error(
            "authentication_binding_inactive",
            "The authentication binding is inactive.",
            binding_key=binding.binding_key,
        )
    try:
        require_declared_mechanism(binding.mechanism_code)
    except UndeclaredAuthenticationMechanismError:
        _error(
            "undeclared_mechanism",
            "The authentication binding names a mechanism no SOT owner declares.",
            mechanism_code=binding.mechanism_code,
        )
    return binding


def _require_legacy_principal_alignment(
    db: Session,
    credential: UserCredential,
    party: Party,
    *,
    expected_principal_kind: CredentialPrincipalKind,
    expected_principal_id: UUID,
) -> None:
    """Prove the command and R1 agree with the authoritative legacy principal."""

    from app.models.subscriber import ResellerUser, Subscriber
    from app.models.system_user import SystemUser

    actual_principal_kind: CredentialPrincipalKind
    actual_principal_id: UUID
    if credential.system_user_id is not None:
        actual_principal_kind = CredentialPrincipalKind.system_user
        actual_principal_id = credential.system_user_id
    elif credential.reseller_user_id is not None:
        actual_principal_kind = CredentialPrincipalKind.reseller_user
        actual_principal_id = credential.reseller_user_id
    elif credential.subscriber_id is not None:
        actual_principal_kind = CredentialPrincipalKind.subscriber
        actual_principal_id = credential.subscriber_id
    else:
        _error(
            "principal_mismatch",
            "The credential has no authoritative legacy principal reference.",
            expected_principal_kind=expected_principal_kind.value,
            actual_principal_kind="none",
        )

    if (
        actual_principal_kind is not expected_principal_kind
        or actual_principal_id != expected_principal_id
    ):
        _error(
            "principal_mismatch",
            "The credential's authoritative legacy principal differs from the "
            "typed command.",
            expected_principal_kind=expected_principal_kind.value,
            actual_principal_kind=actual_principal_kind.value,
        )

    principal_party_id: UUID | None
    principal_party_type: str | None
    if actual_principal_kind is CredentialPrincipalKind.system_user:
        system_user = db.scalar(
            select(SystemUser)
            .where(SystemUser.id == actual_principal_id)
            .with_for_update()
        )
        principal_party_id = (
            system_user.person_party_id if system_user is not None else None
        )
        principal_party_type = PartyType.person.value
    elif actual_principal_kind is CredentialPrincipalKind.reseller_user:
        reseller_user = db.scalar(
            select(ResellerUser)
            .where(ResellerUser.id == actual_principal_id)
            .with_for_update()
        )
        principal_party_id = (
            reseller_user.person_party_id if reseller_user is not None else None
        )
        principal_party_type = PartyType.person.value
    else:
        subscriber = db.scalar(
            select(Subscriber)
            .where(Subscriber.id == actual_principal_id)
            .with_for_update()
        )
        principal_party_id = subscriber.party_id if subscriber is not None else None
        principal_party_type = (
            subscriber.party.party_type
            if subscriber is not None and subscriber.party is not None
            else None
        )

    if principal_party_id is None:
        _error(
            "principal_party_missing",
            "The credential's authoritative legacy principal has no reviewed "
            "Party binding yet.",
        )
    if principal_party_type == PartyType.organization.value:
        _error(
            "organization_administrator_required",
            "An organization-owned account needs separately reviewed human "
            "administrator evidence before its credential can be projected.",
        )
    if principal_party_id != party.id:
        _error(
            "principal_party_mismatch",
            "The requested Person Party differs from the authoritative legacy "
            "principal binding.",
        )


def resolve_binding_for_mechanism(
    db: Session, mechanism_code: str
) -> AuthenticationBinding:
    """Return the sole active installed binding for one declared mechanism.

    This convenience is valid only while cardinality is one. It refuses rather
    than guessing as soon as a second verifier binding is installed.
    """

    code = _required_text(mechanism_code, "mechanism_code")
    try:
        code = require_declared_mechanism(code)
    except UndeclaredAuthenticationMechanismError:
        _error(
            "undeclared_mechanism",
            "No SOT owner declares the requested authentication mechanism.",
            mechanism_code=code,
        )
    matches = list(
        db.scalars(
            select(AuthenticationBinding)
            .where(AuthenticationBinding.mechanism_code == code)
            .where(AuthenticationBinding.is_active.is_(True))
        )
    )
    if not matches:
        _error(
            "authentication_binding_missing",
            "No active authentication binding is installed for the mechanism.",
            mechanism_code=code,
        )
    if len(matches) > 1:
        _error(
            "ambiguous_mechanism_binding",
            "More than one active binding exists; name the exact binding.",
            mechanism_code=code,
            binding_count=len(matches),
        )
    return matches[0]


def _projection_values(credential: UserCredential) -> tuple[object, ...]:
    return (
        credential.party_id,
        credential.authentication_binding_id,
        credential.tenant_id,
        credential.party_bound_at,
        credential.party_binding_source,
        credential.party_binding_reason,
    )


def _bind_credential_party(
    db: Session, command: CredentialPartyBinding
) -> CredentialPartyBindingOutcome:
    if command.context.scope != _COMMAND_SCOPE:
        _error(
            "invalid_command",
            "Credential projection command scope is invalid.",
            field="scope",
        )
    source = _required_text(command.binding_source, "binding_source")
    reason = _required_text(command.binding_reason, "binding_reason")
    tenant = operator_tenant(db)
    if command.tenant_id != operator_tenant_id() or command.tenant_id != tenant.id:
        _error(
            "tenant_mismatch",
            "The command tenant is not Sub's provisioned operator tenant.",
        )

    credential = _locked_credential(db, command.credential_id)
    party = _locked_person_party(db, command.party_id)
    _require_legacy_principal_alignment(
        db,
        credential,
        party,
        expected_principal_kind=command.expected_principal_kind,
        expected_principal_id=command.expected_principal_id,
    )
    binding = _locked_binding(db, command.authentication_binding_id)
    provider_code = _provider_code(credential)
    if binding.mechanism_code != provider_code:
        _error(
            "mechanism_mismatch",
            "The authentication binding does not implement the credential provider.",
            credential_provider=provider_code,
            mechanism_code=binding.mechanism_code,
        )

    projection = _projection_values(credential)
    if any(value is not None for value in projection):
        if not all(value is not None for value in projection):
            _error(
                "partial_projection",
                "The credential carries an incomplete Party projection.",
            )
        exact = (
            credential.party_id == party.id
            and credential.authentication_binding_id == binding.id
            and credential.tenant_id == tenant.id
            and credential.party_binding_source == source
            and credential.party_binding_reason == reason
        )
        if not exact:
            _error(
                "repoint_refused",
                "The credential is already projected with different binding "
                "or evidence; repointing requires a separate reviewed workflow.",
            )
        assert credential.party_bound_at is not None
        return CredentialPartyBindingOutcome(
            credential_id=credential.id,
            party_id=party.id,
            authentication_binding_id=binding.id,
            tenant_id=tenant.id,
            bound_at=_as_utc(credential.party_bound_at),
            replayed=True,
        )

    collision = db.scalar(
        select(UserCredential.id)
        .where(UserCredential.id != credential.id)
        .where(UserCredential.tenant_id == tenant.id)
        .where(UserCredential.party_id == party.id)
        .where(UserCredential.authentication_binding_id == binding.id)
        .with_for_update()
    )
    if collision is not None:
        _error(
            "projection_collision",
            "This tenant, Party and authentication binding already identify "
            "another credential; merge policy must be reviewed first.",
        )

    bound_at = datetime.now(UTC)
    credential.party_id = party.id
    credential.authentication_binding_id = binding.id
    credential.tenant_id = tenant.id
    credential.party_bound_at = bound_at
    credential.party_binding_source = source
    credential.party_binding_reason = reason
    db.flush()
    stage_audit_event(
        db,
        action="credential.party_authentication_projected",
        entity_type="user_credential",
        entity_id=str(credential.id),
        actor_type=AuditActorType.system,
        actor_label=command.context.actor,
        metadata={
            "party_id": str(party.id),
            "authentication_binding_id": str(binding.id),
            "binding_key": binding.binding_key,
            "tenant_id": str(tenant.id),
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "source": source,
        },
    )
    return CredentialPartyBindingOutcome(
        credential_id=credential.id,
        party_id=party.id,
        authentication_binding_id=binding.id,
        tenant_id=tenant.id,
        bound_at=bound_at,
        replayed=False,
    )


def bind_credential_party(
    db: Session, command: CredentialPartyBinding
) -> CredentialPartyBindingOutcome:
    """Project one credential and commit the complete owner transaction."""

    return execute_owner_command(
        db,
        definition=_BIND_COMMAND,
        context=command.context,
        operation=lambda: _bind_credential_party(db, command),
    )


@dataclass(frozen=True, slots=True)
class PrincipalReadinessCohort:
    principal_kind: str
    credentials: int
    principal_party_ready: int
    organization_administrator_review_required: int

    @property
    def remaining(self) -> int:
        return self.credentials - self.principal_party_ready


@dataclass(frozen=True, slots=True)
class CredentialProjectionReport:
    credentials: int
    projected: int
    undeclared_mechanisms: int
    mechanism_mismatches: int
    legacy_person_mismatches: int
    collision_groups: int

    @property
    def remaining(self) -> int:
        return self.credentials - self.projected

    @property
    def is_ready_for_enforcement(self) -> bool:
        return (
            self.remaining == 0
            and self.undeclared_mechanisms == 0
            and self.mechanism_mismatches == 0
            and self.legacy_person_mismatches == 0
            and self.collision_groups == 0
        )


@dataclass(frozen=True, slots=True)
class CredentialConvergenceReport:
    """PII-free separation of principal readiness from new projection state."""

    principal_cohorts: tuple[PrincipalReadinessCohort, ...]
    projection: CredentialProjectionReport

    @property
    def principal_bindings_remaining(self) -> int:
        return sum(cohort.remaining for cohort in self.principal_cohorts)


def credential_convergence_report(db: Session) -> CredentialConvergenceReport:
    """Recompute principal readiness and R1 projection completion from the DB."""

    from app.models.subscriber import ResellerUser, Subscriber
    from app.models.system_user import SystemUser

    cohorts: list[PrincipalReadinessCohort] = []
    projected = 0
    undeclared = 0
    mechanism_mismatches = 0
    person_mismatches = 0
    declared = declared_authentication_mechanisms()

    for kind, principal_column, party_column, model in (
        ("subscriber", UserCredential.subscriber_id, Subscriber.party_id, Subscriber),
        (
            "system_user",
            UserCredential.system_user_id,
            SystemUser.person_party_id,
            SystemUser,
        ),
        (
            "reseller_user",
            UserCredential.reseller_user_id,
            ResellerUser.person_party_id,
            ResellerUser,
        ),
    ):
        legacy_party = aliased(Party)
        rows = list(
            db.execute(
                select(
                    party_column,
                    legacy_party.party_type,
                    UserCredential.party_id,
                    UserCredential.authentication_binding_id,
                    UserCredential.tenant_id,
                    UserCredential.party_bound_at,
                    UserCredential.party_binding_source,
                    UserCredential.party_binding_reason,
                    AuthenticationBinding.mechanism_code,
                    UserCredential.provider,
                )
                .select_from(UserCredential)
                .join(model, model.id == principal_column)
                .outerjoin(legacy_party, legacy_party.id == party_column)
                .outerjoin(
                    AuthenticationBinding,
                    AuthenticationBinding.id
                    == UserCredential.authentication_binding_id,
                )
                .where(principal_column.is_not(None))
            )
        )
        ready = sum(1 for row in rows if row[0] is not None)
        organization_review = sum(
            1
            for row in rows
            if row[1] == PartyType.organization.value and row[2] is None
        )
        cohorts.append(
            PrincipalReadinessCohort(
                principal_kind=kind,
                credentials=len(rows),
                principal_party_ready=ready,
                organization_administrator_review_required=organization_review,
            )
        )
        for row in rows:
            complete = all(value is not None for value in row[2:8])
            if not complete:
                continue
            projected += 1
            mechanism_code = row[8]
            provider = row[9]
            provider_code = str(
                provider.value if hasattr(provider, "value") else provider
            )
            if mechanism_code not in declared:
                undeclared += 1
            if mechanism_code != provider_code:
                mechanism_mismatches += 1
            if row[1] == PartyType.person.value and row[0] != row[2]:
                person_mismatches += 1

    collision_groups = int(
        db.scalar(
            select(func.count()).select_from(
                select(
                    UserCredential.tenant_id,
                    UserCredential.party_id,
                    UserCredential.authentication_binding_id,
                )
                .where(UserCredential.party_id.is_not(None))
                .group_by(
                    UserCredential.tenant_id,
                    UserCredential.party_id,
                    UserCredential.authentication_binding_id,
                )
                .having(func.count(UserCredential.id) > 1)
                .subquery()
            )
        )
        or 0
    )
    return CredentialConvergenceReport(
        principal_cohorts=tuple(cohorts),
        projection=CredentialProjectionReport(
            credentials=sum(cohort.credentials for cohort in cohorts),
            projected=projected,
            undeclared_mechanisms=undeclared,
            mechanism_mismatches=mechanism_mismatches,
            legacy_person_mismatches=person_mismatches,
            collision_groups=collision_groups,
        ),
    )


__all__ = [
    "CredentialBindingError",
    "CredentialConvergenceReport",
    "CredentialPartyBinding",
    "CredentialPartyBindingOutcome",
    "CredentialProjectionReport",
    "PrincipalReadinessCohort",
    "bind_credential_party",
    "credential_convergence_report",
    "resolve_binding_for_mechanism",
]
