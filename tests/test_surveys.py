from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.db import finish_read_transaction
from app.models.comms import (
    SurveyInvitation,
    SurveyResponse,
    SurveyStatus,
    SurveyTriggerType,
)
from app.schemas.comms import SurveyCreate, SurveyUpdate
from app.services import surveys
from app.services.owner_commands import CommandContext
from tests.staff_identity_fixtures import add_bound_staff_user

ROOT = Path(__file__).resolve().parents[1]


def _context(label: str, *, key: str | None = None) -> CommandContext:
    return CommandContext.system(
        actor=str(uuid4()),
        scope="communications.surveys:write",
        reason=label,
        idempotency_key=key,
    )


def _create(
    db,
    *,
    name: str = "Customer Satisfaction",
    public_slug: str | None = None,
    questions: list[dict[str, object]] | None = None,
    trigger_type: SurveyTriggerType = SurveyTriggerType.manual,
    key: str | None = None,
):
    user, person = add_bound_staff_user(db)
    user_id = user.id
    person_id = person.id
    db.commit()
    idempotency_key = key or str(uuid4())
    outcome = surveys.create_survey(
        db,
        surveys.CreateSurveyCommand(
            payload=SurveyCreate(
                name=name,
                public_slug=public_slug,
                trigger_type=trigger_type,
                questions=questions or [],
            ),
            principal_id=user_id,
            principal_type="system_user",
            context=_context("create test Survey", key=idempotency_key),
        ),
    )
    return outcome, user_id, person_id


def test_survey_schema_normalizes_basic_fields_and_defaults() -> None:
    payload = SurveyCreate(
        name="  Customer feedback  ",
        description="   ",
        public_slug=" Customer_Feedback ",
        thank_you_message="   ",
        questions=[],
        status="active",
        created_by_id=str(uuid4()),
    )

    assert payload.name == "Customer feedback"
    assert payload.description is None
    assert payload.public_slug == "customer-feedback"
    assert payload.thank_you_message is None
    assert payload.trigger_type is SurveyTriggerType.manual
    assert "status" not in payload.model_fields_set


@pytest.mark.parametrize(
    "slug",
    ("-leading", "trailing-", "repeated--hyphen", "unsafe/slash", "UPPER--CASE"),
)
def test_survey_schema_rejects_malformed_public_slugs(slug: str) -> None:
    with pytest.raises(ValidationError):
        SurveyCreate(name="Feedback", public_slug=slug)


def test_question_contract_rejects_duplicate_keys_and_invalid_choices() -> None:
    with pytest.raises(ValidationError, match="duplicated"):
        SurveyCreate(
            name="Feedback",
            questions=[
                {"key": "q1", "type": "rating", "label": "First"},
                {"key": "q1", "type": "free_text", "label": "Second"},
            ],
        )

    invalid_payloads = (
        [{"key": "q1", "type": "unknown", "label": "Question"}],
        [
            {
                "key": "q1",
                "type": "multiple_choice",
                "label": "Question",
                "options": ["Only one"],
            }
        ],
        [
            {
                "key": "q1",
                "type": "multiple_choice",
                "label": "Question",
                "options": ["Yes", " yes "],
            }
        ],
        [
            {
                "key": "q1",
                "type": "multiple_choice",
                "label": "Question",
                "options": ["Yes", ""],
            }
        ],
    )
    for questions in invalid_payloads:
        with pytest.raises(ValidationError):
            SurveyCreate(name="Feedback", questions=questions)


def test_non_choice_question_clears_options_and_preserves_order() -> None:
    payload = SurveyCreate(
        name="Feedback",
        questions=[
            {
                "key": "custom_key",
                "type": "rating",
                "label": "First",
                "options": ["discard", "these"],
            },
            {"key": "q4", "type": "free_text", "label": "Second"},
        ],
    )

    assert [question.key for question in payload.questions] == ["custom_key", "q4"]
    assert payload.questions[0].options is None
    assert payload.questions[0].required is True


def test_legacy_update_activation_flag_remains_typed() -> None:
    payload = SurveyUpdate(is_active=False)

    assert payload.is_active is False
    assert payload.model_fields_set == {"is_active"}


def test_form_validation_rejects_malformed_json_and_preserves_valid_state() -> None:
    malformed = surveys.validate_form(
        surveys.SurveyFormValues(name="Feedback", questions_json="{")
    )
    assert malformed.payload is None
    assert malformed.field_errors["questions_json"] == "Questions must be valid JSON."

    invalid = surveys.validate_form(
        surveys.SurveyFormValues(
            name=" Feedback ",
            trigger_type="ticket_closed",
            public_slug="My_Survey",
            questions_json=json.dumps(
                [
                    {
                        "key": "q1",
                        "type": "multiple_choice",
                        "label": "Pick",
                        "required": False,
                        "options": ["One", ""],
                    }
                ]
            ),
        )
    )
    assert invalid.payload is None
    assert invalid.values.name == " Feedback "
    assert invalid.values.trigger_type == "ticket_closed"
    assert invalid.values.public_slug == "my-survey"
    preserved = cast(dict[str, object], invalid.questions_seed[0])
    assert preserved["required"] is False


def test_create_is_draft_idempotent_and_has_no_initial_side_effect_rows(db_session):
    key = str(uuid4())
    outcome, user_id, person_id = _create(db_session, key=key)
    survey = surveys.get_survey(db_session, outcome.survey_id)

    assert survey.status is SurveyStatus.draft
    assert survey.is_active is True
    assert survey.created_by_id == person_id
    assert survey.expires_at is None
    assert survey.segment_filter is None
    assert survey.total_invited == 0
    assert survey.total_responses == 0
    assert survey.avg_rating is None
    assert survey.nps_score is None
    assert db_session.query(SurveyInvitation).count() == 0
    assert db_session.query(SurveyResponse).count() == 0
    finish_read_transaction(db_session)

    replay = surveys.create_survey(
        db_session,
        surveys.CreateSurveyCommand(
            payload=SurveyCreate(name="Customer Satisfaction", questions=[]),
            principal_id=user_id,
            principal_type="system_user",
            context=_context("retry create test Survey", key=key),
        ),
    )
    assert replay.survey_id == outcome.survey_id
    assert replay.replayed is True


def test_create_requires_bound_staff_person(db_session) -> None:
    user, _person = add_bound_staff_user(db_session)
    user.person_party_id = None
    user.party_bound_at = None
    user.party_binding_source = None
    user.party_binding_reason = None
    user_id = user.id
    db_session.commit()

    with pytest.raises(surveys.SurveyDomainError, match="not linked to a Person"):
        surveys.create_survey(
            db_session,
            surveys.CreateSurveyCommand(
                payload=SurveyCreate(name="Feedback"),
                principal_id=user_id,
                principal_type="system_user",
                context=_context("create without person", key=str(uuid4())),
            ),
        )


def test_duplicate_public_slug_is_a_friendly_domain_error(db_session) -> None:
    _create(db_session, public_slug="shared-slug")
    with pytest.raises(surveys.SurveyDomainError) as caught:
        _create(db_session, public_slug="shared_slug")
    assert caught.value.code == "communications.surveys.public_slug_duplicate"
    assert caught.value.field == "public_slug"


def test_empty_draft_cannot_activate_or_distribute(db_session) -> None:
    outcome, _user, _person = _create(db_session)

    with pytest.raises(surveys.SurveyDomainError, match="at least one"):
        surveys.transition_survey(
            db_session,
            surveys.SurveyLifecycleCommand(
                survey_id=outcome.survey_id,
                action=surveys.SurveyLifecycleAction.activate,
                context=_context("activate empty Survey"),
            ),
        )
    with pytest.raises(surveys.SurveyDomainError, match="at least one"):
        surveys.send_survey(
            db_session,
            surveys.SendSurveyCommand(
                survey_id=outcome.survey_id,
                subscriber_ids=(),
                context=_context("send empty Survey", key=str(uuid4())),
            ),
        )


def test_public_access_and_automatic_selection_require_active_lifecycle(db_session):
    question = {"key": "q1", "type": "rating", "label": "Rate us"}
    outcome, _user, _person = _create(
        db_session,
        public_slug="active-feedback",
        questions=[question],
        trigger_type=SurveyTriggerType.ticket_closed,
    )

    with pytest.raises(surveys.SurveyDomainError):
        surveys.get_public_survey(db_session, "active-feedback")
    assert (
        surveys.eligible_automatic_surveys(db_session, SurveyTriggerType.ticket_closed)
        == []
    )
    finish_read_transaction(db_session)

    surveys.transition_survey(
        db_session,
        surveys.SurveyLifecycleCommand(
            survey_id=outcome.survey_id,
            action=surveys.SurveyLifecycleAction.activate,
            context=_context("activate valid Survey"),
        ),
    )
    assert (
        surveys.get_public_survey(db_session, "active-feedback").id == outcome.survey_id
    )
    assert [
        item.id
        for item in surveys.eligible_automatic_surveys(
            db_session, SurveyTriggerType.ticket_closed
        )
    ] == [outcome.survey_id]
    finish_read_transaction(db_session)

    surveys.transition_survey(
        db_session,
        surveys.SurveyLifecycleCommand(
            survey_id=outcome.survey_id,
            action=surveys.SurveyLifecycleAction.pause,
            context=_context("pause Survey"),
        ),
    )
    with pytest.raises(surveys.SurveyDomainError):
        surveys.get_public_survey(db_session, "active-feedback")


def test_reactivation_restores_compatibility_flag(db_session) -> None:
    outcome, _user, _person = _create(
        db_session,
        questions=[{"key": "q1", "type": "rating", "label": "Rate us"}],
    )
    surveys.update_survey(
        db_session,
        surveys.UpdateSurveyCommand(
            survey_id=outcome.survey_id,
            payload=SurveyUpdate(is_active=False),
            context=_context("disable Survey"),
        ),
    )
    surveys.transition_survey(
        db_session,
        surveys.SurveyLifecycleCommand(
            survey_id=outcome.survey_id,
            action=surveys.SurveyLifecycleAction.activate,
            context=_context("reactivate Survey"),
        ),
    )
    survey = surveys.get_survey(db_session, outcome.survey_id)
    assert survey.status is SurveyStatus.active
    assert survey.is_active is True


def test_response_validation_is_authoritative_and_updates_metrics(db_session):
    outcome, _user, _person = _create(
        db_session,
        public_slug="response-feedback",
        questions=[
            {"key": "rating", "type": "rating", "label": "Rate us"},
            {
                "key": "recommend",
                "type": "nps",
                "label": "Recommend us",
                "required": False,
            },
        ],
    )
    surveys.transition_survey(
        db_session,
        surveys.SurveyLifecycleCommand(
            survey_id=outcome.survey_id,
            action=surveys.SurveyLifecycleAction.activate,
            context=_context("activate response Survey"),
        ),
    )

    with pytest.raises(surveys.SurveyDomainError, match="required"):
        surveys.submit_response(
            db_session,
            surveys.SubmitSurveyResponseCommand(
                public_reference="response-feedback",
                invitation_token=None,
                answers=(),
                work_order_id=None,
                ticket_id=None,
                context=_context("invalid public response", key=str(uuid4())),
            ),
        )
    response = surveys.submit_response(
        db_session,
        surveys.SubmitSurveyResponseCommand(
            public_reference="response-feedback",
            invitation_token=None,
            answers=(
                surveys.SurveyAnswer("rating", "5"),
                surveys.SurveyAnswer("recommend", "10"),
            ),
            work_order_id=None,
            ticket_id=None,
            context=_context("valid public response", key=str(uuid4())),
        ),
    )
    stored = surveys.get_response(db_session, response.response_id)
    survey = surveys.get_survey(db_session, outcome.survey_id)
    assert stored.responses == {"rating": "5", "recommend": "10"}
    assert stored.rating == 5
    assert stored.nps_value == 10
    assert survey.total_responses == 1
    assert float(survey.avg_rating) == 5.0
    assert float(survey.nps_score) == 100.0


def test_survey_templates_cover_creation_contract() -> None:
    index = (ROOT / "templates/admin/surveys/index.html").read_text(encoding="utf-8")
    form = (ROOT / "templates/admin/surveys/form.html").read_text(encoding="utf-8")
    macros = (ROOT / "templates/components/ui/macros.html").read_text(encoding="utf-8")

    assert 'ui.action_button("New Survey", "/admin/surveys/new"' in index
    assert "icon=ui.icon_plus()" in index
    assert 'aria-hidden="true"' in macros[macros.index("macro icon_plus") :]
    assert "New Survey - Admin" in form
    assert "Create Survey" in form
    assert "max-w-3xl" in form and "mx-auto" in form
    assert form.count('ui.card(title="') == 2
    assert 'ui.card(title="Basic Information"' in form
    assert 'ui.card(title="Questions"' in form
    assert form.count("<form ") == 1
    assert 'name="_csrf_token"' in form
    assert 'name="questions_json"' in form
    assert "md:grid-cols-2" in form
    assert "lg:grid-cols-3" in form
    assert "dark:" in form
    assert "Submitting..." in form
    assert "guardSubmit" in form
    assert "highest = Math.max" in form
    assert "options: null" in form
    assert "question.options = null" in form
    assert "question.options.length <= 2" in form


def test_admin_create_adapter_uses_html_form_defaults_and_http_303() -> None:
    source = (ROOT / "app/web/admin/surveys.py").read_text(encoding="utf-8")

    assert "Form(...)" not in source
    assert "status_code=303" in source
    assert 'require_permission("customer:write")' in source
    assert "Survey(" not in source
    assert "db.commit" not in source
