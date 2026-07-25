from __future__ import annotations

import pytest

from app.services.action_forms import (
    ActionConfirmation,
    ActionField,
    ActionFieldKind,
    ActionForm,
    ActionFormSubmission,
    ActionHiddenValue,
    ActionOption,
    ActionTone,
)


def _form(*, allowed: bool = True, disabled_reason: str | None = None) -> ActionForm:
    return ActionForm(
        key="billing.verify",
        title="Verify transfer",
        description="Confirm the bank evidence.",
        action_url="/billing/verify",
        submit_label="Verify",
        tone=ActionTone.positive,
        impact="A payment will be created.",
        confirmation=ActionConfirmation(
            title="Confirm posting",
            message="Create the payment?",
        ),
        allowed=allowed,
        disabled_reason=disabled_reason,
        fields=(
            ActionField(
                key="amount",
                label="Amount",
                kind=ActionFieldKind.decimal,
                value="5000.00",
                required=True,
                min_value="0.01",
                step="0.01",
            ),
            ActionField(
                key="allocation",
                label="Allocation",
                kind=ActionFieldKind.select,
                value="yes",
                options=(
                    ActionOption(value="yes", label="Allocate"),
                    ActionOption(value="no", label="Keep as credit"),
                ),
            ),
        ),
    )


def test_submission_binding_preserves_values_and_structured_errors() -> None:
    submission = ActionFormSubmission.from_mapping(
        "billing.verify",
        {"amount": "bad", "allocation": "no"},
        field_errors={"amount": "Enter a valid amount"},
    )

    bound = _form().bind(submission)

    assert bound.field("amount").value == "bad"
    assert bound.field("amount").error == "Enter a valid amount"
    assert bound.field("allocation").value == "no"
    assert bound.general_error is None


def test_submission_can_carry_one_general_command_error() -> None:
    submission = ActionFormSubmission.from_mapping(
        "billing.verify",
        {"amount": "5000.00", "allocation": "yes"},
        general_error="Reference already verified",
    )

    bound = _form().bind(submission)

    assert bound.general_error == "Reference already verified"
    assert all(field.error is None for field in bound.fields)


def test_submission_restriction_drops_transport_fields_before_binding() -> None:
    submission = ActionFormSubmission.from_mapping(
        "billing.verify",
        {"amount": "5000.00", "allocation": "no", "csrf_token": "ignored"},
    )

    bound = _form().bind(submission.restrict({"amount", "allocation"}))

    assert bound.field("allocation").value == "no"


def test_contract_rejects_undeclared_fields_without_explicit_restriction() -> None:
    submission = ActionFormSubmission.from_mapping(
        "billing.verify",
        {"amount": "5000.00", "allocation": "yes", "surprise": "value"},
    )

    with pytest.raises(ValueError, match="Undeclared fields"):
        _form().bind(submission)


def test_visible_disabled_action_requires_an_explanation() -> None:
    with pytest.raises(ValueError, match="needs an explanation"):
        _form(allowed=False)

    disabled = _form(
        allowed=False,
        disabled_reason="This reference already backs a payment.",
    )
    assert disabled.allowed is False


def test_transport_owned_confirmation_cannot_be_forged_as_hidden_state() -> None:
    with pytest.raises(ValueError, match="Reserved or duplicate action-form keys"):
        ActionForm(
            key="billing.hidden-confirmation",
            title="Unsafe confirmation",
            description="A transport value must not stand in for operator consent.",
            action_url="/billing/unsafe",
            submit_label="Submit",
            fields=(),
            hidden_values=(ActionHiddenValue(key="confirmed", value="yes"),),
        )


def test_select_fields_require_unique_declared_options() -> None:
    with pytest.raises(ValueError, match="Duplicate options"):
        ActionField(
            key="choice",
            label="Choice",
            kind=ActionFieldKind.select,
            options=(
                ActionOption(value="same", label="One"),
                ActionOption(value="same", label="Two"),
            ),
        )
