"""Party-keyed staff principal resolution — the authority after cutover.

Before this module, a staff login resolved its principal straight from
``UserCredential.system_user_id``. The Party projection existed and the shadow
report proved it agreed, but agreement is not authority: the reader still used
the legacy key, so the projection could rot without anyone noticing.

This resolves the principal FROM the projection. ``credential.party_id`` names
the person; the Party names its one login principal. ``system_user_id`` is kept
and cross-checked, but it is evidence of drift now, not the key.

**Fails closed, always.** There is deliberately no legacy fallback: a credential
whose projection is missing, conflicting, or ambiguous cannot authenticate. A
fallback would silently restore the old authority the moment the projection
broke, which is the exact failure this cutover exists to prevent.

SystemUser remains the product-owned staff context — roles, assignments, HR
lifecycle. Party identifies the *person*; it does not absorb staff ownership.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import UserCredential
from app.models.system_user import SystemUser


class StaffProjectionRefusal(StrEnum):
    """Why a staff credential could not be resolved through Party identity."""

    #: The credential carries no `party_id`: it was never projected, or the
    #: projection was cleared. Pre-cutover this authenticated fine.
    projection_missing = "staff_projection_missing"
    #: The Party has no principal — the binding was removed from the other side.
    party_has_no_principal = "staff_party_has_no_principal"
    #: The Party owns more than one SystemUser, so a Party-keyed read would
    #: union across principals and could not say whose session this is.
    party_owns_multiple_principals = "staff_party_owns_multiple_principals"
    #: The projection and the legacy key disagree. Never guess which is right.
    projection_conflict = "staff_projection_conflict"


class StaffProjectionError(Exception):
    """A staff credential cannot be resolved through its Party projection.

    `credential_id` is the subject the refusal is about: a credential id, a
    session id, or the Party itself. It is optional because the most basic
    refusal is "there is no identity here at all", which has no subject to name.
    """

    __slots__ = ("credential_id", "refusal")

    def __init__(
        self, refusal: StaffProjectionRefusal, credential_id: UUID | None
    ) -> None:
        self.refusal = refusal
        self.credential_id = credential_id
        super().__init__(f"{refusal.value} for credential {credential_id}")


def resolve_staff_principal_by_party(
    db: Session,
    party_id: UUID | None,
    asserted_system_user_id: UUID | None = None,
    *,
    reference: UUID | None = None,
) -> SystemUser:
    """THE primitive. Identity in, staff context out.

    The query direction is the whole point: it starts at the Party and finds the
    principal, never the reverse. Starting from `system_user_id` and checking the
    Party afterwards would agree on healthy data while leaving the legacy key
    authoritative — which is the defect this cutover exists to remove, and which
    no parity test can detect because both directions return the same answer.

    `asserted_system_user_id` is the Sub-owned staff context travelling
    alongside the identity. It is compared, never used to resolve.

    Both credential login and populated-session validation delegate here, so
    there is one direction and one set of refusals rather than near-duplicates
    that can drift apart.
    """

    subject = reference if reference is not None else party_id
    if party_id is None:
        raise StaffProjectionError(StaffProjectionRefusal.projection_missing, subject)

    principals = (
        db.execute(select(SystemUser).where(SystemUser.person_party_id == party_id))
        .scalars()
        .all()
    )
    if not principals:
        raise StaffProjectionError(
            StaffProjectionRefusal.party_has_no_principal, subject
        )
    if len(principals) > 1:
        # `uq_system_users_person_party_id` makes this unreachable on a migrated
        # catalog. Reaching it means the constraint was lost, not that the
        # population drifted — so refuse loudly rather than pick one.
        raise StaffProjectionError(
            StaffProjectionRefusal.party_owns_multiple_principals, subject
        )

    principal = principals[0]
    if asserted_system_user_id is not None and asserted_system_user_id != principal.id:
        raise StaffProjectionError(StaffProjectionRefusal.projection_conflict, subject)
    return principal


def resolve_staff_principal(db: Session, credential: UserCredential) -> SystemUser:
    """Resolve a login credential's staff principal through the primitive.

    Raises `StaffProjectionError` rather than returning None, so a caller cannot
    accidentally treat "unresolvable" as "anonymous" and continue.
    """

    return resolve_staff_principal_by_party(
        db,
        credential.party_id,
        credential.system_user_id,
        reference=credential.id,
    )


def resolve_staff_principal_assertion(db: Session, system_user_id: UUID) -> SystemUser:
    """Validate a staff principal asserted by an existing session or token.

    Sessions carry `system_user_id`, not `party_id`, so the assertion arrives in
    the legacy shape. This does not make the legacy key authoritative: the
    assertion is only accepted when the Party projection backs it, and the same
    typed refusals apply. A session whose principal has no Party, or whose Party
    owns more than one principal, is refused rather than resolved.

    Deliberately does NOT re-derive identity from any other attribute. If the
    projection cannot vouch for the assertion, the answer is refusal — never a
    reconstruction from name, email, or the credential row.

    Healthy sessions are unaffected: with the cohort fully projected and no
    ambiguous Party, every live staff session validates.
    """

    reference = system_user_id
    principal = db.get(SystemUser, system_user_id)
    if principal is None:
        raise StaffProjectionError(
            StaffProjectionRefusal.party_has_no_principal, reference
        )
    if principal.person_party_id is None:
        raise StaffProjectionError(StaffProjectionRefusal.projection_missing, reference)
    siblings = (
        db.execute(
            select(SystemUser.id).where(
                SystemUser.person_party_id == principal.person_party_id
            )
        )
        .scalars()
        .all()
    )
    if len(siblings) > 1:
        raise StaffProjectionError(
            StaffProjectionRefusal.party_owns_multiple_principals, reference
        )
    return principal
