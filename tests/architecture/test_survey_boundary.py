from __future__ import annotations

from pathlib import Path

from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_survey_owner_has_complete_typed_contract() -> None:
    service = service_relationship("communications.surveys")

    assert service.module == "app.services.surveys"
    assert service.contract is not None
    assert {concern.name for concern in service.contract.concerns} == set(service.owns)
    assert service.contract.transaction.mode.value == "owner_managed"


def test_retired_comms_survey_writers_cannot_return() -> None:
    source = _source("app/services/comms.py")

    assert "class Surveys" not in source
    assert "class SurveyResponses" not in source
    assert "Survey(**" not in source
    assert "SurveyResponse(**" not in source


def test_survey_adapters_do_not_write_or_complete_transactions() -> None:
    for path in (
        "app/web/admin/surveys.py",
        "app/web/public/surveys.py",
        "app/api/comms.py",
        "app/services/events/handlers/surveys.py",
    ):
        source = _source(path)
        assert "db.commit" not in source
        assert "db.rollback" not in source
        assert "db.add(" not in source
        assert "Survey(" not in source
        assert "SurveyResponse(" not in source
        assert "SurveyInvitation(" not in source


def test_public_and_trigger_reads_fail_closed() -> None:
    source = _source("app/services/surveys.py")

    assert ".filter(Survey.status == SurveyStatus.active)" in source
    assert ".filter(Survey.is_active.is_(True))" in source
    assert "SurveyTriggerType.ticket_closed" in source
    assert "SurveyTriggerType.work_order_completed" in source
    assert "source_event_id" in source
    assert "def rebuild_survey_projections(" in source


def test_survey_trigger_handler_consumes_existing_owner_events() -> None:
    source = _source("app/services/events/handlers/surveys.py")

    assert "EventType.ticket_resolution_confirmed" in source
    assert "EventType.work_order_field_outcome_recorded" in source
    assert 'event.payload.get("outcome") != "complete"' in source
    assert "create_trigger_invitations" in source
