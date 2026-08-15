from __future__ import annotations

import csv
import json
import stat
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services import party_identity_audit as identity_audit
from app.services.party_identity_adjudication import (
    PartyAdjudicationAction,
    PartyAdjudicationError,
    PartyIdentityDecision,
    build_party_backfill_plan,
)
from scripts.migration.plan_subscriber_party_backfill import (
    DecisionFileError,
    load_decisions,
    write_decision_template,
    write_plan_artifacts,
)

_PLANNED_AT = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


def _facts(**overrides) -> identity_audit.SubscriberIdentityFacts:
    values = {
        "subscriber_id": uuid.uuid4(),
        "first_name": "Ada",
        "last_name": "Okafor",
        "email": f"ada-{uuid.uuid4().hex}@realbusiness.ng",
        "phone": "+2348012345678",
        "has_active_subscription": True,
        "has_any_subscription": True,
    }
    values.update(overrides)
    return identity_audit.SubscriberIdentityFacts(**values)


def _audit(*facts, generated_at=_PLANNED_AT - timedelta(hours=1)):
    return identity_audit.resolve_subscriber_identity_audit(
        tuple(facts), generated_at=generated_at
    )


def _decision(
    audit,
    row,
    *,
    planned_party_id=None,
    identity_source_subscriber_id=None,
    action=PartyAdjudicationAction.create_active_party.value,
    party_type="person",
    data_classification="production",
    display_name_source="subscriber_full_name",
    reason="Reviewed against protected identity evidence",
):
    return PartyIdentityDecision(
        decision_id=uuid.uuid4(),
        subscriber_id=row.subscriber_id,
        audit_digest=identity_audit.subscriber_identity_audit_digest(audit),
        row_fingerprint=identity_audit.subscriber_audit_row_fingerprint(row),
        action=action,
        planned_party_id=planned_party_id,
        identity_source_subscriber_id=identity_source_subscriber_id,
        party_type=party_type,
        data_classification=data_classification,
        display_name_source=display_name_source,
        reviewer="identity-reviewer-1",
        reviewed_at=_PLANNED_AT - timedelta(minutes=30),
        reason=reason,
    )


def test_audit_digest_is_state_bound_but_not_timestamp_bound():
    facts = (_facts(), _facts(first_name="Bola", last_name="Adeyemi"))
    first = _audit(*facts, generated_at=_PLANNED_AT - timedelta(days=1))
    second = _audit(*facts, generated_at=_PLANNED_AT)
    changed = _audit(
        replace(facts[0], has_active_subscription=False),
        facts[1],
        generated_at=_PLANNED_AT,
    )

    assert identity_audit.subscriber_identity_audit_digest(first) == (
        identity_audit.subscriber_identity_audit_digest(second)
    )
    assert identity_audit.subscriber_identity_audit_digest(first) != (
        identity_audit.subscriber_identity_audit_digest(changed)
    )


def test_explicit_duplicate_group_decisions_can_plan_one_party_for_many_accounts():
    first = _facts()
    second = _facts(email="second@realbusiness.ng")
    audit = _audit(first, second)
    rows = {row.subscriber_id: row for row in audit.rows}
    planned_party_id = uuid.uuid4()
    decisions = tuple(
        _decision(
            audit,
            rows[item.subscriber_id],
            planned_party_id=planned_party_id,
            identity_source_subscriber_id=first.subscriber_id,
        )
        for item in (first, second)
    )

    plan = build_party_backfill_plan(audit, decisions, planned_at=_PLANNED_AT)

    assert len(plan.groups) == 1
    assert plan.groups[0].subscriber_ids == tuple(
        sorted((first.subscriber_id, second.subscriber_id), key=str)
    )
    assert len(plan.bindings) == 2
    assert plan.summary()["artifact_contract"] == {
        "read_only": True,
        "execution_supported": False,
        "execution_requires_separate_approval": True,
        "contains_raw_contact_values": False,
        "contains_display_names": False,
        "contains_reason_text": False,
        "automatic_merge_allowed": False,
    }


def test_medium_duplicate_group_cannot_be_partially_planned():
    first = _facts()
    second = _facts(email="second@realbusiness.ng")
    audit = _audit(first, second)
    first_row = next(
        row for row in audit.rows if row.subscriber_id == first.subscriber_id
    )
    decision = _decision(
        audit,
        first_row,
        planned_party_id=uuid.uuid4(),
        identity_source_subscriber_id=first.subscriber_id,
    )

    with pytest.raises(
        PartyAdjudicationError,
        match="actionable decisions for every medium/high-confidence member",
    ):
        build_party_backfill_plan(audit, (decision,), planned_at=_PLANNED_AT)


def test_explicit_defer_records_review_without_planning_a_write():
    facts = _facts()
    audit = _audit(facts)
    decision = _decision(
        audit,
        audit.rows[0],
        action=PartyAdjudicationAction.defer.value,
        planned_party_id=None,
        identity_source_subscriber_id=None,
        party_type=None,
        data_classification=None,
        display_name_source=None,
        reason="Awaiting stronger identity evidence",
    )

    plan = build_party_backfill_plan(audit, (decision,), planned_at=_PLANNED_AT)

    assert plan.groups == ()
    assert plan.bindings == ()
    assert len(plan.deferred) == 1


def test_plan_refuses_stale_decision_existing_binding_and_invalid_active_classification():
    unbound = _facts()
    audit = _audit(unbound)
    row = audit.rows[0]
    base = _decision(
        audit,
        row,
        planned_party_id=uuid.uuid4(),
        identity_source_subscriber_id=unbound.subscriber_id,
    )

    with pytest.raises(PartyAdjudicationError, match="audit_digest does not match"):
        build_party_backfill_plan(
            audit,
            (replace(base, audit_digest="0" * 64),),
            planned_at=_PLANNED_AT,
        )
    with pytest.raises(
        PartyAdjudicationError,
        match="create_active_party requires data_classification='production'",
    ):
        build_party_backfill_plan(
            audit,
            (replace(base, data_classification="imported_unverified"),),
            planned_at=_PLANNED_AT,
        )

    bound_facts = replace(unbound, party_id=uuid.uuid4())
    bound_audit = _audit(bound_facts)
    bound_decision = _decision(
        bound_audit,
        bound_audit.rows[0],
        planned_party_id=uuid.uuid4(),
        identity_source_subscriber_id=bound_facts.subscriber_id,
    )
    with pytest.raises(PartyAdjudicationError, match="already bound to Party"):
        build_party_backfill_plan(
            bound_audit, (bound_decision,), planned_at=_PLANNED_AT
        )


def test_group_contract_and_display_source_must_be_consistent_and_available():
    first = _facts()
    second = _facts(email="second@realbusiness.ng")
    audit = _audit(first, second)
    rows = {row.subscriber_id: row for row in audit.rows}
    planned_party_id = uuid.uuid4()
    first_decision = _decision(
        audit,
        rows[first.subscriber_id],
        planned_party_id=planned_party_id,
        identity_source_subscriber_id=first.subscriber_id,
    )
    conflicting = _decision(
        audit,
        rows[second.subscriber_id],
        planned_party_id=planned_party_id,
        identity_source_subscriber_id=first.subscriber_id,
        party_type="organization",
    )

    with pytest.raises(PartyAdjudicationError, match="must agree on action"):
        build_party_backfill_plan(
            audit, (first_decision, conflicting), planned_at=_PLANNED_AT
        )

    organization_decisions = tuple(
        replace(
            item,
            party_type="organization",
            display_name_source="company_name",
        )
        for item in (first_decision, replace(conflicting, party_type="person"))
    )
    with pytest.raises(PartyAdjudicationError, match="is unavailable"):
        build_party_backfill_plan(audit, organization_decisions, planned_at=_PLANNED_AT)


def test_template_and_plan_artifacts_are_private_and_exclude_raw_values(tmp_path):
    raw_email = "private.person@realbusiness.ng"
    raw_phone = "+2348012345678"
    raw_reason = f"Confirmed {raw_email} and {raw_phone} in protected evidence"
    facts = _facts(email=raw_email, phone=raw_phone)
    audit = _audit(facts)
    template_paths = write_decision_template(audit, tmp_path / "template")
    decision = _decision(
        audit,
        audit.rows[0],
        planned_party_id=uuid.uuid4(),
        identity_source_subscriber_id=facts.subscriber_id,
        reason=raw_reason,
    )
    plan = build_party_backfill_plan(audit, (decision,), planned_at=_PLANNED_AT)
    plan_paths = write_plan_artifacts(
        plan,
        tmp_path / "plan",
        decision_file_sha256="a" * 64,
    )

    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in (*template_paths, *plan_paths)
    )
    assert raw_email not in combined
    assert raw_phone not in combined
    assert raw_reason not in combined
    assert "identity-reviewer-1" not in combined
    assert stat.S_IMODE((tmp_path / "template").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "plan").stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in (*template_paths, *plan_paths)
    )
    summary = json.loads(plan_paths[0].read_text(encoding="utf-8"))
    assert summary["planned_bindings"] == 1
    assert summary["decision_file_sha256"] == "a" * 64
    assert summary["artifact_contract"]["execution_supported"] is False


def test_decision_loader_skips_blank_rows_and_requires_private_permissions(tmp_path):
    facts = _facts()
    audit = _audit(facts)
    _summary_path, decision_path = write_decision_template(audit, tmp_path / "template")
    with decision_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = tuple(rows[0])
    rows[0]["action"] = "defer"
    rows[0]["reviewer"] = "identity-reviewer-1"
    rows[0]["reviewed_at"] = (_PLANNED_AT - timedelta(minutes=5)).isoformat()
    rows[0]["reason"] = "Needs more identity evidence"
    with decision_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    decision_path.chmod(0o600)

    decisions = load_decisions(decision_path)

    assert len(decisions) == 1
    assert decisions[0].action == "defer"
    decision_path.chmod(0o644)
    with pytest.raises(DecisionFileError, match="expected 0o600"):
        load_decisions(decision_path)


def test_plan_subscriber_party_backfill_uses_the_read_only_snapshot_seam():
    """Behavioural proof lives on PostgreSQL; this pins the wiring.

    The adapter previously issued `SET TRANSACTION ISOLATION LEVEL ...` itself,
    which could never be the first statement once the operator-tenant hook was
    installed, so the report raised on every real run. Asserting the statement
    text — as this test used to — pinned the broken form and kept CI green.
    """

    source = Path("scripts/migration/plan_subscriber_party_backfill.py").read_text(
        encoding="utf-8"
    )

    assert "read_only_snapshot_session" in source
    assert "SET TRANSACTION" not in source
