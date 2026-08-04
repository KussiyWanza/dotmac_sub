"""Per-ONT reconciliation holds.

The fleet-wide `network.ont_reconcile` control halts convergence for every
ONT and, because expired remote-access cleanup and the dialer reconcile run
inside the same task after the gate, silently pauses those too. A hold covers
one device instead.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.network import (
    OntReconcileHold,
    OntReconcileHoldStatus,
    OntReconcileScope,
    OntUnit,
)
from app.services.domain_errors import DomainError
from app.services.network.ont_reconcile_eligibility import (
    HoldRefusal,
    HoldSpec,
    ReconcileHoldError,
    held_ont_ids,
    overdue_holds,
    place_reconcile_hold,
    reconcile_eligibility,
    release_reconcile_hold,
)
from app.services.owner_commands import CommandContext


def _ont(db, serial="HWTC-HOLD-1"):
    ont = OntUnit(serial_number=serial, is_active=True)
    db.add(ont)
    db.flush()
    return ont


_KEY_SEQ = itertools.count(1)


def _ctx(actor="operator@dotmac", reason="cohort under adjudication", key=None):
    # Idempotency is mandatory, so every test context carries a key. Unique by
    # default; pass `key=` explicitly to exercise replay and conflict.
    return CommandContext.system(
        actor=actor,
        scope="ont:test",
        reason=reason,
        idempotency_key=key or f"hold-test-{next(_KEY_SEQ)}",
    )


def _spec(ont, *, reviewer="reviewer@dotmac", days=7, **kw):
    return HoldSpec(
        ont_unit_id=ont.id,
        reason_code=kw.pop("reason_code", "wan_intent_adjudication"),
        explanation=kw.pop(
            "explanation",
            "Unverified WAN intent; management drift not yet adjudicated.",
        ),
        reviewer=reviewer,
        review_due_at=kw.pop("review_due_at", datetime.now(UTC) + timedelta(days=days)),
        **kw,
    )


def _place(db, ont, **kw):
    ctx = kw.pop("context", _ctx())
    spec = _spec(ont, **kw)  # touches ont.id while a transaction is still open
    db.commit()  # ...then hand the owner a transaction-free session
    return place_reconcile_hold(db, spec=spec, context=ctx)


def _expect_refusal(db, spec, ctx):
    # spec is built by the caller BEFORE this commit: touching ont.id on an
    # expired instance would reopen a transaction and the owner refuses one.
    db.commit()
    with pytest.raises(ReconcileHoldError) as excinfo:
        place_reconcile_hold(db, spec=spec, context=ctx)
    return excinfo


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def test_an_unheld_ont_is_eligible(db_session):
    ont = _ont(db_session)
    db_session.commit()

    verdict = reconcile_eligibility(db_session, ont.id)

    assert verdict.eligible is True
    assert verdict.held is False


def test_a_held_ont_is_not_eligible_and_says_why(db_session):
    ont = _ont(db_session)
    hold = _place(db_session, ont)

    verdict = reconcile_eligibility(db_session, ont.id)

    assert verdict.eligible is False
    assert verdict.held is True
    assert verdict.hold_id == str(hold.id)
    assert verdict.reason_code == "wan_intent_adjudication"
    assert verdict.review_due_at is not None


def test_an_absent_ont_identity_fails_closed(db_session):
    """No identity, no eligibility."""
    assert reconcile_eligibility(db_session, None).eligible is False


def test_releasing_restores_eligibility(db_session):
    ont = _ont(db_session)
    hold = _place(db_session, ont)
    hold_id = hold.id
    ont_id = ont.id
    assert reconcile_eligibility(db_session, ont_id).eligible is False

    db_session.commit()
    release_reconcile_hold(
        db_session,
        hold_id=hold_id,
        context=_ctx(reason="adjudicated; safe to converge"),
    )

    assert reconcile_eligibility(db_session, ont_id).eligible is True


# ---------------------------------------------------------------------------
# Evidence is mandatory
# ---------------------------------------------------------------------------


def test_the_reviewer_may_not_be_the_actor(db_session):
    """Suppressing convergence on a customer device is a two-person decision."""
    ont = _ont(db_session)
    spec = _spec(ont, reviewer="operator@dotmac")
    excinfo = _expect_refusal(db_session, spec, _ctx(actor="operator@dotmac"))

    assert excinfo.value.code == HoldRefusal.reviewer_is_actor.value


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("reason_code", "  ", HoldRefusal.missing_reason),
        ("explanation", "", HoldRefusal.missing_explanation),
        ("reviewer", "", HoldRefusal.missing_reviewer),
    ],
)
def test_missing_evidence_is_refused(db_session, field, value, code):
    ont = _ont(db_session)
    spec = _spec(ont, **{field: value})
    excinfo = _expect_refusal(db_session, spec, _ctx())

    assert excinfo.value.code == code.value


def test_a_review_date_in_the_past_is_refused(db_session):
    """A hold placed already-overdue hides the decision it records."""
    ont = _ont(db_session)
    spec = _spec(ont, review_due_at=datetime.now(UTC) - timedelta(hours=1))
    excinfo = _expect_refusal(db_session, spec, _ctx())

    assert excinfo.value.code == HoldRefusal.review_due_in_past.value


def test_every_command_requires_actor_and_reason(db_session):
    ont = _ont(db_session)
    spec = _spec(ont)
    db_session.commit()

    for bad in (_ctx(actor="  "), _ctx(reason="")):
        with pytest.raises(DomainError):
            place_reconcile_hold(db_session, spec=spec, context=bad)


# ---------------------------------------------------------------------------
# review_due_at is NOT an expiry
# ---------------------------------------------------------------------------


def test_an_overdue_hold_still_suppresses(db_session):
    """The load-bearing invariant.

    An expiring hold would hand a suppressed device back to the sweeper at an
    arbitrary moment -- the exact surprise a hold exists to prevent.
    """
    ont = _ont(db_session)
    hold = _place(db_session, ont)
    # Force it overdue without touching status.
    hold.review_due_at = datetime.now(UTC) - timedelta(days=1)
    db_session.flush()

    verdict = reconcile_eligibility(db_session, ont.id)

    assert verdict.eligible is False, "overdue must not mean released"
    assert verdict.overdue is True
    assert hold.status is OntReconcileHoldStatus.active
    assert ont.id in held_ont_ids(db_session)


def test_overdue_holds_are_reported_for_escalation(db_session):
    ont = _ont(db_session)
    hold = _place(db_session, ont)
    hold.review_due_at = datetime.now(UTC) - timedelta(days=2)
    db_session.flush()

    overdue = overdue_holds(db_session)

    assert [h.id for h in overdue] == [hold.id]
    assert all(h.status is OntReconcileHoldStatus.active for h in overdue)


def test_nothing_releases_a_hold_but_the_release_command(db_session):
    """Guard: no timer, no sweep, no scheduled job may end a hold."""
    from pathlib import Path

    source = Path("app/services/network/ont_reconcile_eligibility.py").read_text(
        encoding="utf-8"
    )
    body = source.split('"""', 2)[-1]

    # Only ASSIGNMENTS count; a comparison against `released` is a read.
    releases = [
        line
        for line in body.splitlines()
        if "status = OntReconcileHoldStatus.released" in line
    ]
    assert len(releases) == 1, f"exactly one release path expected, found: {releases}"
    # ...and nothing schedules or times out a release.
    for forbidden in ("expires_at", "auto_release", "timedelta(", "celery"):
        assert forbidden not in body, f"{forbidden} suggests an automatic expiry"


# ---------------------------------------------------------------------------
# One active hold per ONT and scope
# ---------------------------------------------------------------------------


def test_a_second_active_hold_is_refused(db_session):
    ont = _ont(db_session)
    # Build the spec while the instance is still live: reading ont.id after a
    # commit reopens a transaction, which the owner command refuses.
    spec = _spec(ont)
    _place(db_session, ont)

    db_session.commit()
    with pytest.raises(ReconcileHoldError) as excinfo:
        place_reconcile_hold(db_session, spec=spec, context=_ctx())

    assert excinfo.value.code == HoldRefusal.already_held.value


def test_a_released_hold_does_not_block_a_new_one(db_session):
    """Released holds are history; they must not prevent holding again."""
    ont = _ont(db_session)
    spec = _spec(ont)
    hold = _place(db_session, ont)
    hold_id = hold.id
    db_session.commit()
    release_reconcile_hold(db_session, hold_id=hold_id, context=_ctx(reason="done"))

    db_session.commit()
    again = place_reconcile_hold(db_session, spec=spec, context=_ctx())

    assert again.id != hold_id
    assert reconcile_eligibility(db_session, ont.id).eligible is False


def test_a_retried_command_returns_the_same_hold(db_session):
    """Idempotency: a retry must not create a second hold."""
    ont = _ont(db_session)
    spec = _spec(ont)
    db_session.commit()
    ctx = CommandContext.system(
        actor="operator@dotmac",
        scope="ont:test",
        reason="cohort",
        idempotency_key="hold-canary-001",
    )
    first = place_reconcile_hold(db_session, spec=spec, context=ctx)

    db_session.commit()
    second = place_reconcile_hold(db_session, spec=spec, context=ctx)

    assert first.id == second.id


def test_releasing_twice_is_refused(db_session):
    ont = _ont(db_session)
    hold = _place(db_session, ont)
    hold_id = hold.id
    db_session.commit()
    release_reconcile_hold(db_session, hold_id=hold_id, context=_ctx(reason="done"))

    db_session.commit()
    with pytest.raises(ReconcileHoldError) as excinfo:
        release_reconcile_hold(
            db_session, hold_id=hold_id, context=_ctx(reason="again")
        )

    assert excinfo.value.code == HoldRefusal.already_released.value


def test_releasing_an_unknown_hold_is_refused(db_session):
    db_session.commit()
    db_session.commit()
    with pytest.raises(ReconcileHoldError) as excinfo:
        release_reconcile_hold(db_session, hold_id=uuid4(), context=_ctx())

    assert excinfo.value.code == HoldRefusal.not_found.value


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_the_active_hold_index_is_partial(db_session):
    """The uniqueness must apply to ACTIVE holds only.

    A full unique constraint would make a released hold permanently prevent
    re-holding the same ONT, turning history into a lockout.
    """

    index = next(
        ix
        for ix in OntReconcileHold.__table__.indexes
        if ix.name == "uq_ont_reconcile_holds_active_per_ont_scope"
    )
    assert index.unique is True
    for dialect in ("postgresql", "sqlite"):
        where = index.dialect_options[dialect].get("where")
        assert where is not None, (
            f"index must be partial on {dialect}; a full unique constraint "
            "turns released history into a permanent lockout"
        )
        assert "active" in str(where)


def test_scope_is_sweep_only_so_operators_can_still_repair(db_session):
    """A held ONT must retain a legitimate path back to convergence."""
    assert [s.value for s in OntReconcileScope] == ["automatic_sweep"]


# ---------------------------------------------------------------------------
# Sweeper integration
# ---------------------------------------------------------------------------


def test_the_sweeper_skips_held_onts_before_touching_them(db_session, monkeypatch):
    """Checked before ping, read or write.

    Contacting a device to discover it is held would defeat the point.
    """
    from app.models.network import DeviceStatus, OLTDevice
    from app.services.network.reconcile import sweeper as sweeper_mod

    olt = OLTDevice(
        name="OLT-HOLD-TEST",
        vendor="Huawei",
        is_active=True,
        status=DeviceStatus.active,
    )
    db_session.add(olt)
    db_session.flush()
    ont = _ont(db_session, serial="HWTC-HOLD-SWEEP")
    ont.olt_device_id = olt.id
    db_session.flush()
    _place(db_session, ont)
    db_session.commit()

    touched: list = []

    def _never(*args, **kwargs):
        touched.append(args)
        raise AssertionError("a held ONT must not be reconciled")

    monkeypatch.setattr(sweeper_mod, "_sweep_one", _never)

    class _Factory:
        def __call__(self):
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                yield db_session

            return _cm()

    stats = sweeper_mod.run_sweep_once(_Factory(), timeout_sec=1, max_onts=50)

    assert touched == []
    assert stats.held >= 1


def test_held_is_reported_separately_from_unreachable(db_session):
    """ "We chose not to" and "we could not" are different facts."""
    from app.services.network.reconcile.sweeper import SweepStats

    stats = SweepStats(started_at=datetime.now(UTC))
    assert hasattr(stats, "held")
    assert stats.held == 0
    assert stats.skipped_unreachable == 0


# ---------------------------------------------------------------------------
# Idempotency is mandatory and conflict-safe
# ---------------------------------------------------------------------------


def test_a_missing_idempotency_key_is_refused(db_session):
    """Without a key a retry either duplicates the decision or trips the index."""
    ont = _ont(db_session, "HWTC-NOKEY")
    spec = _spec(ont)
    db_session.commit()
    ctx = CommandContext.system(actor="a@dotmac", scope="ont:test", reason="r")

    with pytest.raises(ReconcileHoldError) as excinfo:
        place_reconcile_hold(db_session, spec=spec, context=ctx)

    assert excinfo.value.code == HoldRefusal.missing_idempotency_key.value


def test_a_reused_key_with_a_different_command_is_a_conflict(db_session):
    """A replay is only a replay when the WHOLE command matches.

    Returning the stored hold for a different reason or reviewer would let a
    reused key silently substitute one decision for another.
    """
    ont = _ont(db_session, "HWTC-CONFLICT")
    first = _spec(ont)
    second = _spec(ont, reason_code="something_else")
    db_session.commit()
    ctx = _ctx(key="hold-shared-key")
    place_reconcile_hold(db_session, spec=first, context=ctx)

    db_session.commit()
    with pytest.raises(ReconcileHoldError) as excinfo:
        place_reconcile_hold(
            db_session, spec=second, context=_ctx(key="hold-shared-key")
        )

    assert excinfo.value.code == HoldRefusal.idempotency_conflict.value


# Every field `_replayable` compares, one case each, as (mutated spec, actor).
# `scope` is absent on purpose: `OntReconcileScope` has a single member, so it
# cannot vary yet -- adding a second one must add a case here.
_ORIGINAL_ACTOR = "operator@dotmac"
_CONFLICT_AXIS = {
    "ont_unit_id": lambda ont, other, due: (
        _spec(other, review_due_at=due),
        _ORIGINAL_ACTOR,
    ),
    "reason_code": lambda ont, other, due: (
        _spec(ont, review_due_at=due, reason_code="hardware_replacement"),
        _ORIGINAL_ACTOR,
    ),
    "explanation": lambda ont, other, due: (
        _spec(ont, review_due_at=due, explanation="A different justification."),
        _ORIGINAL_ACTOR,
    ),
    "reviewer": lambda ont, other, due: (
        _spec(ont, review_due_at=due, reviewer="other-lead@dotmac"),
        _ORIGINAL_ACTOR,
    ),
    "actor": lambda ont, other, due: (
        _spec(ont, review_due_at=due),
        "different-operator@dotmac",
    ),
    "review_due_at": lambda ont, other, due: (
        _spec(ont, review_due_at=due + timedelta(seconds=1)),
        _ORIGINAL_ACTOR,
    ),
}


@pytest.mark.parametrize("field", sorted(_CONFLICT_AXIS))
def test_changing_any_compared_field_turns_a_replay_into_a_conflict(db_session, field):
    """The conflict axis, not just the replay axis.

    A replay contract is only half-proven by showing that an identical command
    replays. Each field the owner compares must also be shown to break the
    replay -- otherwise a reused key could silently substitute one decision for
    another along whichever axis went untested.
    """
    key = f"hold-axis-{field}"
    ont = _ont(db_session, f"HWTC-AXIS-{field}")
    other = _ont(db_session, f"HWTC-AXIS-OTHER-{field}")
    due = datetime.now(UTC) + timedelta(days=7)
    # Both specs are built while the transaction is still open: the owner
    # refuses a session that already has one, and touching `ont.id` after the
    # commit would reopen it.
    original = _spec(ont, review_due_at=due)
    mutated, mutated_actor = _CONFLICT_AXIS[field](ont, other, due)

    db_session.commit()
    place_reconcile_hold(
        db_session, spec=original, context=_ctx(key=key, actor=_ORIGINAL_ACTOR)
    )
    db_session.commit()

    with pytest.raises(ReconcileHoldError) as excinfo:
        place_reconcile_hold(
            db_session, spec=mutated, context=_ctx(key=key, actor=mutated_actor)
        )

    assert excinfo.value.code == HoldRefusal.idempotency_conflict.value


def test_an_identical_command_replays_across_separate_contexts(db_session):
    """Replay must not depend on reusing the same context object.

    The operator adapter builds a fresh `CommandContext` per invocation, so the
    replay path has to survive equal-but-distinct contexts. Pinned because this
    is the shape a real retry takes.
    """
    ont = _ont(db_session, "HWTC-REPLAY-FRESH")
    due = datetime.now(UTC) + timedelta(days=7)
    first_spec = _spec(ont, review_due_at=due)
    second_spec = _spec(ont, review_due_at=due)
    db_session.commit()

    first = place_reconcile_hold(
        db_session, spec=first_spec, context=_ctx(key="hold-fresh-ctx")
    )
    db_session.commit()
    second = place_reconcile_hold(
        db_session, spec=second_spec, context=_ctx(key="hold-fresh-ctx")
    )

    assert first.id == second.id
    assert (
        db_session.query(OntReconcileHold)
        .filter(OntReconcileHold.ont_unit_id == ont.id)
        .count()
        == 1
    )


# ---------------------------------------------------------------------------
# Point-of-use decision, serialised on OntUnit
# ---------------------------------------------------------------------------


def test_eligibility_under_lock_sees_a_hold_placed_after_a_set_snapshot(db_session):
    """The blocker this replaces.

    A pass-level set read cannot see a hold placed mid-pass. The point-of-use
    decision must, or the sweeper acts on state someone has already changed.
    """
    from app.services.network.ont_reconcile_eligibility import (
        eligibility_under_lock,
    )

    ont = _ont(db_session, "HWTC-MIDPASS")
    db_session.commit()
    snapshot = held_ont_ids(db_session)  # taken BEFORE the hold exists
    assert ont.id not in snapshot

    _place(db_session, ont)

    assert eligibility_under_lock(db_session, ont.id).eligible is False, (
        "the point-of-use decision must see a hold the snapshot missed"
    )


def test_an_unknown_ont_is_not_eligible_under_lock(db_session):
    from app.services.network.ont_reconcile_eligibility import (
        eligibility_under_lock,
    )

    db_session.commit()
    assert eligibility_under_lock(db_session, uuid4()).eligible is False


def test_placing_against_an_unknown_ont_is_refused(db_session):
    from app.services.network.ont_reconcile_eligibility import HoldSpec

    db_session.commit()
    spec = HoldSpec(
        ont_unit_id=uuid4(),
        reason_code="x",
        explanation="y",
        reviewer="reviewer@dotmac",
        review_due_at=datetime.now(UTC) + timedelta(days=1),
    )
    with pytest.raises(ReconcileHoldError) as excinfo:
        place_reconcile_hold(db_session, spec=spec, context=_ctx())

    assert excinfo.value.code == HoldRefusal.ont_not_found.value


def test_the_lock_order_is_ont_then_hold(db_session):
    """Guard: reversing it would deadlock placement against release."""
    from pathlib import Path

    source = Path("app/services/network/ont_reconcile_eligibility.py").read_text(
        encoding="utf-8"
    )
    for fn in ("def _place(", "def _release("):
        body = source[source.index(fn) :]
        body = body[: body.index("\ndef ", 1)]
        assert "_lock_ont(" in body, f"{fn} must lock the ONT row"
        ont_at = body.index("_lock_ont(")
        hold_at = body.find("with_for_update()")
        if hold_at != -1:
            assert ont_at < hold_at, f"{fn} must lock OntUnit BEFORE the hold"


def test_the_sweeper_decides_at_the_point_of_use(db_session):
    """The set read is an optimisation; the decision is per ONT under lock."""
    from pathlib import Path

    source = Path("app/services/network/reconcile/sweeper.py").read_text(
        encoding="utf-8"
    )
    body = source[source.index("def _sweep_one(") :]
    body = body[: body.index("\ndef ", 1)]
    assert "eligibility_under_lock(" in body
    # ...and it happens before any device contact.
    assert body.index("eligibility_under_lock(") < body.index("is_pingable(")


# ---------------------------------------------------------------------------
# SweepDisposition must behave like an enum
# ---------------------------------------------------------------------------


def test_sweep_dispositions_are_distinct_and_hashable():
    """Regression: a stray @dataclass on the enum made every member equal.

    `@dataclass` generates `__eq__` from fields -- of which an enum has none --
    so `held == unreachable` was True and members were unhashable. Production
    code survived only because it compares with `is`; any `==` or set
    membership would have silently erased the held/unreachable distinction
    that this disposition exists to make.
    """
    from app.services.network.reconcile.sweeper import SweepDisposition

    members = list(SweepDisposition)
    assert len({m for m in members}) == len(members), "members must be hashable"
    assert SweepDisposition.held != SweepDisposition.unreachable
    assert SweepDisposition.held != SweepDisposition.reconciled
    assert SweepDisposition.held == SweepDisposition.held

    # Normal StrEnum serialization: the value, not a dataclass repr.
    assert str(SweepDisposition.held) == "held"
    assert f"{SweepDisposition.unreachable}" == "unreachable"
    assert SweepDisposition.held.value == "held"
    assert {SweepDisposition.held: 1}[SweepDisposition.held] == 1


def test_the_disposition_enum_is_not_a_dataclass():
    """Guard the exact defect, not just its symptom."""
    import dataclasses

    from app.services.network.reconcile.sweeper import SweepDisposition

    assert not dataclasses.is_dataclass(SweepDisposition)
