from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationReadState,
    InboxMessage,
    InboxMessageDirection,
    InboxObservationKind,
)
from app.services import (
    team_inbox_maintenance,
    team_inbox_observations,
    team_inbox_processing,
    team_inbox_read_state,
    team_inbox_realtime,
)
from app.services.owner_commands import CommandContext


def _context(name: str) -> CommandContext:
    return CommandContext.system(
        actor="test:team-inbox-sot",
        scope="team-inbox:test",
        reason=name,
        idempotency_key=name,
    )


def _message_observation(*, body: str = "Please help"):
    return team_inbox_observations.RecordProviderObservationCommand(
        context=_context(f"message:{body}"),
        provider=team_inbox_observations.InboxProvider.meta_cloud_api,
        provider_account_scope="waba-1",
        provider_event_id="message:wamid-1",
        kind=InboxObservationKind.message,
        channel_type=InboxChannelType.whatsapp,
        external_message_id="wamid-1",
        observed_at=datetime(2026, 7, 22, 10, 0, tzinfo=UTC),
        payload=team_inbox_observations.InboundMessageObservation(
            contact_address="+2348035550114",
            body=body,
            external_thread_id="whatsapp:+2348035550114",
        ),
    )


def test_location_observation_requires_valid_coordinate_pair() -> None:
    location = team_inbox_observations.inbound_location_observation(
        latitude="6.5243793",
        longitude="3.3792057",
        name="Customer location",
        address="Lagos, Nigeria",
    )

    assert location is not None
    assert location.latitude == 6.5243793
    assert location.longitude == 3.3792057

    with pytest.raises(team_inbox_observations.TeamInboxObservationError) as exc:
        team_inbox_observations.inbound_location_observation(
            latitude=6.5243793,
            longitude=None,
        )
    assert exc.value.code.endswith("invalid_location")


def test_location_repair_requires_exact_conversation_scope(db_session) -> None:
    with pytest.raises(team_inbox_maintenance.TeamInboxMaintenanceError) as exc:
        team_inbox_maintenance.repair_whatsapp_locations(
            db_session,
            team_inbox_maintenance.RepairWhatsAppLocationsCommand(
                context=_context("invalid-location-repair-scope"),
                conversation_ids=(),
            ),
        )

    assert exc.value.code.endswith("invalid_location_repair_scope")


def test_provider_observation_exact_retry_and_changed_evidence(db_session) -> None:
    first = team_inbox_observations.record_provider_observation(
        db_session, _message_observation()
    )
    replay = team_inbox_observations.record_provider_observation(
        db_session, _message_observation()
    )

    assert replay.observation_id == first.observation_id
    assert (
        replay.outcome is team_inbox_observations.ObservationProcessingOutcome.replayed
    )

    with pytest.raises(team_inbox_observations.TeamInboxObservationError) as exc:
        team_inbox_observations.record_provider_observation(
            db_session, _message_observation(body="changed evidence")
        )
    assert exc.value.code.endswith("provider_event_identity_collision")


def _receipt(
    db_session,
    *,
    status: str,
    observed_at: datetime,
) -> team_inbox_observations.ProviderObservationOutcome:
    event_id = f"receipt:provider-1:{status}:{observed_at.isoformat()}"
    recorded = team_inbox_observations.record_provider_observation(
        db_session,
        team_inbox_observations.RecordProviderObservationCommand(
            context=_context(event_id),
            provider=team_inbox_observations.InboxProvider.meta_cloud_api,
            provider_account_scope="waba-1",
            provider_event_id=event_id,
            kind=InboxObservationKind.delivery_receipt,
            channel_type=InboxChannelType.whatsapp,
            external_message_id="provider-1",
            observed_at=observed_at,
            payload=team_inbox_observations.DeliveryReceiptObservation(status=status),
        ),
    )
    return team_inbox_processing.process_provider_observation(
        db_session,
        observation_id=recorded.observation_id,
        context=_context(f"process:{event_id}"),
    )


def test_reordered_delivery_receipts_do_not_regress(db_session) -> None:
    conversation = InboxConversation(
        channel_type=InboxChannelType.whatsapp.value,
        contact_address="+2348035550114",
    )
    db_session.add(conversation)
    db_session.flush()
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=InboxChannelType.whatsapp.value,
        direction=InboxMessageDirection.outbound.value,
        external_message_id="provider-1",
        body="We are checking.",
        metadata_={},
    )
    db_session.add(message)
    db_session.commit()

    delivered_at = datetime(2026, 7, 22, 10, 5, tzinfo=UTC)
    _receipt(db_session, status="delivered", observed_at=delivered_at)
    result = _receipt(
        db_session,
        status="sent",
        observed_at=delivered_at - timedelta(minutes=2),
    )

    db_session.refresh(message)
    assert result.consequence_kind in {"ignored_reordered", "duplicate"}
    assert message.metadata_["delivery_status"] == "delivered"
    assert message.metadata_["delivery_status_at"] == delivered_at.isoformat()


def test_operator_read_cursor_and_unread_projection_are_idempotent(db_session) -> None:
    person_id = uuid4()
    received_at = datetime.now(UTC) - timedelta(minutes=1)
    conversation = InboxConversation(channel_type=InboxChannelType.email.value)
    db_session.add(conversation)
    db_session.flush()
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=InboxChannelType.email.value,
        direction=InboxMessageDirection.inbound.value,
        body="Unread",
        received_at=received_at,
    )
    db_session.add(message)
    db_session.flush()
    conversation_id = conversation.id
    message_id = message.id
    db_session.commit()

    assert (
        team_inbox_read_state.unread_conversation_count(db_session, person_id=person_id)
        == 1
    )
    db_session.commit()
    first = team_inbox_read_state.mark_conversation_read(
        db_session,
        team_inbox_read_state.MarkConversationReadCommand(
            context=_context("mark-read"),
            conversation_id=conversation_id,
            person_id=person_id,
            through_message_id=message_id,
            read_at=received_at + timedelta(seconds=1),
        ),
    )
    second = team_inbox_read_state.mark_conversation_read(
        db_session,
        team_inbox_read_state.MarkConversationReadCommand(
            context=_context("mark-read-retry"),
            conversation_id=conversation_id,
            person_id=person_id,
            through_message_id=message_id,
            read_at=received_at,
        ),
    )

    assert first.changed is True
    assert second.changed is False
    assert (
        team_inbox_read_state.unread_conversation_count(db_session, person_id=person_id)
        == 0
    )


def test_operator_unread_queries_have_fixed_query_budget(db_session) -> None:
    person_id = uuid4()
    observed_at = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
    conversation_ids = []
    expected_counts = {}
    for index in range(30):
        conversation = InboxConversation(channel_type=InboxChannelType.email.value)
        db_session.add(conversation)
        db_session.flush()
        conversation_ids.append(conversation.id)
        db_session.add_all(
            [
                InboxMessage(
                    conversation_id=conversation.id,
                    channel_type=InboxChannelType.email.value,
                    direction=InboxMessageDirection.inbound.value,
                    body="First",
                    received_at=observed_at,
                ),
                InboxMessage(
                    conversation_id=conversation.id,
                    channel_type=InboxChannelType.email.value,
                    direction=InboxMessageDirection.inbound.value,
                    body="Second",
                    received_at=observed_at + timedelta(minutes=2),
                ),
            ]
        )
        if index % 3 == 0:
            expected_counts[conversation.id] = 2
        else:
            cursor_at = observed_at + timedelta(minutes=1 if index % 3 == 1 else 3)
            db_session.add(
                InboxConversationReadState(
                    conversation_id=conversation.id,
                    person_id=person_id,
                    last_read_at=cursor_at,
                )
            )
            expected_counts[conversation.id] = 1 if index % 3 == 1 else 0
    db_session.commit()

    statements: list[str] = []

    def capture(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", capture)
    try:
        counts = team_inbox_read_state.conversation_unread_message_counts(
            db_session,
            conversation_ids=conversation_ids,
            person_id=person_id,
        )
        unread_ids = team_inbox_read_state.unread_conversation_ids(
            db_session,
            conversation_ids=conversation_ids,
            person_id=person_id,
        )
        fleet_count = team_inbox_read_state.unread_conversation_count(
            db_session,
            person_id=person_id,
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture)

    assert counts == expected_counts
    assert unread_ids == {
        conversation_id
        for conversation_id, count in expected_counts.items()
        if count > 0
    }
    assert fleet_count == 20
    assert len(statements) == 3


def test_realtime_rebuild_uses_durable_projection(db_session, monkeypatch) -> None:
    conversation = InboxConversation(
        channel_type=InboxChannelType.email.value,
        status="open",
    )
    db_session.add(conversation)
    db_session.flush()
    conversation_id = conversation.id
    db_session.commit()
    published: list[tuple[str, object, dict[str, object]]] = []

    monkeypatch.setattr(
        team_inbox_realtime,
        "publish_topic_event",
        lambda topic, *, event_type, payload: published.append(
            (topic, event_type, payload)
        ),
    )

    assert team_inbox_realtime.rebuild_conversation_projection(
        db_session, str(conversation_id)
    )
    assert published[0][2]["conversation_id"] == str(conversation_id)
    assert published[0][2]["projection_rebuilt"] is True
