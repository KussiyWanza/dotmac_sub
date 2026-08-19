"""Reviewed carried-source identity adjudication behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.carried_source_identity import (
    CarriedSourceIdentityAdjudication,
    CarriedSourceIdentityAdjudicationImmutableError,
)
from app.models.event_store import EventStore
from app.models.system_user import SystemUser
from app.services.carried_source_identity_adjudication import (
    OWNER,
    CarriedSourceIdentityAdjudicationError,
    CarriedSourceIdentityBlocker,
    ConfirmCarriedSourceIdentityCommand,
    confirm_carried_source_identity_adjudication,
    preview_carried_source_identity_adjudication,
)
from app.services.owner_commands import CommandContext


def _qualify(subscriber) -> None:  # noqa: ANN001
    subscriber.created_at = datetime(2026, 5, 22, tzinfo=UTC)
    subscriber.splynx_customer_id = None
    subscriber.crm_subscriber_id = uuid4()
    subscriber.metadata_ = {
        "source": "dotmac_omni",
        "crm_person_id": str(uuid4()),
        "crm_project_id": str(uuid4()),
        "crm_quote_id": str(uuid4()),
        "crm_sales_order_id": str(uuid4()),
    }


def _reviewers(db_session) -> tuple[SystemUser, SystemUser]:  # noqa: ANN001
    reviewers = (
        SystemUser(
            first_name="Billing",
            last_name="Reviewer",
            email=f"billing-review-{uuid4().hex}@example.com",
        ),
        SystemUser(
            first_name="Finance",
            last_name="Approver",
            email=f"finance-approve-{uuid4().hex}@example.com",
        ),
    )
    db_session.add_all(reviewers)
    return reviewers


def _command(
    subscriber,  # noqa: ANN001
    preview_fingerprint: str,
    reviewed_by: SystemUser,
    approved_by: SystemUser,
    *,
    idempotency_key: str = "reviewed-native-before-handoff:test",
) -> ConfirmCarriedSourceIdentityCommand:
    return ConfirmCarriedSourceIdentityCommand(
        context=CommandContext.system(
            actor="pytest:billing-migration",
            scope=OWNER,
            reason=(
                "Independent evidence proves this is a pre-handoff Sub-native account."
            ),
            idempotency_key=idempotency_key,
        ),
        account_id=subscriber.id,
        expected_preview_fingerprint=preview_fingerprint,
        evidence_ref="finance-review:carried-source/test",
        evidence_sha256="a" * 64,
        reviewed_by_id=reviewed_by.id,
        approved_by_id=approved_by.id,
    )


def test_preview_is_pii_free_and_requires_complete_native_provenance(
    db_session, subscriber
):
    subscriber.created_at = datetime(2026, 5, 22, tzinfo=UTC)
    subscriber.metadata_ = {"source": "dotmac_omni"}
    db_session.commit()

    preview = preview_carried_source_identity_adjudication(db_session, subscriber.id)

    assert preview.eligible is False
    assert preview.disposition is None
    assert set(preview.blockers) == {
        CarriedSourceIdentityBlocker.missing_crm_subscriber_provenance,
        CarriedSourceIdentityBlocker.incomplete_crm_creation_provenance,
    }
    assert len(preview.fingerprint) == 64
    assert not hasattr(preview, "customer_name")
    assert not hasattr(preview, "email")


def test_dual_review_records_one_decision_audit_and_owner_output(
    db_session, subscriber
):
    _qualify(subscriber)
    reviewed_by, approved_by = _reviewers(db_session)
    db_session.commit()
    preview = preview_carried_source_identity_adjudication(db_session, subscriber.id)
    assert preview.eligible is True
    command = _command(
        subscriber,
        preview.fingerprint,
        reviewed_by,
        approved_by,
    )
    db_session.rollback()

    created = confirm_carried_source_identity_adjudication(db_session, command)
    replayed = confirm_carried_source_identity_adjudication(db_session, command)

    assert created.replayed is False
    assert replayed.replayed is True
    assert replayed.decision_id == created.decision_id
    decision = db_session.scalar(
        select(CarriedSourceIdentityAdjudication).where(
            CarriedSourceIdentityAdjudication.id == created.decision_id
        )
    )
    assert decision is not None
    assert decision.account_id == subscriber.id
    assert subscriber.splynx_customer_id is None
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "carried_source_identity_adjudicated")
        .count()
        == 1
    )
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "billing.carried_source_identity.adjudicated")
        .count()
        == 1
    )
    decision.reason = "Attempted evidence rewrite"
    with pytest.raises(CarriedSourceIdentityAdjudicationImmutableError):
        db_session.flush()
    db_session.rollback()


def test_changed_eligible_provenance_rejects_stale_review(db_session, subscriber):
    _qualify(subscriber)
    reviewed_by, approved_by = _reviewers(db_session)
    db_session.commit()
    preview = preview_carried_source_identity_adjudication(db_session, subscriber.id)
    db_session.rollback()
    metadata = dict(subscriber.metadata_)
    metadata["crm_quote_id"] = str(uuid4())
    subscriber.metadata_ = metadata
    db_session.commit()
    command = _command(
        subscriber,
        preview.fingerprint,
        reviewed_by,
        approved_by,
    )
    db_session.rollback()

    with pytest.raises(CarriedSourceIdentityAdjudicationError) as exc:
        confirm_carried_source_identity_adjudication(
            db_session,
            command,
        )

    assert exc.value.code.endswith("stale_preview")
    assert db_session.query(CarriedSourceIdentityAdjudication).count() == 0


def test_same_staff_cannot_review_and_approve(db_session, subscriber):
    _qualify(subscriber)
    reviewed_by, _ = _reviewers(db_session)
    db_session.commit()
    preview = preview_carried_source_identity_adjudication(db_session, subscriber.id)
    command = _command(
        subscriber,
        preview.fingerprint,
        reviewed_by,
        reviewed_by,
    )
    db_session.rollback()

    with pytest.raises(CarriedSourceIdentityAdjudicationError) as exc:
        confirm_carried_source_identity_adjudication(
            db_session,
            command,
        )

    assert exc.value.code.endswith("reviewer_conflict")
    assert db_session.query(CarriedSourceIdentityAdjudication).count() == 0
