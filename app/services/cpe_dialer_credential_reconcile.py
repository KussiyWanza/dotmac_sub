"""Project the authoritative access credential onto the CPE PPPoE dialer.

Ownership
=========

``AccessCredential`` (and its ``RadiusUser`` twin) is the **authoritative**
subscriber access credential. ``access.radius_projection``
(``app.services.radius_population``) remains its only projection writer for the
FreeRADIUS auth tables. Nothing in this module writes RADIUS.

What the CPE dials with is a **derived projection** of that credential:
``OntUnit.desired_config['wan']['pppoe_username'] / ['pppoe_password']``. This
module is that projection's single canonical writer. Delivery to the physical
CPE stays with the ONT reconciler (``app.services.network.reconcile``), which
already diffs the desired PPPoE username against the ACS-observed one and
pushes over TR-069. This module decides *what* the dialer should hold; the ONT
reconciler decides *how* it gets there.

Why this exists
===============

The operator-facing "set PPPoE credentials" action wrote operator-typed values
straight into ONT desired state and never consulted RADIUS, so a CPE could sit
forever dialing a credential that authentication would never accept. A
*detector* for exactly this already existed —
``pppoe_health.CATEGORY_CREDENTIAL_MISMATCH`` — but it was wired only as a fleet
list filter: never scheduled, never alerted, and it never repaired anything.
This module is the missing reconciler behind that detector.

Secret handling — hard requirement
==================================

The credential value is never logged, never returned, never persisted in a
drift record and never placed in an event payload. Comparison is by
**fingerprint only**: a keyed HMAC-SHA256 over the normalized
``(username, secret)`` pair, using the same credential-encryption key
``app.services.radius_population`` uses for its RADIUS-row fingerprints. A
fingerprint is safe to log, store and diff; the value behind it is not.

Readback
========

Only the PPPoE *username* is readable back from a CPE — TR-069 exposes
``WANPPPConnection.Username`` but never the password. So convergence is proven
in two halves:

* **projection readback** — the fingerprint recorded on the ONT after a
  projection must still equal the authoritative credential's fingerprint;
* **device readback** — the last ACS-observed username (``OntObservation``)
  must equal the authoritative username.

A device-readback mismatch means the value is correct in desired state but the
CPE has not taken it yet, so the ONT is re-flagged for delivery rather than
re-projected.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import and_, select

from app.models.catalog import AccessCredential
from app.models.network import OntAssignment, OntUnit
from app.models.ont_observation import OntObservation
from app.services.credential_crypto import (
    decrypt_credential,
    encrypt_credential,
    get_encryption_key,
)
from app.services.network.ont_desired_config import (
    desired_config,
    desired_config_column,
    get_desired_config_value,
    set_desired_config_values,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ALERT_KIND = "ont.dialer_credential_drift"

# Repair outcomes. ``reason`` values are stable strings — they land in logs and
# task payloads, so they are part of this module's contract.
REASON_MISSING = "dialer_credential_missing"
REASON_FINGERPRINT_MISMATCH = "dialer_credential_fingerprint_mismatch"
REASON_DEVICE_READBACK_MISMATCH = "dialer_credential_device_readback_mismatch"

_DESIRED_USERNAME_PATH = ("wan", "pppoe_username")
_DESIRED_PASSWORD_PATH = ("wan", "pppoe_password")
_FINGERPRINT_KEY = "delivery.dialer_credential_fingerprint"
_FINGERPRINT_AT_KEY = "delivery.dialer_credential_projected_at"


class DialerFingerprintUnavailable(RuntimeError):
    """Raised when no credential-encryption key is configured.

    Without a key there is no safe way to fingerprint a credential — a bare
    digest of a subscriber password is brute-forceable — so this module refuses
    to run rather than degrade to an unkeyed hash.
    """


@dataclass(frozen=True)
class DialerCredentialDrift:
    """One ONT whose dialer projection disagrees with the authoritative record.

    Deliberately carries no username and no secret: only identifiers, a reason,
    and fingerprints. This object is logged and serialized into task payloads.
    """

    ont_unit_id: str
    serial_number: str
    reason: str
    desired_fingerprint: str
    observed_fingerprint: str | None
    repaired: bool


@dataclass(frozen=True)
class DialerCredentialReconcileStats:
    """Roll-up of one reconcile pass."""

    checked: int = 0
    in_sync: int = 0
    projected: int = 0
    awaiting_device: int = 0
    skipped_no_credential: int = 0
    skipped_ambiguous_credential: int = 0
    skipped_no_secret: int = 0
    drifts: tuple[DialerCredentialDrift, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        """Secret-free summary suitable for a Celery result / operation log."""
        return {
            "checked": self.checked,
            "in_sync": self.in_sync,
            "projected": self.projected,
            "awaiting_device": self.awaiting_device,
            "skipped_no_credential": self.skipped_no_credential,
            "skipped_ambiguous_credential": self.skipped_ambiguous_credential,
            "skipped_no_secret": self.skipped_no_secret,
            "drifts": [
                {
                    "ont_unit_id": drift.ont_unit_id,
                    "serial_number": drift.serial_number,
                    "reason": drift.reason,
                    "repaired": drift.repaired,
                    # Short prefixes only: enough to correlate two runs, not
                    # enough to be a distinguishing oracle on its own.
                    "desired_fingerprint": drift.desired_fingerprint[:12],
                    "observed_fingerprint": (
                        drift.observed_fingerprint[:12]
                        if drift.observed_fingerprint
                        else None
                    ),
                }
                for drift in self.drifts
            ],
        }


def dialer_fingerprint(username: str | None, secret: str | None) -> str | None:
    """Keyed digest of one ``(username, secret)`` dialer pair.

    Returns ``None`` when either half is absent — an incomplete pair has no
    meaningful fingerprint and must be treated as "not projected", not as a
    value that happens to differ.

    The digest is keyed with the credential-encryption key so it cannot be
    dictionary-attacked by anyone holding only the logs.
    """
    if not username or not secret:
        return None
    key = get_encryption_key()
    if key is None:
        raise DialerFingerprintUnavailable(
            "Credential encryption key is required to fingerprint dialer "
            "credentials; refusing to compare with an unkeyed digest."
        )
    payload = json.dumps(
        {"username": str(username), "secret": str(secret)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _safe_decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return decrypt_credential(value)
    except ValueError:
        # An undecryptable stored value is indistinguishable from "no value we
        # can compare". Never log the ciphertext.
        return None


def _candidate_rows(db: Session, ont_ids: Sequence[str] | None) -> list[Any]:
    """Active, subscriber-assigned ONTs plus their last ACS observation.

    The desired-config blob is selected through ``desired_config_column`` and
    read via the row mapping, so this module never touches the ORM attribute
    directly — ``network.ont_desired_config`` owns that access boundary.
    """
    stmt = (
        select(
            OntUnit.id,
            OntUnit.serial_number,
            desired_config_column(OntUnit).label("desired_config"),
            OntAssignment.subscriber_id,
            OntAssignment.subscription_id,
            OntAssignment.wan_mode,
            OntAssignment.ip_mode,
            OntAssignment.pppoe_username.label("assignment_pppoe_username"),
            OntObservation.acs_observed_pppoe_username,
        )
        .join(
            OntAssignment,
            and_(
                OntAssignment.ont_unit_id == OntUnit.id,
                OntAssignment.active.is_(True),
            ),
        )
        .outerjoin(OntObservation, OntObservation.ont_unit_id == OntUnit.id)
        .where(OntUnit.is_active.is_(True))
        .where(OntAssignment.subscriber_id.isnot(None))
        # Exact-service grain. A subscriber-only assignment cannot say WHICH
        # service a credential belongs to, and projecting on that basis is how
        # one subscriber's credential reached another service's ONT.
        .where(OntAssignment.subscription_id.isnot(None))
    )
    if ont_ids:
        stmt = stmt.where(OntUnit.id.in_(list(ont_ids)))
    return list(db.execute(stmt).all())


def termination_intent(db: Session, ont_id: Any) -> tuple[bool, str]:
    """Whether this ONT is the authorised PPPoE termination for its service.

    Delegates to ``network.ppp_delivery_authorization``, which reads the
    ``OntWanServiceInstance`` service-intent model. This deliberately does NOT
    read ``OntAssignment.wan_mode``, ``ip_mode`` or ``pppoe_username``:
    migration 084 copied those into desired config and then explicitly set them
    ``NULL``, so surviving values are unexplained residue and cannot authorise
    a device write. An earlier version of this gate trusted exactly those 12
    survivors.

    One owner answers the question for both halves of the containment, so the
    producer cannot stage what delivery would refuse to send.
    """
    from app.services.network.ppp_delivery_authorization import (
        authorize_ppp_termination_intent,
    )

    # Intent only: this gate runs BEFORE anything is staged, so it must not
    # require a staged projection to already exist. Delivery adds the
    # credential and projection checks.
    ruling = authorize_ppp_termination_intent(db, ont_id)
    return ruling.authorized, (
        ruling.refusal.value if ruling.refusal else "managed_ont_pppoe"
    )


def _authoritative_credentials(
    db: Session, subscription_ids: list[Any], *, for_update: bool = False
) -> tuple[dict[Any, AccessCredential], frozenset[Any]]:
    """Exactly one active credential per EXACT subscription, or none.

    Previously this keyed by subscriber and kept the oldest row by
    ``created_at``. Both halves were wrong. Subscriber grain cannot say which
    of a customer's services a credential belongs to, and picking by creation
    order is an ownership decision made by accident -- a customer with two
    services would have had one credential projected onto both ONTs.

    Ambiguity is refused rather than resolved: more than one active credential
    for a service means nobody has said which is authoritative, and a
    projection is a device write.
    """
    if not subscription_ids:
        return {}, frozenset()
    if for_update:
        # Phantom-read guard. Filtering `is_active` BEFORE `FOR UPDATE` locks
        # only the rows that are already active, so it cannot block an inactive
        # credential being activated concurrently, nor a second active
        # credential being inserted. There is no one-active-per-subscription
        # constraint in the schema -- only username uniqueness -- so the
        # "exactly one" ruling would not be transaction-stable.
        #
        # Instead: lock the owning Subscription rows (the parent lock blocks
        # new FK inserts and rebinding), then lock EVERY credential row for
        # them with no active filter (blocking activation and secret changes),
        # and decide the active set in memory.
        from app.models.catalog import Subscription

        db.scalars(
            select(Subscription)
            .where(Subscription.id.in_(subscription_ids))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        locked = db.scalars(
            select(AccessCredential)
            .where(AccessCredential.subscription_id.in_(subscription_ids))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        rows = [row for row in locked if row.is_active]
    else:
        rows = list(
            db.scalars(
                select(AccessCredential)
                .where(AccessCredential.subscription_id.in_(subscription_ids))
                .where(AccessCredential.is_active.is_(True))
            ).all()
        )

    by_subscription: dict[Any, list[AccessCredential]] = {}
    for row in rows:
        by_subscription.setdefault(row.subscription_id, []).append(row)
    return (
        {
            subscription_id: found[0]
            for subscription_id, found in by_subscription.items()
            if len(found) == 1
        },
        frozenset(
            subscription_id
            for subscription_id, found in by_subscription.items()
            if len(found) > 1
        ),
    )


class DialerCredentialRefusal(StrEnum):
    """Why a service has no usable dialer credential. One code per cause."""

    missing = "dialer_credential_missing"
    ambiguous = "dialer_credential_ambiguous"
    unreadable = "dialer_credential_unreadable"
    key_unavailable = "dialer_credential_key_unavailable"


@dataclass(frozen=True, slots=True)
class DialerCredentialAuthority:
    """The keyed fingerprint of the one credential a service may dial with.

    Carries no username and no secret: this object is compared, logged and
    embedded in delivery rulings.
    """

    subscription_id: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class DialerCredentialAuthorityResult:
    """Typed outcome of resolving a service's dialer credential authority."""

    authority: DialerCredentialAuthority | None = None
    refusal: DialerCredentialRefusal | None = None

    @property
    def ok(self) -> bool:
        return self.authority is not None and self.refusal is None


@dataclass(frozen=True, slots=True)
class ProjectCpeDialerCredential:
    """Exact-service participant input for a configuration coordinator."""

    ont_unit_id: UUID
    subscription_id: UUID


@dataclass(frozen=True, slots=True)
class ProjectCpeDialerCredentialOutcome:
    ont_unit_id: UUID
    subscription_id: UUID
    credential_fingerprint: str
    masked_username: str


def authoritative_dialer_fingerprint(
    db: Session,
    *,
    subscription_id: UUID,
    for_update: bool = False,
) -> DialerCredentialAuthorityResult:
    """The authoritative dialer fingerprint for one exact service.

    Public, typed, and owned here because this module owns the credential
    projection: delivery authorization must not re-derive credential ownership
    from a staged payload, which would let an already-projected foreign
    credential authorise itself.

    ``for_update`` is for DELIVERY, which binds this fingerprint into a ruling
    and must not race a credential rotation. It also forces a refresh: two ORM
    reads in one session would otherwise both be served from the identity map
    and reproduce a stale secret. The producer passes ``False`` -- see the lock
    policy in this module's docstring.

    Every failure is a refusal, never a fallback: a credential that cannot be
    read or keyed is indistinguishable from one that does not authorise a
    delivery.
    """
    if subscription_id is None:
        return DialerCredentialAuthorityResult(refusal=DialerCredentialRefusal.missing)
    credentials, ambiguous = _authoritative_credentials(
        db, [subscription_id], for_update=for_update
    )
    if subscription_id in ambiguous:
        return DialerCredentialAuthorityResult(
            refusal=DialerCredentialRefusal.ambiguous
        )
    credential = credentials.get(subscription_id)
    if credential is None:
        return DialerCredentialAuthorityResult(refusal=DialerCredentialRefusal.missing)

    username = (credential.username or "").strip() or None
    secret = _safe_decrypt(credential.secret_hash)
    if secret is None:
        # Undecryptable or absent. Repairing it belongs to the credential
        # owner; here it simply cannot authorise a device write.
        return DialerCredentialAuthorityResult(
            refusal=DialerCredentialRefusal.unreadable
        )
    try:
        fingerprint = dialer_fingerprint(username, secret)
    except DialerFingerprintUnavailable:
        # No encryption key: refuse rather than degrade to an unkeyed digest.
        return DialerCredentialAuthorityResult(
            refusal=DialerCredentialRefusal.key_unavailable
        )
    if fingerprint is None:
        return DialerCredentialAuthorityResult(
            refusal=DialerCredentialRefusal.unreadable
        )
    return DialerCredentialAuthorityResult(
        authority=DialerCredentialAuthority(
            subscription_id=str(subscription_id), fingerprint=fingerprint
        )
    )


def projected_dialer_fingerprint(ont: Any) -> str | None:
    """The fingerprint recorded on the ONT's staged projection, if any."""
    config = desired_config(ont) if ont is not None else {}
    value = get_desired_config_value(
        config, "delivery", "dialer_credential_fingerprint"
    )
    text = str(value or "").strip()
    return text or None


def _masked_username(value: str) -> str:
    """Return useful provenance without exposing a full subscriber identity."""

    local, separator, realm = value.partition("@")
    if not local:
        return "***"
    visible = local[:2]
    masked_local = f"{visible}{'*' * max(3, len(local) - len(visible))}"
    return f"{masked_local}{separator}{realm}" if separator else masked_local


def project_cpe_dialer_credential_for_configuration(
    db: Session,
    command: ProjectCpeDialerCredential,
) -> ProjectCpeDialerCredentialOutcome:
    """Flush-only projection from the exact authoritative access credential."""

    allowed, reason = termination_intent(db, command.ont_unit_id)
    if not allowed:
        raise ValueError(f"PPPoE termination intent is not active: {reason}")
    credentials, ambiguous = _authoritative_credentials(
        db, [command.subscription_id], for_update=True
    )
    if command.subscription_id in ambiguous:
        raise ValueError("The subscription has ambiguous active access credentials")
    credential = credentials.get(command.subscription_id)
    if credential is None:
        raise ValueError("The subscription has no active access credential")
    username = (credential.username or "").strip()
    secret = _safe_decrypt(credential.secret_hash)
    fingerprint = dialer_fingerprint(username, secret)
    if not username or secret is None or fingerprint is None:
        raise ValueError("The authoritative access credential is not deliverable")
    _project(
        db,
        ont_id=command.ont_unit_id,
        username=username,
        secret=secret,
        fingerprint=fingerprint,
    )
    return ProjectCpeDialerCredentialOutcome(
        ont_unit_id=command.ont_unit_id,
        subscription_id=command.subscription_id,
        credential_fingerprint=fingerprint,
        masked_username=_masked_username(username),
    )


def clear_cpe_dialer_projection_for_non_ppp(db: Session, *, ont_unit_id: UUID) -> None:
    """Flush-only retirement of the dialer projection when WAN is not PPPoE."""

    ont = db.get(OntUnit, ont_unit_id)
    if ont is None:
        raise ValueError("ONT not found for dialer projection retirement")
    set_desired_config_values(
        ont,
        {
            "wan.pppoe_username": None,
            "wan.pppoe_password": None,
            _FINGERPRINT_KEY: None,
            _FINGERPRINT_AT_KEY: None,
        },
    )
    db.add(ont)
    db.flush()


def reconcile_cpe_dialer_credentials(
    db: Session,
    *,
    ont_ids: Sequence[str] | None = None,
    apply_repairs: bool = True,
    max_repairs: int = 200,
) -> DialerCredentialReconcileStats:
    """Converge every assigned ONT's dialer projection onto its credential.

    Idempotent: a converged fleet produces zero writes. Drift is repaired by
    rewriting the derived projection from the authoritative record — never the
    other way round, and never by touching RADIUS.

    ``apply_repairs=False`` makes this a pure audit (used by the fleet report
    and by tests) that still reports exactly what a repair pass would change.
    """
    rows = _candidate_rows(db, ont_ids)

    # Filter to ONTs the ledger positively authorises as the PPPoE termination
    # BEFORE any credential is resolved. Skipped rows are counted and reported;
    # nothing about them is written.
    eligible_rows = []
    skipped: dict[str, int] = {}
    for row in rows:
        ok, reason = termination_intent(db, row.id)
        if ok:
            eligible_rows.append(row)
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
    if skipped:
        logger.info(
            "cpe dialer credential sync skipped %d ONT(s) without managed-ONT "
            "PPPoE termination intent: %s",
            sum(skipped.values()),
            skipped,
        )
    rows = eligible_rows

    credentials, ambiguous_credential_subscriptions = _authoritative_credentials(
        db, [row.subscription_id for row in rows if row.subscription_id is not None]
    )

    checked = in_sync = projected = awaiting = no_credential = no_secret = 0
    ambiguous_credential = 0
    drifts: list[DialerCredentialDrift] = []
    repaired_count = 0

    for row in rows:
        checked += 1
        credential = credentials.get(row.subscription_id)
        if (
            credential is None
            and row.subscription_id in ambiguous_credential_subscriptions
        ):
            # Several active credentials for one service. Nobody has said which
            # is authoritative, and that is a different fact from having none --
            # collapsing them hid a real ownership question behind a benign
            # "no credential" count.
            ambiguous_credential += 1
            continue
        if credential is None:
            # Not this reconciler's problem: an ONT with no access credential
            # is surfaced by pppoe_health as CATEGORY_NO_CREDENTIAL.
            no_credential += 1
            continue

        authoritative_username = (credential.username or "").strip() or None
        authoritative_secret = _safe_decrypt(credential.secret_hash)
        desired_fp = dialer_fingerprint(authoritative_username, authoritative_secret)
        if desired_fp is None:
            # A credential with no usable secret cannot be projected. Repairing
            # it belongs to the credential owner, not here.
            no_secret += 1
            continue

        raw_config = row._mapping.get("desired_config")
        config = raw_config if isinstance(raw_config, dict) else {}
        observed_username = (
            str(get_desired_config_value(config, *_DESIRED_USERNAME_PATH) or "").strip()
            or None
        )
        observed_secret = _safe_decrypt(
            get_desired_config_value(config, *_DESIRED_PASSWORD_PATH)
        )
        observed_fp = dialer_fingerprint(observed_username, observed_secret)

        if observed_fp == desired_fp:
            # Projection is correct. Second half of the readback: has the CPE
            # actually taken it? Only the username is readable from TR-069.
            device_username = (row.acs_observed_pppoe_username or "").strip() or None
            if (
                device_username is not None
                and device_username != authoritative_username
            ):
                awaiting += 1
                drift = DialerCredentialDrift(
                    ont_unit_id=str(row.id),
                    serial_number=str(row.serial_number or ""),
                    reason=REASON_DEVICE_READBACK_MISMATCH,
                    desired_fingerprint=desired_fp,
                    observed_fingerprint=observed_fp,
                    repaired=False,
                )
                drifts.append(drift)
                _log_drift(drift)
                if apply_repairs:
                    _flag_for_delivery(db, row.id)
                continue
            in_sync += 1
            continue

        reason = REASON_MISSING if observed_fp is None else REASON_FINGERPRINT_MISMATCH
        will_repair = apply_repairs and repaired_count < max_repairs
        if will_repair:
            _project(
                db,
                ont_id=row.id,
                username=authoritative_username,
                secret=authoritative_secret,
                fingerprint=desired_fp,
            )
            repaired_count += 1
            projected += 1
        drift = DialerCredentialDrift(
            ont_unit_id=str(row.id),
            serial_number=str(row.serial_number or ""),
            reason=reason,
            desired_fingerprint=desired_fp,
            observed_fingerprint=observed_fp,
            repaired=will_repair,
        )
        drifts.append(drift)
        _log_drift(drift)
        if will_repair:
            _emit_reconciled_event(db, drift, subscriber_id=row.subscriber_id)

    # Transaction mode is PARTICIPANT: the caller's session context owns the
    # commit. This owner flushes so later reads in the same pass see its writes.
    return DialerCredentialReconcileStats(
        checked=checked,
        in_sync=in_sync,
        projected=projected,
        awaiting_device=awaiting,
        skipped_no_credential=no_credential,
        skipped_ambiguous_credential=ambiguous_credential,
        skipped_no_secret=no_secret,
        drifts=tuple(drifts),
    )


def _project(
    db: Session,
    *,
    ont_id: Any,
    username: str | None,
    secret: str | None,
    fingerprint: str,
) -> None:
    """Rewrite the derived dialer projection from the authoritative record.

    The password is re-encrypted at rest with the same helper the operator form
    uses. ``delivery.pending_apply`` hands delivery to the ONT reconciler, which
    is the only writer that talks to the CPE.
    """
    from datetime import UTC, datetime

    ont = db.get(OntUnit, ont_id)
    if ont is None:  # pragma: no cover - row vanished mid-pass
        return
    set_desired_config_values(
        ont,
        {
            "wan.pppoe_username": username,
            "wan.pppoe_password": encrypt_credential(secret),
            _FINGERPRINT_KEY: fingerprint,
            _FINGERPRINT_AT_KEY: datetime.now(UTC).isoformat(),
            "delivery.pending_apply": True,
        },
    )
    db.add(ont)
    db.flush()


def _flag_for_delivery(db: Session, ont_id: Any) -> None:
    """Desired state is already right; the CPE just has not taken it yet."""
    ont = db.get(OntUnit, ont_id)
    if ont is None:  # pragma: no cover - row vanished mid-pass
        return
    if get_desired_config_value(desired_config(ont), "delivery", "pending_apply"):
        return
    set_desired_config_values(ont, {"delivery.pending_apply": True})
    db.add(ont)
    db.flush()


def _emit_reconciled_event(
    db: Session,
    drift: DialerCredentialDrift,
    *,
    subscriber_id: Any,
) -> None:
    """Record the repair durably. Fingerprint-only payload — no credential.

    Emitted per repaired ONT so downstream consumers can see that a customer's
    dialer was corrected without ever being handed the value it was corrected
    to.
    """
    from app.services.events import emit_event
    from app.services.events.types import EventType

    emit_event(
        db,
        EventType.ont_dialer_credential_reconciled,
        {
            "ont_id": drift.ont_unit_id,
            "ont_serial": drift.serial_number,
            "reason": drift.reason,
            "credential_fingerprint": drift.desired_fingerprint[:12],
            "previous_fingerprint": (
                drift.observed_fingerprint[:12] if drift.observed_fingerprint else None
            ),
        },
        actor="cpe_dialer_credential_reconciler",
        subscriber_id=subscriber_id,
    )


def _log_drift(drift: DialerCredentialDrift) -> None:
    """Structured, secret-free alert line. Fingerprints only — no values."""
    logger.warning(
        ALERT_KIND,
        extra={
            "alert_kind": ALERT_KIND,
            "ont_id": drift.ont_unit_id,
            "serial_number": drift.serial_number,
            "reason": drift.reason,
            "repaired": drift.repaired,
            "desired_fingerprint": drift.desired_fingerprint[:12],
            "observed_fingerprint": (
                drift.observed_fingerprint[:12] if drift.observed_fingerprint else None
            ),
        },
    )


__all__ = (
    "ALERT_KIND",
    "REASON_DEVICE_READBACK_MISMATCH",
    "REASON_FINGERPRINT_MISMATCH",
    "REASON_MISSING",
    "DialerCredentialDrift",
    "DialerCredentialReconcileStats",
    "DialerFingerprintUnavailable",
    "ProjectCpeDialerCredential",
    "ProjectCpeDialerCredentialOutcome",
    "clear_cpe_dialer_projection_for_non_ppp",
    "dialer_fingerprint",
    "project_cpe_dialer_credential_for_configuration",
    "reconcile_cpe_dialer_credentials",
)
