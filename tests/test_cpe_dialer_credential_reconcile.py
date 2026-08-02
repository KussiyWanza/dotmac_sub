"""The CPE PPPoE dialer is a derived projection of the access credential.

``AccessCredential``/``RadiusUser`` is authoritative. What an ONT dials with is
derived from it, and ``app.services.cpe_dialer_credential_reconcile`` is that
projection's only writer. Before this reconciler existed, the operator "set
PPPoE credentials" action wrote whatever was typed straight into ONT desired
state and never consulted RADIUS, so a CPE could dial forever with a credential
authentication would never accept. ``pppoe_health.CATEGORY_CREDENTIAL_MISMATCH``
detected exactly that and repaired nothing.

Security invariant under test: comparison happens by keyed fingerprint, and no
credential value is ever returned, logged, or stored in a drift record.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.catalog import AccessCredential
from app.models.network import OntAssignment, OntUnit
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.cpe_dialer_credential_reconcile import (
    REASON_DEVICE_READBACK_MISMATCH,
    REASON_FINGERPRINT_MISMATCH,
    REASON_MISSING,
    dialer_fingerprint,
    reconcile_cpe_dialer_credentials,
)
from app.services.credential_crypto import decrypt_credential, encrypt_credential
from app.services.network.ont_desired_config import desired_config

# Deliberately not hex-representable: the secret-leak assertions below check
# that this string never appears in a fingerprint, and a purely decimal
# username could collide with hex digits by chance.
AUTHORITATIVE_USERNAME = "pppoe-100024456@dotmac"
AUTHORITATIVE_SECRET = "PVgWc3Ch-authoritative"  # noqa: S105 - test fixture value


def _subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Dialer",
        last_name="Owner",
        email=f"dialer-{uuid.uuid4().hex[:8]}@example.com",
        status=SubscriberStatus.active,
    )
    db.add(subscriber)
    db.flush()
    return subscriber


_SUBSCRIPTIONS: dict = {}


def _subscription_for(db, subscriber):
    """One subscription per subscriber, shared by credential and assignment.

    The reconciler now works at exact-service grain, so a credential and an ONT
    assignment only pair up when they name the SAME subscription. These fixtures
    predate that -- the module created no subscriptions at all -- so this gives
    each subscriber one and threads it through both sides without changing every
    call site.
    """
    from app.models.catalog import (
        AccessType,
        CatalogOffer,
        PriceBasis,
        ServiceType,
        Subscription,
    )

    existing = _SUBSCRIPTIONS.get(str(subscriber.id))
    if existing is not None:
        return existing
    offer = CatalogOffer(
        name=f"dialer-offer-{uuid.uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
    )
    db.add(offer)
    db.flush()
    subscription = Subscription(subscriber_id=subscriber.id, offer_id=offer.id)
    db.add(subscription)
    db.flush()
    _SUBSCRIPTIONS[str(subscriber.id)] = subscription
    return subscription


def _credential(db, subscriber, *, username=AUTHORITATIVE_USERNAME, secret=None):
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        subscription_id=_subscription_for(db, subscriber).id,
        username=username,
        secret_hash=encrypt_credential(
            AUTHORITATIVE_SECRET if secret is None else secret
        ),
        is_active=True,
    )
    db.add(credential)
    db.flush()
    return credential


def _assigned_ont(db, subscriber, *, serial, desired=None) -> OntUnit:
    ont = OntUnit(
        serial_number=serial,
        is_active=True,
        desired_config=desired or {},
    )
    db.add(ont)
    db.flush()
    db.add(
        OntAssignment(
            ont_unit_id=ont.id,
            subscriber_id=subscriber.id,
            subscription_id=_subscription_for(db, subscriber).id,
            active=True,
        )
    )
    # Declared PPPoE service intent. Authorisation comes from
    # OntWanServiceInstance, not from OntAssignment fields -- migration 084
    # cleared those, so surviving values there are residue.
    from app.models.network import OntWanServiceInstance

    db.add(
        OntWanServiceInstance(
            ont_id=ont.id,
            name="internet",
            connection_type="pppoe",
            is_active=True,
        )
    )
    db.flush()
    return ont


def _observation(db, ont, device_username):
    """Minimal ``OntObservation`` row carrying the last ACS-observed username."""
    from datetime import UTC, datetime

    from app.models.ont_observation import OntObservation

    row = OntObservation(
        ont_unit_id=ont.id,
        last_reconciled_at=datetime.now(UTC),
        last_reconcile_duration_ms=0,
        mgmt_ip_pingable=True,
        olt_present=True,
        acs_present=True,
        acs_observed_pppoe_username=device_username,
    )
    db.add(row)
    db.flush()
    return row


def _dialer(config) -> tuple[str | None, str | None]:
    wan = (config or {}).get("wan") or {}
    return wan.get("pppoe_username"), decrypt_credential(wan.get("pppoe_password"))


# ── Fingerprinting ──────────────────────────────────────────────────────────


def test_fingerprint_is_stable_and_pair_sensitive():
    base = dialer_fingerprint(AUTHORITATIVE_USERNAME, AUTHORITATIVE_SECRET)

    assert base == dialer_fingerprint(AUTHORITATIVE_USERNAME, AUTHORITATIVE_SECRET)
    assert base != dialer_fingerprint(AUTHORITATIVE_USERNAME, "other-secret")
    assert base != dialer_fingerprint("other-user", AUTHORITATIVE_SECRET)


def test_fingerprint_never_contains_the_credential():
    fingerprint = dialer_fingerprint(AUTHORITATIVE_USERNAME, AUTHORITATIVE_SECRET)

    assert fingerprint is not None
    assert AUTHORITATIVE_SECRET not in fingerprint
    assert AUTHORITATIVE_USERNAME not in fingerprint
    assert len(fingerprint) == 64  # SHA-256 hex


@pytest.mark.parametrize(
    ("username", "secret"),
    [(None, AUTHORITATIVE_SECRET), (AUTHORITATIVE_USERNAME, None), (None, None)],
)
def test_incomplete_pairs_have_no_fingerprint(username, secret):
    assert dialer_fingerprint(username, secret) is None


# ── Projection ──────────────────────────────────────────────────────────────


def test_operator_typed_dialer_values_are_converged_onto_the_credential(db_session):
    """The exact defect: an operator typed a username/password into the ONT
    form. It changed nothing about authentication, and nothing repaired it."""
    subscriber = _subscriber(db_session)
    _credential(db_session, subscriber)
    ont = _assigned_ont(
        db_session,
        subscriber,
        serial="DIALER-DRIFT-001",
        desired={
            "wan": {
                "pppoe_username": "typed-by-operator",
                "pppoe_password": encrypt_credential("typed-by-operator-secret"),
            }
        },
    )
    db_session.commit()

    stats = reconcile_cpe_dialer_credentials(db_session)
    db_session.commit()
    db_session.refresh(ont)

    assert stats.projected == 1
    username, secret = _dialer(desired_config(ont))
    assert username == AUTHORITATIVE_USERNAME
    assert secret == AUTHORITATIVE_SECRET

    drift = next(item for item in stats.drifts if item.ont_unit_id == str(ont.id))
    assert drift.reason == REASON_FINGERPRINT_MISMATCH
    assert drift.repaired is True


def test_projection_hands_delivery_to_the_ont_reconciler(db_session):
    """This owner writes desired state only. The ONT reconciler is the sole
    writer that talks to the CPE, so a repair just flags for delivery."""
    subscriber = _subscriber(db_session)
    _credential(db_session, subscriber)
    ont = _assigned_ont(db_session, subscriber, serial="DIALER-DELIVERY-001")
    db_session.commit()

    reconcile_cpe_dialer_credentials(db_session)
    db_session.commit()
    db_session.refresh(ont)

    assert desired_config(ont)["delivery"]["pending_apply"] is True
    assert desired_config(ont)["delivery"]["dialer_credential_fingerprint"] == (
        dialer_fingerprint(AUTHORITATIVE_USERNAME, AUTHORITATIVE_SECRET)
    )


def test_missing_dialer_values_are_reported_as_missing_not_mismatched(db_session):
    subscriber = _subscriber(db_session)
    _credential(db_session, subscriber)
    ont = _assigned_ont(db_session, subscriber, serial="DIALER-EMPTY-001")
    db_session.commit()

    stats = reconcile_cpe_dialer_credentials(db_session)

    drift = next(item for item in stats.drifts if item.ont_unit_id == str(ont.id))
    assert drift.reason == REASON_MISSING
    assert drift.observed_fingerprint is None


def test_converged_fleet_is_a_no_op(db_session):
    subscriber = _subscriber(db_session)
    _credential(db_session, subscriber)
    ont = _assigned_ont(
        db_session,
        subscriber,
        serial="DIALER-SYNCED-001",
        desired={
            "wan": {
                "pppoe_username": AUTHORITATIVE_USERNAME,
                "pppoe_password": encrypt_credential(AUTHORITATIVE_SECRET),
            }
        },
    )
    db_session.commit()

    stats = reconcile_cpe_dialer_credentials(db_session)
    db_session.commit()
    db_session.refresh(ont)

    assert stats.in_sync == 1
    assert stats.projected == 0
    assert stats.drifts == ()
    assert "delivery" not in (ont.desired_config or {})


def test_second_pass_after_repair_converges(db_session):
    """Idempotence: the reconciler must not oscillate."""
    subscriber = _subscriber(db_session)
    _credential(db_session, subscriber)
    _assigned_ont(
        db_session,
        subscriber,
        serial="DIALER-IDEMPOTENT-001",
        desired={"wan": {"pppoe_username": "stale"}},
    )
    db_session.commit()

    assert reconcile_cpe_dialer_credentials(db_session).projected == 1
    db_session.commit()

    second = reconcile_cpe_dialer_credentials(db_session)

    assert second.projected == 0
    assert second.in_sync == 1


def test_audit_mode_reports_without_writing(db_session):
    subscriber = _subscriber(db_session)
    _credential(db_session, subscriber)
    ont = _assigned_ont(db_session, subscriber, serial="DIALER-AUDIT-001")
    db_session.commit()

    stats = reconcile_cpe_dialer_credentials(db_session, apply_repairs=False)
    db_session.commit()
    db_session.refresh(ont)

    assert stats.drifts and stats.drifts[0].repaired is False
    assert stats.projected == 0
    assert _dialer(desired_config(ont)) == (None, None)


# ── Readback ────────────────────────────────────────────────────────────────


def test_device_readback_mismatch_reflags_instead_of_reprojecting(db_session):
    """Desired state is already right, but the CPE still reports the old
    username — the value has not been taken yet, so re-flag for delivery
    rather than rewriting a correct projection."""
    subscriber = _subscriber(db_session)
    _credential(db_session, subscriber)
    ont = _assigned_ont(
        db_session,
        subscriber,
        serial="DIALER-READBACK-001",
        desired={
            "wan": {
                "pppoe_username": AUTHORITATIVE_USERNAME,
                "pppoe_password": encrypt_credential(AUTHORITATIVE_SECRET),
            }
        },
    )
    _observation(db_session, ont, "stale-on-device")
    db_session.commit()

    stats = reconcile_cpe_dialer_credentials(db_session)
    db_session.commit()
    db_session.refresh(ont)

    assert stats.awaiting_device == 1
    assert stats.projected == 0
    drift = next(item for item in stats.drifts if item.ont_unit_id == str(ont.id))
    assert drift.reason == REASON_DEVICE_READBACK_MISMATCH
    assert desired_config(ont)["delivery"]["pending_apply"] is True
    # The correct projection is untouched.
    assert _dialer(desired_config(ont))[0] == AUTHORITATIVE_USERNAME


def test_matching_device_readback_counts_as_converged(db_session):
    subscriber = _subscriber(db_session)
    _credential(db_session, subscriber)
    ont = _assigned_ont(
        db_session,
        subscriber,
        serial="DIALER-READBACK-OK-001",
        desired={
            "wan": {
                "pppoe_username": AUTHORITATIVE_USERNAME,
                "pppoe_password": encrypt_credential(AUTHORITATIVE_SECRET),
            }
        },
    )
    _observation(db_session, ont, AUTHORITATIVE_USERNAME)
    db_session.commit()

    stats = reconcile_cpe_dialer_credentials(db_session)

    assert stats.in_sync == 1
    assert stats.drifts == ()


# ── Boundaries ──────────────────────────────────────────────────────────────


def test_ont_without_an_access_credential_is_skipped_not_cleared(db_session):
    """No credential is pppoe_health's CATEGORY_NO_CREDENTIAL, not this
    owner's problem — and it must never wipe an existing dialer value."""
    subscriber = _subscriber(db_session)
    ont = _assigned_ont(
        db_session,
        subscriber,
        serial="DIALER-NOCRED-001",
        desired={"wan": {"pppoe_username": "left-alone"}},
    )
    db_session.commit()

    stats = reconcile_cpe_dialer_credentials(db_session)
    db_session.commit()
    db_session.refresh(ont)

    assert stats.skipped_no_credential == 1
    assert stats.projected == 0
    assert desired_config(ont)["wan"]["pppoe_username"] == "left-alone"


def test_unassigned_ont_is_not_a_candidate(db_session):
    ont = OntUnit(serial_number="DIALER-UNASSIGNED-001", is_active=True)
    db_session.add(ont)
    db_session.commit()

    stats = reconcile_cpe_dialer_credentials(db_session, ont_ids=[str(ont.id)])

    assert stats.checked == 0


def test_reconciler_never_writes_radius():
    """Hard boundary: access.radius_projection owns the auth tables."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "cpe_dialer_credential_reconcile.py"
    ).read_text(encoding="utf-8")

    assert "radcheck" not in source
    assert "radreply" not in source
    assert "radusergroup" not in source


def test_payload_carries_no_credential_values(db_session):
    subscriber = _subscriber(db_session)
    _credential(db_session, subscriber)
    _assigned_ont(
        db_session,
        subscriber,
        serial="DIALER-PAYLOAD-001",
        desired={
            "wan": {
                "pppoe_username": "typed-by-operator",
                "pppoe_password": encrypt_credential("typed-by-operator-secret"),
            }
        },
    )
    db_session.commit()

    payload = repr(reconcile_cpe_dialer_credentials(db_session).as_payload())

    assert AUTHORITATIVE_SECRET not in payload
    assert "typed-by-operator-secret" not in payload
    assert AUTHORITATIVE_USERNAME not in payload
