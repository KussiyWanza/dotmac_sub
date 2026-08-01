"""Retirement of a shadowing NAS-local PPPoE secret.

Covers the properties that make removal corrective rather than a second
authority: intent-specific preconditions, count-only readback, plan
fingerprinting, durable operation evidence, and a cancellation path that cannot
roll back an authoritative lifecycle transition.
"""

from __future__ import annotations

import pytest

from app.models.catalog import (
    ConnectionType,
    NasDevice,
    NasVendor,
    Subscription,
    SubscriptionStatus,
)
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationType,
)
from app.models.subscriber import Subscriber
from app.services.nas import local_secret_policy as policy


@pytest.fixture()
def nas_device(db_session):
    device = NasDevice(
        name="Eagle Access",
        code="EAGLE",
        vendor=NasVendor.mikrotik,
        nas_ip="10.10.0.1",
        default_connection_type=ConnectionType.pppoe,
    )
    db_session.add(device)
    db_session.flush()
    return device


def _subscription(
    db_session, catalog_offer, *, login, tag, status=SubscriptionStatus.active, nas=None
):
    subscriber = Subscriber(
        first_name="Retire",
        last_name="Case",
        email=f"{tag}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=status,
        login=login,
        provisioning_nas_device_id=nas.id if nas is not None else None,
    )
    db_session.add(sub)
    db_session.flush()
    return sub


class _Device:
    """Fake RouterOS that answers count probes and records every command."""

    def __init__(self, present: int = 1, *, removal_works: bool = True, garbage=False):
        self.present = present
        self.removal_works = removal_works
        self.garbage = garbage
        self.commands: list[str] = []

    def __call__(self, command: str) -> str:
        self.commands.append(command)
        if "remove" in command:
            if self.removal_works:
                self.present = 0
            return ""
        return "not-a-number" if self.garbage else str(self.present)


def _operator(actor="michael", reason="Eagle cohort 1"):
    return policy.CleanupProvenance(
        kind=policy.ProvenanceKind.operator, actor=actor, reference=reason
    )


def _request(nas, login, intent, provenance=None):
    return policy.LocalSecretCleanupRequest(
        nas_device_id=nas.id,
        login=login,
        intent=intent,
        provenance=provenance or _operator(),
    )


# ---------------------------------------------------------------------------
# Secret-safe readback
# ---------------------------------------------------------------------------


def test_readback_is_count_only_and_never_retains_device_output(
    db_session, catalog_offer, nas_device
):
    """``print detail`` echoes the stored PPP password, so it is never used."""
    _subscription(db_session, catalog_offer, login="acct-1", tag="one")
    db_session.commit()
    device = _Device(present=1)

    plan = policy.plan_cleanup(
        db_session,
        _request(nas_device, "acct-1", policy.CleanupIntent.migrate_to_radius),
        run_command=device,
    )

    assert plan.present_count == 1
    assert all("print" not in command for command in device.commands)
    assert all("detail" not in command for command in device.commands)
    # No field on the plan carries raw device output.
    assert not any(
        isinstance(value, str) and "password" in value.lower()
        for value in plan.as_payload().values()
    )
    assert "device_readback" not in plan.as_payload()


def test_unreadable_count_fails_closed(db_session, catalog_offer, nas_device):
    """An unparseable probe must never be read as 'already absent'."""
    _subscription(db_session, catalog_offer, login="acct-1", tag="one")
    db_session.commit()
    device = _Device(present=1, garbage=True)

    with pytest.raises(policy.LocalSecretCleanupError) as excinfo:
        policy.plan_cleanup(
            db_session,
            _request(nas_device, "acct-1", policy.CleanupIntent.migrate_to_radius),
            run_command=device,
        )

    assert excinfo.value.code == policy.CLEANUP_UNVERIFIED


# ---------------------------------------------------------------------------
# Intent-specific preconditions
# ---------------------------------------------------------------------------


def test_migrate_intent_requires_radius_to_serve_the_login(
    db_session, catalog_offer, nas_device
):
    db_session.commit()
    device = _Device(present=1)

    with pytest.raises(policy.LocalSecretCleanupError) as excinfo:
        policy.apply_cleanup(
            db_session,
            _request(nas_device, "unprojected", policy.CleanupIntent.migrate_to_radius),
            run_command=device,
        )

    assert excinfo.value.code == policy.CLEANUP_RADIUS_NOT_SERVING
    assert all("remove" not in command for command in device.commands)


def test_terminal_intent_succeeds_exactly_where_migrate_intent_fails(
    db_session, catalog_offer, nas_device
):
    """The two intents assert opposite things about RADIUS, by design."""
    _subscription(
        db_session,
        catalog_offer,
        login="gone",
        tag="gone",
        status=SubscriptionStatus.canceled,
        nas=nas_device,
    )
    db_session.commit()

    with pytest.raises(policy.LocalSecretCleanupError) as excinfo:
        policy.apply_cleanup(
            db_session,
            _request(nas_device, "gone", policy.CleanupIntent.migrate_to_radius),
            run_command=_Device(present=1),
        )
    assert excinfo.value.code == policy.CLEANUP_RADIUS_NOT_SERVING

    device = _Device(present=1)
    outcome = policy.apply_cleanup(
        db_session,
        _request(nas_device, "gone", policy.CleanupIntent.terminal_retirement),
        run_command=device,
    )

    assert outcome.removed
    assert outcome.verified_absent


def test_terminal_intent_refuses_a_live_dependant(
    db_session, catalog_offer, nas_device
):
    """A canceled service must not retire a login another live service uses."""
    _subscription(
        db_session,
        catalog_offer,
        login="shared",
        tag="dead",
        status=SubscriptionStatus.canceled,
    )
    _subscription(db_session, catalog_offer, login="shared", tag="live")
    db_session.commit()
    device = _Device(present=1)

    with pytest.raises(policy.LocalSecretCleanupError) as excinfo:
        policy.apply_cleanup(
            db_session,
            _request(nas_device, "shared", policy.CleanupIntent.terminal_retirement),
            run_command=device,
        )

    assert excinfo.value.code == policy.CLEANUP_DEPENDENT_SUBSCRIPTION
    assert all("remove" not in command for command in device.commands)


def test_terminal_intent_refuses_while_radius_still_projects(
    db_session, catalog_offer, nas_device
):
    """Retiring before the terminal projection converged would cut access."""
    _subscription(db_session, catalog_offer, login="still-live", tag="live")
    db_session.commit()
    device = _Device(present=1)

    with pytest.raises(policy.LocalSecretCleanupError) as excinfo:
        policy.apply_cleanup(
            db_session,
            _request(
                nas_device, "still-live", policy.CleanupIntent.terminal_retirement
            ),
            run_command=device,
        )

    assert excinfo.value.code == policy.CLEANUP_DEPENDENT_SUBSCRIPTION


def test_shared_login_is_its_own_refusal_code(db_session, catalog_offer, nas_device):
    """Distinct from 'RADIUS not serving' — an operator must tell them apart."""
    _subscription(db_session, catalog_offer, login="shared", tag="a")
    _subscription(db_session, catalog_offer, login="shared", tag="b")
    db_session.commit()

    with pytest.raises(policy.LocalSecretCleanupError) as excinfo:
        policy.apply_cleanup(
            db_session,
            _request(nas_device, "shared", policy.CleanupIntent.migrate_to_radius),
            run_command=_Device(present=1),
        )

    assert excinfo.value.code == policy.CLEANUP_SHARED_LOGIN
    assert excinfo.value.code != policy.CLEANUP_RADIUS_NOT_SERVING


# ---------------------------------------------------------------------------
# Provenance and fingerprinting
# ---------------------------------------------------------------------------


def test_operator_provenance_requires_actor_and_reason(
    db_session, catalog_offer, nas_device
):
    _subscription(db_session, catalog_offer, login="acct-1", tag="one")
    db_session.commit()

    for provenance in (_operator(actor="  "), _operator(reason="")):
        with pytest.raises(policy.LocalSecretCleanupError) as excinfo:
            policy.apply_cleanup(
                db_session,
                _request(
                    nas_device,
                    "acct-1",
                    policy.CleanupIntent.migrate_to_radius,
                    provenance,
                ),
                run_command=_Device(present=1),
            )
        assert excinfo.value.code == policy.CLEANUP_INVALID_REQUEST


def test_event_provenance_needs_a_reference_not_a_human_reviewer(
    db_session, catalog_offer, nas_device
):
    provenance = policy.CleanupProvenance(
        kind=policy.ProvenanceKind.event, actor="handler", reference=""
    )
    with pytest.raises(policy.LocalSecretCleanupError, match="event reference"):
        provenance.validate()

    ok = policy.CleanupProvenance(
        kind=policy.ProvenanceKind.event, actor="handler", reference="evt-1"
    )
    ok.validate()
    assert ok.as_payload()["provenance_kind"] == "event"


def test_fingerprint_mismatch_refuses_a_stale_plan(
    db_session, catalog_offer, nas_device
):
    _subscription(db_session, catalog_offer, login="acct-1", tag="one")
    db_session.commit()

    with pytest.raises(policy.LocalSecretCleanupError) as excinfo:
        policy.apply_cleanup(
            db_session,
            _request(nas_device, "acct-1", policy.CleanupIntent.migrate_to_radius),
            run_command=_Device(present=1),
            expected_fingerprint="deadbeefdeadbeef",
        )

    assert excinfo.value.code == policy.CLEANUP_FINGERPRINT_MISMATCH


def test_fingerprint_tracks_the_device_and_the_cohort(
    db_session, catalog_offer, nas_device
):
    _subscription(db_session, catalog_offer, login="acct-1", tag="one")
    db_session.commit()
    request = _request(nas_device, "acct-1", policy.CleanupIntent.migrate_to_radius)

    first = policy.plan_cleanup(db_session, request, run_command=_Device(present=1))
    same = policy.plan_cleanup(db_session, request, run_command=_Device(present=1))
    moved = policy.plan_cleanup(db_session, request, run_command=_Device(present=2))

    assert first.fingerprint == same.fingerprint
    assert first.fingerprint != moved.fingerprint


# ---------------------------------------------------------------------------
# Durable execution
# ---------------------------------------------------------------------------


def test_success_records_a_succeeded_operation(db_session, catalog_offer, nas_device):
    _subscription(db_session, catalog_offer, login="acct-1", tag="one")
    db_session.commit()
    device = _Device(present=1)

    outcome = policy.apply_cleanup(
        db_session,
        _request(nas_device, "acct-1", policy.CleanupIntent.migrate_to_radius),
        run_command=device,
    )

    operation = db_session.get(NetworkOperation, outcome.operation_id)
    assert operation is not None
    assert operation.operation_type is NetworkOperationType.nas_local_secret_retire
    assert operation.status is NetworkOperationStatus.succeeded
    assert operation.input_payload["intent"] == "migrate_to_radius"
    assert operation.input_payload["actor"] == "michael"
    assert operation.correlation_key.endswith(":acct-1")


def test_unverified_removal_records_a_durable_failure(
    db_session, catalog_offer, nas_device
):
    """A still-present secret must be visible and retryable, not a log line."""
    _subscription(db_session, catalog_offer, login="acct-1", tag="one")
    db_session.commit()
    device = _Device(present=1, removal_works=False)

    with pytest.raises(policy.LocalSecretCleanupError) as excinfo:
        policy.apply_cleanup(
            db_session,
            _request(nas_device, "acct-1", policy.CleanupIntent.migrate_to_radius),
            run_command=device,
        )

    assert excinfo.value.code == policy.CLEANUP_UNVERIFIED
    failed = (
        db_session.query(NetworkOperation)
        .filter(
            NetworkOperation.operation_type
            == NetworkOperationType.nas_local_secret_retire
        )
        .all()
    )
    assert [op.status for op in failed] == [NetworkOperationStatus.failed]
    assert "still present" in (failed[0].error or "")


def test_device_error_records_a_durable_failure_and_reraises(
    db_session, catalog_offer, nas_device
):
    _subscription(db_session, catalog_offer, login="acct-1", tag="one")
    db_session.commit()

    def _explode(command: str) -> str:
        if "remove" in command:
            raise RuntimeError("ssh closed")
        return "1"

    with pytest.raises(RuntimeError, match="ssh closed"):
        policy.apply_cleanup(
            db_session,
            _request(nas_device, "acct-1", policy.CleanupIntent.migrate_to_radius),
            run_command=_explode,
        )

    failed = (
        db_session.query(NetworkOperation)
        .filter(
            NetworkOperation.operation_type
            == NetworkOperationType.nas_local_secret_retire
        )
        .all()
    )
    assert [op.status for op in failed] == [NetworkOperationStatus.failed]


def test_absent_secret_is_a_verified_no_op_that_opens_no_operation(
    db_session, catalog_offer, nas_device
):
    _subscription(db_session, catalog_offer, login="acct-1", tag="one")
    db_session.commit()
    device = _Device(present=0)

    outcome = policy.apply_cleanup(
        db_session,
        _request(nas_device, "acct-1", policy.CleanupIntent.migrate_to_radius),
        run_command=device,
    )

    assert not outcome.removed
    assert outcome.verified_absent
    assert outcome.operation_id is None
    assert all("remove" not in command for command in device.commands)
    assert db_session.query(NetworkOperation).count() == 0


# ---------------------------------------------------------------------------
# Cancellation staging
# ---------------------------------------------------------------------------


def test_terminal_retirement_staging_uses_event_provenance(
    db_session, catalog_offer, nas_device
):
    sub = _subscription(
        db_session,
        catalog_offer,
        login="gone",
        tag="gone",
        status=SubscriptionStatus.canceled,
        nas=nas_device,
    )
    db_session.commit()
    device = _Device(present=1)

    outcome = policy.stage_terminal_retirement(
        db_session,
        subscription_id=str(sub.id),
        event_reference="evt-99",
        run_command=device,
    )

    assert outcome is not None and outcome.verified_absent
    operation = db_session.get(NetworkOperation, outcome.operation_id)
    assert operation.input_payload["provenance_kind"] == "event"
    assert operation.input_payload["reference"] == "evt-99"
    assert operation.input_payload["intent"] == "terminal_retirement"


def test_staging_never_raises_into_the_cancellation_path(
    db_session, catalog_offer, nas_device
):
    """The lifecycle transition is authoritative; a dead NAS must not undo it."""
    sub = _subscription(
        db_session,
        catalog_offer,
        login="gone",
        tag="gone",
        status=SubscriptionStatus.canceled,
        nas=nas_device,
    )
    db_session.commit()

    def _unreachable(command: str) -> str:
        raise RuntimeError("no route to host")

    assert (
        policy.stage_terminal_retirement(
            db_session,
            subscription_id=str(sub.id),
            event_reference="evt-1",
            run_command=_unreachable,
        )
        is None
    )


def test_staging_is_a_no_op_without_a_login_or_nas(
    db_session, catalog_offer, nas_device
):
    sub = _subscription(
        db_session,
        catalog_offer,
        login="gone",
        tag="gone",
        status=SubscriptionStatus.canceled,
    )
    db_session.commit()

    assert (
        policy.stage_terminal_retirement(
            db_session,
            subscription_id=str(sub.id),
            event_reference="evt-1",
            run_command=_Device(present=1),
        )
        is None
    )
