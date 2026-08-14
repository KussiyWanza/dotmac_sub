from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.party import PartyType
from app.services import party
from app.services.owner_commands import CommandContext
from app.services.subscriber_party_binding_repair import (
    COMMAND_SCOPE,
    BindSubscriberToExistingPartyCommand,
    CreateAndBindSubscriberPartyCommand,
    SubscriberPartyBindingRepairError,
    bind_subscriber_to_existing_party,
    create_and_bind_subscriber_party,
    resolve_repair_context,
)


def _context() -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{uuid4()}",
        scope=COMMAND_SCOPE,
        reason="Reviewed customer identity repair",
        idempotency_key=f"test-subscriber-party-binding:{command_id}",
    )


def test_create_and_bind_requires_review_and_records_pii_free_audit(
    db_session, subscriber
):
    outcome = create_and_bind_subscriber_party(
        db_session,
        CreateAndBindSubscriberPartyCommand(
            context=_context(),
            subscriber_id=subscriber.id,
            party_type=PartyType.person,
            party_display_name="Reviewed Test Customer",
            binding_reason="Reviewed the signed customer identity evidence.",
        ),
    )

    db_session.refresh(subscriber)
    assert outcome.party_created is True
    assert subscriber.party_id == outcome.party_id
    assert subscriber.party_binding_source == "admin_customer_party_binding_repair"
    assert (
        subscriber.party_binding_reason
        == "Reviewed the signed customer identity evidence."
    )
    audit = (
        db_session.query(AuditEvent)
        .filter_by(
            action="party.subscriber_binding_repaired", entity_id=str(subscriber.id)
        )
        .one()
    )
    assert audit.metadata_["party_id"] == str(outcome.party_id)
    assert "binding_reason" not in audit.metadata_
    assert "latitude" not in audit.metadata_
    assert "longitude" not in audit.metadata_


def test_existing_party_binding_replays_only_with_identical_evidence(
    db_session, subscriber
):
    target = party.create_party(
        db_session, party_type=PartyType.person, display_name="Exact target"
    )
    db_session.commit()
    reason = "Reviewed the authoritative identity record."
    first = bind_subscriber_to_existing_party(
        db_session,
        BindSubscriberToExistingPartyCommand(
            context=_context(),
            subscriber_id=subscriber.id,
            party_id=target.id,
            binding_reason=reason,
        ),
    )
    replay = bind_subscriber_to_existing_party(
        db_session,
        BindSubscriberToExistingPartyCommand(
            context=_context(),
            subscriber_id=subscriber.id,
            party_id=target.id,
            binding_reason=reason,
        ),
    )
    assert first.replayed is False
    assert replay.replayed is True


def test_repair_refuses_repoint_and_context_exposes_that_block(db_session, subscriber):
    first_party = party.create_party(
        db_session, party_type=PartyType.person, display_name="First target"
    )
    second_party = party.create_party(
        db_session, party_type=PartyType.person, display_name="Second target"
    )
    db_session.commit()
    bind_subscriber_to_existing_party(
        db_session,
        BindSubscriberToExistingPartyCommand(
            context=_context(),
            subscriber_id=subscriber.id,
            party_id=first_party.id,
            binding_reason="Reviewed exact initial identity evidence.",
        ),
    )

    with pytest.raises(SubscriberPartyBindingRepairError, match="repoint"):
        bind_subscriber_to_existing_party(
            db_session,
            BindSubscriberToExistingPartyCommand(
                context=_context(),
                subscriber_id=subscriber.id,
                party_id=second_party.id,
                binding_reason="Reviewed a different identity evidence record.",
            ),
        )
    db_session.rollback()
    context = resolve_repair_context(db_session, subscriber_id=subscriber.id)
    assert context.can_repair is False
    assert "repointing" in (context.unavailable_reason or "").lower()


def test_repair_requires_meaningful_review_evidence(db_session, subscriber):
    with pytest.raises(SubscriberPartyBindingRepairError, match="Review evidence"):
        create_and_bind_subscriber_party(
            db_session,
            CreateAndBindSubscriberPartyCommand(
                context=_context(),
                subscriber_id=subscriber.id,
                party_type=PartyType.person,
                party_display_name="Reviewed Test Customer",
                binding_reason="too short",
            ),
        )
