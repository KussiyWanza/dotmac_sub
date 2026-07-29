from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.audit import AuditActorType, AuditEvent
from app.models.party import PartyType
from app.models.subscriber import Subscriber
from app.services import party as party_service
from app.services.crm_customer_name_repair import (
    WINDOW_START,
    apply_name_remediation_plan,
    build_name_remediation_plan,
)


def _audit_name_change(
    db_session,
    subscriber: Subscriber,
    *,
    old_display_name: str,
    new_display_name: str,
    occurred_at: datetime,
) -> None:
    db_session.add(
        AuditEvent(
            actor_type=AuditActorType.service,
            actor_id="crm_webhook",
            action="crm_customer_identity_update",
            entity_type="subscriber",
            entity_id=str(subscriber.id),
            status_code=200,
            is_success=True,
            occurred_at=occurred_at,
            metadata_={
                "source": "crm_customer_webhook",
                "crm_person_id": f"crm-{subscriber.id}",
                "changes": {
                    "display_name": {
                        "old": old_display_name,
                        "new": new_display_name,
                    }
                },
            },
        )
    )
    db_session.commit()


def test_build_name_remediation_plan_selects_placeholder_overwrites_and_reviews(
    db_session,
):
    good = Subscriber(
        first_name="Customer",
        last_name="Unknown",
        display_name="Customer Unknown",
        email="good@example.com",
    )
    suspicious = Subscriber(
        first_name="Unknown",
        last_name="Customer",
        display_name="Unknown Customer",
        email="suspicious@example.com",
    )
    db_session.add_all([good, suspicious])
    db_session.commit()

    _audit_name_change(
        db_session,
        good,
        old_display_name="Original Customer",
        new_display_name="Customer Unknown",
        occurred_at=WINDOW_START + timedelta(minutes=1),
    )
    _audit_name_change(
        db_session,
        suspicious,
        old_display_name="Customer Unknown",
        new_display_name="Unknown Customer",
        occurred_at=WINDOW_START + timedelta(minutes=2),
    )

    plan = build_name_remediation_plan(
        db_session,
        deployment_target="test-cluster",
        window_start=WINDOW_START,
        window_end=WINDOW_START + timedelta(hours=1),
    )

    assert plan.manifest["counts"]["selected"] == 1
    assert plan.manifest["counts"]["review"] == 1
    assert len(plan.digest) == 64
    assert {row["subscriber_id"] for row in plan.manifest["rows"]} == {str(good.id)}


def test_apply_name_remediation_plan_restores_old_name_and_records_digest(
    db_session,
):
    subscriber = Subscriber(
        first_name="Customer",
        last_name="Unknown",
        display_name="Customer Unknown",
        email="apply@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()

    _audit_name_change(
        db_session,
        subscriber,
        old_display_name="Ada Lovelace",
        new_display_name="Customer Unknown",
        occurred_at=WINDOW_START + timedelta(minutes=3),
    )

    plan = build_name_remediation_plan(
        db_session,
        deployment_target="prod-a",
        window_start=WINDOW_START,
        window_end=WINDOW_START + timedelta(hours=1),
    )
    result = apply_name_remediation_plan(
        db_session,
        plan,
        expected_digest=plan.digest,
        deployment_target="prod-a",
        actor_id="reviewer-1",
    )

    assert result["status"] == "applied"
    db_session.refresh(subscriber)
    assert subscriber.first_name == "Ada"
    assert subscriber.last_name == "Lovelace"
    assert subscriber.display_name == "Ada Lovelace"
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "subscriber_name_correction_applied")
        .filter(AuditEvent.entity_id == str(subscriber.id))
        .one()
        .metadata_["manifest_digest"]
        == plan.digest
    )


def test_apply_name_remediation_plan_reports_already_applied_on_exact_replay(
    db_session,
):
    subscriber = Subscriber(
        first_name="Customer",
        last_name="Unknown",
        display_name="Customer Unknown",
        email="replay@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()

    _audit_name_change(
        db_session,
        subscriber,
        old_display_name="Grace Hopper",
        new_display_name="Customer Unknown",
        occurred_at=WINDOW_START + timedelta(minutes=4),
    )

    plan = build_name_remediation_plan(
        db_session,
        deployment_target="prod-b",
        window_start=WINDOW_START,
        window_end=WINDOW_START + timedelta(hours=1),
    )
    apply_name_remediation_plan(
        db_session,
        plan,
        expected_digest=plan.digest,
        deployment_target="prod-b",
    )
    replay = apply_name_remediation_plan(
        db_session,
        plan,
        expected_digest=plan.digest,
        deployment_target="prod-b",
    )

    assert replay["status"] == "already_applied"


def test_apply_name_remediation_plan_rejects_party_bound_rows(db_session):
    identity = party_service.create_party(
        db_session, party_type=PartyType.person, display_name="Party Identity"
    )
    subscriber = Subscriber(
        first_name="Customer",
        last_name="Unknown",
        display_name="Customer Unknown",
        email="party@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()
    party_service.bind_subscriber_account(
        db_session,
        subscriber_id=subscriber.id,
        party_id=identity.id,
        source="reviewed_identity_worklist",
        reason="Historical repair safety check",
    )
    _audit_name_change(
        db_session,
        subscriber,
        old_display_name="Party Identity",
        new_display_name="Customer Unknown",
        occurred_at=WINDOW_START + timedelta(minutes=5),
    )

    plan = build_name_remediation_plan(
        db_session,
        deployment_target="prod-c",
        window_start=WINDOW_START,
        window_end=WINDOW_START + timedelta(hours=1),
    )
    with pytest.raises(ValueError, match="party-bound"):
        apply_name_remediation_plan(
            db_session,
            plan,
            expected_digest=plan.digest,
            deployment_target="prod-c",
        )


def test_apply_name_remediation_plan_rolls_back_on_drift(db_session):
    first = Subscriber(
        first_name="Customer",
        last_name="Unknown",
        display_name="Customer Unknown",
        email="first@example.com",
    )
    second = Subscriber(
        first_name="Customer",
        last_name="Unknown",
        display_name="Customer Unknown",
        email="second@example.com",
    )
    db_session.add_all([first, second])
    db_session.commit()
    _audit_name_change(
        db_session,
        first,
        old_display_name="First Original",
        new_display_name="Customer Unknown",
        occurred_at=WINDOW_START + timedelta(minutes=6),
    )
    _audit_name_change(
        db_session,
        second,
        old_display_name="Second Original",
        new_display_name="Customer Unknown",
        occurred_at=WINDOW_START + timedelta(minutes=7),
    )

    plan = build_name_remediation_plan(
        db_session,
        deployment_target="prod-d",
        window_start=WINDOW_START,
        window_end=WINDOW_START + timedelta(hours=1),
    )
    second.display_name = "Drifted"
    db_session.commit()

    with pytest.raises(ValueError, match="drifted since plan generation"):
        apply_name_remediation_plan(
            db_session,
            plan,
            expected_digest=plan.digest,
            deployment_target="prod-d",
        )

    db_session.refresh(first)
    assert first.display_name == "Customer Unknown"
