"""Behavior tests for the `ActionReadiness` shared contract."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.schemas.action_readiness import ActionReadinessRead
from app.services.action_readiness import (
    ActionableBlocker,
    ActionCorrelation,
    ActionReadiness,
    BlockerEvidence,
    NextAction,
    OperationReference,
    ReadinessImpact,
    ReadinessState,
    RepairAction,
)

# A real registered, decision-making SOT owner used across these tests.
OWNER = "financial.payment_proofs"
# A real registered but pure-vocabulary (TransactionMode.NOT_APPLICABLE) owner.
PURE_VOCABULARY_OWNER = "ui.projection_contracts"

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _evidence(summary: str = "Bank statement mismatch") -> BlockerEvidence:
    return BlockerEvidence(summary=summary, observed_at=NOW, detail_url="/admin/x")


def _blocker(
    code: str = "amount_mismatch",
    *,
    owner: str = OWNER,
    impact: ReadinessImpact = ReadinessImpact.blocking,
) -> ActionableBlocker:
    return ActionableBlocker(
        code=code,
        owner=owner,
        customer_message="We could not verify this transfer yet.",
        staff_detail="Bank statement amount does not match the claimed amount.",
        evidence=_evidence(),
        impact=impact,
    )


# ── BlockerEvidence ──────────────────────────────────────────────────────


def test_blocker_evidence_requires_non_blank_summary() -> None:
    with pytest.raises(ValueError, match="summary"):
        BlockerEvidence(summary="   ")


def test_blocker_evidence_accepts_valid_summary() -> None:
    assert BlockerEvidence(summary="ok").summary == "ok"


def test_blocker_evidence_rejects_naive_observed_at() -> None:
    with pytest.raises(ValueError, match="observed_at"):
        BlockerEvidence(summary="ok", observed_at=datetime(2026, 1, 1))


def test_blocker_evidence_accepts_tz_aware_observed_at() -> None:
    BlockerEvidence(summary="ok", observed_at=NOW)


def test_blocker_evidence_rejects_non_relative_detail_url() -> None:
    with pytest.raises(ValueError, match="detail_url"):
        BlockerEvidence(summary="ok", detail_url="https://example.com/x")


def test_blocker_evidence_accepts_relative_detail_url() -> None:
    BlockerEvidence(summary="ok", detail_url="/admin/x")


# ── RepairAction ─────────────────────────────────────────────────────────


def test_repair_action_requires_registered_real_owner() -> None:
    with pytest.raises(ValueError, match="registered SOT service"):
        RepairAction(
            key="retry",
            label="Retry",
            owner="totally.unregistered.nonsense",
            runner="app.services.payment_proofs.review_eligibility",
        )


def test_repair_action_accepts_registered_owner() -> None:
    action = RepairAction(
        key="retry",
        label="Retry",
        owner=OWNER,
        runner="app.services.payment_proofs.review_eligibility",
        action_url="/admin/x",
    )
    assert action.owner == OWNER


def test_repair_action_rejects_non_relative_url() -> None:
    with pytest.raises(ValueError, match="action_url"):
        RepairAction(
            key="retry",
            label="Retry",
            owner=OWNER,
            runner="app.services.payment_proofs.review_eligibility",
            action_url="https://example.com",
        )


# ── ActionableBlocker ────────────────────────────────────────────────────


def test_actionable_blocker_requires_registered_real_owner() -> None:
    with pytest.raises(ValueError, match="registered SOT service"):
        _blocker(owner="totally.unregistered.nonsense")


def test_actionable_blocker_rejects_pure_vocabulary_owner() -> None:
    with pytest.raises(ValueError, match="pure-vocabulary"):
        _blocker(owner=PURE_VOCABULARY_OWNER)


def test_actionable_blocker_accepts_real_decision_owner() -> None:
    blocker = _blocker()
    assert blocker.owner == OWNER


def test_actionable_blocker_requires_non_blank_messages() -> None:
    with pytest.raises(ValueError, match="customer_message"):
        ActionableBlocker(
            code="x",
            owner=OWNER,
            customer_message="  ",
            staff_detail="detail",
            evidence=_evidence(),
        )


# ── NextAction ───────────────────────────────────────────────────────────


def test_next_action_requires_registered_real_owner() -> None:
    with pytest.raises(ValueError, match="registered SOT service"):
        NextAction(key="retry", label="Retry", owner="nonsense.owner")


def test_next_action_accepts_registered_owner() -> None:
    NextAction(key="retry", label="Retry", owner=OWNER, url="/admin/x")


def test_next_action_rejects_non_relative_url() -> None:
    with pytest.raises(ValueError, match="url"):
        NextAction(key="retry", label="Retry", owner=OWNER, url="not-relative")


# ── ActionReadiness invariants ───────────────────────────────────────────


def test_ready_state_rejects_blocking_blocker() -> None:
    with pytest.raises(ValueError, match="zero"):
        ActionReadiness(
            action_key="k",
            subject_type="payment_proof",
            subject_id="1",
            owner=OWNER,
            state=ReadinessState.ready,
            evaluated_at=NOW,
            blockers=(_blocker(),),
        )


def test_ready_state_accepts_zero_blocking_blockers() -> None:
    ActionReadiness(
        action_key="k",
        subject_type="payment_proof",
        subject_id="1",
        owner=OWNER,
        state=ReadinessState.ready,
        evaluated_at=NOW,
    )


def test_ready_state_accepts_advisory_only_blockers() -> None:
    ActionReadiness(
        action_key="k",
        subject_type="payment_proof",
        subject_id="1",
        owner=OWNER,
        state=ReadinessState.ready,
        evaluated_at=NOW,
        blockers=(_blocker(impact=ReadinessImpact.advisory),),
    )


def test_blocked_state_requires_at_least_one_blocking_blocker() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ActionReadiness(
            action_key="k",
            subject_type="payment_proof",
            subject_id="1",
            owner=OWNER,
            state=ReadinessState.blocked,
            evaluated_at=NOW,
        )


def test_blocked_state_accepts_a_blocking_blocker() -> None:
    readiness = ActionReadiness(
        action_key="k",
        subject_type="payment_proof",
        subject_id="1",
        owner=OWNER,
        state=ReadinessState.blocked,
        evaluated_at=NOW,
        blockers=(_blocker(),),
    )
    assert readiness.is_ready is False


def test_blocker_codes_must_be_unique() -> None:
    with pytest.raises(ValueError, match="unique"):
        ActionReadiness(
            action_key="k",
            subject_type="payment_proof",
            subject_id="1",
            owner=OWNER,
            state=ReadinessState.blocked,
            evaluated_at=NOW,
            blockers=(_blocker("dup"), _blocker("dup")),
        )


def test_next_action_clears_blocker_code_must_be_declared() -> None:
    with pytest.raises(ValueError, match="clears_blocker_code"):
        ActionReadiness(
            action_key="k",
            subject_type="payment_proof",
            subject_id="1",
            owner=OWNER,
            state=ReadinessState.blocked,
            evaluated_at=NOW,
            blockers=(_blocker("real_code"),),
            next_actions=(
                NextAction(
                    key="x",
                    label="x",
                    owner=OWNER,
                    clears_blocker_code="wrong_code",
                ),
            ),
        )


def test_next_action_clears_blocker_code_accepts_declared_code() -> None:
    ActionReadiness(
        action_key="k",
        subject_type="payment_proof",
        subject_id="1",
        owner=OWNER,
        state=ReadinessState.blocked,
        evaluated_at=NOW,
        blockers=(_blocker("real_code"),),
        next_actions=(
            NextAction(
                key="x",
                label="x",
                owner=OWNER,
                clears_blocker_code="real_code",
            ),
        ),
    )


def test_evaluated_at_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="evaluated_at"):
        ActionReadiness(
            action_key="k",
            subject_type="payment_proof",
            subject_id="1",
            owner=OWNER,
            state=ReadinessState.ready,
            evaluated_at=datetime(2026, 1, 1),
        )


def test_action_readiness_rejects_registered_pure_vocabulary_owner() -> None:
    with pytest.raises(ValueError, match="pure-vocabulary"):
        ActionReadiness(
            action_key="k",
            subject_type="payment_proof",
            subject_id="1",
            owner=PURE_VOCABULARY_OWNER,
            state=ReadinessState.ready,
            evaluated_at=NOW,
        )


def test_primary_blocker_and_advisories_properties() -> None:
    blocking = _blocker("blocking_one")
    advisory = _blocker("advisory_one", impact=ReadinessImpact.advisory)
    readiness = ActionReadiness(
        action_key="k",
        subject_type="payment_proof",
        subject_id="1",
        owner=OWNER,
        state=ReadinessState.blocked,
        evaluated_at=NOW,
        blockers=(blocking, advisory),
    )
    assert readiness.primary_blocker is blocking
    assert readiness.advisories == (advisory,)
    assert readiness.blocking_blockers == (blocking,)


def test_primary_blocker_is_none_when_ready() -> None:
    readiness = ActionReadiness(
        action_key="k",
        subject_type="payment_proof",
        subject_id="1",
        owner=OWNER,
        state=ReadinessState.ready,
        evaluated_at=NOW,
    )
    assert readiness.primary_blocker is None
    assert readiness.is_ready is True


# ── Correlation field-name parity with CommandContext ───────────────────


def test_action_correlation_field_names_match_command_context() -> None:
    """`ActionCorrelation` must byte-match `CommandContext`'s correlation fields.

    Only the test file imports the heavy `owner_commands` module (it pulls in
    SQLAlchemy); `action_readiness.py` itself never does.
    """

    from app.services.owner_commands import CommandContext

    shared = {"correlation_id", "causation_id", "idempotency_key"}
    correlation_fields = {f.name for f in dataclasses.fields(ActionCorrelation)}
    command_context_fields = {f.name for f in dataclasses.fields(CommandContext)}

    assert shared <= correlation_fields
    assert shared <= command_context_fields


def test_action_correlation_constructs_with_operation_reference() -> None:
    correlation = ActionCorrelation(
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="idem-1",
        workflow_key="wf-1",
        operation_reference=OperationReference(
            kind="payment_proof_review",
            id=uuid4(),
            status="submitted",
            detail_url="/admin/x",
        ),
    )
    assert correlation.operation_reference is not None


# ── readiness_panel() ─────────────────────────────────────────────────────


def _readiness_with_blocker() -> ActionReadiness:
    return ActionReadiness(
        action_key="k",
        subject_type="payment_proof",
        subject_id="1",
        owner=OWNER,
        state=ReadinessState.blocked,
        evaluated_at=NOW,
        blockers=(_blocker(),),
        correlation=ActionCorrelation(correlation_id=uuid4()),
    )


def test_readiness_panel_selects_customer_message_for_customer_audience() -> None:
    from app.services.web_action_readiness import readiness_panel

    panel = readiness_panel(_readiness_with_blocker(), audience="customer")
    assert panel.headline == "We could not verify this transfer yet."
    assert panel.blockers[0].detail is None
    assert panel.show_correlation is False


def test_readiness_panel_selects_staff_detail_for_staff_audience() -> None:
    from app.services.web_action_readiness import readiness_panel

    panel = readiness_panel(_readiness_with_blocker(), audience="staff")
    assert "Bank statement amount" in panel.headline
    assert panel.blockers[0].detail == panel.headline
    assert panel.show_correlation is True


def test_readiness_panel_tone_maps_from_state() -> None:
    from app.schemas.status_presentation import StatusTone
    from app.services.web_action_readiness import readiness_panel

    ready = ActionReadiness(
        action_key="k",
        subject_type="payment_proof",
        subject_id="1",
        owner=OWNER,
        state=ReadinessState.ready,
        evaluated_at=NOW,
    )
    panel = readiness_panel(ready)
    assert panel.presentation.tone == StatusTone.positive

    blocked_panel = readiness_panel(_readiness_with_blocker())
    assert blocked_panel.presentation.tone == StatusTone.negative


# ── ActionReadinessRead.from_projection() ─────────────────────────────────


def test_from_projection_round_trips_with_staff_detail() -> None:
    read = ActionReadinessRead.from_projection(
        _readiness_with_blocker(), include_staff_detail=True
    )
    assert (
        read.blockers[0].staff_detail
        == "Bank statement amount does not match the claimed amount."
    )
    assert read.state == ReadinessState.blocked


def test_from_projection_nulls_staff_detail_when_excluded() -> None:
    read = ActionReadinessRead.from_projection(
        _readiness_with_blocker(), include_staff_detail=False
    )
    assert read.blockers[0].staff_detail is None
    assert read.blockers[0].customer_message == "We could not verify this transfer yet."


def test_runner_never_appears_in_repair_action_schema() -> None:
    from app.schemas.action_readiness import RepairActionRead

    assert "runner" not in RepairActionRead.model_fields
    schema = RepairActionRead.model_json_schema()
    assert "runner" not in schema.get("properties", {})
