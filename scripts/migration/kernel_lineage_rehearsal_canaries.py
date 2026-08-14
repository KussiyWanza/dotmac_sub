"""Materialize synthetic lineage canaries from a PII-free evidence bundle.

This module has deliberately no CLI. The only sanctioned caller is the
throwaway-database rehearsal; production runs the read-only exporter only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.auth import AuthenticationBinding, AuthProvider, UserCredential
from app.models.party import Party, PartyRole, PartyType
from app.models.radius import RadiusServer
from app.models.rbac import Role
from app.models.subscriber import Reseller, ResellerUser, Subscriber
from app.models.system_user import SystemUser
from app.services.operator_tenant import OPERATOR_TENANT_ID
from scripts.migration.kernel_lineage_rehearsal_evidence import (
    CredentialPrincipalKind,
    KernelLineageRehearsalEvidence,
    ProjectionState,
    ValidWindowShape,
)

CANARY_PREFIX = "kernel-lineage-canary"


@dataclass(frozen=True, slots=True)
class CanaryTableDigest:
    table_name: Literal[
        "roles",
        "user_credentials",
        "audit_events",
        "parties",
        "party_roles",
    ]
    row_count: int
    rows_sha256: str


def _person(db: Session, marker: str) -> Party:
    party = Party(
        party_type=PartyType.person.value,
        display_name=marker,
        data_classification="test",
    )
    db.add(party)
    db.flush()
    return party


@dataclass(frozen=True, slots=True)
class CredentialPrincipalReference:
    subscriber_id: UUID | None = None
    system_user_id: UUID | None = None
    reseller_user_id: UUID | None = None


def _credential_principal(
    db: Session,
    *,
    principal_kind: CredentialPrincipalKind,
    marker: str,
) -> CredentialPrincipalReference:
    principal: Subscriber | SystemUser | ResellerUser
    if principal_kind is CredentialPrincipalKind.SUBSCRIBER:
        reseller = Reseller(
            name=f"{marker}-reseller",
            code=f"lineage-{uuid4().hex[:12]}",
            is_active=True,
            is_house=False,
        )
        db.add(reseller)
        db.flush()
        principal = Subscriber(
            first_name="Synthetic",
            last_name="Canary",
            email=f"{marker}@example.invalid",
            reseller_id=reseller.id,
        )
    elif principal_kind is CredentialPrincipalKind.SYSTEM_USER:
        principal = SystemUser(
            first_name="Synthetic",
            last_name="Canary",
            email=f"{marker}@example.invalid",
            is_active=True,
        )
    else:
        principal = ResellerUser(
            email=f"{marker}@example.invalid",
            full_name="Synthetic Canary",
            is_active=True,
        )
    db.add(principal)
    db.flush()
    if principal_kind is CredentialPrincipalKind.SUBSCRIBER:
        return CredentialPrincipalReference(subscriber_id=principal.id)
    if principal_kind is CredentialPrincipalKind.SYSTEM_USER:
        return CredentialPrincipalReference(system_user_id=principal.id)
    return CredentialPrincipalReference(reseller_user_id=principal.id)


def _seed_roles(db: Session, evidence: KernelLineageRehearsalEvidence) -> None:
    for index, cohort in enumerate(evidence.roles):
        if cohort.projection_state is ProjectionState.PARTIAL:
            raise ValueError("cannot synthesize an invalid partial role projection")
        projected = cohort.projection_state is ProjectionState.PROJECTED
        marker = f"{CANARY_PREFIX}-role-{index}"
        name_length = max(1, min(cohort.maximum_name_length, 120))
        unique_character = chr(ord("a") + (index % 26))
        role_name = (unique_character + ("r" * name_length))[:name_length]
        db.add(
            Role(
                name=role_name,
                description=CANARY_PREFIX,
                is_active=cohort.is_active,
                tenant_id=OPERATOR_TENANT_ID if projected else None,
                slug=marker if projected else None,
            )
        )


def _seed_credentials(db: Session, evidence: KernelLineageRehearsalEvidence) -> None:
    for index, cohort in enumerate(evidence.credentials):
        if cohort.projection_state is ProjectionState.PARTIAL:
            raise ValueError(
                "cannot synthesize an invalid partial credential projection"
            )
        marker = f"{CANARY_PREFIX}-credential-{index}-{uuid4().hex[:8]}"
        principal = _credential_principal(
            db,
            principal_kind=cohort.principal_kind,
            marker=marker,
        )
        projected = cohort.projection_state is ProjectionState.PROJECTED
        party = _person(db, f"{marker}-party") if projected else None
        binding: AuthenticationBinding | None = None
        if projected:
            binding = AuthenticationBinding(
                binding_key=marker,
                mechanism_code=cohort.provider.value,
                name=marker,
                is_active=True,
            )
            db.add(binding)
            db.flush()
        radius_server: RadiusServer | None = None
        if cohort.has_radius_override:
            radius_server = RadiusServer(
                name=marker,
                host=f"{index}.lineage.invalid",
                is_active=True,
            )
            db.add(radius_server)
            db.flush()
        db.add(
            UserCredential(
                subscriber_id=principal.subscriber_id,
                system_user_id=principal.system_user_id,
                reseller_user_id=principal.reseller_user_id,
                provider=AuthProvider(cohort.provider.value),
                username=marker,
                password_hash=(
                    "synthetic-not-a-secret"
                    if cohort.provider.value == AuthProvider.local.value
                    else None
                ),
                radius_server_id=radius_server.id if radius_server else None,
                party_id=party.id if party else None,
                authentication_binding_id=binding.id if binding else None,
                tenant_id=OPERATOR_TENANT_ID if projected else None,
                party_bound_at=datetime.now(UTC) if projected else None,
                party_binding_source=CANARY_PREFIX if projected else None,
                party_binding_reason=(
                    "synthetic structural rehearsal" if projected else None
                ),
                is_active=cohort.is_active,
            )
        )


def _seed_audit_events(db: Session, evidence: KernelLineageRehearsalEvidence) -> None:
    historical_ids: list[UUID] = []
    for index, cohort in enumerate(evidence.audit_events):
        marker = f"{CANARY_PREFIX}-audit-{index}"
        party = _person(db, f"{marker}-party") if cohort.has_actor_party_id else None
        event = AuditEvent(
            actor_type=AuditActorType(cohort.actor_type.value),
            actor_id=marker if cohort.has_actor_id else None,
            actor_party_id=party.id if party else None,
            action=marker,
            entity_type="kernel_lineage_canary",
            entity_id=marker,
            status_code=200,
            is_success=True,
            is_active=cohort.is_active,
            request_id=CANARY_PREFIX,
            metadata_={"synthetic": True},
            details={"synthetic": True} if cohort.has_details else None,
        )
        db.add(event)
        db.flush()
        if not cohort.has_created_at:
            historical_ids.append(event.id)
    if historical_ids:
        db.execute(
            text("UPDATE audit_events SET created_at = NULL WHERE id = ANY(:ids)"),
            {"ids": historical_ids},
        )


def _seed_party_roles(db: Session, evidence: KernelLineageRehearsalEvidence) -> None:
    now = datetime.now(UTC)
    for index, cohort in enumerate(evidence.party_roles):
        marker = f"{CANARY_PREFIX}-party-role-{index}"
        party = _person(db, marker)
        valid_from: datetime | None = None
        valid_until: datetime | None = None
        if cohort.valid_window in {
            ValidWindowShape.START_ONLY,
            ValidWindowShape.BOUNDED,
        }:
            valid_from = now
        if cohort.valid_window in {
            ValidWindowShape.END_ONLY,
            ValidWindowShape.BOUNDED,
        }:
            valid_until = now + timedelta(days=1)
        db.add(
            PartyRole(
                party_id=party.id,
                role_type=cohort.role_type.value,
                role_key=cohort.role_key.value,
                status=cohort.status.value,
                valid_from=valid_from,
                valid_until=valid_until,
                source=CANARY_PREFIX,
                metadata_={"synthetic": True} if cohort.has_metadata else None,
            )
        )


def seed_rehearsal_canaries(
    db: Session,
    evidence: KernelLineageRehearsalEvidence,
) -> None:
    """Create one synthetic row for each observed structural cohort."""

    _seed_roles(db, evidence)
    _seed_credentials(db, evidence)
    _seed_audit_events(db, evidence)
    _seed_party_roles(db, evidence)
    db.commit()


CanaryTableName = Literal[
    "roles",
    "user_credentials",
    "audit_events",
    "parties",
    "party_roles",
]

_CANARY_QUERIES: dict[CanaryTableName, str] = {
    "roles": "SELECT * FROM roles WHERE description = :marker ORDER BY id",
    "user_credentials": (
        "SELECT * FROM user_credentials WHERE username LIKE :prefix ORDER BY id"
    ),
    "audit_events": (
        "SELECT * FROM audit_events WHERE request_id = :marker ORDER BY id"
    ),
    "parties": ("SELECT * FROM parties WHERE display_name LIKE :prefix ORDER BY id"),
    "party_roles": ("SELECT * FROM party_roles WHERE source = :marker ORDER BY id"),
}


def fingerprint_rehearsal_canaries(db: Session) -> tuple[CanaryTableDigest, ...]:
    """Hash complete synthetic rows so a lineage step cannot mutate them silently."""

    digests: list[CanaryTableDigest] = []
    for table_name, query in _CANARY_QUERIES.items():
        rows = db.execute(
            text(f"SELECT row_to_json(canary)::text FROM ({query}) AS canary"),
            {"marker": CANARY_PREFIX, "prefix": f"{CANARY_PREFIX}%"},
        ).scalars()
        serialized_rows = tuple(str(row) for row in rows)
        serialized = "\n".join(serialized_rows)
        digests.append(
            CanaryTableDigest(
                table_name=table_name,
                row_count=len(serialized_rows),
                rows_sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(digests)
