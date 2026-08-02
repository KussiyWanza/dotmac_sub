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
from typing import TYPE_CHECKING, Any

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
        authorize_ppp_delivery,
    )

    ruling = authorize_ppp_delivery(db, ont_id)
    return ruling.authorized, (
        ruling.refusal.value if ruling.refusal else "managed_ont_pppoe"
    )


def _authoritative_credentials(
    db: Session, subscription_ids: list[Any]
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
    rows = db.scalars(
        select(AccessCredential)
        .where(AccessCredential.subscription_id.in_(subscription_ids))
        .where(AccessCredential.is_active.is_(True))
    ).all()

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
    "dialer_fingerprint",
    "reconcile_cpe_dialer_credentials",
)
