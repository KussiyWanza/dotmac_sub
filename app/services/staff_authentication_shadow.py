"""Shadow parity for staff authentication, before any reader changes.

Owner: ``party.staff_authentication_shadow``. The module owns only the
read-only comparison and its cutover-readiness verdict. It does not own or
write credential, MFA, lockout, session, SystemUser, or Party state.

## What this proves, and what it deliberately does not

Migration 527 lets a credential name the Party it authenticates. Nothing reads
that column yet: staff login still resolves `credential.system_user_id` to a
`SystemUser`, MFA methods are keyed on `system_user_id`, lockout state lives on
the credential row, and sessions are keyed on `system_user_id`.

Cutting reads over means resolving those same four things through the Party
instead. That is safe only if the Party-keyed answer is *identical* to the
principal-keyed answer for every staff credential in the population. This module
computes both answers and reports where they differ — read-only, aggregate, and
PII-free, so it can run against a production-derived restore and have its output
pasted into a review.

It changes no reader, writes no row, and authorizes no cutover. It is the
evidence the cutover waits on, not the cutover.

## Why the ambiguity cohorts are the real finding

A missing projection is ordinary, expected debt: the credential simply has not
been through the approved adoption plan yet, and the number shrinks as batches
run. The dangerous cohorts are the ones where a Party-keyed read would return a
*different* answer rather than no answer:

- **A Party owning more than one SystemUser.** Party-keyed MFA and session reads
  would union two principals' artifacts. One person legitimately holding a staff
  principal and a reseller principal is fine — the constraint is one *SystemUser*
  per Person Party, which `bind_system_user_principal` already enforces going
  forward and which this report re-checks against real data.
- **A principal holding more than one active credential.** Lockout lives on the
  credential row, so a Party-keyed lockout read has to choose, and the two rows
  can disagree — a locked-out operator could appear unlocked.
- **A credential and its principal disagreeing about the Party.** Neither answer
  can be trusted; this is corruption, not debt.
- **An active credential whose principal has no Party.** A credential projection
  cannot substitute for the missing canonical staff identity binding; allowing
  it would make the authentication constraint overwrite the ownership record.

Each is counted separately because they need different dispositions, and each is
blocking on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.auth import MFAMethod, SessionStatus, UserCredential
from app.models.auth import Session as AuthSession
from app.models.system_user import SystemUser

#: Stable codes, safe to put in a runbook or a gate. Order is fixed so two runs
#: over one population produce identical output.
BLOCKING_PARTY_DISAGREEMENT = "party_disagreement"
BLOCKING_PRINCIPAL_UNBOUND = "principal_unbound"
BLOCKING_AMBIGUOUS_PARTY_PRINCIPALS = "party_owns_multiple_system_users"
BLOCKING_MULTI_CREDENTIAL_PRINCIPALS = "principal_holds_multiple_active_credentials"
BLOCKING_PROJECTION_INCOMPLETE = "projection_incomplete"


@dataclass(frozen=True, slots=True)
class StaffAuthenticationParityReport:
    """Whether staff authentication can be resolved through Party identically."""

    credentials: int
    projection_complete: int
    principal_unbound: int
    party_disagreements: int
    parties_with_multiple_principals: int
    principals_with_multiple_active_credentials: int
    mfa_methods: int
    mfa_methods_on_ambiguous_parties: int
    live_sessions: int
    live_sessions_on_ambiguous_parties: int
    locked_credentials: int
    locked_credentials_on_multi_credential_principals: int

    @property
    def projection_remaining(self) -> int:
        return self.credentials - self.projection_complete

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        """Every reason the read cutover is not yet safe, in a stable order."""

        reasons: list[str] = []
        if self.party_disagreements:
            reasons.append(BLOCKING_PARTY_DISAGREEMENT)
        if self.principal_unbound:
            reasons.append(BLOCKING_PRINCIPAL_UNBOUND)
        if self.parties_with_multiple_principals:
            reasons.append(BLOCKING_AMBIGUOUS_PARTY_PRINCIPALS)
        if self.principals_with_multiple_active_credentials:
            reasons.append(BLOCKING_MULTI_CREDENTIAL_PRINCIPALS)
        if self.projection_remaining:
            reasons.append(BLOCKING_PROJECTION_INCOMPLETE)
        return tuple(reasons)

    @property
    def is_read_cutover_safe(self) -> bool:
        return not self.blocking_reasons

    def as_dict(self) -> dict[str, object]:
        return {
            "credentials": self.credentials,
            "projection_complete": self.projection_complete,
            "projection_remaining": self.projection_remaining,
            "principal_unbound": self.principal_unbound,
            "party_disagreements": self.party_disagreements,
            "parties_with_multiple_principals": self.parties_with_multiple_principals,
            "principals_with_multiple_active_credentials": (
                self.principals_with_multiple_active_credentials
            ),
            "mfa_methods": self.mfa_methods,
            "mfa_methods_on_ambiguous_parties": self.mfa_methods_on_ambiguous_parties,
            "live_sessions": self.live_sessions,
            "live_sessions_on_ambiguous_parties": (
                self.live_sessions_on_ambiguous_parties
            ),
            "locked_credentials": self.locked_credentials,
            "locked_credentials_on_multi_credential_principals": (
                self.locked_credentials_on_multi_credential_principals
            ),
            "blocking_reasons": list(self.blocking_reasons),
            "is_read_cutover_safe": self.is_read_cutover_safe,
        }


def _ambiguous_party_ids(db: Session) -> set[UUID]:
    """Person Parties owning more than one SystemUser — Party-keyed reads union."""

    rows = db.execute(
        select(SystemUser.person_party_id)
        .where(SystemUser.person_party_id.is_not(None))
        .group_by(SystemUser.person_party_id)
        .having(func.count(SystemUser.id) > 1)
    ).scalars()
    return {party_id for party_id in rows if party_id is not None}


def _multi_credential_principal_ids(db: Session) -> set[UUID]:
    """SystemUsers holding more than one active credential — lockout is ambiguous."""

    rows = db.execute(
        select(UserCredential.system_user_id)
        .where(
            UserCredential.system_user_id.is_not(None),
            UserCredential.is_active.is_(True),
        )
        .group_by(UserCredential.system_user_id)
        .having(func.count(UserCredential.id) > 1)
    ).scalars()
    return {principal_id for principal_id in rows if principal_id is not None}


def staff_authentication_parity_report(
    db: Session,
) -> StaffAuthenticationParityReport:
    """Compare principal-keyed and Party-keyed staff authentication resolution.

    Read-only. Emits counts only: no name, email, username, credential, token,
    session id, Party UUID or principal UUID appears in the result, so the
    report is safe to attach to a review of a production-derived restore.
    """

    now = datetime.now(UTC)
    ambiguous_parties = _ambiguous_party_ids(db)
    multi_credential_principals = _multi_credential_principal_ids(db)

    rows = list(
        db.execute(
            select(
                UserCredential.system_user_id,
                UserCredential.party_id,
                SystemUser.person_party_id,
                UserCredential.locked_until,
            )
            .select_from(UserCredential)
            .join(SystemUser, SystemUser.id == UserCredential.system_user_id)
            .where(
                UserCredential.system_user_id.is_not(None),
                UserCredential.is_active.is_(True),
            )
        )
    )

    projection_complete = 0
    principal_unbound = 0
    disagreements = 0
    locked = 0
    locked_ambiguous = 0
    for principal_id, credential_party_id, principal_party_id, locked_until in rows:
        if credential_party_id is not None:
            projection_complete += 1
        if principal_party_id is None:
            principal_unbound += 1
        if (
            credential_party_id is not None
            and principal_party_id is not None
            and credential_party_id != principal_party_id
        ):
            disagreements += 1
        if locked_until is not None and _as_utc(locked_until) > now:
            locked += 1
            if principal_id in multi_credential_principals:
                locked_ambiguous += 1

    mfa_rows = list(
        db.execute(
            select(SystemUser.person_party_id)
            .select_from(MFAMethod)
            .join(SystemUser, SystemUser.id == MFAMethod.system_user_id)
            .where(MFAMethod.system_user_id.is_not(None))
        ).scalars()
    )
    session_rows = list(
        db.execute(
            select(SystemUser.person_party_id)
            .select_from(AuthSession)
            .join(SystemUser, SystemUser.id == AuthSession.system_user_id)
            .where(
                AuthSession.system_user_id.is_not(None),
                AuthSession.status == SessionStatus.active,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
        ).scalars()
    )

    return StaffAuthenticationParityReport(
        credentials=len(rows),
        projection_complete=projection_complete,
        principal_unbound=principal_unbound,
        party_disagreements=disagreements,
        parties_with_multiple_principals=len(ambiguous_parties),
        principals_with_multiple_active_credentials=len(multi_credential_principals),
        mfa_methods=len(mfa_rows),
        mfa_methods_on_ambiguous_parties=sum(
            1 for party_id in mfa_rows if party_id in ambiguous_parties
        ),
        live_sessions=len(session_rows),
        live_sessions_on_ambiguous_parties=sum(
            1 for party_id in session_rows if party_id in ambiguous_parties
        ),
        locked_credentials=locked,
        locked_credentials_on_multi_credential_principals=locked_ambiguous,
    )


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; comparing those to an aware now raises."""

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "BLOCKING_AMBIGUOUS_PARTY_PRINCIPALS",
    "BLOCKING_MULTI_CREDENTIAL_PRINCIPALS",
    "BLOCKING_PARTY_DISAGREEMENT",
    "BLOCKING_PRINCIPAL_UNBOUND",
    "BLOCKING_PROJECTION_INCOMPLETE",
    "StaffAuthenticationParityReport",
    "staff_authentication_parity_report",
]
