"""PII-free Fiber conversion lifecycle projection guarantees."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.models.party import Party
from app.models.sales import Lead, LeadConversionMilestone, LeadOriginCapture
from app.services.events.types import Event, EventType
from app.services.marketing_conversion_projection import (
    ConversionStage,
    _stages_for_event,
    project_conversion_event,
)
from app.services.owner_commands import CommandContext


def _fiber_origin(db_session) -> tuple[UUID, UUID]:
    now = datetime(2026, 9, 19, 11, 30, tzinfo=UTC)
    party = Party(party_type="person", display_name="Private Customer")
    db_session.add(party)
    db_session.flush()
    lead = Lead(
        party_id=party.id,
        party_bound_at=now,
        party_binding_source="pytest",
        party_binding_reason="exact identity fixture",
        title="Fiber inquiry",
        lead_source="Website",
        status="new",
    )
    db_session.add(lead)
    db_session.flush()
    origin = LeadOriginCapture(
        lead_id=lead.id,
        source_interaction_id="conversion-projection-fixture",
        capture_method="landing_page",
        source_platform="website",
        lead_source="Website",
        journey_id=uuid4(),
        customer_reference="FBR-ABCDEF12",
        external_form_id="fiber-coverage-v1",
        utm_source="google",
        utm_medium="cpc",
        utm_campaign="abuja_home",
        utm_content="search_01",
        utm_term="fiber internet abuja",
        landing_path="/coverage/",
        captured_at=now,
        submitted_at=now,
        capture_source="pytest",
        capture_reason="signed Fiber inquiry fixture",
    )
    db_session.add(origin)
    lead_id = lead.id
    origin_id = origin.id
    db_session.commit()
    return lead_id, origin_id


def test_all_required_lifecycle_events_map_to_exact_stages() -> None:
    cases = (
        (EventType.lead_created, {}, (ConversionStage.visitor, ConversionStage.lead)),
        (
            EventType.fiber_coverage_evaluated,
            {},
            (ConversionStage.coverage_check,),
        ),
        (
            EventType.lead_updated,
            {"status": "qualified"},
            (ConversionStage.qualified_lead,),
        ),
        (EventType.payment_received, {}, (ConversionStage.payment,)),
        (EventType.appointment_scheduled, {}, (ConversionStage.installation,)),
        (
            EventType.subscription_activated,
            {},
            (ConversionStage.activated_subscriber,),
        ),
    )
    for event_type, payload, expected in cases:
        assert (
            _stages_for_event(Event(event_type=event_type, payload=payload)) == expected
        )

    assert not hasattr(ConversionStage, "registration")


def test_projection_is_idempotent_and_emits_no_pii(db_session, monkeypatch) -> None:
    lead_id, origin_id = _fiber_origin(db_session)
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        "app.services.marketing_conversion_projection.settings",
        SimpleNamespace(conversion_ingest_api_key="test-conversion-key"),
    )
    monkeypatch.setattr(
        "app.services.marketing_conversion_projection.emit_event",
        lambda _db, _event_type, payload, **_kwargs: emitted.append(payload),
    )
    event = Event(
        event_type=EventType.lead_created,
        payload={
            "lead_id": str(lead_id),
            "origin_capture_id": str(origin_id),
        },
    )

    for attempt in range(2):
        project_conversion_event(
            db_session,
            event=event,
            context=CommandContext.system(
                actor="pytest",
                scope="marketing:conversion-projection",
                reason="test idempotency",
                idempotency_key=f"projection-attempt:{attempt}",
            ),
        )

    assert db_session.query(LeadConversionMilestone).count() == 2
    assert len(emitted) == 2
    assert {item["stage"] for item in emitted} == {"visitor", "lead"}
    forbidden = {"full_name", "name", "phone", "email", "address"}
    assert all(forbidden.isdisjoint(item) for item in emitted)
    assert all(len(str(item["subject_key"])) == 64 for item in emitted)
