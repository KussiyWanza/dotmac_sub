from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.party import (
    Party,
    PartyIdentityBackfillReceipt,
    PartyIdentityStatus,
    PartyRole,
)
from app.models.subscriber import Subscriber
from app.services import party as party_registry
from app.services import party_identity_audit as identity_audit
from app.services.party_identity_adjudication import (
    PartyAdjudicationAction,
    PartyAdjudicationError,
    PartyIdentityDecision,
    build_party_backfill_plan,
)
from app.services.party_identity_backfill import (
    PartyBackfillExecutionApproval,
    PartyIdentityBackfillError,
    _set_serializable_read_write,
    execute_party_backfill_plan,
)
from scripts.migration.execute_subscriber_party_backfill import (
    ExecutionFileError,
    _validate_plan_summary,
    load_approval,
)

_EXECUTED_AT = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
_DECISION_SHA = "1" * 64
_PLAN_FILE_SHA = "2" * 64
_APPROVAL_FILE_SHA = "3" * 64


def _subscriber(email: str, *, phone: str = "+2348012345678") -> Subscriber:
    return Subscriber(
        first_name="Ada",
        last_name="Okafor",
        email=email,
        phone=phone,
    )


def _facts(account: Subscriber, **overrides) -> identity_audit.SubscriberIdentityFacts:
    values = {
        "subscriber_id": account.id,
        "first_name": account.first_name,
        "last_name": account.last_name,
        "display_name": account.display_name,
        "company_name": account.company_name,
        "legal_name": account.legal_name,
        "email": account.email,
        "phone": account.phone,
        "party_id": account.party_id,
        "has_active_subscription": True,
        "has_any_subscription": True,
    }
    values.update(overrides)
    return identity_audit.SubscriberIdentityFacts(**values)


def _audit(*facts):
    return identity_audit.resolve_subscriber_identity_audit(
        tuple(facts), generated_at=_EXECUTED_AT - timedelta(hours=1)
    )


def _decision(
    audit,
    subscriber_id,
    planned_party_id,
    *,
    identity_source_subscriber_id=None,
    action=PartyAdjudicationAction.create_active_party.value,
    party_type="person",
    data_classification="production",
    display_name_source="subscriber_full_name",
):
    row = next(item for item in audit.rows if item.subscriber_id == subscriber_id)
    return PartyIdentityDecision(
        decision_id=uuid.uuid4(),
        subscriber_id=subscriber_id,
        audit_digest=identity_audit.subscriber_identity_audit_digest(audit),
        row_fingerprint=identity_audit.subscriber_audit_row_fingerprint(row),
        action=action,
        planned_party_id=planned_party_id,
        identity_source_subscriber_id=(identity_source_subscriber_id or subscriber_id),
        party_type=party_type,
        data_classification=data_classification,
        display_name_source=display_name_source,
        reviewer="identity-reviewer-1",
        reviewed_at=_EXECUTED_AT - timedelta(minutes=45),
        reason="Reviewed against protected identity evidence",
    )


def _approval(plan, **overrides) -> PartyBackfillExecutionApproval:
    values = {
        "plan_digest": plan.plan_digest,
        "audit_digest": plan.audit_digest,
        "decision_file_sha256": _DECISION_SHA,
        "plan_file_sha256": _PLAN_FILE_SHA,
        "approved_by": "identity-approver-1",
        "approved_at": _EXECUTED_AT - timedelta(minutes=15),
        "expires_at": _EXECUTED_AT + timedelta(hours=1),
        "reason": "Approved exact protected Party backfill plan",
        "maximum_parties": len(plan.groups),
        "maximum_bindings": len(plan.bindings),
    }
    values.update(overrides)
    return PartyBackfillExecutionApproval(**values)


def _execute(db_session, audit, decisions, approval):
    return execute_party_backfill_plan(
        db_session,
        audit=audit,
        decisions=decisions,
        approval=approval,
        decision_file_sha256=_DECISION_SHA,
        plan_file_sha256=_PLAN_FILE_SHA,
        approval_file_sha256=_APPROVAL_FILE_SHA,
        executed_at=_EXECUTED_AT,
    )


def test_exact_approved_plan_creates_party_binding_and_receipt_only(db_session):
    account = _subscriber("ada-backfill@realbusiness.ng")
    db_session.add(account)
    db_session.flush()
    original_is_active = account.is_active
    original_status = account.status
    audit = _audit(_facts(account))
    planned_party_id = uuid.uuid4()
    decision = _decision(audit, account.id, planned_party_id)
    plan = build_party_backfill_plan(
        audit, (decision,), planned_at=_EXECUTED_AT - timedelta(minutes=30)
    )

    outcome = _execute(db_session, audit, (decision,), _approval(plan))

    party = db_session.get(Party, planned_party_id)
    receipt = db_session.get(PartyIdentityBackfillReceipt, outcome.receipt_id)
    assert party is not None
    assert party.display_name == "Ada Okafor"
    assert party.status == PartyIdentityStatus.active.value
    assert party.metadata_["identity_backfill"]["plan_digest"] == plan.plan_digest
    assert account.party_id == planned_party_id
    assert account.party_binding_source == f"party_backfill:{plan.plan_digest}"
    assert str(decision.decision_id) in account.party_binding_reason
    assert receipt is not None
    assert receipt.manifest == plan.digest_payload()
    assert outcome.parties_created == 1
    assert outcome.bindings_created == 1
    assert outcome.replayed is False
    assert db_session.query(PartyRole).count() == 0
    assert account.is_active == original_is_active
    assert account.status == original_status


def test_quarantine_action_creates_quarantined_unverified_party(db_session):
    account = _subscriber("unverified-backfill@realbusiness.ng")
    db_session.add(account)
    db_session.flush()
    audit = _audit(_facts(account, has_active_subscription=False))
    planned_party_id = uuid.uuid4()
    decision = _decision(
        audit,
        account.id,
        planned_party_id,
        action=PartyAdjudicationAction.create_quarantined_party.value,
        data_classification="imported_unverified",
    )
    plan = build_party_backfill_plan(audit, (decision,), planned_at=_EXECUTED_AT)

    _execute(db_session, audit, (decision,), _approval(plan))

    party = db_session.get(Party, planned_party_id)
    assert party is not None
    assert party.status == PartyIdentityStatus.quarantined.value
    assert party.data_classification == "imported_unverified"
    assert account.party_id == planned_party_id


def test_exact_retry_verifies_receipt_without_recreating_rows(db_session):
    account = _subscriber("retry-backfill@realbusiness.ng")
    db_session.add(account)
    db_session.flush()
    audit = _audit(_facts(account))
    decision = _decision(audit, account.id, uuid.uuid4())
    plan = build_party_backfill_plan(audit, (decision,), planned_at=_EXECUTED_AT)
    approval = _approval(plan)
    first = _execute(db_session, audit, (decision,), approval)

    second = _execute(db_session, audit, (decision,), approval)

    assert second.receipt_id == first.receipt_id
    assert second.replayed is True
    assert second.parties_created == 0
    assert second.bindings_created == 0
    assert db_session.query(PartyIdentityBackfillReceipt).count() == 1
    assert db_session.query(Party).count() == 1


def test_retry_refuses_binding_drift_instead_of_repointing(db_session):
    account = _subscriber("drift-backfill@realbusiness.ng")
    db_session.add(account)
    db_session.flush()
    audit = _audit(_facts(account))
    decision = _decision(audit, account.id, uuid.uuid4())
    plan = build_party_backfill_plan(audit, (decision,), planned_at=_EXECUTED_AT)
    approval = _approval(plan)
    _execute(db_session, audit, (decision,), approval)
    account.party_binding_reason = "manually changed"
    db_session.flush()

    with pytest.raises(PartyIdentityBackfillError, match="binding reason drifted"):
        _execute(db_session, audit, (decision,), approval)


def test_collision_and_stale_decision_are_refused_before_binding(db_session):
    account = _subscriber("collision-backfill@realbusiness.ng")
    db_session.add(account)
    db_session.flush()
    audit = _audit(_facts(account))
    planned_party_id = uuid.uuid4()
    decision = _decision(audit, account.id, planned_party_id)
    plan = build_party_backfill_plan(audit, (decision,), planned_at=_EXECUTED_AT)
    party_registry.create_party(
        db_session,
        party_id=planned_party_id,
        party_type="person",
        display_name="Unrelated Existing Party",
    )

    with pytest.raises(PartyIdentityBackfillError, match="already exist"):
        _execute(db_session, audit, (decision,), _approval(plan))
    assert account.party_id is None
    assert db_session.query(PartyIdentityBackfillReceipt).count() == 0

    stale = replace(decision, row_fingerprint="0" * 64)
    with pytest.raises(PartyAdjudicationError, match="row_fingerprint"):
        _execute(db_session, audit, (stale,), _approval(plan))


def test_expired_approval_and_count_overflow_are_refused(db_session):
    account = _subscriber("approval-backfill@realbusiness.ng")
    db_session.add(account)
    db_session.flush()
    audit = _audit(_facts(account))
    decision = _decision(audit, account.id, uuid.uuid4())
    plan = build_party_backfill_plan(audit, (decision,), planned_at=_EXECUTED_AT)

    with pytest.raises(PartyIdentityBackfillError, match="approval has expired"):
        _execute(
            db_session,
            audit,
            (decision,),
            _approval(plan, expires_at=_EXECUTED_AT - timedelta(seconds=1)),
        )
    with pytest.raises(PartyIdentityBackfillError, match="cannot exceed 24 hours"):
        _execute(
            db_session,
            audit,
            (decision,),
            _approval(
                plan,
                approved_at=_EXECUTED_AT - timedelta(minutes=1),
                expires_at=_EXECUTED_AT + timedelta(hours=25),
            ),
        )
    with pytest.raises(PartyIdentityBackfillError, match="exceeds the approved"):
        _execute(
            db_session,
            audit,
            (decision,),
            _approval(plan, maximum_parties=0),
        )
    assert account.party_id is None


def test_defer_only_plan_does_not_create_an_execution_receipt(db_session):
    account = _subscriber("defer-only@realbusiness.ng")
    db_session.add(account)
    db_session.flush()
    audit = _audit(_facts(account))
    decision = _decision(audit, account.id, uuid.uuid4())
    deferred = replace(
        decision,
        action=PartyAdjudicationAction.defer.value,
        planned_party_id=None,
        identity_source_subscriber_id=None,
        party_type=None,
        data_classification=None,
        display_name_source=None,
    )
    plan = build_party_backfill_plan(audit, (deferred,), planned_at=_EXECUTED_AT)

    with pytest.raises(PartyIdentityBackfillError, match="no actionable"):
        _execute(db_session, audit, (deferred,), _approval(plan))

    assert db_session.query(PartyIdentityBackfillReceipt).count() == 0


def test_execution_is_atomic_when_a_binding_command_fails(db_session, monkeypatch):
    first = _subscriber("atomic-one@realbusiness.ng", phone="+2348011111111")
    second = _subscriber("atomic-two@realbusiness.ng", phone="+2348022222222")
    db_session.add_all((first, second))
    db_session.flush()
    audit = _audit(_facts(first), _facts(second))
    planned_party_id = uuid.uuid4()
    decisions = tuple(
        _decision(
            audit,
            account.id,
            planned_party_id,
            identity_source_subscriber_id=first.id,
        )
        for account in (first, second)
    )
    plan = build_party_backfill_plan(audit, decisions, planned_at=_EXECUTED_AT)
    original_bind = party_registry.bind_subscriber_account
    calls = 0

    def fail_second_binding(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise party_registry.PartyInvariantError("simulated binding failure")
        return original_bind(*args, **kwargs)

    monkeypatch.setattr(party_registry, "bind_subscriber_account", fail_second_binding)

    with pytest.raises(party_registry.PartyInvariantError, match="simulated"):
        _execute(db_session, audit, decisions, _approval(plan))

    assert first.party_id is None
    assert second.party_id is None
    assert db_session.get(Party, planned_party_id) is None
    assert db_session.query(PartyIdentityBackfillReceipt).count() == 0


def test_private_approval_loader_and_serializable_transaction_contract(tmp_path):
    approval_path = tmp_path / "approval.json"
    payload = {
        "contract_version": 1,
        "plan_digest": "a" * 64,
        "audit_digest": "b" * 64,
        "decision_file_sha256": "c" * 64,
        "plan_file_sha256": "d" * 64,
        "approved_by": "identity-approver-1",
        "approved_at": "2026-07-17T11:00:00+00:00",
        "expires_at": "2026-07-17T13:00:00+00:00",
        "reason": "Approved protected execution",
        "maximum_parties": 2,
        "maximum_bindings": 3,
    }
    approval_path.write_text(json.dumps(payload), encoding="utf-8")
    approval_path.chmod(0o600)

    approval = load_approval(approval_path)

    assert approval.maximum_parties == 2
    plan_summary = {
        "plan_digest": approval.plan_digest,
        "audit_digest": approval.audit_digest,
        "decision_file_sha256": approval.decision_file_sha256,
        "planned_at": "2026-07-17T10:30:00+00:00",
        "planned_parties": 2,
        "planned_bindings": 3,
        "artifact_contract": {
            "automatic_merge_allowed": False,
            "execution_requires_separate_approval": True,
        },
    }
    _validate_plan_summary(
        plan_summary,
        approval=approval,
        decision_file_sha256=approval.decision_file_sha256,
    )
    with pytest.raises(ExecutionFileError, match="approval predates"):
        _validate_plan_summary(
            {**plan_summary, "planned_at": "2026-07-17T12:00:00+00:00"},
            approval=approval,
            decision_file_sha256=approval.decision_file_sha256,
        )
    approval_path.chmod(0o640)
    with pytest.raises(ExecutionFileError, match="mode 0o600"):
        load_approval(approval_path)

    # The contract is now requested as connection execution options, not issued
    # as `SET TRANSACTION`: that statement is only legal as the first one in a
    # transaction, and the operator-tenant hook has always run one before it.
    requested: list[dict[str, object]] = []
    postgresql_db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        connection=lambda **kwargs: requested.append(kwargs),
    )
    _set_serializable_read_write(postgresql_db)
    assert requested == [
        {
            "execution_options": {
                "isolation_level": "SERIALIZABLE",
                "postgresql_readonly": False,
            }
        }
    ]


def test_receipt_stores_hashes_not_raw_approval_text(db_session):
    account = _subscriber("private-approval@realbusiness.ng")
    db_session.add(account)
    db_session.flush()
    audit = _audit(_facts(account))
    decision = _decision(audit, account.id, uuid.uuid4())
    plan = build_party_backfill_plan(audit, (decision,), planned_at=_EXECUTED_AT)
    approval = _approval(
        plan,
        approved_by="private.approver@dotmac.ng",
        reason="Protected approval reason must not enter the database",
    )

    outcome = _execute(db_session, audit, (decision,), approval)
    receipt = db_session.get(PartyIdentityBackfillReceipt, outcome.receipt_id)
    serialized = json.dumps(receipt.manifest, sort_keys=True)

    assert (
        receipt.approved_by_sha256
        == hashlib.sha256(approval.approved_by.encode()).hexdigest()
    )
    assert (
        receipt.approval_reason_sha256
        == hashlib.sha256(approval.reason.encode()).hexdigest()
    )
    assert approval.approved_by not in serialized
    assert approval.reason not in serialized


def test_execution_entrypoint_keeps_confirmation_and_owner_boundaries():
    script_source = (
        Path(__file__).resolve().parents[1]
        / "scripts/migration/execute_subscriber_party_backfill.py"
    ).read_text(encoding="utf-8")
    owner_source = (
        Path(__file__).resolve().parents[1] / "app/services/party_identity_backfill.py"
    ).read_text(encoding="utf-8")

    assert '"--execute"' in script_source
    assert '"--confirm-plan-digest"' in script_source
    assert "execute_party_backfill_transaction(" in script_source
    assert "SERIALIZABLE, READ WRITE" not in script_source
    assert "db.commit()" not in script_source
    assert "db.rollback()" not in script_source
    # The owner still owns the write transaction's isolation contract; it now
    # requests it through the shared seam instead of holding its own SQL.
    assert "SERIALIZABLE, READ WRITE" not in owner_source
    assert "begin_serializable_write(" in owner_source
    assert owner_source.count("db.commit()") == 1
    assert "merge_party" not in script_source
    assert "repoint_party" not in script_source
