#!/usr/bin/env python3
"""Export a PII-free production-shape bundle for the kernel lineage rehearsal.

The bundle contains catalog fingerprints, row counts, and bounded structural
cohorts only. It never exports identifiers, names, contact data, credential
material, audit details, or timestamps. Scratch-database canaries are generated
from these cohorts; no production row is copied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from app.db import read_only_snapshot_session

BUNDLE_SCHEMA_VERSION: Literal[1] = 1
LineageTableName = Literal[
    "tenants",
    "tenant_domains",
    "roles",
    "user_credentials",
    "audit_events",
    "party_roles",
]
LINEAGE_TABLES: tuple[LineageTableName, ...] = (
    "tenants",
    "tenant_domains",
    "roles",
    "user_credentials",
    "audit_events",
    "party_roles",
)
RevisionId = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]{3}_[a-z0-9_]{1,28}$"),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ProjectionState(StrEnum):
    LEGACY = "legacy"
    PROJECTED = "projected"
    PARTIAL = "partial"


class CredentialPrincipalKind(StrEnum):
    SUBSCRIBER = "subscriber"
    SYSTEM_USER = "system_user"
    RESELLER_USER = "reseller_user"


class CredentialProvider(StrEnum):
    LOCAL = "local"
    RADIUS = "radius"
    SSO = "sso"


class AuditActorKind(StrEnum):
    SYSTEM = "system"
    USER = "user"
    API_KEY = "api_key"
    SERVICE = "service"


class PartyRoleKind(StrEnum):
    PROSPECT = "prospect"
    CUSTOMER = "customer"
    SUBSCRIBER = "subscriber"
    RESELLER = "reseller"
    VENDOR = "vendor"
    PARTNER = "partner"
    STAFF = "staff"
    AGENT = "agent"


class PartyRoleKey(StrEnum):
    DEFAULT = "default"
    REFERRAL = "referral"
    TECHNOLOGY = "technology"
    INFRASTRUCTURE = "infrastructure"
    STRATEGIC = "strategic"


class PartyRoleState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ENDED = "ended"


class ValidWindowShape(StrEnum):
    NONE = "none"
    START_ONLY = "start_only"
    END_ONLY = "end_only"
    BOUNDED = "bounded"


class _EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TableContract(_EvidenceModel):
    table_name: LineageTableName
    row_count: int = Field(ge=0)
    columns_sha256: Sha256
    constraints_sha256: Sha256
    indexes_sha256: Sha256
    rls_enabled: bool
    rls_forced: bool


class RoleCohort(_EvidenceModel):
    projection_state: ProjectionState
    is_active: bool
    count: int = Field(gt=0)
    maximum_name_length: int = Field(gt=0)


class CredentialCohort(_EvidenceModel):
    principal_kind: CredentialPrincipalKind
    provider: CredentialProvider
    projection_state: ProjectionState
    is_active: bool
    has_radius_override: bool
    count: int = Field(gt=0)


class AuditCohort(_EvidenceModel):
    actor_type: AuditActorKind
    has_actor_id: bool
    has_actor_party_id: bool
    has_details: bool
    has_created_at: bool
    is_active: bool
    count: int = Field(gt=0)


class PartyRoleCohort(_EvidenceModel):
    role_type: PartyRoleKind
    role_key: PartyRoleKey
    status: PartyRoleState
    valid_window: ValidWindowShape
    has_metadata: bool
    count: int = Field(gt=0)


class KernelLineageRehearsalEvidence(_EvidenceModel):
    schema_version: Literal[1] = BUNDLE_SCHEMA_VERSION
    source_revisions: tuple[RevisionId, ...] = Field(min_length=1)
    tables: tuple[TableContract, ...]
    roles: tuple[RoleCohort, ...]
    credentials: tuple[CredentialCohort, ...]
    audit_events: tuple[AuditCohort, ...]
    party_roles: tuple[PartyRoleCohort, ...]

    @model_validator(mode="after")
    def require_each_table_contract_once(self) -> KernelLineageRehearsalEvidence:
        names = tuple(contract.table_name for contract in self.tables)
        if len(names) != len(LINEAGE_TABLES) or set(names) != set(LINEAGE_TABLES):
            raise ValueError(
                "bundle must contain each lineage table contract exactly once"
            )
        return self

    def canonical_json(self) -> str:
        return self.model_dump_json(exclude_none=False)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _hash_rows(rows: Sequence[RowMapping]) -> str:
    payload = json.dumps(
        [dict(row) for row in rows],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mapping_rows(db: Session, statement: str, **params: object) -> list[RowMapping]:
    return list(db.execute(text(statement), params).mappings())


def _table_contract(db: Session, table_name: LineageTableName) -> TableContract:
    if table_name not in LINEAGE_TABLES:
        raise ValueError(f"unsupported lineage table {table_name!r}")
    columns = _mapping_rows(
        db,
        """
        SELECT column_name, data_type, udt_name, is_nullable,
               character_maximum_length, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :table_name
        ORDER BY ordinal_position
        """,
        table_name=table_name,
    )
    constraints = _mapping_rows(
        db,
        """
        SELECT conname, contype, pg_get_constraintdef(oid) AS definition
        FROM pg_constraint
        WHERE conrelid = to_regclass(:qualified_name)
        ORDER BY conname
        """,
        qualified_name=f"public.{table_name}",
    )
    indexes = _mapping_rows(
        db,
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = :table_name
        ORDER BY indexname
        """,
        table_name=table_name,
    )
    catalog = db.execute(
        text(
            """
            SELECT relrowsecurity, relforcerowsecurity
            FROM pg_class
            WHERE oid = to_regclass(:qualified_name)
            """
        ),
        {"qualified_name": f"public.{table_name}"},
    ).one()
    row_count = db.scalar(text(f'SELECT count(*) FROM "{table_name}"'))
    return TableContract(
        table_name=table_name,
        row_count=int(row_count or 0),
        columns_sha256=_hash_rows(columns),
        constraints_sha256=_hash_rows(constraints),
        indexes_sha256=_hash_rows(indexes),
        rls_enabled=bool(catalog[0]),
        rls_forced=bool(catalog[1]),
    )


def _projection_state(value: object) -> ProjectionState:
    return ProjectionState(str(value))


def _role_cohorts(db: Session) -> tuple[RoleCohort, ...]:
    rows = _mapping_rows(
        db,
        """
        SELECT CASE
                 WHEN tenant_id IS NULL AND slug IS NULL THEN 'legacy'
                 WHEN tenant_id IS NOT NULL AND slug IS NOT NULL THEN 'projected'
                 ELSE 'partial'
               END AS projection_state,
               is_active,
               count(*) AS count,
               max(length(name)) AS maximum_name_length
        FROM roles
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
    )
    return tuple(
        RoleCohort(
            projection_state=_projection_state(row["projection_state"]),
            is_active=bool(row["is_active"]),
            count=int(row["count"]),
            maximum_name_length=int(row["maximum_name_length"] or 0),
        )
        for row in rows
    )


def _credential_cohorts(db: Session) -> tuple[CredentialCohort, ...]:
    rows = _mapping_rows(
        db,
        """
        SELECT CASE
                 WHEN subscriber_id IS NOT NULL THEN 'subscriber'
                 WHEN system_user_id IS NOT NULL THEN 'system_user'
                 ELSE 'reseller_user'
               END AS principal_kind,
               provider::text AS provider,
               CASE
                 WHEN party_id IS NULL AND authentication_binding_id IS NULL
                  AND tenant_id IS NULL AND party_bound_at IS NULL
                  AND party_binding_source IS NULL
                  AND party_binding_reason IS NULL THEN 'legacy'
                 WHEN party_id IS NOT NULL AND authentication_binding_id IS NOT NULL
                  AND tenant_id IS NOT NULL AND party_bound_at IS NOT NULL
                  AND party_binding_source IS NOT NULL
                  AND party_binding_reason IS NOT NULL THEN 'projected'
                 ELSE 'partial'
               END AS projection_state,
               is_active,
               radius_server_id IS NOT NULL AS has_radius_override,
               count(*) AS count
        FROM user_credentials
        GROUP BY 1, 2, 3, 4, 5
        ORDER BY 1, 2, 3, 4, 5
        """,
    )
    return tuple(
        CredentialCohort(
            principal_kind=CredentialPrincipalKind(str(row["principal_kind"])),
            provider=CredentialProvider(str(row["provider"])),
            projection_state=_projection_state(row["projection_state"]),
            is_active=bool(row["is_active"]),
            has_radius_override=bool(row["has_radius_override"]),
            count=int(row["count"]),
        )
        for row in rows
    )


def _audit_cohorts(db: Session) -> tuple[AuditCohort, ...]:
    rows = _mapping_rows(
        db,
        """
        SELECT actor_type::text AS actor_type,
               actor_id IS NOT NULL AND btrim(actor_id) <> '' AS has_actor_id,
               actor_party_id IS NOT NULL AS has_actor_party_id,
               details IS NOT NULL AS has_details,
               created_at IS NOT NULL AS has_created_at,
               is_active,
               count(*) AS count
        FROM audit_events
        GROUP BY 1, 2, 3, 4, 5, 6
        ORDER BY 1, 2, 3, 4, 5, 6
        """,
    )
    return tuple(
        AuditCohort(
            actor_type=AuditActorKind(str(row["actor_type"])),
            has_actor_id=bool(row["has_actor_id"]),
            has_actor_party_id=bool(row["has_actor_party_id"]),
            has_details=bool(row["has_details"]),
            has_created_at=bool(row["has_created_at"]),
            is_active=bool(row["is_active"]),
            count=int(row["count"]),
        )
        for row in rows
    )


def _valid_window(value: object) -> ValidWindowShape:
    return ValidWindowShape(str(value))


def _party_role_cohorts(db: Session) -> tuple[PartyRoleCohort, ...]:
    rows = _mapping_rows(
        db,
        """
        SELECT role_type, role_key, status,
               CASE
                 WHEN valid_from IS NULL AND valid_until IS NULL THEN 'none'
                 WHEN valid_from IS NOT NULL AND valid_until IS NULL THEN 'start_only'
                 WHEN valid_from IS NULL AND valid_until IS NOT NULL THEN 'end_only'
                 ELSE 'bounded'
               END AS valid_window,
               metadata IS NOT NULL AS has_metadata,
               count(*) AS count
        FROM party_roles
        GROUP BY 1, 2, 3, 4, 5
        ORDER BY 1, 2, 3, 4, 5
        """,
    )
    return tuple(
        PartyRoleCohort(
            role_type=PartyRoleKind(str(row["role_type"])),
            role_key=PartyRoleKey(str(row["role_key"])),
            status=PartyRoleState(str(row["status"])),
            valid_window=_valid_window(row["valid_window"]),
            has_metadata=bool(row["has_metadata"]),
            count=int(row["count"]),
        )
        for row in rows
    )


def collect_kernel_lineage_evidence(db: Session) -> KernelLineageRehearsalEvidence:
    """Read one repeatable, aggregate-only snapshot from a migrated database."""

    revisions = tuple(
        str(value)
        for value in db.scalars(
            text("SELECT version_num FROM alembic_version ORDER BY version_num")
        )
    )
    return KernelLineageRehearsalEvidence(
        source_revisions=revisions,
        tables=tuple(_table_contract(db, table) for table in LINEAGE_TABLES),
        roles=_role_cohorts(db),
        credentials=_credential_cohorts(db),
        audit_events=_audit_cohorts(db),
        party_roles=_party_role_cohorts(db),
    )


def write_private_bundle(path: Path, evidence: KernelLineageRehearsalEvidence) -> None:
    """Create a mode-0600 bundle without overwriting prior evidence."""

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(evidence.canonical_json())
            stream.write("\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise


def read_bundle(path: Path) -> KernelLineageRehearsalEvidence:
    return KernelLineageRehearsalEvidence.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def target_contract_errors(
    db: Session,
    evidence: KernelLineageRehearsalEvidence,
) -> tuple[str, ...]:
    """Compare source and scratch schemas without comparing their row counts."""

    current_revisions = tuple(
        str(value)
        for value in db.scalars(
            text("SELECT version_num FROM alembic_version ORDER BY version_num")
        )
    )
    errors: list[str] = []
    if current_revisions != evidence.source_revisions:
        errors.append(
            "source revisions differ: "
            f"source={evidence.source_revisions!r} target={current_revisions!r}"
        )
    expected_by_table = {contract.table_name: contract for contract in evidence.tables}
    if set(expected_by_table) != set(LINEAGE_TABLES):
        errors.append("source bundle does not contain every lineage table contract")
        return tuple(errors)
    for table_name in LINEAGE_TABLES:
        expected = expected_by_table[table_name]
        actual = _table_contract(db, table_name)
        for field_name in (
            "columns_sha256",
            "constraints_sha256",
            "indexes_sha256",
            "rls_enabled",
            "rls_forced",
        ):
            if getattr(actual, field_name) != getattr(expected, field_name):
                errors.append(f"{table_name}.{field_name} differs")
    return tuple(errors)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="create this mode-0600 file; default prints the aggregate JSON",
    )
    args = parser.parse_args(argv)

    with read_only_snapshot_session() as db:
        evidence = collect_kernel_lineage_evidence(db)
        db.rollback()

    if args.output is None:
        print(evidence.canonical_json())
    else:
        write_private_bundle(args.output, evidence)
        print(
            json.dumps(
                {
                    "path": str(args.output),
                    "schema_version": evidence.schema_version,
                    "sha256": evidence.sha256,
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
