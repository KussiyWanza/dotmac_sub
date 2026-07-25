"""Read-side projection for reviewed payment-configuration actions."""

from __future__ import annotations

from app.services import payment_configuration_staff_actions as staff_actions
from app.services.action_forms import (
    ActionConfirmation,
    ActionForm,
    ActionHiddenValue,
    ActionTone,
)
from app.services.domain_errors import DomainError


def settings_url(resource: staff_actions.PaymentConfigurationResource) -> str:
    return {
        staff_actions.PaymentConfigurationResource.collection_account: (
            "/admin/settings/billing/collection-accounts"
        ),
        staff_actions.PaymentConfigurationResource.payment_channel: (
            "/admin/settings/billing/payment-channels"
        ),
        staff_actions.PaymentConfigurationResource.channel_mapping: (
            "/admin/settings/billing/payment-channel-accounts"
        ),
    }[resource]


def build_action_form(
    preview: staff_actions.PaymentConfigurationActionPreview,
) -> ActionForm:
    action_label = preview.action.value.replace("_", " ").title()
    resource_label = preview.resource.value.replace("_", " ")
    return ActionForm(
        key=f"payment_configuration.{preview.resource.value}.{preview.action.value}",
        title=f"{action_label} {resource_label}",
        description=(
            "Apply the exact reviewed lifecycle change. The owner rechecks current "
            "configuration and writes the decision audit atomically."
        ),
        action_url=(
            "/admin/settings/billing/payment-configuration/"
            f"{preview.resource.value}/{preview.resource_id}/"
            f"{preview.action.value}/confirm"
        ),
        submit_label=f"Confirm {action_label}",
        fields=(),
        hidden_values=(ActionHiddenValue("preview_fingerprint", preview.fingerprint),),
        confirmation=ActionConfirmation(
            title=f"Confirm {action_label.lower()}",
            message=(
                "I reviewed the exact configuration impact above and want to "
                "apply this lifecycle change."
            ),
        ),
        tone=(
            ActionTone.negative
            if preview.action is staff_actions.PaymentConfigurationAction.deactivate
            else ActionTone.positive
        ),
        impact=" · ".join(f"{fact.label}: {fact.value}" for fact in preview.facts),
        allowed=preview.allowed,
        disabled_reason=preview.blocked_reason,
    )


def review_state(
    db,
    *,
    resource: staff_actions.PaymentConfigurationResource,
    resource_id,
    action: staff_actions.PaymentConfigurationAction,
    page_error: str | None = None,
) -> dict[str, object]:
    preview = staff_actions.preview_staff_action(
        db,
        resource=resource,
        resource_id=resource_id,
        action=action,
    )
    return {
        "configuration_preview": preview,
        "configuration_action_form": build_action_form(preview),
        "cancel_url": settings_url(resource),
        "page_error": page_error,
    }


def error_status(error: DomainError) -> int:
    if error.code.endswith(".not_found"):
        return 404
    if error.code.endswith((".stale_preview", ".active_caller_transaction")):
        return 409
    return 400
