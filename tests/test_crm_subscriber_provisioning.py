from __future__ import annotations

import uuid

import pytest

from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.idempotency import IdempotencyKey
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.crm_provisioning import CRMSubscriberProvisionRequest
from app.services.crm_subscriber_provisioning import (
    CRM_PROVISIONING_SCOPE,
    CRMSubscriberProvisioningError,
    ProvisionCRMSubscriberCommand,
    provision_crm_subscriber,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext


def _payload(**overrides) -> CRMSubscriberProvisionRequest:
    values = {
        "crm_person_id": "crm-person-100",
        "crm_project_id": "crm-project-100",
        "crm_quote_id": "crm-quote-100",
        "crm_sales_order_id": "crm-order-100",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "display_name": "Ada Lovelace",
        "email": "ada.crm@example.com",
        "phone": "08030000000",
        "city": "Abuja",
        "region": "FCT",
        "country_code": "NG",
    }
    values.update(overrides)
    return CRMSubscriberProvisionRequest(**values)


def _command(
    payload: CRMSubscriberProvisionRequest | None = None,
    *,
    key: str = "crm-person-100",
) -> ProvisionCRMSubscriberCommand:
    command_id = uuid.uuid4()
    return ProvisionCRMSubscriberCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="dotmac_crm",
            scope=CRM_PROVISIONING_SCOPE,
            reason="CRM sales customer provisioning",
            idempotency_key=key,
        ),
        payload=payload or _payload(),
    )


def test_provision_creates_canonical_new_customer_with_evidence(db_session):
    result = provision_crm_subscriber(db_session, _command())

    subscriber = db_session.get(Subscriber, result.subscriber_id)
    assert result.outcome == "created"
    assert result.replayed is False
    assert subscriber is not None
    assert subscriber.status == SubscriberStatus.new
    assert subscriber.metadata_["source"] == "dotmac_crm"
    assert subscriber.metadata_["crm_person_id"] == "crm-person-100"
    assert subscriber.metadata_["crm_sales_order_id"] == "crm-order-100"
    assert (
        db_session.query(IdempotencyKey)
        .filter_by(scope="crm_subscriber_provision", key="crm-person-100")
        .count()
        == 1
    )
    assert (
        db_session.query(AuditEvent)
        .filter_by(action="customer.crm_subscriber_provisioned")
        .count()
        == 1
    )
    assert (
        db_session.query(EventStore).filter_by(event_type="subscriber.created").count()
        == 1
    )


def test_exact_idempotency_replay_returns_original_without_new_evidence(db_session):
    first = provision_crm_subscriber(db_session, _command())
    second = provision_crm_subscriber(db_session, _command())

    assert second.subscriber_id == first.subscriber_id
    assert second.outcome == "reused"
    assert second.replayed is True
    assert db_session.query(Subscriber).count() == 1
    assert (
        db_session.query(AuditEvent)
        .filter_by(action="customer.crm_subscriber_provisioned")
        .count()
        == 1
    )
    assert (
        db_session.query(EventStore).filter_by(event_type="subscriber.created").count()
        == 1
    )


def test_idempotency_key_reuse_with_changed_payload_fails_closed(db_session):
    provision_crm_subscriber(db_session, _command())

    with pytest.raises(CRMSubscriberProvisioningError) as raised:
        provision_crm_subscriber(
            db_session,
            _command(_payload(email="different@example.com")),
        )

    assert raised.value.code.endswith("idempotency_conflict")
    assert db_session.query(Subscriber).count() == 1


def test_exact_provenance_reuses_existing_customer_without_mutating_identity(
    db_session,
):
    existing = Subscriber(
        first_name="Canonical",
        last_name="Customer",
        email="canonical@example.com",
        metadata_={"crm_person_id": "crm-person-100"},
    )
    db_session.add(existing)
    db_session.flush()
    existing_id = existing.id
    db_session.commit()

    result = provision_crm_subscriber(db_session, _command())

    db_session.refresh(existing)
    assert result.subscriber_id == existing_id
    assert result.outcome == "reused"
    assert result.replayed is False
    assert existing.first_name == "Canonical"
    assert existing.email == "canonical@example.com"
    assert db_session.query(Subscriber).count() == 1


def test_missing_idempotency_key_is_rejected(db_session):
    with pytest.raises(DomainError) as raised:
        provision_crm_subscriber(db_session, _command(key=""))

    assert raised.value.code.endswith("invalid_command_context")
    assert db_session.query(Subscriber).count() == 0
