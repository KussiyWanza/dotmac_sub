"""PostgreSQL concurrency contract for ONT admission and hold decisions.

Static guards cannot prove this boundary. A pass-level "held set" read looks
correct in a single-session test and is still wrong: a hold placed after the
snapshot is invisible, and the sweeper touches a device someone just decided to
protect. Only two real sessions competing for the same row can show it.

Four things this file is careful about, because an earlier version was not:

* The stale snapshot is produced by intercepting the REAL ``held_ont_ids`` call
  inside ``run_sweep_once`` -- committing the hold from another session while
  that call is in flight and then returning its empty result. Taking a separate
  snapshot beforehand proves nothing: the sweeper would call ``held_ont_ids``
  again afterwards, see the hold in the pre-filter, and never reach the
  point-of-use path under test.
* The symmetric admission test returns a genuinely stale positive catalog
  after committing revocation, proving a snapshot cannot remain authority.
* Blocking is asserted only after placement has demonstrably REACHED
  ``_lock_ont``. A bare negative wait is scheduler-dependent -- it passes
  whether the thread is blocked on the lock or simply hasn't been scheduled.
* Every committed row is cleaned up per test. The engine fixture is
  session-scoped, so a leaked ONT from an earlier test joins the fleet-wide
  sweep and invalidates exact counters like ``held == 1``.
* Events are released and threads joined in ``finally``, so a failed assertion
  cannot strand a worker holding a row lock and wedge the rest of the suite.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker

from app.models.network import (
    DeviceStatus,
    OLTDevice,
    OntReconcileAdmission,
    OntReconcileHold,
    OntReconcileHoldStatus,
    OntUnit,
)
from app.services.network import ont_reconcile_eligibility as elig_mod
from app.services.network.ont_reconcile_eligibility import (
    AdmissionSpec,
    HoldRefusal,
    HoldSpec,
    ReconcileHoldError,
    admit_reconcile_cohort_member,
    place_reconcile_hold,
    release_reconcile_hold,
    revoke_reconcile_admission,
)
from app.services.network.reconcile.sweeper import (
    SweepDisposition,
    _sweep_one,
    run_sweep_once,
)
from app.services.owner_commands import CommandContext

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _require_postgres(engine):
    """The shared fixture silently falls back to SQLite, where ``FOR UPDATE``
    is a no-op and this entire contract is vacuous."""
    if engine.dialect.name != "postgresql":
        pytest.skip(
            "row-lock concurrency contract requires PostgreSQL; "
            f"got {engine.dialect.name}"
        )


@pytest.fixture()
def factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def seeded(factory):
    """One ONT + OLT, torn down afterwards.

    The engine is session-scoped: without cleanup a previous test's ONT stays
    in the fleet and the next ``run_sweep_once`` counts it, so assertions like
    ``held == 1`` become order-dependent.
    """
    created: dict[str, uuid.UUID] = {}
    suffix = uuid.uuid4().hex[:10]
    with factory() as setup:
        olt = OLTDevice(
            name=f"OLT-CONC-{suffix}",
            vendor="Huawei",
            is_active=True,
            status=DeviceStatus.active,
            # `ck_olt_devices_config_pack_required`: an ACTIVE, non-UISP OLT
            # must carry a usable config pack. That constraint lives in a
            # migration, so a metadata-built test database does not enforce it
            # and an incomplete row only fails where the schema came from
            # migrations -- i.e. CI and production.
            config_pack={
                "internet_vlan_id": 203,
                "management_vlan_id": 201,
                "tr069_olt_profile_id": 2,
            },
        )
        setup.add(olt)
        setup.flush()
        ont = OntUnit(
            serial_number=f"HWTC-CONC-{suffix}",
            is_active=True,
            olt_device_id=olt.id,
        )
        setup.add(ont)
        setup.commit()
        created["ont_id"] = ont.id
        created["olt_id"] = olt.id

    with factory() as admit_db:
        explanation = "Reviewed integration admission for lock testing."
        admission = admit_reconcile_cohort_member(
            admit_db,
            spec=AdmissionSpec(
                ont_unit_id=created["ont_id"],
                cohort_key="pytest-concurrency",
                reason_code="postgres_lock_contract",
                explanation=explanation,
                reviewer="reviewer@dotmac",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
            context=CommandContext.system(
                actor="operator@dotmac",
                scope="ont:concurrency",
                reason=explanation,
                idempotency_key=f"conc-admit-{suffix}",
            ),
        )
        created["admission_id"] = admission.id

    yield created

    with factory() as teardown:
        teardown.execute(
            delete(OntReconcileAdmission).where(
                OntReconcileAdmission.ont_unit_id == created["ont_id"]
            )
        )
        teardown.execute(
            delete(OntReconcileHold).where(
                OntReconcileHold.ont_unit_id == created["ont_id"]
            )
        )
        teardown.execute(delete(OntUnit).where(OntUnit.id == created["ont_id"]))
        teardown.execute(delete(OLTDevice).where(OLTDevice.id == created["olt_id"]))
        teardown.commit()


def _ctx(actor="operator@dotmac", reason="concurrency test", key=None):
    return CommandContext.system(
        actor=actor,
        scope="ont:concurrency",
        reason=reason,
        idempotency_key=key or f"conc-{uuid.uuid4().hex[:12]}",
    )


def _spec(ont_id, **kw):
    return HoldSpec(
        ont_unit_id=ont_id,
        reason_code=kw.pop("reason_code", "concurrency_probe"),
        explanation=kw.pop("explanation", "Placed by the concurrency contract test."),
        reviewer=kw.pop("reviewer", "reviewer@dotmac"),
        review_due_at=datetime.now(UTC) + timedelta(days=3),
    )


def _join(thread: threading.Thread, errors: list[BaseException], label: str) -> None:
    # A failed assertion can abort before a thread is started; joining an
    # unstarted thread raises and masks the real failure.
    if thread.ident is None:
        return
    thread.join(timeout=30)
    assert not thread.is_alive(), f"{label} thread did not terminate"
    assert not errors, f"{label} raised: {errors!r}"


def test_placement_waits_for_the_sweepers_lock_lifetime(factory, seeded, monkeypatch):
    """The PRODUCTION sweeper holds the parent lock through its device work.

    Driven through ``_sweep_one``: the claim is about the sweeper's transaction
    lifetime, not about PostgreSQL's locking.
    """
    ont_id = seeded["ont_id"]

    in_device_work = threading.Event()
    may_finish_device_work = threading.Event()
    placement_at_lock = threading.Event()
    placement_completed = threading.Event()
    sweep_errors: list[BaseException] = []
    place_errors: list[BaseException] = []
    landed_during_device_work: list[bool] = []

    # A bare seeded ONT has no config pack, so `desired_from_ont_unit` cannot
    # build a desired state and `_sweep_one` would bail before reaching device
    # work. Stub only the desired-state resolution and the ping: the claim
    # under test is that the OntUnit lock taken inside `_sweep_one` is held
    # across `reconcile_fn` and transaction completion, which this preserves.
    from types import SimpleNamespace

    from app.services.network.reconcile import sweeper as sweeper_mod

    monkeypatch.setattr(
        sweeper_mod,
        "desired_from_ont_unit",
        lambda db, ont: SimpleNamespace(mgmt_ip="10.255.255.1"),
    )

    # Signal the moment placement actually reaches the lock, so "still blocked"
    # is a statement about the lock and not about thread scheduling.
    real_lock_ont = elig_mod._lock_ont

    def _signalling_lock_ont(db, unit_id):
        # ONLY the placer's call counts. The sweeper reaches _lock_ont too (via
        # eligibility_under_lock), and signalling on its call would fire the
        # event before the placer thread had started -- proving nothing.
        if threading.current_thread().name == "placer":
            placement_at_lock.set()
        return real_lock_ont(db, unit_id)

    monkeypatch.setattr(elig_mod, "_lock_ont", _signalling_lock_ont)

    def _reconcile_fn(*args, **kwargs):
        in_device_work.set()
        may_finish_device_work.wait(timeout=25)
        landed_during_device_work.append(placement_completed.is_set())

        class _Result:
            success = True

        return _Result()

    def _sweeper() -> None:
        try:
            with factory() as sweep_db:
                _sweep_one(
                    sweep_db,
                    ont_id,
                    timeout_sec=30,
                    ping_function=lambda ip, count, timeout_sec: True,
                    reconcile_fn=_reconcile_fn,
                )
                sweep_db.commit()
        except BaseException as exc:  # noqa: BLE001
            sweep_errors.append(exc)

    def _placer() -> None:
        try:
            with factory() as place_db:
                place_reconcile_hold(place_db, spec=_spec(ont_id), context=_ctx())
                place_db.commit()
        except BaseException as exc:  # noqa: BLE001
            place_errors.append(exc)
        finally:
            placement_completed.set()

    sweeper = threading.Thread(target=_sweeper, name="sweeper")
    placer = threading.Thread(target=_placer, name="placer")
    try:
        sweeper.start()
        assert in_device_work.wait(timeout=25), "sweeper never reached device work"

        placer.start()
        assert placement_at_lock.wait(timeout=25), (
            "placement never reached _lock_ont; the wait below would prove "
            "nothing about blocking"
        )

        # It is AT the lock. If the lock is doing its job it cannot get past.
        assert not placement_completed.wait(timeout=3), (
            "placement completed while the sweeper held the OntUnit lock "
            "through device work -- the parent lock is not held for its "
            "stated lifetime"
        )
    finally:
        # Release the worker unconditionally, then join both. A failed
        # assertion above must not leave a thread parked on a row lock.
        may_finish_device_work.set()
        _join(sweeper, sweep_errors, "sweeper")
        _join(placer, place_errors, "placer")

    assert not sweep_errors, f"sweeper raised: {sweep_errors!r}"
    assert not place_errors, f"placer raised: {place_errors!r}"
    assert placement_completed.is_set(), "placement never completed"
    assert landed_during_device_work == [False], (
        "placement must not land while device work is in flight"
    )


def test_a_stale_prefilter_does_not_let_the_sweep_touch_a_held_ont(
    factory, seeded, monkeypatch
):
    """The point-of-use decision, forced by a genuinely stale pre-filter.

    ``held_ont_ids`` is intercepted INSIDE ``run_sweep_once``: the hold is
    committed from another session while that call is in flight, and the call
    then returns its empty result. The sweeper therefore proceeds with a
    pre-filter that does not contain the ONT, and only the per-ONT decision can
    stop it.
    """
    ont_id = seeded["ont_id"]
    real_held = elig_mod.held_ont_ids
    intercepted: list[frozenset] = []

    def _stale_held_ont_ids(db, **kwargs):
        result = real_held(db, **kwargs)  # empty: the hold does not exist yet
        # Commit the hold from an independent session, mid-call.
        with factory() as place_db:
            place_reconcile_hold(place_db, spec=_spec(ont_id), context=_ctx())
            place_db.commit()
        intercepted.append(result)
        return result  # deliberately stale

    monkeypatch.setattr(elig_mod, "held_ont_ids", _stale_held_ont_ids)

    pings: list = []
    reconciles: list = []

    def _ping(ip, count, timeout_sec):
        # Arity matters: is_pingable swallows a TypeError from a mis-shaped
        # stub and returns False, which would silently turn every ONT into
        # "unreachable" and quietly void the assertions below.
        pings.append(ip)
        return True

    def _reconcile(*args, **kwargs):
        reconciles.append(args)
        raise AssertionError("a held ONT must never be reconciled")

    stats = run_sweep_once(
        factory,
        timeout_sec=10,
        max_onts=50,
        ping_function=_ping,
        reconcile_fn=_reconcile,
    )

    assert intercepted, "held_ont_ids was never called by run_sweep_once"
    assert ont_id not in intercepted[0], (
        "the pre-filter must have been stale for this test to mean anything"
    )
    assert pings == [], "a held ONT must not be pinged"
    assert reconciles == [], "a held ONT must not be reconciled"
    assert stats.held == 1, f"expected exactly one held ONT, got {stats.held}"
    assert stats.skipped_unreachable == 0, (
        "a deliberate exclusion must not be reported as an outage"
    )


def test_a_stale_admission_catalog_cannot_authorize_after_revocation(
    factory, seeded, monkeypatch
):
    """The catalog may be stale; lock-time authority must still fail closed."""
    ont_id = seeded["ont_id"]
    admission_id = seeded["admission_id"]
    real_admitted = elig_mod.admitted_ont_ids
    intercepted: list[frozenset] = []

    def _stale_admitted_ont_ids(db, **kwargs):
        result = real_admitted(db, **kwargs)
        assert ont_id in result
        with factory() as revoke_db:
            revoke_reconcile_admission(
                revoke_db,
                admission_id=admission_id,
                context=_ctx(reason="cohort paused before point of use"),
            )
        intercepted.append(result)
        return result

    monkeypatch.setattr(elig_mod, "admitted_ont_ids", _stale_admitted_ont_ids)
    pings: list = []
    reconciles: list = []

    stats = run_sweep_once(
        factory,
        timeout_sec=10,
        max_onts=50,
        ping_function=lambda ip, count, timeout_sec: pings.append(ip) or True,
        reconcile_fn=lambda *args, **kwargs: reconciles.append((args, kwargs)),
    )

    assert intercepted and ont_id in intercepted[0]
    assert pings == []
    assert reconciles == []
    assert stats.total_onts == 1
    assert stats.not_admitted == 1
    assert stats.held == 0
    assert stats.skipped_unreachable == 0
    assert stats.reconciled == 0


def test_the_sweep_step_reports_held_not_unreachable(factory, seeded):
    ont_id = seeded["ont_id"]
    with factory() as place_db:
        place_reconcile_hold(place_db, spec=_spec(ont_id), context=_ctx())
        place_db.commit()

    pinged: list = []
    with factory() as sweep_db:
        outcome = _sweep_one(
            sweep_db,
            ont_id,
            timeout_sec=5,
            # Records rather than raising: is_pingable would swallow an
            # exception here and the test would pass for the wrong reason.
            ping_function=lambda ip, count, timeout_sec: pinged.append(ip) or True,
            reconcile_fn=lambda *a, **k: pytest.fail("must not reconcile"),
        )

    assert pinged == [], "a held ONT must not be pinged"
    assert outcome.disposition is SweepDisposition.held
    assert outcome.disposition is not SweepDisposition.unreachable


def test_concurrent_release_returns_concurrent_release(factory, seeded):
    """Both sessions observe ACTIVE before competing for the parent lock.

    Without that ordering the loser would simply read an already-released row
    and return ``already_released`` -- which proves nothing about the race.
    """
    ont_id = seeded["ont_id"]
    with factory() as place_db:
        hold = place_reconcile_hold(place_db, spec=_spec(ont_id), context=_ctx())
        place_db.commit()
        hold_id = hold.id

    both_saw_active = threading.Barrier(2, timeout=25)
    codes: list[str] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def _release(label: str) -> None:
        try:
            with factory() as db:
                seen = db.get(OntReconcileHold, hold_id)
                assert seen.status is OntReconcileHoldStatus.active, (
                    f"{label} did not observe an active hold"
                )
                # That read opened a transaction; the owner command requires a
                # transaction-free session at entry. Close it BEFORE the
                # barrier so both threads race from the same clean state.
                db.commit()
                both_saw_active.wait()
                try:
                    release_reconcile_hold(
                        db, hold_id=hold_id, context=_ctx(reason=f"race-{label}")
                    )
                    db.commit()
                    with guard:
                        codes.append("released")
                except ReconcileHoldError as exc:
                    with guard:
                        codes.append(exc.code)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    first = threading.Thread(target=_release, args=("a",), name="release-a")
    second = threading.Thread(target=_release, args=("b",), name="release-b")
    try:
        first.start()
        second.start()
    finally:
        # Join in finally so a failed assertion cannot strand a worker holding
        # a row lock and wedge the rest of the suite.
        _join(first, errors, "release-a")
        _join(second, errors, "release-b")

    assert sorted(codes) == sorted(
        ["released", HoldRefusal.concurrent_release.value]
    ), (
        "exactly one release must succeed and the loser must report "
        f"concurrent_release, got {codes}"
    )

    with factory() as check:
        final = check.get(OntReconcileHold, hold_id)
        assert final.status is OntReconcileHoldStatus.released
