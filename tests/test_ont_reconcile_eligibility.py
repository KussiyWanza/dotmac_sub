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
    OntReconcileAdmission,
    OntReconcileAdmissionStatus,
    OntReconcileHold,
    OntReconcileHoldStatus,
    OntReconcileScope,
    OntUnit,
)
from app.services.domain_errors import DomainError
from app.services.network.ont_reconcile_eligibility import (
    AdmissionRefusal,
    AdmissionSpec,
    EligibilityRefusal,
    HoldRefusal,
    HoldSpec,
    OverdueHoldAlertSeverity,
    ReconcileAdmissionError,
    ReconcileHoldError,
    admit_reconcile_cohort_member,
    admitted_ont_ids,
    held_ont_ids,
    overdue_hold_alerts,
    overdue_holds,
    place_reconcile_hold,
    reconcile_eligibility,
    release_reconcile_hold,
    revoke_reconcile_admission,
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


def _admission_spec(ont, **kw):
    return AdmissionSpec(
        ont_unit_id=ont.id,
        cohort_key=kw.pop("cohort_key", "cohort-1-verified"),
        reason_code=kw.pop("reason_code", "initial_verified_rollout"),
        explanation=kw.pop(
            "explanation", "Canonical PON identity and sentinel review passed."
        ),
        reviewer=kw.pop("reviewer", "reviewer@dotmac"),
        expires_at=kw.pop("expires_at", datetime.now(UTC) + timedelta(days=7)),
        **kw,
    )


def _admit(db, ont, **kw):
    context = kw.pop("context", _ctx(key=f"admit-test-{next(_KEY_SEQ)}"))
    spec = _admission_spec(ont, **kw)
    db.commit()
    return admit_reconcile_cohort_member(db, spec=spec, context=context)


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


def test_an_unadmitted_ont_fails_closed(db_session):
    ont = _ont(db_session)
    db_session.commit()

    verdict = reconcile_eligibility(db_session, ont.id)

    assert verdict.eligible is False
    assert verdict.held is False
    assert verdict.refusal is EligibilityRefusal.not_admitted


def test_a_reviewed_unheld_admission_is_eligible(db_session):
    ont = _ont(db_session)
    admission = _admit(db_session, ont)

    verdict = reconcile_eligibility(db_session, ont.id)

    assert verdict.eligible is True
    assert verdict.held is False
    assert verdict.admitted is True
    assert verdict.admission_id == str(admission.id)
    assert verdict.cohort_key == "cohort-1-verified"


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
    _admit(db_session, ont)
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
# Positive cohort admission
# ---------------------------------------------------------------------------


def test_admission_requires_distinct_review(db_session):
    ont = _ont(db_session, "HWTC-ADMIT-REVIEW")
    spec = _admission_spec(ont, reviewer="operator@dotmac")
    db_session.commit()

    with pytest.raises(ReconcileAdmissionError) as excinfo:
        admit_reconcile_cohort_member(
            db_session,
            spec=spec,
            context=_ctx(actor="operator@dotmac", key="admit-self-review"),
        )

    assert excinfo.value.code == AdmissionRefusal.reviewer_is_actor.value


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("cohort_key", " ", AdmissionRefusal.missing_cohort),
        ("reason_code", "", AdmissionRefusal.missing_reason),
        ("explanation", "", AdmissionRefusal.missing_explanation),
        ("reviewer", "", AdmissionRefusal.missing_reviewer),
    ],
)
def test_admission_requires_named_evidence(db_session, field, value, code):
    ont = _ont(db_session, f"HWTC-ADMIT-{field}")
    spec = _admission_spec(ont, **{field: value})
    db_session.commit()

    with pytest.raises(ReconcileAdmissionError) as excinfo:
        admit_reconcile_cohort_member(
            db_session,
            spec=spec,
            context=_ctx(key=f"admit-missing-{field}"),
        )

    assert excinfo.value.code == code.value


def test_admission_requires_a_future_absolute_expiry(db_session):
    ont = _ont(db_session, "HWTC-ADMIT-EXPIRY")
    naive = _admission_spec(
        ont, expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
    )
    expired = _admission_spec(ont, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    db_session.commit()
    with pytest.raises(ReconcileAdmissionError) as excinfo:
        admit_reconcile_cohort_member(
            db_session, spec=naive, context=_ctx(key="admit-naive-expiry")
        )
    assert excinfo.value.code == AdmissionRefusal.expiry_missing_timezone.value

    with pytest.raises(ReconcileAdmissionError) as excinfo:
        admit_reconcile_cohort_member(
            db_session, spec=expired, context=_ctx(key="admit-past-expiry")
        )
    assert excinfo.value.code == AdmissionRefusal.expiry_in_past.value


def test_expiry_removes_authority_without_a_writer(db_session):
    ont = _ont(db_session, "HWTC-ADMIT-ELAPSED")
    admission = _admit(db_session, ont)
    admission.admitted_at = datetime.now(UTC) - timedelta(days=2)
    admission.expires_at = datetime.now(UTC) - timedelta(days=1)
    db_session.flush()

    verdict = reconcile_eligibility(db_session, ont.id)

    assert verdict.eligible is False
    assert verdict.refusal is EligibilityRefusal.not_admitted
    assert ont.id not in admitted_ont_ids(db_session)


def test_revocation_removes_authority(db_session):
    ont = _ont(db_session, "HWTC-ADMIT-REVOKED")
    admission = _admit(db_session, ont)
    admission_id = admission.id
    ont_id = ont.id
    db_session.commit()

    revoked = revoke_reconcile_admission(
        db_session,
        admission_id=admission_id,
        context=_ctx(reason="acceptance paused", key="admit-revoke-unused"),
    )

    assert revoked.status is OntReconcileAdmissionStatus.revoked
    assert reconcile_eligibility(db_session, ont_id).eligible is False


def test_admission_replay_is_idempotent_and_changed_input_conflicts(db_session):
    ont = _ont(db_session, "HWTC-ADMIT-REPLAY")
    expires_at = datetime.now(UTC) + timedelta(days=7)
    spec = _admission_spec(ont, expires_at=expires_at)
    changed = _admission_spec(ont, expires_at=expires_at, cohort_key="different-cohort")
    key = "admit-replay-one"
    db_session.commit()

    first = admit_reconcile_cohort_member(db_session, spec=spec, context=_ctx(key=key))
    second = admit_reconcile_cohort_member(db_session, spec=spec, context=_ctx(key=key))
    assert first.id == second.id
    # Reading an ORM result after the owner commits may refresh the expired
    # instance and open a caller transaction. Commands require a clean entry.
    db_session.commit()

    with pytest.raises(ReconcileAdmissionError) as excinfo:
        admit_reconcile_cohort_member(db_session, spec=changed, context=_ctx(key=key))
    assert excinfo.value.code == AdmissionRefusal.idempotency_conflict.value


def test_an_expired_admission_can_be_renewed_with_reviewed_history(db_session):
    ont = _ont(db_session, "HWTC-ADMIT-RENEW")
    first = _admit(db_session, ont)
    first.admitted_at = datetime.now(UTC) - timedelta(days=2)
    first.expires_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()

    second = _admit(
        db_session,
        ont,
        cohort_key="cohort-2-verified",
        context=_ctx(key="admit-renewed"),
    )

    db_session.refresh(first)
    assert first.status is OntReconcileAdmissionStatus.expired
    assert second.status is OntReconcileAdmissionStatus.active
    assert first.id != second.id
    assert reconcile_eligibility(db_session, ont.id).admission_id == str(second.id)


def test_a_second_effective_admission_is_refused(db_session):
    ont = _ont(db_session, "HWTC-ADMIT-DUPLICATE")
    first = _admission_spec(ont)
    second = _admission_spec(ont, cohort_key="other-cohort")
    db_session.commit()
    admit_reconcile_cohort_member(
        db_session, spec=first, context=_ctx(key="admit-active-first")
    )

    with pytest.raises(ReconcileAdmissionError) as excinfo:
        admit_reconcile_cohort_member(
            db_session, spec=second, context=_ctx(key="admit-active-second")
        )

    assert excinfo.value.code == AdmissionRefusal.already_active.value


def test_admission_requires_an_idempotency_key(db_session):
    ont = _ont(db_session, "HWTC-ADMIT-NOKEY")
    spec = _admission_spec(ont)
    db_session.commit()

    with pytest.raises(ReconcileAdmissionError) as excinfo:
        admit_reconcile_cohort_member(
            db_session,
            spec=spec,
            context=CommandContext.system(
                actor="operator@dotmac", scope="ont:test", reason="reviewed"
            ),
        )

    assert excinfo.value.code == AdmissionRefusal.missing_idempotency_key.value


def test_the_active_admission_index_is_partial(db_session):
    indexes = {index.name: index for index in OntReconcileAdmission.__table__.indexes}
    index = indexes["uq_ont_reconcile_admissions_active_per_ont_scope"]

    assert index.unique is True
    assert "status = 'active'" in str(index.dialect_options["postgresql"]["where"])
    assert "status = 'active'" in str(index.dialect_options["sqlite"]["where"])


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

    alerts = overdue_hold_alerts(db_session)
    assert len(alerts) == 1
    assert alerts[0].severity is OverdueHoldAlertSeverity.critical
    assert alerts[0].target_url == f"/admin/network/onts/{ont.id}"


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
    for forbidden in ("hold.expires_at", "auto_release", "timedelta(", "celery"):
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


def test_point_of_use_refuses_unadmitted_ont_before_ping(db_session):
    from app.services.network.reconcile.sweeper import SweepDisposition, _sweep_one

    ont = _ont(db_session, serial="HWTC-NOT-ADMITTED-SWEEP")
    ont_id = ont.id
    db_session.commit()
    pings: list[str] = []

    outcome = _sweep_one(
        db_session,
        ont_id,
        timeout_sec=1,
        ping_function=lambda ip, count, timeout_sec: pings.append(ip) or True,
        reconcile_fn=lambda *args, **kwargs: pytest.fail("must not reconcile"),
    )

    assert outcome.disposition is SweepDisposition.not_admitted
    assert pings == []


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
    _admit(db_session, ont)
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
    assert stats.not_admitted == 0
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
    _admit(db_session, ont)
    snapshot = held_ont_ids(db_session)  # taken BEFORE the hold exists
    assert ont.id not in snapshot

    _place(db_session, ont)

    assert eligibility_under_lock(db_session, ont.id).eligible is False, (
        "the point-of-use decision must see a hold the snapshot missed"
    )


def test_eligibility_under_lock_sees_revocation_after_admission_snapshot(db_session):
    from app.services.network.ont_reconcile_eligibility import (
        eligibility_under_lock,
    )

    ont = _ont(db_session, "HWTC-ADMISSION-MIDPASS")
    admission = _admit(db_session, ont)
    ont_id = ont.id
    admission_id = admission.id
    snapshot = admitted_ont_ids(db_session)
    assert ont_id in snapshot
    db_session.commit()

    revoke_reconcile_admission(
        db_session,
        admission_id=admission_id,
        context=_ctx(reason="cohort paused", key="admit-revoke-midpass"),
    )

    verdict = eligibility_under_lock(db_session, ont_id)
    assert verdict.eligible is False
    assert verdict.refusal is EligibilityRefusal.not_admitted


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
    for fn in (
        "def _place(",
        "def _release(",
        "def _admit(",
        "def _revoke_admission(",
    ):
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
    assert SweepDisposition.not_admitted != SweepDisposition.held
    assert SweepDisposition.not_admitted != SweepDisposition.unreachable
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
