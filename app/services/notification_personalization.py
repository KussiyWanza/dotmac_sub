"""Typed personalization contract for customer-facing email templates.

This is the editorial and rendering contract for the email catalog.  Template
rows remain editable data, but every email code must declare the facts it may
use, the action it should drive, and the safe fallback behavior for missing
facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class EmailPersonalizationSpec:
    """Typed contract for one email template code."""

    code: str
    purpose: str
    required: frozenset[str]
    optional: frozenset[str]
    primary_action: str
    missing_data_fallback: str

    @property
    def allowed(self) -> frozenset[str]:
        return self.required | self.optional


_COMMON = frozenset({"subscriber_name", "offer_name", "portal_url"})
_BILLING = frozenset({"invoice_number", "amount", "due_date", "invoice_url"})


def _spec(
    code: str,
    purpose: str,
    *,
    required: frozenset[str] = frozenset(),
    optional: frozenset[str] = frozenset(),
    primary_action: str,
    missing_data_fallback: str = "Omit the sentence and keep the action available.",
) -> EmailPersonalizationSpec:
    return EmailPersonalizationSpec(
        code=code,
        purpose=purpose,
        required=required,
        optional=optional,
        primary_action=primary_action,
        missing_data_fallback=missing_data_fallback,
    )


EMAIL_PERSONALIZATION_MATRIX: Final[dict[str, EmailPersonalizationSpec]] = {
    "subscriber_created": _spec(
        "subscriber_created",
        "Welcome",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"portal_url"}),
        primary_action="Open your account",
    ),
    "subscriber_updated": _spec(
        "subscriber_updated",
        "Profile change",
        required=frozenset({"subscriber_name", "updated_fields"}),
        optional=frozenset({"portal_url"}),
        primary_action="Review your profile",
    ),
    "subscription_created": _spec(
        "subscription_created",
        "Subscription created",
        required=_COMMON,
        primary_action="View your service",
    ),
    "subscription_activated": _spec(
        "subscription_activated",
        "Service activated",
        required=_COMMON,
        primary_action="Open your account",
    ),
    "subscription_suspended": _spec(
        "subscription_suspended",
        "Service suspended",
        required=_COMMON,
        optional=_BILLING,
        primary_action="Restore your service",
    ),
    "subscription_resumed": _spec(
        "subscription_resumed",
        "Service resumed",
        required=_COMMON,
        primary_action="Open your account",
    ),
    "subscription_canceled": _spec(
        "subscription_canceled",
        "Subscription canceled",
        required=_COMMON,
        primary_action="Contact support",
    ),
    "subscription_expiring": _spec(
        "subscription_expiring",
        "Renewal reminder",
        required=_COMMON,
        optional=frozenset({"renewed_through", "due_date", "amount"}),
        primary_action="Renew your service",
        missing_data_fallback="Never say ‘soon’; use the exact end date when supplied, otherwise say only that renewal is available.",
    ),
    "subscription_renewal_invoice_ready": _spec(
        "subscription_renewal_invoice_ready",
        "Renewal invoice",
        required=_COMMON,
        optional=_BILLING,
        primary_action="View and pay your renewal invoice",
    ),
    "subscription_expired": _spec(
        "subscription_expired",
        "Service expired",
        required=_COMMON,
        primary_action="Restore your service",
    ),
    "subscription_upgraded": _spec(
        "subscription_upgraded",
        "Plan upgraded",
        required=frozenset({"subscriber_name", "old_offer_name", "new_offer_name"}),
        optional=frozenset({"portal_url"}),
        primary_action="View your updated plan",
    ),
    "subscription_downgraded": _spec(
        "subscription_downgraded",
        "Plan changed",
        required=frozenset({"subscriber_name", "old_offer_name", "new_offer_name"}),
        optional=frozenset({"portal_url"}),
        primary_action="View your updated plan",
    ),
    "suspension_warning": _spec(
        "suspension_warning",
        "Payment reminder",
        required=frozenset({"subscriber_name", "invoice_number", "amount"}),
        optional=frozenset({"due_date", "portal_url"}),
        primary_action="Pay your invoice",
    ),
    "invoice_created": _spec(
        "invoice_created",
        "Invoice created",
        required=frozenset({"subscriber_name", "invoice_number", "amount", "due_date"}),
        optional=frozenset({"offer_name", "invoice_url", "portal_url"}),
        primary_action="View and pay your invoice",
    ),
    "invoice_sent": _spec(
        "invoice_sent",
        "Invoice delivery",
        required=frozenset({"subscriber_name", "invoice_number", "amount", "due_date"}),
        optional=frozenset({"invoice_url", "portal_url"}),
        primary_action="Review your invoice",
    ),
    "invoice_paid": _spec(
        "invoice_paid",
        "Invoice paid",
        required=frozenset({"subscriber_name", "invoice_number"}),
        optional=frozenset({"amount", "invoice_url", "portal_url"}),
        primary_action="View your billing history",
    ),
    "invoice_overdue": _spec(
        "invoice_overdue",
        "Overdue invoice",
        required=frozenset({"subscriber_name", "invoice_number", "amount"}),
        optional=frozenset({"due_date", "invoice_url", "portal_url"}),
        primary_action="Pay your overdue invoice",
    ),
    "payment_received": _spec(
        "payment_received",
        "Payment receipt",
        required=frozenset(
            {"subscriber_name", "amount", "receipt_number", "receipt_url"}
        ),
        primary_action="View your receipt",
    ),
    "prepaid_service_renewed": _spec(
        "prepaid_service_renewed",
        "Prepaid renewal",
        required=frozenset({"subscriber_name", "offer_name", "amount"}),
        optional=frozenset({"renewed_through", "portal_url"}),
        primary_action="View your renewed service",
    ),
    "payment_failed": _spec(
        "payment_failed",
        "Payment failure",
        required=frozenset({"subscriber_name", "amount"}),
        optional=frozenset({"invoice_number", "portal_url"}),
        primary_action="Retry your payment",
    ),
    "payment_refunded": _spec(
        "payment_refunded",
        "Payment refund",
        required=frozenset({"subscriber_name", "amount"}),
        optional=frozenset({"receipt_number", "portal_url"}),
        primary_action="Review your account",
    ),
    "payment_reversed": _spec(
        "payment_reversed",
        "Payment reversal",
        required=frozenset({"subscriber_name", "amount"}),
        optional=frozenset({"receipt_number", "portal_url"}),
        primary_action="Contact billing",
    ),
    "usage_warning": _spec(
        "usage_warning",
        "Usage warning",
        required=frozenset({"subscriber_name", "offer_name", "usage_percent"}),
        optional=frozenset({"portal_url"}),
        primary_action="Review your usage",
    ),
    "usage_exhausted": _spec(
        "usage_exhausted",
        "Usage exhausted",
        required=frozenset({"subscriber_name", "offer_name"}),
        optional=frozenset({"portal_url"}),
        primary_action="Restore your full speed",
    ),
    "provisioning_failed": _spec(
        "provisioning_failed",
        "Installation issue",
        required=_COMMON,
        optional=frozenset({"service_order_id", "portal_url"}),
        primary_action="View installation status",
    ),
    "service_order_assigned": _spec(
        "service_order_assigned",
        "Order assigned",
        required=frozenset({"subscriber_name", "service_order_id"}),
        optional=frozenset({"portal_url"}),
        primary_action="View your service order",
    ),
    "service_order_created": _spec(
        "service_order_created",
        "Order created",
        required=frozenset({"subscriber_name", "service_order_id"}),
        optional=frozenset({"portal_url"}),
        primary_action="View your service order",
    ),
    "service_order_completed": _spec(
        "service_order_completed",
        "Order complete",
        required=frozenset({"subscriber_name", "service_order_id"}),
        optional=frozenset({"portal_url"}),
        primary_action="View completed work",
    ),
    "service_outage": _spec(
        "service_outage",
        "Service outage",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"location", "portal_url"}),
        primary_action="View the service update",
    ),
    "service_restoration": _spec(
        "service_restoration",
        "Service restored",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"location", "portal_url"}),
        primary_action="Check your connection",
    ),
    "ont_offline": _spec(
        "ont_offline",
        "Connection offline",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"location", "device_serial", "portal_url"}),
        primary_action="Check your connection",
    ),
    "ont_online": _spec(
        "ont_online",
        "Connection restored",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"location", "device_serial", "portal_url"}),
        primary_action="Check your connection",
    ),
    "ont_signal_degraded": _spec(
        "ont_signal_degraded",
        "Connection quality",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"location", "device_serial", "portal_url"}),
        primary_action="Run a connection check",
    ),
    "ont_discovered": _spec(
        "ont_discovered",
        "Network device discovered",
        required=frozenset({"device_serial", "location"}),
        primary_action="Review the device assignment",
    ),
    "referral_reward_issued": _spec(
        "referral_reward_issued",
        "Referral reward",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"amount", "portal_url"}),
        primary_action="View your reward",
    ),
    "customer_support_availability": _spec(
        "customer_support_availability",
        "Support availability",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"portal_url"}),
        primary_action="Contact support",
    ),
    "important_account_information": _spec(
        "important_account_information",
        "Important account information",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"portal_url"}),
        primary_action="Review your account",
    ),
    "emergency_network_maintenance_gudu": _spec(
        "emergency_network_maintenance_gudu",
        "Network maintenance",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"location", "portal_url"}),
        primary_action="View the maintenance update",
    ),
    "intermittent_connectivity_email": _spec(
        "intermittent_connectivity_email",
        "Intermittent connectivity",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"location", "portal_url"}),
        primary_action="Report a connection issue",
    ),
    "internet_service_outage_update_karsana_axis": _spec(
        "internet_service_outage_update_karsana_axis",
        "Outage update",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"location", "portal_url"}),
        primary_action="View the outage update",
    ),
    "payment_method_paystack_only": _spec(
        "payment_method_paystack_only",
        "Payment method update",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"portal_url"}),
        primary_action="Update your payment method",
    ),
    "plan_change_approved": _spec(
        "plan_change_approved",
        "Plan change approved",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"old_offer_name", "new_offer_name", "portal_url"}),
        primary_action="View your updated plan",
    ),
    "plan_change_requested": _spec(
        "plan_change_requested",
        "Plan change requested",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"offer_name", "portal_url"}),
        primary_action="View your request",
    ),
    "provisioning_completed": _spec(
        "provisioning_completed",
        "Installation complete",
        required=_COMMON,
        primary_action="Open your service",
    ),
    "quote_accepted": _spec(
        "quote_accepted",
        "Quote accepted",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"portal_url"}),
        primary_action="View your next steps",
    ),
    "quote_sent": _spec(
        "quote_sent",
        "Quote delivery",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"portal_url"}),
        primary_action="Review your quote",
    ),
    "selfcare_billing_plan_change_guide": _spec(
        "selfcare_billing_plan_change_guide",
        "Billing and plan guide",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"portal_url"}),
        primary_action="Manage your account",
    ),
    "service_extended": _spec(
        "service_extended",
        "Service extended",
        required=_COMMON,
        optional=frozenset({"renewed_through", "portal_url"}),
        primary_action="View your service",
    ),
    "slow_browsing_support": _spec(
        "slow_browsing_support",
        "Slow browsing support",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"portal_url"}),
        primary_action="View support options",
    ),
    "technician_assigned": _spec(
        "technician_assigned",
        "Technician assignment",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"service_order_id", "location", "portal_url"}),
        primary_action="View your appointment",
    ),
    "ticket_created": _spec(
        "ticket_created",
        "Support ticket created",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"service_order_id", "portal_url"}),
        primary_action="View your support request",
    ),
    "ticket_resolved": _spec(
        "ticket_resolved",
        "Support ticket resolved",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"service_order_id", "portal_url"}),
        primary_action="Review the resolution",
    ),
    "ticket_updated": _spec(
        "ticket_updated",
        "Support ticket updated",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"service_order_id", "portal_url"}),
        primary_action="View the latest update",
    ),
    "update_customer_details": _spec(
        "update_customer_details",
        "Profile reminder",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"portal_url"}),
        primary_action="Update your details",
    ),
    "work_order_completed": _spec(
        "work_order_completed",
        "Work completed",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"service_order_id", "location", "portal_url"}),
        primary_action="Review completed work",
    ),
    "work_order_scheduled": _spec(
        "work_order_scheduled",
        "Work scheduled",
        required=frozenset({"subscriber_name"}),
        optional=frozenset({"service_order_id", "location", "portal_url"}),
        primary_action="View your appointment",
    ),
}


def personalization_spec(code: str) -> EmailPersonalizationSpec | None:
    """Return the typed contract for a template code, if catalogued."""

    return EMAIL_PERSONALIZATION_MATRIX.get(code)
