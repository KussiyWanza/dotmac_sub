"""``service_intent.offer_access_requirement`` — the sole owner of:

- access-classified offer-version admission
- the immutable access requirement for an exact offer version
- reviewed classification of legacy/unclassified versions

Release 1 only. Release 2 (rejecting ``unclassified`` at admission and
dropping the DB default) is separate, later work — see
``docs/designs/CATALOG_ACCESS_REQUIREMENT_AUTHORITY.md``.

``service_intent.catalog_policy`` (``app/services/catalog/policies.py``) is a
deliberately separate, untouched owner. This module never imports it and
never reads/writes its tables.

Both public commands (admission and reviewed classification) enter through
``execute_owner_command`` and perform their own persistence and transaction
completion here — a caller builds the command and reads the result; it never
constructs the ``OfferVersion`` row or the classification row itself.

Authorization for admission has ONE owner: ``authorize_offer_version_
admission``, below. It decides the WHOLE question — the compound
``catalog:write AND (catalog:billing_write OR catalog:offer_version:
admission)`` permission rule, AND the ERP staff leave-write restriction
(``app/services/erp_staff_access.py``) — never just the leaf permission
grant. TWO call sites DELEGATE to this one owner rather than each deciding
independently: ``app/api/catalog.py``'s route dependency
(``_require_offer_version_admission``), and this module's own
``verify_admission_authorization``, called from ``_admit`` inside the same
transaction as the write, re-verifying against the live database for
whichever principal was supplied (never a trusted caller-supplied flag).
Because both delegate to the same function, there is exactly one decision
to get right, not two independently-maintained approximations of one rule
that can silently disagree (the earlier shape — the route composing
``require_any_permission`` while the command called a bare permission
primitive — refused an active staff leave restriction over HTTP while
allowing it through the command directly; that class of drift is now
structurally impossible, not merely tested against).
``AdmitOfferVersionCommand.principal`` remains the audit/attribution
identity as well, but it is no longer unchecked evidence. Classification has
no pre-authorizing route (its only caller is a trust-the-operator CLI); its
permission is re-verified fresh, inside this module, immediately before the
write.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.auth import ApiKey
from app.models.catalog import (
    AccessRequirement,
    BillingCycle,
    CatalogOffer,
    ContractTerm,
    OfferAccessRequirementClassification,
    OfferStatus,
    OfferVersion,
)
from app.models.domain_settings import SettingDomain
from app.models.idempotency import IdempotencyKey
from app.models.rbac import Role, SubscriberRole
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.schemas.catalog import OfferVersionCreate, OfferVersionUpdate
from app.services import catalog_billing_governance as billing_governance
from app.services import settings_spec
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.auth_dependencies import has_permission
from app.services.common import validate_enum
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.system_user_assignments import system_user_role_names

logger = logging.getLogger(__name__)

OWNER = "service_intent.offer_access_requirement"
CLASSIFY_PERMISSION = "catalog:offer_access_requirement:classify"
ADMISSION_SCOPE = "catalog:offer_version:admission"

#: The two other legs of the compound admission rule, named here so
#: ``_admission_permission_granted`` below and ``app/api/catalog.py``'s
#: route-level guards (``_require_offer_version_admission`` and the
#: router's own blanket ``catalog:write`` gate) spell the SAME two keys from
#: ONE place — never a second, independently-typed copy of either string
#: that could drift out of sync with the rule this module enforces.
WRITE_PERMISSION = "catalog:write"
BILLING_WRITE_PERMISSION = "catalog:billing_write"

_ADMIT_CONCERN = "access-classified offer-version admission"
_ADMIT_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=_ADMIT_CONCERN,
    name="admit_offer_version",
)

#: A SEPARATE registered command, under the SAME already-declared concern
#: (``_ADMIT_CONCERN`` — this IS a consequence of an admission attempt, not
#: a new decision surface), whose sole job is durably recording a
#: staff-leave admission denial. Kept genuinely distinct from
#: ``_ADMIT_COMMAND`` so ``record_leave_denial_evidence`` runs as a real,
#: registered, manifest-validated owner command — its own root transaction,
#: begun and completed by ``execute_owner_command`` — never a helper that
#: commits/rolls back a session directly (``docs/CODING_STANDARD.md`` § 3:
#: "nested domain helpers... never call commit() or rollback()
#: independently"; round 14 finding 1 corrected an earlier version of this
#: function that did exactly that).
_RECORD_LEAVE_DENIAL_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=_ADMIT_CONCERN,
    name="record_admission_leave_denial",
)

_CLASSIFY_CONCERN = "reviewed classification of legacy/unclassified versions"
_CLASSIFY_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=_CLASSIFY_CONCERN,
    name="classify_offer_version_access_requirement",
)

_UPDATE_CONCERN = "mutation of an already-admitted offer version"
_UPDATE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=_UPDATE_CONCERN,
    name="update_offer_version",
)

#: Real classifications. ``unclassified`` is never a valid classification
#: target — it is the state being replaced, never the state applied.
_REAL_CLASSIFICATIONS = (
    AccessRequirement.network_access,
    AccessRequirement.no_network_access,
)

#: Match ``offer_access_requirement_classifications.idempotency_key``/
#: ``.review_reference`` (``app/models/catalog.py``'s ``String(120)``/
#: ``String(200)`` columns) exactly. Checked here, before either value
#: reaches the database, so an oversized CLI/API input is a typed
#: validation error instead of a raw database error.
_IDEMPOTENCY_KEY_MAX_LENGTH = 120
_REVIEW_REFERENCE_MAX_LENGTH = 200

#: Scope for admission's row in the shared ``idempotency_keys`` ledger
#: (``app/models/idempotency.py``) — the same generic replay-safe mechanism
#: several other owners already use, rather than a bespoke per-owner table.
_ADMISSION_IDEMPOTENCY_SCOPE = "offer_version_admission"

# The shared ledger's generic account/ref columns are not an admission
# contract.  Admission stores its produced-row identity and request
# fingerprint together in ref_id, while account_id remains unused.
_ADMISSION_RESULT_EVIDENCE_RE = re.compile(
    r"(?P<offer_version_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\|"
    r"(?P<fingerprint>[0-9a-f]{64})\Z"
)

#: Name of the DB-level unique constraint on
#: ``(offer_versions.offer_id, offer_versions.version_number)``
#: (``alembic/versions/611_offer_versions_unique_version_number.py``). Used
#: to distinguish an actual duplicate-version-number race from an unrelated
#: integrity violation (e.g. a dangling FK on ``region_zone_id``) hitting the
#: same broad ``except IntegrityError`` — the latter must never be mislabeled
#: as ``duplicate_version_number``.
_DUPLICATE_VERSION_NUMBER_CONSTRAINT = "uq_offer_versions_offer_id_version_number"


class OfferAccessRequirementError(DomainError):
    """Fail-closed offer-access-requirement admission/classification error."""


def _encode_admission_result_evidence(offer_version_id: UUID, fingerprint: str) -> str:
    """Bind the produced OfferVersion identity to its request fingerprint."""

    encoded = f"{offer_version_id}|{fingerprint}"
    if _ADMISSION_RESULT_EVIDENCE_RE.fullmatch(encoded) is None:
        raise ValueError("invalid admission result evidence")
    return encoded


def _decode_admission_result_evidence(value: str | None) -> tuple[UUID, str]:
    """Decode strict, lowercase admission replay evidence."""

    match = _ADMISSION_RESULT_EVIDENCE_RE.fullmatch(value or "")
    if match is None:
        raise ValueError("invalid admission result evidence")
    return UUID(match["offer_version_id"]), match["fingerprint"]


def _error(
    suffix: str, message: str, *, retryable: bool, **details: object
) -> OfferAccessRequirementError:
    """Every call site states ``retryable`` explicitly (no default): the SOT
    manifest declares only ``stale_preview`` as retryable and every other
    refusal as terminal — a silent ``DomainError``-inherited default of
    ``True`` would contradict that contract for every other code."""

    return OfferAccessRequirementError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=dict(details),
        retryable=retryable,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _admission_fingerprint(payload: OfferVersionCreate) -> str:
    """Fingerprint of the exact client-supplied admission request.

    Computed over the RAW payload the caller supplied (never over
    server-resolved catalog defaults) so a genuine retry of the same request
    fingerprints identically regardless of how default resolution evolves.

    ``model_dump()`` alone is not enough: Pydantic fills every OMITTED
    optional field (e.g. ``billing_cycle``) with its schema default before
    this ever sees it, so an omitted field and an explicit value that
    happens to equal that same default are indistinguishable in the dumped
    dict — a request that left ``billing_cycle`` unset (letting
    ``_admit``'s own settings-resolved default decide) could silently
    collide or diverge against one that explicitly asked for the schema
    default. Including ``model_fields_set`` makes "omitted" and "explicitly
    set to the default value" fingerprint differently.
    """

    return _digest(
        {
            "payload": payload.model_dump(mode="json"),
            "fields_set": sorted(payload.model_fields_set),
        }
    )


def _is_duplicate_version_number_violation(exc: IntegrityError) -> bool:
    """True only for the specific (offer_id, version_number) unique violation.

    Distinguishes it from any other ``IntegrityError`` (e.g. a dangling FK on
    ``region_zone_id``/``usage_allowance_id``/``sla_profile_id``/
    ``policy_set_id``) that must never be mislabeled as a duplicate. Matches
    on the real Postgres constraint name when available, and falls back to
    both column names appearing together (SQLite's error text names columns,
    not the constraint) — a plain FK-violation message names neither pair.
    """

    message = str(exc.orig) if exc.orig is not None else str(exc)
    if _DUPLICATE_VERSION_NUMBER_CONSTRAINT in message:
        return True
    return "offer_id" in message and "version_number" in message


def principal_label(system_user_id: UUID) -> str:
    """The recorded-identity string for an authenticated ``SystemUser``.

    This — never a caller-supplied free-text actor string — is what gets
    written as ``classified_by``, the audit actor, the event actor, and the
    ``authenticated_principal`` evidence field. It is derived here, once,
    from the exact id the caller authenticated, so nothing downstream can be
    told to attribute a classification to a different name than the one RBAC
    actually verified.
    """

    return f"system_user:{system_user_id}"


def _verify_classify_permission(db: Session, system_user_id: UUID) -> None:
    """Re-verify RBAC permission INSIDE the command's own transaction.

    A permission check performed earlier (e.g. for a CLI preview) is a
    look-then-act race: the grant could be revoked between that check and
    this command's commit. The only check that counts is the one taken here,
    against the live row, under the same lock/transaction as the write it
    gates.
    """

    # populate_existing=True forces a fresh read of this row even if an
    # earlier call in this same transaction already populated the identity
    # map for this id — without it, a second call here could silently return
    # the FIRST call's cached object and never observe a commit (e.g. a
    # revoked grant or a deactivated principal) that happened in between.
    user = db.get(SystemUser, system_user_id, populate_existing=True)
    if user is None or not user.is_active:
        raise _error(
            "permission_denied",
            "Classification requires an active, authenticated staff principal.",
            retryable=False,
        )
    granted = has_permission(
        {
            "principal_id": str(system_user_id),
            "principal_type": "system_user",
            "roles": set(system_user_role_names(db, system_user_id)),
        },
        db,
        CLASSIFY_PERMISSION,
    )
    if not granted:
        raise _error(
            "permission_denied",
            "Classification requires the catalog:offer_access_requirement:"
            "classify permission.",
            retryable=False,
        )


@dataclass(frozen=True, slots=True)
class StaffPrincipal:
    """An admission attributed to an authenticated staff (system_user)
    principal. The ROUTE (``app/api/catalog.py``'s ``_require_offer_
    version_admission``, on ``admission_router``) already authorizes the
    request by delegating to ``authorize_offer_version_admission`` before
    this command ever runs; this id is ALSO re-verified against the
    identical decision inside the command itself
    (``verify_admission_authorization``, delegating to the SAME owner), as
    defense in depth for a caller that reaches this command directly. It remains the
    recorded audit/attribution identity either way."""

    system_user_id: UUID


@dataclass(frozen=True, slots=True)
class ApiKeyPrincipal:
    """An admission attributed to an authenticated API-key principal,
    re-verified inside the command for the same reason as
    :class:`StaffPrincipal`."""

    api_key_id: UUID


@dataclass(frozen=True, slots=True)
class SubscriberPrincipal:
    """An admission attributed to an authenticated subscriber principal.

    A subscriber can be mapped to the ``admin`` role (or any role holding
    the compound admission permission) via the seeded role-assignment path
    (``scripts/seed/seed_rbac.py``, ``app/services/subscriber_assignments.py``,
    ``app/services/auth_flow.py``'s login role resolution) — this is a
    supported, if unverified-in-production, caller shape, not a theoretical
    one, and it is preserved here rather than silently refused. Re-verified
    against the identical compound permission inside the command, the same
    as every other non-exempt principal type.
    """

    subscriber_id: UUID


@dataclass(frozen=True, slots=True)
class MachineCredentialPrincipal:
    """An admission attributed to a kernel-issued machine credential
    (``dotmac_kernel.machine_auth``), authenticated via
    ``auth_dependencies._machine_principal`` and distinguished from a
    legacy local ``ApiKeyPrincipal`` by ``credential_kind == "machine"`` on
    the auth dict (both currently surface as ``principal_type == "api_key"``
    at the HTTP layer for backward compatibility — see
    ``app/api/catalog.py``'s ``_admission_principal``).

    THIS IS THE EXACT, NON-GROWING COMPATIBILITY PATH for machine-credential
    admission, and — per Michael's ruling — SHADOW MODE IS ITS TERMINAL
    STATE on this branch, not a staging step toward a flag flip. A
    cross-repository census established that the published Kernel
    (``dotmac-kernel==0.1.0a94``, Sub's actual pin) cannot supply what real
    enforcement would need: the ``MachinePrincipal`` it returns carries
    ``credential_id``/``tenant_id``/``label``/``scopes``, but no
    ``application`` field (at this pin) and no expiry/revocation evidence
    on the principal at all (checked inside ``authenticate_machine``, but
    not surfaced) — Sub would have to reinterpret raw authentication facts
    Kernel owns to fill that gap, which is exactly the wrong place for that
    decision to live. Enforcement here waits for a Kernel successor that
    publishes a verified machine principal carrying identity, kind,
    attribution, effective leaf scopes, expiry, and revocation evidence
    (recorded in Knowledge:
    ``dotmac-kernel-verified-machine-authentication-successor-contract``).
    Until that contract exists and is adopted, ``authorize_offer_version_
    admission`` runs this principal's authorization decision in SHADOW /
    WOULD-REFUSE mode only: it computes and logs whether the compound rule
    would have refused, using the scopes captured at authentication time
    (below), but never raises — admission proceeds regardless, identical
    to every machine credential's behavior before this module had any
    command-level check at all. This is the ONE and ONLY principal type
    this module treats this way: the ``credential_kind == "machine"``
    branch in ``authorize_offer_version_admission`` names exactly this
    case, and
    ``test_machine_credential_is_the_only_shadow_mode_principal`` fails the
    build if that set ever silently grows to cover another principal type.

    ``scopes`` is a SNAPSHOT taken at authentication time, not a live
    re-read — this module has no live query surface into the kernel's
    credential/scope storage, and building one to work around the missing
    contract is exactly the workaround this ruling forbids. A scope revoked
    between authentication and this command's transaction is not observed
    by this snapshot; that gap stays open until the successor contract
    lands, not something this module can safely close on its own.
    """

    credential_id: UUID
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SystemAdmission:
    """An admission with no authenticated end-user context at all — internal
    tooling, seed data, or a test fixture calling this command directly
    rather than through the authorized HTTP route.

    Construction of this type is confined to an explicit, test-enforced
    allowlist of call sites
    (``tests/architecture/test_offer_access_requirement_boundary.py``). That
    guard is a BUILD-TIME/reviewed-call-site guarantee — it proves no
    *committed, non-test* file outside the allowlist constructs this type —
    it is NOT an unforgeable runtime credential: any code actually running
    inside that allowed module can still construct one. It exists to make an
    unreviewed new "no actor" admission path visible in review, not to
    cryptographically bind identity. It carries no RBAC identity to
    re-verify, so ``verify_admission_authorization`` exempts it outright.
    """

    reason: str


#: The closed set of ways an admission can be attributed. Recorded for
#: audit/attribution; every member except ``SystemAdmission`` is ALSO
#: re-verified against RBAC by ``verify_admission_authorization`` — see
#: ``AdmitOfferVersionCommand.principal``'s docstring.
AdmissionPrincipal = (
    StaffPrincipal
    | ApiKeyPrincipal
    | SubscriberPrincipal
    | MachineCredentialPrincipal
    | SystemAdmission
)
_ADMISSION_PRINCIPAL_TYPES = (
    StaffPrincipal,
    ApiKeyPrincipal,
    SubscriberPrincipal,
    MachineCredentialPrincipal,
    SystemAdmission,
)


def admission_actor_label(principal: AdmissionPrincipal) -> str:
    """Human-readable ``CommandContext.actor`` label for one principal."""

    if isinstance(principal, StaffPrincipal):
        return f"system_user:{principal.system_user_id}"
    if isinstance(principal, ApiKeyPrincipal):
        return f"api_key:{principal.api_key_id}"
    if isinstance(principal, SubscriberPrincipal):
        return f"subscriber:{principal.subscriber_id}"
    if isinstance(principal, MachineCredentialPrincipal):
        return f"machine_credential:{principal.credential_id}"
    return f"system:{principal.reason}"


def _admission_actor_evidence(
    principal: AdmissionPrincipal,
) -> AuditActor:
    """Typed billing-governance attribution, never an authorization input."""

    if isinstance(principal, StaffPrincipal):
        return AuditActor.user(str(principal.system_user_id))
    if isinstance(principal, ApiKeyPrincipal):
        return AuditActor.api_key(str(principal.api_key_id))
    if isinstance(principal, SubscriberPrincipal):
        return AuditActor.user(str(principal.subscriber_id))
    if isinstance(principal, MachineCredentialPrincipal):
        # The audit enum has no machine_credential member. Preserve the
        # authenticated credential class as api_key rather than allowing a
        # string fallback to relabel it as an anonymous system action.
        return AuditActor.api_key(str(principal.credential_id))
    return AuditActor.system()


def _subscriber_role_names(db: Session, subscriber_id: UUID) -> tuple[str, ...]:
    """Live role-name read for one subscriber.

    The identical ``SubscriberRole -> Role`` join ``has_permission``'s own
    subscriber branch and ``auth_flow._load_rbac_claims``'s subscriber
    branch already use — read fresh here (never through the 300-second
    RBAC-claims cache ``claims_for_principal`` sits on top of) so a role
    granted or revoked moments earlier is observed immediately. Mirrors
    ``system_user_assignments.system_user_role_names``'s shape for the
    sibling principal type.
    """

    rows = (
        db.execute(
            select(Role.name)
            .join(SubscriberRole, SubscriberRole.role_id == Role.id)
            .where(
                SubscriberRole.subscriber_id == subscriber_id,
                Role.is_active.is_(True),
            )
            .distinct()
            .order_by(Role.name)
        )
        .scalars()
        .all()
    )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class AdmissionAuthorizationClaims:
    """Typed boundary for ``authorize_offer_version_admission`` — the exact
    claims its decision needs, constructed identically by both callers: the
    ROUTE (from a live, HTTP-authenticated auth dict — cached/session
    claims) and the COMMAND (from a fresh per-principal database re-read —
    live claims), so the SAME typed shape reaches the one decision function
    regardless of which adapter built it. An untyped ``auth: dict`` was
    flagged (round 12) as exactly the kind of free-form primitive bag this
    repository's coding rules forbid as an owner-interface boundary: two
    differently-constructed dicts (route-cached vs. command-reconstructed)
    could silently diverge in shape or key spelling with nothing to catch
    it, and a typo'd key would read as "no grant" rather than fail loudly.

    ``auth_dependencies.has_permission``/``staff_write_restricted``'s own
    ``dict`` parameters are unrelated, pre-existing, file-wide shared
    infrastructure used across the whole application and are out of scope
    for this change; ``as_dict()`` below is the one, single, explicit
    translation point into that shared shape, kept as narrow as possible.

    ``credential_kind`` (round 13 finding 2) carries
    ``auth_dependencies``'s ``"machine"``/``"legacy_api_key"`` stamp
    through to the decision itself — not just to principal resolution.
    Before this field existed, the ROUTE built claims with no way to say
    "this is a machine credential", so ``authorize_offer_version_
    admission`` enforced the compound rule against it exactly like any
    other API key and refused a valid kernel credential outright,
    UNREACHABLE before the shadow branch (only constructed afterward, from
    the now-already-refused auth dict, inside ``create_offer_version``) —
    the exact lockout this whole migration exists to prevent, live again.
    ``authorize_offer_version_admission`` reads THIS field to route to
    ``_shadow_check_machine_credential_admission`` (evaluate, log, never
    refuse) INSTEAD of the enforced check, for both callers identically.
    """

    principal_id: str
    principal_type: str
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    credential_kind: str | None = None

    def as_dict(self) -> dict[str, object]:
        """The exact shape ``auth_dependencies``'s dict-based primitives
        expect. Never constructed by hand anywhere else in this module."""

        return {
            "principal_id": self.principal_id,
            "principal_type": self.principal_type,
            "roles": set(self.roles),
            "scopes": set(self.scopes),
        }


def _admission_permission_granted(auth: dict, db: Session) -> bool:
    """The compound permission LEG of the admission decision: ``catalog:write
    AND (catalog:billing_write OR catalog:offer_version:admission)``.

    Re-derived here via the SAME ``has_permission`` function
    ``auth_dependencies.py``'s own ``require_permission``/
    ``require_any_permission``/``require_method_permission`` dependencies
    call — there is exactly one place that decides what a role/scope means.

    This is a LEG, not the whole decision — it says nothing about the ERP
    staff leave-write restriction. Nothing outside ``authorize_offer_version_
    admission`` (the one owner, below) may call this directly for an actual
    authorization decision; it exists as a private helper of that owner.
    Takes the raw ``dict`` shape (an internal implementation detail of the
    typed owner above it, never a public boundary of its own).
    """

    return has_permission(auth, db, WRITE_PERMISSION) and (
        has_permission(auth, db, BILLING_WRITE_PERMISSION)
        or has_permission(auth, db, ADMISSION_SCOPE)
    )


def authorize_offer_version_admission(
    db: Session,
    claims: AdmissionAuthorizationClaims,
    *,
    request_id: str | None = None,
) -> None:
    """THE single owner of the offer-version-admission authorization
    decision — not a leaf permission check, the WHOLE decision: the
    compound permission rule above, AND (for a ``system_user`` principal)
    the ERP staff leave-write restriction
    (``app/services/erp_staff_access.py``'s ``staff_write_restricted`` /
    ``audit_denied_write``).

    ``app/api/catalog.py``'s route dependency and this module's own
    in-transaction command re-check (``verify_admission_authorization``)
    both DELEGATE to this ONE function rather than each independently
    deciding — there is exactly one decision, so there is nothing for the
    two call sites to disagree about, and no name-matching guard is needed
    to keep them "in sync": there is only one implementation to keep at all.
    An active staff leave restriction (account active, roles/grants intact,
    writes refused) is refused here exactly as it is over HTTP — a
    caller reaching this command directly must not get a MORE permissive
    answer than the route would have given the identical principal.

    ONLY GATE ON THE HTTP PATH TOO (round 12 finding 2 fix): the offer-
    version admission routes are mounted on ``app/api/catalog.py``'s
    ``admission_router``, which deliberately carries NO blanket router-level
    dependency — unlike this file's other catalog routes, which sit under a
    pre-existing, admission-unrelated ``catalog:write`` gate
    (``require_method_permission``) whose own ``require_permission``
    independently applies the identical ``erp_staff_access.staff_write_
    restricted`` check via older, separate plumbing (a bare-string
    HTTPException detail, and an inline-committed audit write). That older
    check cannot produce a DIFFERENT verdict from this one (both call the
    same underlying primitive) and never runs ahead of this function for
    these two routes specifically, because they are exempt from that
    blanket gate — this function is the one and only decision-maker for
    both the route and a caller who reaches ``admit_offer_version``
    directly (a background job, CLI, or other non-route caller, which
    bypasses the router entirely regardless).

    THIS FUNCTION NEVER COMMITS AND NEVER WRITES AUDIT EVIDENCE ITSELF
    (round 13 correction of an earlier, wrong fix): an in-transaction
    ``db.commit()`` here, when called from ``verify_admission_authorization``
    inside ``execute_owner_command``'s owned transaction, is rejected by
    that boundary's own ``before_commit`` guard
    (``_reject_helper_commit`` in ``app/services/owner_commands.py``) —
    "only the public command boundary may commit its transaction" — so the
    caller got ``OwnerCommandError(nested_transaction_completion)`` instead
    of ``permission_denied``, and the staged audit row was rolled back
    anyway. The fix was worse than the bug it targeted.

    Michael's ruling: a denial rolls back NORMALLY, like any other refusal.
    When the refusal is specifically a staff-leave restriction, this
    function attaches everything needed to reconstruct the audit evidence
    onto the raised error's ``details`` (``leave_restricted=True``, the
    principal identity, and the restriction's own two fields) — it does
    NOT write anything. The OUTER command boundary
    (``admit_offer_version``) and the route adapter
    (``app/api/catalog.py``'s ``_require_offer_version_admission``) each
    call ``record_leave_denial_evidence`` AFTER the denial has already
    unwound in their own transaction, through a genuinely separate one. A
    failure recording that evidence is logged and swallowed — it must
    never mask or replace the ``permission_denied`` the caller already has.

    MACHINE CREDENTIALS ARE SHADOW-ONLY (round 13 finding 2 fix):
    ``claims.credential_kind == "machine"`` routes to
    ``_shadow_check_machine_credential_admission`` — evaluate, log, NEVER
    raise — instead of the enforced check below, for BOTH callers
    identically. This must be checked BEFORE the compound-permission
    enforcement, not after: a valid kernel machine credential without the
    newer, narrower admission scopes must still succeed all the way
    through, not get refused here and never reach the shadow path at all
    (which is exactly what happened when the route built claims with no
    ``credential_kind`` and the typed ``MachineCredentialPrincipal`` was
    only constructed AFTER this dependency had already succeeded or
    failed).
    """

    if claims.credential_kind == "machine":
        _shadow_check_machine_credential_admission(claims)
        return

    auth = claims.as_dict()
    if not _admission_permission_granted(auth, db):
        raise _error(
            "permission_denied",
            "Offer version admission requires catalog:write and either "
            "catalog:billing_write or catalog:offer_version:admission.",
            retryable=False,
        )

    # Local import mirrors auth_dependencies.py's own lazy-import convention
    # for this exact module (avoids a load-time dependency on ERP staff
    # access plumbing for every caller of offer_access_requirement that never
    # touches it). staff_write_restricted() itself no-ops for any
    # principal_type other than "system_user", so this is safe to call
    # unconditionally for every principal that reaches this point.
    from app.services import erp_staff_access

    restriction = erp_staff_access.staff_write_restricted(db, auth, method="POST")
    if restriction is None:
        return
    raise _error(
        "permission_denied",
        "Offer version admission is refused: an active staff leave "
        "restriction permits read-only access.",
        retryable=False,
        leave_restricted=True,
        principal_id=claims.principal_id,
        principal_type=claims.principal_type,
        restriction_id=restriction.restriction_id,
        restriction_source_system=restriction.source_system,
        request_id=request_id,
    )


def record_leave_denial_evidence(db: Session, exc: OfferAccessRequirementError) -> None:
    """Durably record a staff-leave admission denial, through a genuinely
    SEPARATE registered owner command (``_RECORD_LEAVE_DENIAL_COMMAND``) —
    called AFTER ``authorize_offer_version_admission`` has already raised
    and that raise has already propagated past its caller's own
    transaction boundary (the route's plain session, or
    ``execute_owner_command``'s rollback for the direct-command path). This
    is a REGISTERED command, not a helper: session lifecycle (open/close)
    stays the adapter's job, but beginning and completing THIS transaction
    is ``execute_owner_command``'s job, exactly as it is for admission and
    classification — this function's own callback stays flush-only and
    never calls ``commit()``/``rollback()`` itself (round 14 finding 1: an
    earlier version of this function did both directly, which
    ``docs/CODING_STANDARD.md`` § 3 forbids for a nested helper, and which
    silently rolled back any of the CALLER's own in-flight work still
    sitting in the session at that point).

    No-ops for any error that is not a leave-restriction denial
    (``exc.details["leave_restricted"]`` unset) — an ordinary
    compound-permission refusal has no prior audit event to reconstruct.

    Best-effort and STRICTLY NON-MASKING: this function never raises. A
    failure — including the defensive rollback of any lingering caller
    transaction below, or the registered command itself failing manifest
    validation or its own transaction — is logged and swallowed, exactly
    because the caller already has the real, correct ``permission_denied``
    to return: losing the audit trail a second time must never turn into
    losing the original refusal too.
    """

    if not isinstance(exc, OfferAccessRequirementError) or not exc.details.get(
        "leave_restricted"
    ):
        return

    from uuid import uuid4

    from app.services import erp_staff_access

    try:
        # Defensive only: execute_owner_command itself requires a
        # transaction-free session at entry and would otherwise roll back
        # and refuse to run at all — clearing any lingering, already-
        # abandoned caller transaction here is what lets the registered
        # command actually execute instead of silently no-op'ing on this
        # guard.
        if db.in_transaction():
            db.rollback()

        principal_id = exc.details.get("principal_id")
        auth = {
            "principal_id": principal_id,
            "principal_type": exc.details.get("principal_type"),
        }
        restriction_id = exc.details.get("restriction_id")
        source_system = exc.details.get("restriction_source_system")
        if not isinstance(restriction_id, str) or not restriction_id:
            raise ValueError("leave denial lacks restriction identity")
        if not isinstance(source_system, str) or not source_system:
            raise ValueError("leave denial lacks restriction source")
        request_id = exc.details.get("request_id")

        def _operation() -> None:
            erp_staff_access.audit_denied_write_identity(
                db,
                auth=auth,
                restriction_id=restriction_id,
                source_system=source_system,
                request_id=request_id,
                permission_key=ADMISSION_SCOPE,
            )

        command_id = uuid4()
        execute_owner_command(
            db,
            definition=_RECORD_LEAVE_DENIAL_COMMAND,
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=str(principal_id or "unknown"),
                scope=ADMISSION_SCOPE,
                reason="record a refused admission attempt for audit",
            ),
            operation=_operation,
        )
    except Exception:
        logger.exception(
            "offer_version_admission.leave_denial_audit_failed principal_id=%s",
            exc.details.get("principal_id"),
        )
        try:
            db.rollback()
        except Exception:  # pragma: no cover - defensive, session may be unusable
            pass


def _shadow_check_machine_credential_admission(
    claims: AdmissionAuthorizationClaims,
) -> None:
    """SHADOW / WOULD-REFUSE evaluation for a machine-credential admission —
    evaluates and LOGS what the compound rule would have decided; NEVER
    raises, and NEVER refuses, no matter what. This is the TERMINAL state
    for a machine credential on this branch, not a staging step (see
    ``MachineCredentialPrincipal``'s own docstring for the full ruling):
    the published Kernel Sub actually depends on cannot yet supply a
    verified machine principal carrying the identity, attribution, scope,
    expiry, and revocation evidence real enforcement would require, so a
    machine credential that ``origin/main`` authorized must still succeed
    here — hard-enforcing today, with what this module can actually see,
    would be an uncensused, silent access retirement.

    TWO PROPERTIES this function is held to, both round-14 corrections of
    the earlier shape:

    1. NEVER RAISES. The earlier version called the DB-backed
       ``_admission_permission_granted``/``has_permission``, so an RBAC
       query error or statement timeout escaped as an unhandled exception
       — a 500 in front of admission, which directly contradicts "evaluate
       and log, never raise." Everything below is wrapped so no exception
       from the evaluation itself can ever propagate.
    2. READS ONLY THE CAPTURED SNAPSHOT, never a live authority table.
       ``_admission_permission_granted`` also takes the ``principal_type
       == "api_key"`` branch of ``has_permission``, which — for anything
       NOT ``"system_user"`` — queries ``SubscriberRole``/
       ``SubscriberPermission`` keyed by ``principal_id``. A machine
       credential's UUID coincidentally matching (or a wildcard grant
       existing for) an unrelated subscriber row could make this shadow
       diagnostic report "authorized" for a credential whose ACTUAL
       captured scopes authorize nothing — a diagnostic that can lie is
       worse than one that is silent. The check below is pure in-memory
       set membership against ``claims.scopes`` (via
       ``auth_dependencies._expand_permission_keys``, the SAME
       alias/wildcard-expansion primitive ``has_permission`` itself uses
       for its own scope-intersection shortcut — not a second, differently
       -written expansion), and touches no database table at all.

    Works ONLY from what this module can actually see: ``claims.scopes``,
    the snapshot captured at authentication time
    (``auth_dependencies._machine_principal``, via its ``credential_kind``
    stamp) and threaded through by BOTH callers — the route
    (``app/api/catalog.py``'s ``_require_offer_version_admission``, from
    the live HTTP auth dict) and the command
    (``verify_admission_authorization``, from the typed
    ``MachineCredentialPrincipal``). This module has no live query surface
    into the kernel's own credential/scope storage, and building one to
    work around the missing successor contract is exactly the workaround
    Michael's ruling forbids — the logged decision is necessarily a
    point-in-time approximation, a diagnostic/inventory aid, never a live
    re-verification.
    """

    try:
        from app.services.auth_dependencies import _expand_permission_keys

        def _scope_satisfies(permission_key: str) -> bool:
            possible = set(_expand_permission_keys(permission_key))
            return bool(claims.scopes & possible)

        would_be_granted = _scope_satisfies(WRITE_PERMISSION) and (
            _scope_satisfies(BILLING_WRITE_PERMISSION)
            or _scope_satisfies(ADMISSION_SCOPE)
        )
    except Exception:
        # The evaluation itself must never be able to refuse or crash
        # admission — this is a diagnostic aid, not a gate. Log and treat
        # as "could not evaluate", never as a refusal.
        logger.exception(
            "offer_version_admission.machine_credential_shadow_failed: "
            "shadow evaluation itself raised; admission proceeds "
            "regardless (diagnostic only) credential_id=%s",
            claims.principal_id,
        )
        return

    if would_be_granted:
        logger.info(
            "offer_version_admission.machine_credential_shadow: would be "
            "authorized (compatibility path, not enforced) credential_id=%s",
            claims.principal_id,
        )
        return
    logger.warning(
        "offer_version_admission.machine_credential_shadow: WOULD REFUSE "
        "under the compound admission rule (catalog:write AND "
        "(catalog:billing_write OR catalog:offer_version:admission)) but "
        "admission is NOT enforced against machine credentials yet — "
        "proceeding under the compatibility path. credential_id=%s scopes=%s",
        claims.principal_id,
        sorted(claims.scopes),
    )


def verify_admission_authorization(
    db: Session, principal: AdmissionPrincipal, *, request_id: str | None = None
) -> None:
    """Re-verify the admission/mutation authorization decision INSIDE the
    caller's own transaction — defense in depth on top of the route-level
    gate (``app/api/catalog.py``'s ``_require_offer_version_admission``),
    which stays in place and is not removed by this check. A caller that
    reaches this directly (bypassing the route) is held to the IDENTICAL
    decision, made by the SAME owner function
    (``authorize_offer_version_admission``) the route delegates to — not a
    second, independently-maintained approximation of it. PUBLIC (not
    underscore-prefixed) because ``_admit`` (admission), ``OfferVersions.
    update`` (PATCH — every other field mutation of an already-admitted
    row), and ``OfferVersions.delete`` (deactivation) all call it (``app/
    services/catalog/offers.py``) — this is the ONE reusable live-claims
    rebuild + owner delegation, not several separately maintained copies of
    the same per-principal-type resolution. Not every mutation in this
    module is covered by this list — only these three call it today; a new
    mutation of an already-admitted offer version must call it too, not be
    assumed to inherit the property.

    ``SystemAdmission`` is exempt — see its own docstring; it carries no
    RBAC identity to check.

    Called TWICE from ``_admit``: once immediately after the advisory locks
    are acquired (before any existence check), and again immediately before
    the ``OfferVersion`` INSERT, narrowing the window between a permission
    read and the write it gates — the same two-checkpoint discipline
    ``_classify`` already uses. ``OfferVersions.update`` calls it ONCE,
    immediately before its own mutation (see that function for why a
    single check suffices there).

    RESIDUAL PREMISE, stated precisely rather than left implicit: neither
    caller locks the principal's own authorization state
    (``system_users``/``roles``/``role_permissions``/``permissions``/
    ``subscriber_roles``/``subscriber_permissions``/``api_keys`` rows) —
    admission's advisory locks only serialize concurrent admissions of the
    SAME idempotency key or (offer_id, version_number) target, and an
    update holds no lock of its own kind at all. A revoke of the exact
    grant that made THIS check pass, committed by another transaction in
    the narrow window between this check and the caller's own commit, is
    not observed — the write still proceeds and persists under what is, by
    the time it lands, already-revoked authority. This is the identical
    class of residual window ``_classify`` documents and accepts for the
    same reason: closing it fully would require row-locking the entire
    RBAC surface (six-plus tables, several of them shared by every other
    authorization check in the system) for the duration of every admission
    or update, which is judged disproportionate given that the acting
    principal is recorded (audit/attribution — never anonymous) and the
    resulting row is ordinary, visible, correctable data: an operator can
    deactivate or correct it through the existing offer-version
    admin/repair paths like any other wrongly-mutated row, it is not a
    silent or unrecoverable state. The window is one commit wide, not
    open-ended.
    """

    if isinstance(principal, SystemAdmission):
        return

    if isinstance(principal, StaffPrincipal):
        # populate_existing=True: force a fresh read even if an earlier call
        # in this same transaction already populated the identity map for
        # this id — mirrors _verify_classify_permission's same discipline.
        user = db.get(SystemUser, principal.system_user_id, populate_existing=True)
        if user is None or not user.is_active:
            raise _error(
                "permission_denied",
                "Offer version admission requires an active, authenticated "
                "staff principal.",
                retryable=False,
            )
        claims = AdmissionAuthorizationClaims(
            principal_id=str(principal.system_user_id),
            principal_type="system_user",
            roles=frozenset(system_user_role_names(db, principal.system_user_id)),
        )
    elif isinstance(principal, ApiKeyPrincipal):
        api_key = db.get(ApiKey, principal.api_key_id, populate_existing=True)
        now = datetime.now(UTC)
        expired = api_key is not None and (
            api_key.expires_at is not None and _utc(api_key.expires_at) <= now
        )
        if (
            api_key is None
            or not api_key.is_active
            or api_key.revoked_at is not None
            or expired
        ):
            raise _error(
                "permission_denied",
                "Offer version admission requires an active, unrevoked, "
                "unexpired API-key principal.",
                retryable=False,
            )
        # API keys carry no roles (auth_dependencies._api_key_principal);
        # their access is exactly their scopes, wildcard-aware via the same
        # has_permission() call every other principal type goes through.
        claims = AdmissionAuthorizationClaims(
            principal_id=str(principal.api_key_id),
            principal_type="api_key",
            scopes=frozenset(api_key.scopes or ()),
        )
    elif isinstance(principal, SubscriberPrincipal):
        subscriber = db.get(Subscriber, principal.subscriber_id, populate_existing=True)
        if subscriber is None or not subscriber.is_active:
            raise _error(
                "permission_denied",
                "Offer version admission requires an active, authenticated "
                "subscriber principal.",
                retryable=False,
            )
        claims = AdmissionAuthorizationClaims(
            principal_id=str(principal.subscriber_id),
            principal_type="subscriber",
            roles=frozenset(_subscriber_role_names(db, principal.subscriber_id)),
        )
    elif isinstance(principal, MachineCredentialPrincipal):
        # EXACT, NON-GROWING COMPATIBILITY PATH: credential_kind="machine"
        # routes the SAME authorize_offer_version_admission call below to
        # its shadow branch — see MachineCredentialPrincipal's own
        # docstring for why, and
        # test_machine_credential_is_the_only_shadow_mode_principal for the
        # guard that fails the build if this set of one silently grows.
        # Going through the one owner (rather than a separate return here,
        # the round-12 shape) is what round 13 finding 2 required: the
        # route and the command now reach shadow mode through the
        # identical function, keyed on the identical field.
        claims = AdmissionAuthorizationClaims(
            principal_id=str(principal.credential_id),
            principal_type="api_key",
            scopes=frozenset(principal.scopes),
            credential_kind="machine",
        )
    else:  # pragma: no cover - closed union; __post_init__ already refuses
        # any object outside AdmissionPrincipal at construction time.
        raise _error(
            "permission_denied",
            "Offer version admission requires a recognized principal.",
            retryable=False,
        )

    authorize_offer_version_admission(db, claims, request_id=request_id)


def _lock_key(*parts: object) -> int:
    """Stable signed-bigint advisory-lock key for one tuple of parts.

    sha256-derived (never the builtin ``hash``, which is per-process salted)
    so every process/worker derives the same key for the same parts —
    mirrors ``app/services/radio_registration.py``'s ``mac_lock_key``.
    """

    digest = hashlib.sha256(
        "|".join(str(part) for part in parts).encode("utf-8")
    ).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


def _acquire_xact_lock(db: Session, *parts: object) -> None:
    """Transaction-scoped advisory lock, released at commit/rollback.

    No-op on non-PostgreSQL engines (SQLite tests), as in
    ``app/services/radio_registration.py::acquire_mac_lock``.
    """

    bind = db.get_bind()
    dialect_name = bind.dialect.name if bind is not None else ""
    if dialect_name != "postgresql":
        return
    db.execute(select(func.pg_advisory_xact_lock(_lock_key(*parts))))


# --------------------------------------------------------------------------
# Admission command (the only way OfferVersions.create persists a row) and
# the immutability guard (called from OfferVersions.update).
# --------------------------------------------------------------------------


def validate_admission_access_requirement(value: object) -> AccessRequirement:
    """Validate the explicit access requirement supplied at creation.

    Release 1 requires the field explicitly on every new
    :class:`OfferVersion` and accepts ``unclassified`` as an explicit value.
    There is no application-level fallback: a caller that did not supply a
    real enum member is rejected here rather than silently defaulted.
    """

    if not isinstance(value, AccessRequirement):
        raise _error(
            "invalid_access_requirement",
            "Offer version creation requires an explicit access_requirement value.",
            value=value,
            retryable=False,
        )
    return value


def assert_access_requirement_immutable(update_payload: Mapping[str, object]) -> None:
    """Fail closed if any offer-version update path ever carries this field.

    Defense in depth: ``OfferVersionUpdate`` deliberately has no
    ``access_requirement`` field, so this should be unreachable in practice.
    A future edit that reintroduces the field on the update schema fails
    loudly here instead of silently becoming a second, ungoverned writer.
    """

    if "access_requirement" in update_payload:
        raise _error(
            "immutable_access_requirement",
            "offer_versions.access_requirement is immutable outside the "
            "reviewed classification command.",
            retryable=False,
        )


@dataclass(frozen=True, slots=True)
class AdmitOfferVersionCommand:
    context: CommandContext
    payload: OfferVersionCreate
    #: REQUIRED, no default. Authorization for admission has ONE decision
    #: owner (``authorize_offer_version_admission``); TWO callers delegate
    #: to it — the route (``app/api/catalog.py``'s ``_require_offer_
    #: version_admission``, on ``admission_router``, which carries NO
    #: blanket router-level gate of its own) and this command's own
    #: ``verify_admission_authorization`` (called from ``_admit``), which
    #: re-derives and checks the identical decision against the live
    #: database for whichever principal is supplied — never a caller-
    #: asserted boolean. ``SystemAdmission`` is exempt (see its docstring).
    #: ``principal`` remains the recorded audit/attribution identity as
    #: well; every caller must construct one explicitly (see
    #: ``AdmissionPrincipal``).
    principal: AdmissionPrincipal

    def __post_init__(self) -> None:
        # The closed union (``StaffPrincipal | ApiKeyPrincipal |
        # SubscriberPrincipal | SystemAdmission``) is only a static type
        # hint — Python does not enforce it at runtime, so a caller passing
        # ``principal=None`` or any arbitrary object would otherwise be
        # accepted silently. This makes the closed set a real runtime
        # guarantee: only omitting the argument raises (a bare ``TypeError``
        # from the dataclass constructor); passing something outside the
        # union now raises here.
        if not isinstance(self.principal, _ADMISSION_PRINCIPAL_TYPES):
            raise TypeError(
                "AdmitOfferVersionCommand.principal must be a StaffPrincipal, "
                "ApiKeyPrincipal, SubscriberPrincipal, or SystemAdmission "
                f"instance; got {type(self.principal).__name__!r}"
            )


@dataclass(frozen=True, slots=True)
class AdmitOfferVersionResult:
    """Typed outcome of ``admit_offer_version`` — matches this module's own
    ``OfferAccessRequirementClassificationResult`` pattern for the sibling
    command, rather than returning the mutable ORM row directly."""

    offer_version: OfferVersion
    #: True when this result reflects a prior admission returned by an
    #: exact idempotency-key replay, not a freshly persisted row.
    replayed: bool


def admit_offer_version(
    db: Session, command: AdmitOfferVersionCommand
) -> AdmitOfferVersionResult:
    """The one path that persists a new ``OfferVersion`` row.

    Owns its own transaction end to end: offer lookup, catalog-default
    resolution, access-requirement admission, the INSERT itself, and the
    billing-governance audit participant all run inside one
    ``execute_owner_command`` boundary. ``OfferVersions.create`` is a thin
    adapter over this — it does not construct the row itself.

    A staff-leave denial rolls back normally (see ``authorize_offer_version_
    admission``'s own docstring); this OUTER boundary then records the
    denial's audit evidence in a genuinely separate transaction, AFTER the
    rollback above has already completed and released the session — never
    inside it.
    """

    try:
        return execute_owner_command(
            db,
            definition=_ADMIT_COMMAND,
            context=command.context,
            operation=lambda: _admit(db, command),
        )
    except OfferAccessRequirementError as exc:
        record_leave_denial_evidence(db, exc)
        raise


def _admit(db: Session, command: AdmitOfferVersionCommand) -> AdmitOfferVersionResult:
    payload = command.payload
    offer = db.get(CatalogOffer, payload.offer_id)
    if not offer:
        raise _error(
            "offer_not_found",
            "Offer not found.",
            offer_id=str(payload.offer_id),
            retryable=False,
        )

    key = (command.context.idempotency_key or "").strip()
    if key and len(key) > _IDEMPOTENCY_KEY_MAX_LENGTH:
        raise _error(
            "idempotency_key_too_long",
            "idempotency_key exceeds the stored column's maximum length.",
            max_length=_IDEMPOTENCY_KEY_MAX_LENGTH,
            retryable=False,
        )

    # Lock order is fixed and identical for every admission: the idempotency
    # key first (if supplied), then the (offer_id, version_number) target.
    # Every caller acquires them in this same order, so this can never
    # deadlock against another admission.
    if key:
        _acquire_xact_lock(db, "offer_access_requirement:admit_idempotency", key)

    # Serialize concurrent admissions targeting the SAME (offer_id,
    # version_number) before either one can observe the other's uncommitted
    # existence check — without this lock, two racing retries of one
    # command (e.g. a client's retried POST after a dropped response) could
    # both pass the lookup below and both insert, creating a duplicate
    # version rather than a safe no-op/typed conflict.
    _acquire_xact_lock(
        db, "offer_access_requirement:admit", payload.offer_id, payload.version_number
    )

    # Re-verify the compound admission permission here, under the advisory
    # locks acquired above and inside this same transaction, immediately
    # before any existence check or write — the same "re-check under the
    # lock" discipline classify's own _verify_classify_permission follows.
    # This is defense in depth on top of the route-level gate, which stays
    # in place; it is not a substitute for it.
    verify_admission_authorization(
        db, command.principal, request_id=str(command.context.correlation_id)
    )

    fingerprint = _admission_fingerprint(payload)
    if key:
        reservation = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _ADMISSION_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
        )
        if reservation is not None:
            try:
                evidence_version_id, evidence_fingerprint = (
                    _decode_admission_result_evidence(reservation.ref_id)
                )
            except ValueError:
                raise _error(
                    "idempotency_conflict",
                    "The prior admission has missing or malformed result evidence.",
                    offer_id=str(payload.offer_id),
                    version_number=payload.version_number,
                    retryable=False,
                ) from None
            if (
                reservation.account_id is not None
                or evidence_fingerprint != fingerprint
            ):
                raise _error(
                    "idempotency_conflict",
                    "This idempotency key was already used for a different "
                    "offer version admission.",
                    offer_id=str(payload.offer_id),
                    version_number=payload.version_number,
                    retryable=False,
                )
            replayed_version = db.get(OfferVersion, evidence_version_id)
            if replayed_version is None:
                raise _error(
                    "idempotency_conflict",
                    "The prior admission result is no longer available.",
                    offer_id=str(payload.offer_id),
                    version_number=payload.version_number,
                    retryable=False,
                )
            return AdmitOfferVersionResult(
                offer_version=replayed_version, replayed=True
            )

    existing = db.scalar(
        select(OfferVersion).where(
            OfferVersion.offer_id == payload.offer_id,
            OfferVersion.version_number == payload.version_number,
        )
    )
    if existing is not None:
        raise _error(
            "duplicate_version_number",
            "This offer already has a version with this version_number; a "
            "retried admission must not resubmit an existing version_number "
            "as a new row.",
            offer_id=str(payload.offer_id),
            version_number=payload.version_number,
            retryable=False,
        )

    data = payload.model_dump()
    data["access_requirement"] = validate_admission_access_requirement(
        data.get("access_requirement")
    )
    fields_set = payload.model_fields_set
    if "billing_cycle" not in fields_set:
        default_billing_cycle = settings_spec.resolve_value(
            db, SettingDomain.catalog, "default_billing_cycle"
        )
        if default_billing_cycle:
            data["billing_cycle"] = validate_enum(
                default_billing_cycle, BillingCycle, "billing_cycle"
            )
    if "contract_term" not in fields_set:
        default_contract_term = settings_spec.resolve_value(
            db, SettingDomain.catalog, "default_contract_term"
        )
        if default_contract_term:
            data["contract_term"] = validate_enum(
                default_contract_term, ContractTerm, "contract_term"
            )
    if "status" not in fields_set:
        default_status = settings_spec.resolve_value(
            db, SettingDomain.catalog, "default_offer_status"
        )
        if default_status:
            data["status"] = validate_enum(default_status, OfferStatus, "status")

    # Re-verify again, immediately before the write — narrows the window
    # opened by the settings_spec.resolve_value() calls above (each is a
    # separate statement, any of which could in principle yield to another
    # transaction) between the first check and the INSERT. This mirrors
    # _classify's own "verify at the top, verify again right before the
    # mutation" discipline. It narrows the window; it does not close it —
    # see verify_admission_authorization's docstring for the precise,
    # honestly-stated residual premise this does NOT eliminate.
    verify_admission_authorization(
        db, command.principal, request_id=str(command.context.correlation_id)
    )

    version = OfferVersion(**data)
    db.add(version)
    try:
        db.flush()
    except IntegrityError as exc:
        # Defense in depth for a race the advisory lock above should already
        # have serialized (e.g. a writer that bypasses this command's lock):
        # a raw constraint violation still surfaces as this command's own
        # typed conflict. Only the SPECIFIC (offer_id, version_number)
        # unique violation is ever mislabeled as duplicate_version_number —
        # any other integrity violation (e.g. a dangling FK on
        # region_zone_id/usage_allowance_id/sla_profile_id/policy_set_id)
        # surfaces as its own distinct, honestly-named typed error.
        if _is_duplicate_version_number_violation(exc):
            raise _error(
                "duplicate_version_number",
                "This offer already has a version with this version_number.",
                offer_id=str(payload.offer_id),
                version_number=payload.version_number,
                retryable=False,
            ) from exc
        raise _error(
            "admission_integrity_violation",
            "Offer version admission violated a database integrity "
            "constraint unrelated to version_number uniqueness.",
            offer_id=str(payload.offer_id),
            version_number=payload.version_number,
            detail=str(exc.orig) if exc.orig is not None else str(exc),
            retryable=False,
        ) from exc

    if key:
        db.add(
            IdempotencyKey(
                scope=_ADMISSION_IDEMPOTENCY_SCOPE,
                key=key,
                account_id=None,
                ref_id=_encode_admission_result_evidence(version.id, fingerprint),
            )
        )
        try:
            db.flush()
        except IntegrityError as exc:
            # The idempotency-key advisory lock above should already have
            # serialized concurrent uses of this exact key; this is defense
            # in depth against a writer that bypasses that lock.
            raise _error(
                "idempotency_conflict",
                "This idempotency key was already used for a different "
                "offer version admission.",
                offer_id=str(payload.offer_id),
                version_number=payload.version_number,
                retryable=False,
            ) from exc

    billing_governance.stage_billing_catalog_change(
        db,
        action="version_created",
        entity_type="offer_version",
        entity_id=version.id,
        changes=data,
        actor=_admission_actor_evidence(command.principal),
        offer_id=version.offer_id,
    )
    actor_label = admission_actor_label(command.principal)
    emit_event(
        db,
        EventType.catalog_offer_version_admitted,
        {
            "schema_version": 1,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "idempotency_key": key or None,
            "offer_id": str(version.offer_id),
            "offer_version_id": str(version.id),
            "version_number": version.version_number,
            "access_requirement": version.access_requirement.value,
            "authenticated_principal": actor_label,
        },
        actor=actor_label,
    )
    return AdmitOfferVersionResult(offer_version=version, replayed=False)


# --------------------------------------------------------------------------
# Update command (the only way OfferVersions.update mutates a row).
# --------------------------------------------------------------------------


def _assert_offer_version_identity_immutable(
    update_payload: Mapping[str, object],
) -> None:
    """Fail closed if any update path ever carries ``offer_id`` or
    ``version_number``.

    ``(offer_id, version_number)`` is this row's immutable identity, enforced
    at admission by ``admit_offer_version``'s advisory lock, existence
    check, and DB-level unique constraint
    (``uq_offer_versions_offer_id_version_number``). ``OfferVersionUpdate``
    deliberately has neither field, so this should be unreachable in
    practice — defense in depth, matching
    ``assert_access_requirement_immutable``'s same shape, against a future
    edit reintroducing either field on the update schema with no lock/
    duplicate-check guarding it.
    """

    identity_fields = {"offer_id", "version_number"} & set(update_payload)
    if identity_fields:
        raise _error(
            "immutable_offer_version_identity",
            "offer_versions.offer_id and .version_number are immutable "
            "outside admission.",
            identity_fields=sorted(identity_fields),
            retryable=False,
        )


@dataclass(frozen=True, slots=True)
class UpdateOfferVersionCommand:
    context: CommandContext
    offer_version_id: UUID
    payload: OfferVersionUpdate
    #: REQUIRED, no default — the same closed AdmissionPrincipal union
    #: admission uses, re-verified against the identical owner
    #: (``authorize_offer_version_admission``, via
    #: ``verify_admission_authorization``) inside this command's own
    #: transaction, immediately before the mutation. A caller with no
    #: authenticated actor must pass ``SystemAdmission`` explicitly.
    principal: AdmissionPrincipal

    def __post_init__(self) -> None:
        if not isinstance(self.principal, _ADMISSION_PRINCIPAL_TYPES):
            raise TypeError(
                "UpdateOfferVersionCommand.principal must be a "
                "StaffPrincipal, ApiKeyPrincipal, SubscriberPrincipal, "
                "MachineCredentialPrincipal, or SystemAdmission instance; "
                f"got {type(self.principal).__name__!r}"
            )


def update_offer_version(
    db: Session, command: UpdateOfferVersionCommand
) -> OfferVersion:
    """The one path that mutates an already-admitted ``OfferVersion``
    row's fields.

    Owns its own transaction end to end (round 16 — Michael's ruling on
    round 15 finding 3): lookup, read-only validation, the in-transaction
    authorization recheck, the mutation, and the billing-governance audit
    participant all run inside ONE ``execute_owner_command`` boundary.
    ``OfferVersions.update`` (``app/services/catalog/offers.py``) is a
    thin adapter over this — it does not mutate the row or complete the
    transaction itself. Before this, ``OfferVersions.update`` called
    ``db.commit()`` directly: a direct caller with unrelated pending work
    in the same session had that work silently committed alongside the
    version update. ``execute_owner_command`` refuses to run at all with a
    pending caller transaction, rather than completing it.
    """

    try:
        return execute_owner_command(
            db,
            definition=_UPDATE_COMMAND,
            context=command.context,
            operation=lambda: _update(db, command),
        )
    except OfferAccessRequirementError as exc:
        record_leave_denial_evidence(db, exc)
        raise


def _update(db: Session, command: UpdateOfferVersionCommand) -> OfferVersion:
    version = db.get(OfferVersion, command.offer_version_id)
    if version is None:
        raise _error(
            "offer_version_not_found",
            "The offer version does not exist.",
            offer_version_id=str(command.offer_version_id),
            retryable=False,
        )

    data = command.payload.model_dump(exclude_unset=True)
    assert_access_requirement_immutable(data)
    _assert_offer_version_identity_immutable(data)
    changes = billing_governance.billing_field_changes(version, data)
    billing_governance.assert_offer_version_update_safe(db, version, changes)

    # Immediately before the mutation, not at the top of this function —
    # narrowing the window between the re-check and the write it gates to
    # the read-only validation above, which touches no session state. This
    # now runs inside the SAME owner-managed transaction the mutation and
    # the audit participant below complete in — not merely "the same
    # session", a structural fact of this command's transaction boundary.
    verify_admission_authorization(
        db, command.principal, request_id=str(command.context.correlation_id)
    )

    for key, value in data.items():
        setattr(version, key, value)

    critical_changes = billing_governance.billing_critical_changes(
        "offer_version", changes
    )
    if critical_changes:
        billing_governance.stage_billing_catalog_change(
            db,
            action="version_updated",
            entity_type="offer_version",
            entity_id=version.id,
            changes=critical_changes,
            actor=_admission_actor_evidence(command.principal),
            offer_id=version.offer_id,
        )
    return version


# --------------------------------------------------------------------------
# Read-only operations worklist of remaining unclassified rows.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnclassifiedOfferVersionRow:
    offer_version_id: UUID
    offer_id: UUID
    version_number: int
    name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UnclassifiedOfferVersionsWorklist:
    total_count: int
    rows: tuple[UnclassifiedOfferVersionRow, ...]
    limit: int
    offset: int


def list_unclassified_offer_versions(
    db: Session, *, limit: int = 50, offset: int = 0
) -> UnclassifiedOfferVersionsWorklist:
    """Deterministic, paginated report of every remaining unclassified row."""

    if limit < 1 or limit > 500:
        raise _error(
            "invalid_worklist_page",
            "Worklist limit must be between 1 and 500.",
            retryable=False,
        )
    if offset < 0:
        raise _error(
            "invalid_worklist_page",
            "Worklist offset cannot be negative.",
            retryable=False,
        )

    base_query = select(OfferVersion).where(
        OfferVersion.access_requirement == AccessRequirement.unclassified
    )
    total_count = int(
        db.scalar(select(func.count()).select_from(base_query.subquery())) or 0
    )
    rows = db.scalars(
        base_query.order_by(OfferVersion.created_at.asc(), OfferVersion.id.asc())
        .limit(limit)
        .offset(offset)
    ).all()
    return UnclassifiedOfferVersionsWorklist(
        total_count=total_count,
        rows=tuple(
            UnclassifiedOfferVersionRow(
                offer_version_id=row.id,
                offer_id=row.offer_id,
                version_number=row.version_number,
                name=row.name,
                created_at=_utc(row.created_at),
            )
            for row in rows
        ),
        limit=limit,
        offset=offset,
    )


# --------------------------------------------------------------------------
# Reviewed classification command.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreviewClassifyOfferAccessRequirementQuery:
    offer_version_id: UUID
    proposed_access_requirement: AccessRequirement
    review_reference: str


@dataclass(frozen=True, slots=True)
class OfferAccessRequirementClassificationPreview:
    offer_version_id: UUID
    current_access_requirement: AccessRequirement
    proposed_access_requirement: AccessRequirement
    row_updated_at: datetime
    review_reference: str
    preview_fingerprint: str
    #: True when this preview reflects an already-recorded transition
    #: (the stored fingerprint of a prior classification), not a fresh one.
    already_applied: bool = False


@dataclass(frozen=True, slots=True)
class ClassifyOfferAccessRequirementCommand:
    context: CommandContext
    query: PreviewClassifyOfferAccessRequirementQuery
    expected_preview_fingerprint: str
    #: The authenticated principal. Permission is re-verified fresh, inside
    #: this command's own transaction — never trusted from an earlier,
    #: separately-computed boolean.
    authorized_system_user_id: UUID


@dataclass(frozen=True, slots=True)
class OfferAccessRequirementClassificationResult:
    offer_version_id: UUID
    previous_access_requirement: AccessRequirement
    new_access_requirement: AccessRequirement
    replayed: bool


def _preview_fingerprint(
    *,
    offer_version_id: UUID,
    current: AccessRequirement,
    proposed: AccessRequirement,
    row_updated_at: datetime,
    review_reference: str,
) -> str:
    return _digest(
        {
            "offer_version_id": str(offer_version_id),
            "current_access_requirement": current.value,
            "proposed_access_requirement": proposed.value,
            "row_updated_at": row_updated_at.isoformat(),
            "review_reference": review_reference,
        }
    )


def preview_classify_offer_version_access_requirement(
    db: Session, query: PreviewClassifyOfferAccessRequirementQuery
) -> OfferAccessRequirementClassificationPreview:
    """Preview one explicit reclassification without changing any records.

    If this exact transition (offer version -> proposed target) was already
    recorded by a prior classification, the preview reflects that recorded
    transition and its STORED fingerprint instead of raising — this is what
    lets a genuine retry (same idempotency key, same inputs) reach the
    command's replay branch instead of being refused before it ever tries.
    A version already classified to a DIFFERENT target, or classified
    without any recorded row (e.g. admitted directly with a real value), is
    still refused: only the exact-target case previews as replayable.
    """

    review_reference = query.review_reference.strip()
    if not review_reference:
        raise _error(
            "missing_review_reference",
            "Classification requires a durable review reference.",
            retryable=False,
        )
    if len(review_reference) > _REVIEW_REFERENCE_MAX_LENGTH:
        raise _error(
            "review_reference_too_long",
            "review_reference exceeds the stored column's maximum length.",
            max_length=_REVIEW_REFERENCE_MAX_LENGTH,
            retryable=False,
        )
    if query.proposed_access_requirement not in _REAL_CLASSIFICATIONS:
        raise _error(
            "invalid_target_classification",
            "The proposed classification must be a real access requirement, "
            "never unclassified.",
            retryable=False,
        )
    version = db.get(OfferVersion, query.offer_version_id)
    if version is None:
        raise _error(
            "offer_version_not_found",
            "The offer version does not exist.",
            offer_version_id=str(query.offer_version_id),
            retryable=False,
        )
    current = version.access_requirement

    existing = db.scalar(
        select(OfferAccessRequirementClassification).where(
            OfferAccessRequirementClassification.offer_version_id == version.id
        )
    )
    if existing is not None:
        if existing.new_access_requirement == query.proposed_access_requirement:
            # An already-applied transition previews as replayable ONLY when
            # the caller's review_reference matches what was actually
            # recorded. Silently substituting the STORED reference here
            # (ignoring what the caller supplied) would let a caller "replay"
            # against a review reference that was never reviewed — the
            # command's documented "same material inputs" guarantee.
            if existing.review_reference != review_reference:
                raise _error(
                    "review_reference_mismatch",
                    "This offer version was already classified under a "
                    "different review reference; a replay must supply the "
                    "exact review reference recorded on the original "
                    "classification.",
                    offer_version_id=str(version.id),
                    retryable=False,
                )
            return OfferAccessRequirementClassificationPreview(
                offer_version_id=version.id,
                current_access_requirement=existing.previous_access_requirement,
                proposed_access_requirement=existing.new_access_requirement,
                row_updated_at=_utc(version.updated_at),
                review_reference=existing.review_reference,
                preview_fingerprint=existing.preview_fingerprint,
                already_applied=True,
            )
        raise _error(
            "already_classified",
            "Only an unclassified offer version may be reviewed-classified; "
            "real-to-real and real-to-unclassified changes are refused.",
            offer_version_id=str(version.id),
            current_access_requirement=current.value,
            retryable=False,
        )
    if current is not AccessRequirement.unclassified:
        raise _error(
            "already_classified",
            "Only an unclassified offer version may be reviewed-classified; "
            "real-to-real and real-to-unclassified changes are refused.",
            offer_version_id=str(version.id),
            current_access_requirement=current.value,
            retryable=False,
        )
    row_updated_at = _utc(version.updated_at)
    fingerprint = _preview_fingerprint(
        offer_version_id=version.id,
        current=current,
        proposed=query.proposed_access_requirement,
        row_updated_at=row_updated_at,
        review_reference=review_reference,
    )
    return OfferAccessRequirementClassificationPreview(
        offer_version_id=version.id,
        current_access_requirement=current,
        proposed_access_requirement=query.proposed_access_requirement,
        row_updated_at=row_updated_at,
        review_reference=review_reference,
        preview_fingerprint=fingerprint,
    )


def classify_offer_version_access_requirement(
    db: Session, command: ClassifyOfferAccessRequirementCommand
) -> OfferAccessRequirementClassificationResult:
    """Apply one reviewed, fingerprint-bound, RBAC-gated reclassification."""

    return execute_owner_command(
        db,
        definition=_CLASSIFY_COMMAND,
        context=command.context,
        operation=lambda: _classify(db, command),
    )


def _classify(
    db: Session, command: ClassifyOfferAccessRequirementCommand
) -> OfferAccessRequirementClassificationResult:
    _verify_classify_permission(db, command.authorized_system_user_id)
    actor = principal_label(command.authorized_system_user_id)

    key = (command.context.idempotency_key or "").strip()
    if not key:
        raise _error(
            "missing_idempotency_key",
            "Classification requires an idempotency key.",
            retryable=False,
        )
    if len(key) > _IDEMPOTENCY_KEY_MAX_LENGTH:
        raise _error(
            "idempotency_key_too_long",
            "idempotency_key exceeds the stored column's maximum length.",
            max_length=_IDEMPOTENCY_KEY_MAX_LENGTH,
            retryable=False,
        )
    reason = (command.context.reason or "").strip()
    if not reason:
        raise _error(
            "missing_reason",
            "Classification requires a reason.",
            retryable=False,
        )

    # Serialize every command sharing this idempotency key BEFORE either one
    # can observe the other's uncommitted state. Without this, two
    # concurrent commands with the SAME key but DIFFERENT offer versions
    # each lock a different OfferVersion row below and can both pass the
    # existing_by_key lookup, with the loser hitting the unique-constraint
    # flush as a raw database error instead of a typed conflict.
    _acquire_xact_lock(db, "offer_access_requirement:classify", key)

    version = lock_for_update(db, OfferVersion, command.query.offer_version_id)
    if version is None:
        raise _error(
            "offer_version_not_found",
            "The offer version does not exist.",
            offer_version_id=str(command.query.offer_version_id),
            retryable=False,
        )
    proposed = command.query.proposed_access_requirement
    review_reference = command.query.review_reference.strip()

    # The idempotency key is globally unique (one durable record per
    # command), so a key reused for a different version or a different
    # target is a typed conflict, checked BEFORE any write — never a raw
    # database constraint violation.
    existing_by_key = db.scalar(
        select(OfferAccessRequirementClassification).where(
            OfferAccessRequirementClassification.idempotency_key == key
        )
    )
    if existing_by_key is not None:
        exact_replay = (
            existing_by_key.offer_version_id == version.id
            and existing_by_key.new_access_requirement == proposed
            and existing_by_key.preview_fingerprint
            == command.expected_preview_fingerprint
            and existing_by_key.reason == reason
            and existing_by_key.classified_by == actor
            # A key reused with the SAME target but a DIFFERENT
            # review_reference is not a valid replay: the documented
            # "same material inputs" guarantee covers review_reference too.
            and existing_by_key.review_reference == review_reference
        )
        if exact_replay:
            return OfferAccessRequirementClassificationResult(
                offer_version_id=existing_by_key.offer_version_id,
                previous_access_requirement=existing_by_key.previous_access_requirement,
                new_access_requirement=existing_by_key.new_access_requirement,
                replayed=True,
            )
        raise _error(
            "idempotency_conflict",
            "This idempotency key was already used for a different "
            "classification command.",
            offer_version_id=str(version.id),
            retryable=False,
        )

    # No key match: a different key can never replay an already-classified
    # version. A genuine retry MUST reuse its original idempotency key —
    # that is what the key is for.
    existing_by_version = db.scalar(
        select(OfferAccessRequirementClassification).where(
            OfferAccessRequirementClassification.offer_version_id == version.id
        )
    )
    if existing_by_version is not None:
        raise _error(
            "already_classified",
            "Only an unclassified offer version may be reviewed-classified; "
            "real-to-real and real-to-unclassified changes are refused.",
            offer_version_id=str(version.id),
            current_access_requirement=existing_by_version.new_access_requirement.value,
            retryable=False,
        )
    if version.access_requirement is not AccessRequirement.unclassified:
        raise _error(
            "already_classified",
            "Only an unclassified offer version may be reviewed-classified; "
            "real-to-real and real-to-unclassified changes are refused.",
            offer_version_id=str(version.id),
            current_access_requirement=version.access_requirement.value,
            retryable=False,
        )

    preview = preview_classify_offer_version_access_requirement(db, command.query)
    if preview.preview_fingerprint != command.expected_preview_fingerprint:
        raise _error(
            "stale_preview",
            "The offer version changed after review; preview it again.",
            retryable=True,
        )

    # Re-verify permission again, immediately before the write, under the
    # row lock acquired above. The check at the top of this function is a
    # fast fail; only THIS second check — taken right before the mutation,
    # against the live row, inside the same transaction as the write it
    # gates — closes the window where the grant could have been revoked (or
    # the principal deactivated) between the first check and now. Ordinary
    # READ COMMITTED does not stabilize this for us just because it is the
    # same transaction: a fresh statement here reads the current committed
    # state, not a snapshot from the top of the function.
    _verify_classify_permission(db, command.authorized_system_user_id)

    version.access_requirement = proposed
    db.flush()

    classification = OfferAccessRequirementClassification(
        offer_version_id=version.id,
        previous_access_requirement=AccessRequirement.unclassified,
        new_access_requirement=proposed,
        review_reference=review_reference,
        reason=reason,
        preview_fingerprint=preview.preview_fingerprint,
        idempotency_key=key,
        classified_by=actor,
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
    )
    db.add(classification)
    try:
        db.flush()
    except IntegrityError as exc:
        # Defense in depth for a race the advisory lock above should already
        # have serialized: a raw unique-constraint violation on
        # idempotency_key or the one-row-per-version constraint still
        # surfaces as this command's own typed conflict, never a raw
        # database error.
        raise _error(
            "idempotency_conflict",
            "This idempotency key or offer version was already used for a "
            "different classification command.",
            offer_version_id=str(version.id),
            retryable=False,
        ) from exc

    evidence = {
        "schema_version": 1,
        "command_id": str(command.context.command_id),
        "correlation_id": str(command.context.correlation_id),
        "idempotency_key": key,
        "offer_version_id": str(version.id),
        "previous_access_requirement": AccessRequirement.unclassified.value,
        "new_access_requirement": proposed.value,
        "review_reference": review_reference,
        "reason": reason,
        "authenticated_principal": actor,
    }
    stage_audit_event(
        db,
        action="offer_access_requirement_classified",
        entity_type="offer_version",
        entity_id=str(version.id),
        actor=AuditActor.user(str(command.authorized_system_user_id)),
        request_id=str(command.context.correlation_id),
        metadata=evidence,
    )
    emit_event(
        db,
        EventType.catalog_offer_access_requirement_classified,
        evidence,
        actor=actor,
    )
    return OfferAccessRequirementClassificationResult(
        offer_version_id=version.id,
        previous_access_requirement=AccessRequirement.unclassified,
        new_access_requirement=proposed,
        replayed=False,
    )


__all__ = [
    "ADMISSION_SCOPE",
    "CLASSIFY_PERMISSION",
    "AdmissionAuthorizationClaims",
    "AdmissionPrincipal",
    "AdmitOfferVersionCommand",
    "AdmitOfferVersionResult",
    "ApiKeyPrincipal",
    "ClassifyOfferAccessRequirementCommand",
    "OWNER",
    "MachineCredentialPrincipal",
    "OfferAccessRequirementClassificationPreview",
    "OfferAccessRequirementClassificationResult",
    "OfferAccessRequirementError",
    "PreviewClassifyOfferAccessRequirementQuery",
    "StaffPrincipal",
    "SubscriberPrincipal",
    "SystemAdmission",
    "UnclassifiedOfferVersionRow",
    "UnclassifiedOfferVersionsWorklist",
    "UpdateOfferVersionCommand",
    "admission_actor_label",
    "admit_offer_version",
    "assert_access_requirement_immutable",
    "authorize_offer_version_admission",
    "classify_offer_version_access_requirement",
    "list_unclassified_offer_versions",
    "preview_classify_offer_version_access_requirement",
    "principal_label",
    "record_leave_denial_evidence",
    "update_offer_version",
    "validate_admission_access_requirement",
    "verify_admission_authorization",
]
