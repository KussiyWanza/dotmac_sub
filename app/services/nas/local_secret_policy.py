"""NAS-local PPPoE secret boundary — keeps the NAS a transport, not an authority.

Owner: ``network.nas_local_secret_boundary``.

``docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md`` assigns the customer IPv4 to the
active exact-service ``IPAssignment`` and states that NAS configuration carries
no independent customer IP. A RouterOS ``/ppp secret`` breaks that: RouterOS
consults RADIUS only when the username is ABSENT from ``/ppp secret``
(https://help.mikrotik.com/docs/spaces/ROS/pages/132350049/PPP+AAA), so a local
secret does not merely override an attribute — it bypasses
``access.radius_projection`` entirely and becomes a second, unreconciled
authority for the customer's address and access state.

Three defences, because an action label alone is not a boundary:

1. :func:`decide` rules on the requested action for the hard-coded activation
   command builder.
2. :func:`assert_command_text_allowed` inspects RENDERED command text, so an
   operator-editable template filed under a benign action (``reset_session``,
   ``backup_config``) cannot smuggle a local-secret mutation past the label
   check. The same guard runs when a template is SAVED, so the bad row is
   rejected at authoring time rather than only at execution time.
3. :func:`apply_cleanup` is the single sanctioned mutation, and it records
   intent, provenance and verified outcome through ``NetworkOperation`` rather
   than a log line.

Prohibited for MikroTik PPPoE: ``create``, ``suspend``, ``unsuspend``,
``change_ip`` and ``change_speed``. ``change_speed`` writes a local profile onto
the same shadowing record; migrating how speed enforcement works is a separate
slice, but continuing to mutate the shadow authority in the meantime would
contradict this boundary.

Deletion is the one corrective operation: it removes parallel state rather than
asserting new authority. It runs only through :func:`apply_cleanup`, under one
of two typed intents — see :class:`CleanupIntent`.

Out of scope, deliberately unchanged: DHCP, IPoE, static and hotspot
provisioning of any action.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import (
    ConnectionType,
    NasDevice,
    NasVendor,
    ProvisioningAction,
    Subscription,
)
from app.services.domain_errors import DomainError

logger = logging.getLogger(__name__)

#: The owner that actually holds per-customer PPPoE access state.
RADIUS_OWNER = "access.radius_projection"

#: The owner that holds session disconnect, so a retired local "suspend" does
#: not silently become a gap in enforcement.
SESSION_ENFORCEMENT_OWNER = "access.session_enforcement"

BOUNDARY_OWNER = "network.nas_local_secret_boundary"


class LocalSecretAction(StrEnum):
    """Per-subscriber operations that touch a NAS-local PPPoE secret."""

    create = "create"
    delete = "delete"
    suspend = "suspend"
    unsuspend = "unsuspend"
    change_ip = "change_ip"
    change_speed = "change_speed"


class LocalSecretDecision(StrEnum):
    """What a caller may do with the requested operation."""

    #: The NAS must not carry this state at all; emit nothing.
    prohibited = "prohibited"
    #: Corrective removal — allowed, but only via ``plan_cleanup``/``apply_cleanup``.
    cleanup_only = "cleanup_only"
    #: Not a PPPoE local-secret operation; this policy has no opinion.
    not_applicable = "not_applicable"


#: ``ProvisioningAction`` values that render into a local-secret mutation.
_ACTION_BY_PROVISIONING_ACTION: dict[ProvisioningAction, LocalSecretAction] = {
    ProvisioningAction.create_user: LocalSecretAction.create,
    ProvisioningAction.delete_user: LocalSecretAction.delete,
    ProvisioningAction.suspend_user: LocalSecretAction.suspend,
    ProvisioningAction.unsuspend_user: LocalSecretAction.unsuspend,
    ProvisioningAction.change_ip: LocalSecretAction.change_ip,
    ProvisioningAction.change_speed: LocalSecretAction.change_speed,
}


# ---------------------------------------------------------------------------
# Stable machine codes — one per distinct refusal, not one per category
# ---------------------------------------------------------------------------

CLEANUP_INVALID_REQUEST = "nas_local_secret_cleanup_invalid_request"
CLEANUP_SHARED_LOGIN = "nas_local_secret_cleanup_shared_login"
CLEANUP_RADIUS_NOT_SERVING = "nas_local_secret_cleanup_radius_not_serving"
CLEANUP_RADIUS_STILL_SERVING = "nas_local_secret_cleanup_radius_still_serving"
CLEANUP_DEPENDENT_SUBSCRIPTION = "nas_local_secret_cleanup_dependent_subscription"
CLEANUP_FINGERPRINT_MISMATCH = "nas_local_secret_cleanup_fingerprint_mismatch"
CLEANUP_UNVERIFIED = "nas_local_secret_cleanup_unverified"
COMMAND_TEXT_REJECTED = "nas_local_secret_command_text_rejected"

CLEANUP_ERROR_CODES = (
    CLEANUP_INVALID_REQUEST,
    CLEANUP_SHARED_LOGIN,
    CLEANUP_RADIUS_NOT_SERVING,
    CLEANUP_RADIUS_STILL_SERVING,
    CLEANUP_DEPENDENT_SUBSCRIPTION,
    CLEANUP_FINGERPRINT_MISMATCH,
    CLEANUP_UNVERIFIED,
)


class LocalSecretCleanupError(DomainError):
    """The cleanup operation refused or could not verify itself.

    Every raise carries a distinct ``code``: a shared login, a login RADIUS
    still serves, and an unverified removal are different operational facts and
    an operator must be able to tell them apart without parsing prose.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(code=code, message=message, details=details)


class LocalSecretCommandRejected(DomainError):
    """Rendered or stored command text would mutate a NAS-local secret."""

    def __init__(self, message: str, *, matches: Sequence[str]) -> None:
        super().__init__(
            code=COMMAND_TEXT_REJECTED,
            message=message,
            details={"matches": list(matches)},
        )


# ---------------------------------------------------------------------------
# Defence 1 — action rulings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalSecretRuling:
    """Typed, provenanced outcome of a boundary check.

    Emitted even when nothing happens: an activation that legitimately performs
    no NAS work must record *why*, so "no command was sent" is distinguishable
    from "the push silently failed".
    """

    action: LocalSecretAction | None
    connection_type: ConnectionType
    vendor: NasVendor
    decision: LocalSecretDecision
    owner: str
    reason: str

    @property
    def emits_commands(self) -> bool:
        return self.decision is LocalSecretDecision.not_applicable

    def as_log_extra(self) -> dict[str, str]:
        return {
            "nas_local_secret_action": self.action.value if self.action else "none",
            "nas_local_secret_decision": self.decision.value,
            "connection_type": self.connection_type.value,
            "vendor": self.vendor.value,
            "owner": self.owner,
            "reason": self.reason,
        }


def decide(
    *,
    vendor: NasVendor,
    connection_type: ConnectionType,
    action: LocalSecretAction | None,
) -> LocalSecretRuling:
    """Rule on one per-subscriber NAS operation.

    ``action is None`` means the caller's action does not map to a local-secret
    mutation at all, which is always ``not_applicable``. A ``not_applicable``
    ruling is NOT a clearance for the command text — see
    :func:`assert_command_text_allowed`.
    """
    if (
        action is None
        or vendor is not NasVendor.mikrotik
        or connection_type is not ConnectionType.pppoe
    ):
        return LocalSecretRuling(
            action=action,
            connection_type=connection_type,
            vendor=vendor,
            decision=LocalSecretDecision.not_applicable,
            owner="",
            reason="not a MikroTik PPPoE local-secret operation",
        )

    if action is LocalSecretAction.delete:
        return LocalSecretRuling(
            action=action,
            connection_type=connection_type,
            vendor=vendor,
            decision=LocalSecretDecision.cleanup_only,
            owner=BOUNDARY_OWNER,
            reason=(
                "removing a local secret is corrective and runs only through the "
                "typed cleanup operation, which checks the login's dependants "
                "and verifies the device"
            ),
        )

    owner = (
        SESSION_ENFORCEMENT_OWNER
        if action in {LocalSecretAction.suspend, LocalSecretAction.unsuspend}
        else RADIUS_OWNER
    )
    return LocalSecretRuling(
        action=action,
        connection_type=connection_type,
        vendor=vendor,
        decision=LocalSecretDecision.prohibited,
        owner=owner,
        reason=(
            "NAS action not applicable — RADIUS owned. A local PPPoE secret "
            "bypasses the RADIUS projection instead of overriding it, so the NAS "
            f"must carry no per-customer record; {owner} owns this state"
        ),
    )


def decide_for_provisioning_action(
    *,
    vendor: NasVendor,
    connection_type: ConnectionType,
    action: ProvisioningAction,
) -> LocalSecretRuling:
    """Rule on a template-driven ``ProvisioningAction``."""
    return decide(
        vendor=vendor,
        connection_type=connection_type,
        action=_ACTION_BY_PROVISIONING_ACTION.get(action),
    )


# ---------------------------------------------------------------------------
# Defence 2 — command-text guard
# ---------------------------------------------------------------------------

#: Any mutation of the local secret store, plus the customer-address attribute
#: on its own. Removal is included: a template must not remove secrets either,
#: because removal has to carry intent, provenance and verification.
_PROHIBITED_COMMAND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ppp-secret-add", re.compile(r"/ppp\s+secret\s+add", re.IGNORECASE)),
    ("ppp-secret-set", re.compile(r"/ppp\s+secret\s+set", re.IGNORECASE)),
    ("ppp-secret-remove", re.compile(r"/ppp\s+secret\s+remove", re.IGNORECASE)),
    (
        "ppp-secret-path",
        re.compile(r"/ppp/secret/(add|set|remove)", re.IGNORECASE),
    ),
    ("remote-address", re.compile(r"remote-address\s*=", re.IGNORECASE)),
)


def scan_command_text(text: str | None) -> tuple[str, ...]:
    """Return the prohibited local-secret constructs present in ``text``."""
    if not text:
        return ()
    return tuple(
        label for label, pattern in _PROHIBITED_COMMAND_PATTERNS if pattern.search(text)
    )


def assert_command_text_allowed(text: str | None, *, context: str) -> None:
    """Reject command text that would mutate a NAS-local secret.

    Applied to RENDERED template output at execution time and to stored
    template bodies at save time. The action label is not trusted: a template
    filed under ``reset_session`` is still refused if its body touches the
    local secret store.
    """
    matches = scan_command_text(text)
    if not matches:
        return
    raise LocalSecretCommandRejected(
        f"{context} contains prohibited NAS-local secret command(s): "
        f"{', '.join(matches)}. The NAS carries no per-customer PPPoE record; "
        f"{RADIUS_OWNER} owns access and {BOUNDARY_OWNER} owns corrective removal.",
        matches=matches,
    )


# ---------------------------------------------------------------------------
# Defence 3 — the one sanctioned mutation
# ---------------------------------------------------------------------------


class CleanupIntent(StrEnum):
    """Why a local secret is being removed. The preconditions differ.

    The two intents are not interchangeable: each asserts the OPPOSITE thing
    about RADIUS, so running one on the other's evidence would be wrong in
    exactly the dangerous direction.
    """

    #: The service continues; RADIUS takes over. Requires that RADIUS
    #: verifiably serves exactly ONE unambiguous login.
    migrate_to_radius = "migrate_to_radius"

    #: The service is terminal; RADIUS absence is expected and correct.
    #: Requires that NO nonterminal subscription still depends on the login.
    terminal_retirement = "terminal_retirement"


class ProvenanceKind(StrEnum):
    operator = "operator"
    event = "event"


@dataclass(frozen=True, slots=True)
class CleanupProvenance:
    """Who or what authorised this removal, and on what evidence.

    A staged terminal retirement is authorised by a durable event, not by a
    person. Synthesising a human reviewer for it would make the field a lie, so
    event provenance is a first-class kind with its own required reference.
    """

    kind: ProvenanceKind
    actor: str
    reference: str

    def validate(self) -> None:
        if not self.actor.strip():
            raise LocalSecretCleanupError(
                "Local-secret cleanup requires an actor.",
                code=CLEANUP_INVALID_REQUEST,
            )
        if not self.reference.strip():
            label = (
                "a recorded reason"
                if self.kind is ProvenanceKind.operator
                else "an originating event reference"
            )
            raise LocalSecretCleanupError(
                f"Local-secret cleanup requires {label}.",
                code=CLEANUP_INVALID_REQUEST,
            )

    def as_payload(self) -> dict[str, str]:
        return {
            "provenance_kind": self.kind.value,
            "actor": self.actor,
            "reference": self.reference,
        }


@dataclass(frozen=True, slots=True)
class LocalSecretCleanupRequest:
    """Typed input for removing one shadowing local secret."""

    nas_device_id: UUID
    login: str
    intent: CleanupIntent
    provenance: CleanupProvenance


@dataclass(frozen=True, slots=True)
class LocalSecretCleanupPlan:
    """What cleanup would do, and whether it is allowed to proceed.

    Carries no raw device output. The readback is a COUNT: the RouterOS detail
    print for a local secret echoes the stored PPP password, so the probe must
    never use it nor retain a response body.
    """

    login: str
    nas_device_id: UUID
    nas_device_name: str
    intent: CleanupIntent
    present_count: int
    nonterminal_subscription_ids: tuple[str, ...]
    terminal_subscription_ids: tuple[str, ...]
    radius_projected: bool
    blocked_code: str = ""
    blocked_reason: str = ""

    @property
    def present_on_device(self) -> bool:
        return self.present_count > 0

    @property
    def allowed(self) -> bool:
        return not self.blocked_code and self.present_on_device

    @property
    def fingerprint(self) -> str:
        """Deterministic digest of the decision inputs.

        ``--apply`` must echo this back. If the device or the subscription
        cohort changed between preview and apply, the digest changes and the
        apply is refused rather than acting on a stale plan.
        """
        material = "|".join(
            (
                str(self.nas_device_id),
                self.login,
                self.intent.value,
                str(self.present_count),
                ",".join(self.nonterminal_subscription_ids),
                ",".join(self.terminal_subscription_ids),
                "1" if self.radius_projected else "0",
                self.blocked_code,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def as_payload(self) -> dict[str, object]:
        return {
            "login": self.login,
            "nas_device_id": str(self.nas_device_id),
            "nas_device_name": self.nas_device_name,
            "intent": self.intent.value,
            "present_count": self.present_count,
            "nonterminal_subscription_ids": list(self.nonterminal_subscription_ids),
            "terminal_subscription_ids": list(self.terminal_subscription_ids),
            "radius_projected": self.radius_projected,
            "blocked_code": self.blocked_code,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class LocalSecretCleanupOutcome:
    """Result of an applied cleanup, including the post-removal count."""

    plan: LocalSecretCleanupPlan
    removed: bool
    verified_absent: bool
    remaining_count: int
    operation_id: str | None = None
    provenance: CleanupProvenance | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "removed": self.removed,
            "verified_absent": self.verified_absent,
            "remaining_count": self.remaining_count,
            "operation_id": self.operation_id,
        }
        payload.update(self.plan.as_payload())
        if self.provenance is not None:
            payload.update(self.provenance.as_payload())
        return payload


CommandRunner = Callable[[str], str]

#: Existence probe only. ``:len`` over ``find`` returns an integer; unlike
#: a detail print it cannot echo the stored PPP password.
_COUNT_TEMPLATE = ':put [:len [/ppp secret find name="{login}"]]'
_REMOVE_TEMPLATE = '/ppp secret remove [find name="{login}"]'


def _sanitised_login(login: str) -> str:
    from app.services.enforcement import _sanitize_routeros_value

    value = _sanitize_routeros_value((login or "").strip())
    if not value:
        raise LocalSecretCleanupError(
            "A login is required for local-secret cleanup.",
            code=CLEANUP_INVALID_REQUEST,
        )
    return value


def _parse_count(response: str | None) -> int:
    """Read the integer from a count probe, refusing to guess.

    An unreadable probe is treated as unknown, never as zero: a silent zero
    would report a still-present secret as already cleaned.
    """
    text = (response or "").strip()
    match = re.search(r"\d+", text)
    if not match:
        raise LocalSecretCleanupError(
            "The NAS did not return a readable local-secret count; refusing to "
            "assume the secret is absent.",
            code=CLEANUP_UNVERIFIED,
        )
    return int(match.group())


def _subscriptions_for_login(
    db: Session, login: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(nonterminal_ids, terminal_ids)`` for every subscription on this login."""
    from app.services.radius_access_state import (
        ACTIVE_STATUSES,
        BLOCKED_STATUSES,
        TERMINATED_STATUSES,
    )

    nonterminal: list[str] = []
    terminal: list[str] = []
    projected = ACTIVE_STATUSES | BLOCKED_STATUSES
    for sub_id, status in db.execute(
        select(Subscription.id, Subscription.status).where(Subscription.login == login)
    ).all():
        if status in projected:
            nonterminal.append(str(sub_id))
        elif status in TERMINATED_STATUSES:
            terminal.append(str(sub_id))
    return tuple(sorted(nonterminal)), tuple(sorted(terminal))


def _radius_projects_login(db: Session, login: str) -> bool:
    """Whether the RADIUS owner intends to serve this login."""
    from app.services.radius_access_state import ACTIVE_STATUSES, BLOCKED_STATUSES
    from app.services.radius_projection_planner import plan_login_radius_projections

    subscriptions = (
        db.execute(
            select(Subscription).where(
                Subscription.status.in_(ACTIVE_STATUSES | BLOCKED_STATUSES),
                Subscription.login == login,
            )
        )
        .unique()
        .scalars()
        .all()
    )
    if not subscriptions:
        return False
    return login in plan_login_radius_projections(db, subscriptions)


def _block(
    intent: CleanupIntent,
    nonterminal: tuple[str, ...],
    radius_projected: bool,
) -> tuple[str, str]:
    """Intent-specific preconditions. Returns ``(code, reason)`` or ``("", "")``."""
    if len(nonterminal) > 1:
        return (
            CLEANUP_SHARED_LOGIN,
            f"login is shared by {len(nonterminal)} nonterminal subscriptions "
            f"({', '.join(nonterminal)}); adjudicate service ownership first",
        )

    if intent is CleanupIntent.migrate_to_radius:
        if not nonterminal:
            return (
                CLEANUP_RADIUS_NOT_SERVING,
                "no nonterminal subscription carries this login, so there is no "
                "service to migrate; use terminal_retirement if the service ended",
            )
        if not radius_projected:
            return (
                CLEANUP_RADIUS_NOT_SERVING,
                "RADIUS has no active projection for this login, so removing the "
                "local secret would remove the customer's only means of "
                "authenticating",
            )
        return ("", "")

    # terminal_retirement
    if nonterminal:
        return (
            CLEANUP_DEPENDENT_SUBSCRIPTION,
            f"nonterminal subscription {nonterminal[0]} still depends on this "
            "login; retiring the secret would affect a live service",
        )
    if radius_projected:
        return (
            CLEANUP_RADIUS_STILL_SERVING,
            "RADIUS still projects this login, so the terminal projection has not "
            "converged; retire the secret only once RADIUS has stopped serving it",
        )
    return ("", "")


def plan_cleanup(
    db: Session,
    request: LocalSecretCleanupRequest,
    *,
    run_command: CommandRunner | None = None,
) -> LocalSecretCleanupPlan:
    """Read-only preview: is removing this local secret provably corrective?"""
    login = _sanitised_login(request.login)
    device = db.get(NasDevice, request.nas_device_id)
    if device is None:
        raise LocalSecretCleanupError(
            f"NAS device {request.nas_device_id} was not found.",
            code=CLEANUP_INVALID_REQUEST,
        )

    runner = run_command or _ssh_runner(device)
    present_count = _parse_count(runner(_COUNT_TEMPLATE.format(login=login)))

    nonterminal, terminal = _subscriptions_for_login(db, login)
    radius_projected = _radius_projects_login(db, login)
    code, reason = _block(request.intent, nonterminal, radius_projected)

    return LocalSecretCleanupPlan(
        login=login,
        nas_device_id=request.nas_device_id,
        nas_device_name=device.name,
        intent=request.intent,
        present_count=present_count,
        nonterminal_subscription_ids=nonterminal,
        terminal_subscription_ids=terminal,
        radius_projected=radius_projected,
        blocked_code=code,
        blocked_reason=reason,
    )


def apply_cleanup(
    db: Session,
    request: LocalSecretCleanupRequest,
    *,
    run_command: CommandRunner | None = None,
    expected_fingerprint: str | None = None,
) -> LocalSecretCleanupOutcome:
    """Remove one shadowing local secret, verified and durably recorded.

    Every attempt that reaches the device writes a ``NetworkOperation``: intent
    and provenance as input, verified counts as output, and a durable failure
    row when the device errors or the removal cannot be proven. A failure is
    retryable through the operation ledger and never disappears into a log line.
    """
    from app.models.network_operation import (
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network_operations import network_operations

    request.provenance.validate()

    device = db.get(NasDevice, request.nas_device_id)
    if device is None:
        raise LocalSecretCleanupError(
            f"NAS device {request.nas_device_id} was not found.",
            code=CLEANUP_INVALID_REQUEST,
        )
    runner = run_command or _ssh_runner(device)
    plan = plan_cleanup(db, request, run_command=runner)

    if expected_fingerprint is not None and expected_fingerprint != plan.fingerprint:
        raise LocalSecretCleanupError(
            f"Plan fingerprint changed for {plan.login} "
            f"(expected {expected_fingerprint}, now {plan.fingerprint}); "
            "re-preview before applying.",
            code=CLEANUP_FINGERPRINT_MISMATCH,
            details={"login": plan.login},
        )
    if plan.blocked_code:
        raise LocalSecretCleanupError(
            f"Refusing local-secret cleanup for {plan.login}: {plan.blocked_reason}",
            code=plan.blocked_code,
            details={"login": plan.login, "nas_device": plan.nas_device_name},
        )
    if not plan.present_on_device:
        # Nothing to retire, proven by count. Deliberately opens no operation:
        # a no-op is not a device action.
        return LocalSecretCleanupOutcome(
            plan=plan,
            removed=False,
            verified_absent=True,
            remaining_count=0,
            provenance=request.provenance,
        )

    operation = network_operations.start(
        db,
        NetworkOperationType.nas_local_secret_retire,
        NetworkOperationTargetType.nas,
        str(request.nas_device_id),
        # One active retirement per (NAS, login): a duplicate event delivery or
        # a re-run while the first is in flight is rejected, not repeated.
        correlation_key=f"nas_local_secret_retire:{request.nas_device_id}:{plan.login}",
        input_payload={**plan.as_payload(), **request.provenance.as_payload()},
        initiated_by=request.provenance.actor,
    )
    operation_id = str(operation.id)
    network_operations.mark_running(db, operation_id)
    db.flush()

    try:
        runner(_REMOVE_TEMPLATE.format(login=plan.login))
        remaining = _parse_count(runner(_COUNT_TEMPLATE.format(login=plan.login)))
    except Exception as exc:
        network_operations.mark_failed(db, operation_id, str(exc))
        raise

    verified_absent = remaining == 0
    outcome = LocalSecretCleanupOutcome(
        plan=plan,
        removed=True,
        verified_absent=verified_absent,
        remaining_count=remaining,
        operation_id=operation_id,
        provenance=request.provenance,
    )
    if not verified_absent:
        network_operations.mark_failed(
            db,
            operation_id,
            f"Local secret for {plan.login} still present on "
            f"{plan.nas_device_name} after removal (count={remaining}).",
        )
        raise LocalSecretCleanupError(
            f"Removed the local secret for {plan.login} on {plan.nas_device_name} "
            "but the device still reports it; the parallel authority is still live.",
            code=CLEANUP_UNVERIFIED,
            details={"login": plan.login, "nas_device": plan.nas_device_name},
        )

    network_operations.mark_succeeded(
        db, operation_id, output_payload=outcome.as_payload()
    )
    logger.info("nas_local_secret_cleanup_applied", extra=outcome.as_payload())
    return outcome


def stage_terminal_retirement(
    db: Session,
    *,
    subscription_id: str,
    event_reference: str,
    run_command: CommandRunner | None = None,
) -> LocalSecretCleanupOutcome | None:
    """Retire a canceled service's shadowing secret, if it has one.

    Called from the durable ``subscription.canceled`` handler AFTER the terminal
    RADIUS projection has succeeded. Never raises into the caller: the
    cancellation is authoritative, so a device failure must stay retryable and
    visible through the operation ledger rather than roll back a lifecycle
    transition that has already been decided.
    """
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        return None
    login = (subscription.login or "").strip()
    nas_device_id = subscription.provisioning_nas_device_id
    if not login or nas_device_id is None:
        return None

    request = LocalSecretCleanupRequest(
        nas_device_id=nas_device_id,
        login=login,
        intent=CleanupIntent.terminal_retirement,
        provenance=CleanupProvenance(
            kind=ProvenanceKind.event,
            actor="events.enforcement.subscription_canceled",
            reference=event_reference,
        ),
    )
    try:
        return apply_cleanup(db, request, run_command=run_command)
    except LocalSecretCleanupError as exc:
        # A refusal is a real answer (shared login, RADIUS not yet converged),
        # not a crash. It is recorded and left for the operator sweep.
        logger.warning(
            "nas_local_secret_terminal_retirement_refused",
            extra={
                "subscription_id": str(subscription_id),
                "login": login,
                "refusal_code": exc.code,
                "refusal_reason": exc.message,
            },
        )
        return None
    except Exception as exc:  # noqa: BLE001 - cancellation must not roll back
        logger.error(
            "nas_local_secret_terminal_retirement_failed",
            extra={
                "subscription_id": str(subscription_id),
                "login": login,
                "failure": str(exc),
            },
        )
        return None


def _ssh_runner(device: NasDevice) -> CommandRunner:
    """Default runner: one command per SSH round trip."""

    def _run(command: str) -> str:
        from app.services.nas.provisioner import DeviceProvisioner

        return DeviceProvisioner._execute_ssh(device, command)

    return _run


def cleanup_candidates(db: Session, nas_device_id: UUID) -> Sequence[str]:
    """Logins provisioned against this NAS, as operator cohort input.

    Deliberately returns candidates only — it never plans or applies. The
    operator selects an explicit bounded cohort; each member is planned,
    fingerprinted and verified individually.
    """
    rows = db.scalars(
        select(Subscription.login)
        .where(Subscription.provisioning_nas_device_id == nas_device_id)
        .where(Subscription.login.is_not(None))
    ).all()
    return tuple(sorted({(row or "").strip() for row in rows if (row or "").strip()}))
