from __future__ import annotations

import inspect
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.ont_assignment_cutover import (
    OntAssignmentCutoverProposalBatch,
    OntAssignmentCutoverVerificationAttestation,
)
from app.models.ont_assignment_identity import OntAssignmentIdentityDecision
from app.services.network import ont_assignment_cutover_coverage as owner
from app.services.network.ont_assignment_cutover import audit_ont_assignment_cutover
from app.services.network.ont_assignment_cutover_batches import (
    propose_ont_assignment_cutover_batch,
    review_ont_assignment_cutover_batch,
)
from app.services.network.ont_assignment_cutover_coverage import (
    reconcile_ont_assignment_cutover_coverage,
)
from app.services.network.ont_assignment_cutover_verification import (
    attest_ont_assignment_cutover_verification,
    preview_ont_assignment_cutover_verification,
)
from app.services.network.ont_assignment_identity import (
    execute_assignment_identity_repair,
)
from app.web.admin import network_fiber_plant


def _drift_assignment(db_session) -> OntAssignment:
    suffix = uuid.uuid4().hex[:10]
    olt = OLTDevice(
        name=f"Coverage OLT {suffix}",
        hostname=f"coverage-{suffix}.example.test",
        is_active=True,
    )
    db_session.add(olt)
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name="0/1/1", is_active=True)
    db_session.add(pon)
    db_session.flush()
    ont = OntUnit(
        serial_number=f"COVERAGE-{suffix}",
        olt_device_id=olt.id,
        pon_port_id=pon.id,
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=pon.id,
        assigned_at=datetime.now(UTC),
        active=True,
    )
    db_session.add(assignment)
    db_session.commit()
    return assignment


def _propose(db_session, assignment, *, suffix: str = "one"):
    report = audit_ont_assignment_cutover(db_session)
    finding = next(
        finding for finding in report.findings if finding.assignment_id == assignment.id
    )
    proposal = propose_ont_assignment_cutover_batch(
        db_session,
        expected_report_sha256=report.report_sha256,
        items=[
            {
                "action": "deactivate",
                "assignment_id": str(assignment.id),
                "duplicate_assignment_ids": [],
                "finding_sha256": finding.input_sha256,
                "reason": f"Exact stale assignment evidence {suffix}",
            }
        ],
        proposed_by=f"planner-{suffix}@example.com",
        reason=f"Coverage reconciliation fixture {suffix}",
        source_name="phase-14-test",
    )
    return proposal


def _review(db_session, proposal, *, action: str = "approve"):
    return review_ont_assignment_cutover_batch(
        db_session,
        proposal.batch.id,
        expected_manifest_sha256=proposal.batch.manifest_sha256,
        action=action,
        reviewed_by="reviewer@example.com",
        review_notes="Immutable proposal evidence independently reviewed",
    )


def _attest(db_session, proposal):
    preview = preview_ont_assignment_cutover_verification(
        db_session,
        proposal.batch.id,
        expected_manifest_sha256=proposal.batch.manifest_sha256,
        verified_by="verifier@example.com",
        verification_notes="Current audit and terminal results checked",
    )
    return attest_ont_assignment_cutover_verification(
        db_session,
        proposal.batch.id,
        expected_manifest_sha256=proposal.batch.manifest_sha256,
        expected_evidence_sha256=preview.evidence_sha256,
        verified_by="verifier@example.com",
        verification_notes="Current audit and terminal results checked",
    )


def test_current_finding_without_lineage_is_unassigned_and_read_only(db_session):
    assignment = _drift_assignment(db_session)
    before = (
        db_session.query(OntAssignmentCutoverProposalBatch).count(),
        db_session.query(OntAssignmentIdentityDecision).count(),
        db_session.query(OntAssignmentCutoverVerificationAttestation).count(),
    )

    report = reconcile_ont_assignment_cutover_coverage(db_session)
    replay = reconcile_ont_assignment_cutover_coverage(db_session)

    finding = next(
        row
        for row in report.current_findings
        if row["assignment_id"] == str(assignment.id)
    )
    assert finding["coverage_state"] == "unassigned"
    assert report.coverage_counts["unassigned"] == 1
    assert report.ready_for_constraint_authorization_review is False
    assert replay.coverage_report_sha256 == report.coverage_report_sha256
    assert before == (
        db_session.query(OntAssignmentCutoverProposalBatch).count(),
        db_session.query(OntAssignmentIdentityDecision).count(),
        db_session.query(OntAssignmentCutoverVerificationAttestation).count(),
    )


def test_exact_lineage_distinguishes_review_and_execution_state(db_session):
    assignment = _drift_assignment(db_session)
    proposal = _propose(db_session, assignment)

    proposed = reconcile_ont_assignment_cutover_coverage(db_session)
    assert proposed.current_findings[0]["coverage_state"] == "exact_pending_review"

    _review(db_session, proposal)
    approved = reconcile_ont_assignment_cutover_coverage(db_session)
    assert approved.current_findings[0]["coverage_state"] == "exact_pending_execution"
    assert approved.decision_counts["pending"] == 1


def test_changed_finding_is_superseded_evidence_not_exact_coverage(db_session):
    assignment = _drift_assignment(db_session)
    _propose(db_session, assignment)
    assignment.assigned_at = None
    db_session.commit()

    report = reconcile_ont_assignment_cutover_coverage(db_session)

    assert report.current_findings[0]["coverage_state"] == "superseded_evidence"
    assert report.current_findings[0]["exact_lineage_ids"] == []
    assert len(report.current_findings[0]["scope_lineage_ids"]) == 1


def test_multiple_exact_historical_lineages_are_ambiguous(db_session):
    assignment = _drift_assignment(db_session)
    first = _propose(db_session, assignment, suffix="first")
    _review(db_session, first, action="decline")
    _propose(db_session, assignment, suffix="second")

    report = reconcile_ont_assignment_cutover_coverage(db_session)

    assert (
        report.current_findings[0]["coverage_state"] == "ambiguous_overlapping_coverage"
    )
    assert len(report.current_findings[0]["exact_lineage_ids"]) == 2


def test_applied_clean_verified_batch_satisfies_conservative_gates(db_session):
    assignment = _drift_assignment(db_session)
    proposal = _propose(db_session, assignment)
    reviewed = _review(db_session, proposal)
    execute_assignment_identity_repair(
        db_session,
        reviewed.decisions[0].id,
        executed_by="executor@example.com",
    )
    _attest(db_session, proposal)

    report = reconcile_ont_assignment_cutover_coverage(db_session)

    assert report.current_findings == ()
    assert report.lineages[0]["scope_state"] == "clean"
    assert report.lineages[0]["verification_state"] == "current"
    assert report.batch_verification_counts["current"] == 1
    assert report.ready_for_constraint_authorization_review is True
    assert all(gate["ready"] is True for gate in report.gates)


def test_later_assignment_drift_supersedes_verification_without_rewriting_it(
    db_session,
):
    assignment = _drift_assignment(db_session)
    proposal = _propose(db_session, assignment)
    reviewed = _review(db_session, proposal)
    execute_assignment_identity_repair(
        db_session,
        reviewed.decisions[0].id,
        executed_by="executor@example.com",
    )
    attestation = _attest(db_session, proposal).attestation
    assignment.active = True
    db_session.commit()

    report = reconcile_ont_assignment_cutover_coverage(db_session)

    assert report.lineages[0]["scope_state"] == "residual"
    assert report.lineages[0]["verification_state"] == "superseded_report"
    assert report.current_findings[0]["coverage_state"] == "superseded_evidence"
    assert report.ready_for_constraint_authorization_review is False
    db_session.refresh(attestation)
    assert attestation.fresh_report_sha256 != report.cutover_report_sha256


def test_changed_terminal_result_is_decision_drift_and_result_blocker(db_session):
    assignment = _drift_assignment(db_session)
    proposal = _propose(db_session, assignment)
    reviewed = _review(db_session, proposal)
    decision = execute_assignment_identity_repair(
        db_session,
        reviewed.decisions[0].id,
        executed_by="executor@example.com",
    )
    _attest(db_session, proposal)
    decision.result_payload = {**decision.result_payload, "tampered": True}
    db_session.commit()

    report = reconcile_ont_assignment_cutover_coverage(db_session)

    assert report.lineages[0]["verification_state"] == "decision_drift"
    assert report.batch_verification_counts["decision_drift"] == 1
    assert report.decision_result_blockers[0]["code"] == (
        "terminal_decision_result_digest_mismatch"
    )
    assert report.ready_for_constraint_authorization_review is False


def test_coverage_interface_has_no_mutation_or_constraint_mode(monkeypatch):
    from scripts.network import audit_ont_assignment_cutover_coverage as command

    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_ont_assignment_cutover_coverage.py", "--compact"],
    )
    args = command.parse_args()
    source = inspect.getsource(owner)
    command_source = inspect.getsource(command)

    assert args.compact is True
    assert "execute_assignment_identity_repair" not in source
    assert "OntAssignment(" not in source
    assert "def execute_" not in source
    assert 'add_parser("execute"' not in command_source
    assert 'add_parser("apply"' not in command_source
    assert "enable_constraint" not in source
    assert "read_only_snapshot_session()" in command_source


def test_admin_route_and_template_expose_read_only_coverage_projection():
    paths = {route.path: route for route in network_fiber_plant.router.routes}
    route = paths["/network/ont-assignment-cutover-coverage"]
    template = Path(
        "templates/admin/network/fiber/ont_assignment_cutover_coverage.html"
    )

    assert route.methods == {"GET"}
    assert template.exists()
    content = template.read_text()
    assert "necessary evidence for a separate authorization review" in content
    assert "cannot execute repairs or enable constraints" in content
