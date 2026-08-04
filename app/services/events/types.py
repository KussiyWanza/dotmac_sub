"""Event types and data structures for the event system.

This module defines all event types used throughout the application,
plus the Event dataclass that encapsulates event data.
"""

import enum
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


class EventType(enum.Enum):
    """All event types supported by the event system (~40 events).

    Event naming convention: {entity}.{action}
    """

    # Subscriber events
    subscriber_created = "subscriber.created"
    subscriber_updated = "subscriber.updated"
    subscriber_billing_approval_changed = "subscriber.billing_approval_changed"
    subscriber_suspended = "subscriber.suspended"
    subscriber_reactivated = "subscriber.reactivated"
    subscriber_throttled = "subscriber.throttled"
    subscriber_unthrottled = "subscriber.unthrottled"

    # Subscription events (8)
    subscription_created = "subscription.created"
    subscription_activated = "subscription.activated"
    subscription_suspended = "subscription.suspended"
    subscription_resumed = "subscription.resumed"
    subscription_disabled = "subscription.disabled"
    subscription_canceled = "subscription.canceled"
    subscription_upgraded = "subscription.upgraded"
    subscription_downgraded = "subscription.downgraded"
    subscription_expiring = "subscription.expiring"
    subscription_renewal_invoice_ready = "subscription.renewal_invoice_ready"
    subscription_expired = "subscription.expired"
    subscription_suspension_warning = "subscription.suspension_warning"
    subscription_deleted = "subscription.deleted"
    subscription_correction_applied = "subscription.correction_applied"
    access_credential_binding_changed = "access_credential.binding_changed"

    # Billing - Invoice events (4)
    invoice_created = "invoice.created"
    invoice_sent = "invoice.sent"
    invoice_paid = "invoice.paid"
    invoice_overdue = "invoice.overdue"

    # Billing - Payment events (4)
    payment_received = "payment.received"
    payment_failed = "payment.failed"
    payment_refunded = "payment.refunded"
    payment_reversed = "payment.reversed"
    payment_provider_event_processed = "payment_provider_event.processed"
    payment_provider_event_failed = "payment_provider_event.failed"
    payment_gateway_finance_identity_ensured = (
        "payment_gateway.finance_identity_ensured"
    )
    integration_installation_manifest_adopted = (
        "integration.installation.manifest_adopted"
    )
    integration_installation_capability_provisioned = (
        "integration.installation.capability_provisioned"
    )
    integration_job_capability_activated = "integration.job.capability_activated"
    oauth_token_refreshed = "oauth_token.refreshed"
    oauth_token_refresh_failed = "oauth_token.refresh_failed"
    account_credit_deposited = "account_credit.deposited"
    prepaid_service_renewed = "prepaid_service.renewed"
    subscription_billing_treatment_changed = "subscription_billing_treatment.changed"
    subscription_service_granted = "subscription_service.granted"
    billing_shadow_delivery_recorded = "billing.shadow_delivery.recorded"
    billing_cutover_verification_recorded = "billing.cutover_verification.recorded"
    billing_cutover_verification_approved = "billing.cutover_verification.approved"
    customer_subledger_opening_positions_captured = (
        "customer_subledger.opening_positions_captured"
    )
    customer_subledger_authority_activated = "customer_subledger.authority_activated"

    # Billing - Bank-transfer evidence lifecycle
    payment_proof_submitted = "payment_proof.submitted"
    payment_proof_verified = "payment_proof.verified"
    payment_proof_rejected = "payment_proof.rejected"
    payment_proof_corrected = "payment_proof.corrected"
    topup_intent_direct_transfer_created = "topup_intent.direct_transfer_created"
    topup_intent_direct_transfer_canceled = "topup_intent.direct_transfer_canceled"
    topup_intent_direct_transfer_submitted = "topup_intent.direct_transfer_submitted"
    topup_intent_direct_transfer_proof_rejected = (
        "topup_intent.direct_transfer_proof_rejected"
    )
    topup_intent_completed = "topup_intent.completed"
    topup_intent_expired = "topup_intent.expired"
    topup_intent_gateway_created = "topup_intent.gateway_created"
    topup_intent_failed = "topup_intent.failed"
    withholding_tax_receivable_recorded = "withholding_tax.receivable_recorded"
    withholding_tax_status_changed = "withholding_tax.status_changed"

    # Billing - Consolidated billing account payment (1)
    billing_account_payment_received = "billing_account.payment_received"

    # Billing - Payment arrangement events (1)
    arrangement_defaulted = "arrangement.defaulted"

    # Prepaid enforcement control-state evidence
    prepaid_enforcement_timer_changed = "prepaid_enforcement.timer_changed"
    prepaid_coverage_reconciled = "prepaid_coverage.reconciled"
    prepaid_renewal_terms_backfilled = "prepaid_renewal_terms.backfilled"
    prepaid_renewal_terms_corrected = "prepaid_renewal_terms.corrected"
    prepaid_renewal_terms_audited = "prepaid_renewal_terms.audited"
    prepaid_proforma_adopted = "prepaid_proforma.adopted"
    prepaid_draft_reconciled = "prepaid_draft.reconciled"
    prepaid_billing_calendar_reconciled = "prepaid_billing_calendar.reconciled"
    ip_assignment_service_ownership_reconciled = (
        "ip_assignment.service_ownership_reconciled"
    )
    ip_assignment_lifecycle_repaired = "ip_assignment.lifecycle_repaired"
    ip_assignment_served_projection_repaired = (
        "ip_assignment.served_projection_repaired"
    )

    # Billing - Outage compensation
    service_extension_created = "billing.service_extension_created"
    service_extension_applied = "billing.service_extension_applied"
    service_extension_canceled = "billing.service_extension_canceled"
    service_extension_anchor_repaired = "billing.service_extension_anchor_repaired"
    service_extended = "billing.service_extended"

    # Usage events (5)
    usage_recorded = "usage.recorded"
    usage_warning = "usage.warning"
    usage_exhausted = "usage.exhausted"
    usage_topped_up = "usage.topped_up"
    addon_expiring = "usage.addon_expiring"
    fup_runtime_state_changed = "fup.runtime_state_changed"
    fup_policy_changed = "fup_policy.changed"

    # Operations - Provisioning events (3)
    provisioning_started = "provisioning.started"
    provisioning_completed = "provisioning.completed"
    provisioning_failed = "provisioning.failed"

    # Operations - Service Order events (5)
    service_order_created = "service_order.created"
    service_order_assigned = "service_order.assigned"
    service_order_activation_requested = "service_order.activation_requested"
    service_order_completed = "service_order.completed"
    service_order_recovered = "service_order.recovered"

    # Shared operational service-team lifecycle
    service_team_changed = "service_team.changed"
    service_team_membership_changed = "service_team.membership_changed"
    # Retired producer; retained so durable historical events remain decodable.
    service_team_party_cutover_adopted = "service_team.party_cutover_adopted"
    workqueue_action_coordinated = "workqueue.action_coordinated"

    # Operations - vendor installation project lifecycle
    # Materials / vendor / ERP chain outputs
    # (docs/designs/MATERIALS_VENDOR_ERP_CHAIN.md). Staged atomically with
    # each owning transition; the materials lifecycle projection handler
    # applies cross-owner consequences with durable receipts.
    field_material_request_approved = "field_material_request.approved"
    field_material_request_fulfilled = "field_material_request.fulfilled"
    field_material_consumption_recorded = "field_material.consumption_recorded"
    vendor_purchase_invoice_approved = "vendor_purchase_invoice.approved"
    vendor_purchase_invoice_payment_observed = (
        "vendor_purchase_invoice.payment_observed"
    )
    vendor_material_release_requested = "vendor_material_release.requested"
    vendor_material_release_reviewed = "vendor_material_release.reviewed"
    vendor_material_release_issued = "vendor_material_release.issued"
    vendor_advance_requested = "vendor_advance.requested"
    vendor_advance_reviewed = "vendor_advance.reviewed"
    vendor_advance_settled = "vendor_advance.settled"
    vendor_project_published = "vendor_project.published"
    vendor_project_assigned = "vendor_project.assigned"
    vendor_project_started = "vendor_project.started"
    vendor_project_completed = "vendor_project.completed"
    vendor_quote_changed = "vendor_quote.changed"
    vendor_purchase_invoice_changed = "vendor_purchase_invoice.changed"
    vendor_route_revision_changed = "vendor_route_revision.changed"
    vendor_route_revision_accepted = "vendor_route_revision.accepted"
    vendor_route_revision_rejected = "vendor_route_revision.rejected"
    vendor_as_built_submitted = "vendor_as_built.submitted"
    vendor_submission_confirmed = "vendor_submission.confirmed"
    vendor_project_verified = "vendor_project.verified"
    vendor_project_rework_requested = "vendor_project.rework_requested"
    vendor_as_built_accepted = "vendor_as_built.accepted"
    vendor_as_built_rejected = "vendor_as_built.rejected"

    # Operations - Appointment events (2)
    appointment_scheduled = "appointment.scheduled"
    appointment_missed = "appointment.missed"

    # Native sales vertical events. Future agent/inbox
    # lead/quote lifecycle was webhook-silent in the CRM's event system;
    # automation consumes these.
    lead_created = "lead.created"
    lead_account_converted = "lead.account_converted"
    quote_created = "quote.created"
    quote_accepted = "quote.accepted"
    sales_order_paid = "sales_order.paid"
    sales_order_funding_satisfied = "sales_order.funding_satisfied"
    sales_order_fulfilled = "sales_order.fulfilled"
    project_created = "project.created"
    installation_scope_created = "installation_scope.created"
    implementation_released = "implementation.released"
    service_order_released = "service_order.released"
    customer_experience_ready = "customer_experience.ready"
    customer_experience_accepted = "customer_experience.accepted"
    customer_experience_needs_attention = "customer_experience.needs_attention"

    # Network events (5)
    device_offline = "device.offline"
    device_online = "device.online"
    device_projection_reconciled = "device_projection.reconciled"
    session_started = "session.started"
    session_ended = "session.ended"

    # OLT events (3)
    olt_created = "olt.created"
    olt_updated = "olt.updated"
    olt_deleted = "olt.deleted"

    # ONT events (5)
    ont_discovered = "ont.discovered"
    ont_online = "ont.online"
    ont_offline = "ont.offline"
    ont_signal_degraded = "ont.signal_degraded"
    ont_signal_delta = "ont.signal_delta"
    ont_config_updated = "ont.config_updated"
    ont_moved = "ont.moved"
    ont_feature_toggled = "ont.feature_toggled"
    ont_ddm_alert = "ont.ddm_alert"

    # ONT destructive operations (audit events)
    ont_authorized = "ont.authorized"
    ont_deauthorized = "ont.deauthorized"
    ont_factory_reset = "ont.factory_reset"
    ont_rebooted = "ont.rebooted"
    ont_service_port_created = "ont.service_port_created"
    ont_service_port_deleted = "ont.service_port_deleted"
    ont_tr069_bound = "ont.tr069_bound"
    ont_commissioning_requested = "ont.commissioning_requested"
    ont_commissioning_state_changed = "ont.commissioning_state_changed"

    # Fiber splice plans (cut sheets)
    fiber_splice_plan_issued = "fiber.splice_plan_issued"
    fiber_splice_plan_cancelled = "fiber.splice_plan_cancelled"
    fiber_splice_plan_item_executed = "fiber.splice_plan_item_executed"

    # ONT credential changes (audit events)
    ont_pppoe_credentials_set = "ont.pppoe_credentials_set"
    # Emitted when the derived CPE dialer projection is converged back onto the
    # authoritative access credential. Payload is fingerprint-only — it never
    # carries a username or a secret.
    ont_dialer_credential_reconciled = "ont.dialer_credential_reconciled"
    ont_wifi_password_set = "ont.wifi_password_set"
    ont_wifi_config_updated = "ont.wifi_config_updated"

    # ONT lifecycle events
    ont_decommissioned = "ont.decommissioned"

    # Fiber plant events. Emitted by network.as_built_plant_projection when the
    # cable an accepted vendor as-built proved was built is bound to two
    # terminations and put into service — the moment it becomes visible to
    # every is_active-filtered plant and map read.
    fiber_segment_activated = "fiber_segment.activated"

    # OLT circuit breaker events
    olt_circuit_opened = "olt.circuit_opened"
    olt_circuit_closed = "olt.circuit_closed"

    # Collections - Dunning events (4)
    dunning_started = "dunning.started"
    dunning_action_executed = "dunning.action_executed"
    dunning_resolved = "dunning.resolved"
    dunning_paused = "dunning.paused"

    # Enforcement locks (2)
    enforcement_lock_created = "enforcement_lock.created"
    enforcement_lock_resolved = "enforcement_lock.resolved"

    # Network alert (legacy, kept for compatibility)
    network_alert = "network.alert"

    # Customer portal events (4)
    customer_login = "customer.login"
    customer_logout = "customer.logout"
    customer_ticket_created = "customer.ticket_created"

    # Identity / onboarding invitation lifecycle
    # (docs/designs/IDENTITY_ONBOARDING_CHAIN.md)
    access_invitation_issued = "access_invitation.issued"
    access_invitation_accepted = "access_invitation.accepted"
    access_invitation_expired = "access_invitation.expired"

    # Support ticket / work-order lifecycle outputs
    # (docs/designs/TICKET_WORK_ORDER_HANDOFF_SOT.md). Staged atomically with
    # each owning transition; the support lifecycle projection handler
    # applies cross-owner consequences with durable receipts.
    ticket_resolution_requested = "ticket.resolution_requested"
    ticket_resolution_confirmed = "ticket.resolution_confirmed"
    ticket_resolution_disputed = "ticket.resolution_disputed"
    ticket_merged = "ticket.merged"
    ticket_work_order_issued = "ticket.work_order_issued"
    work_order_field_outcome_recorded = "work_order.field_outcome_recorded"
    customer_password_changed = "customer.password_changed"  # noqa: S105

    # Reseller events (5)
    reseller_created = "reseller.created"
    reseller_user_provisioned = "reseller_user.provisioned"
    reseller_login = "reseller.login"
    reseller_logout = "reseller.logout"
    reseller_impersonated = "reseller.impersonated"

    # Staff and subscriber identity/authorization lifecycle (6)
    vendor_user_provisioned = "vendor_user.provisioned"
    vendor_user_revoked = "vendor_user.revoked"
    vendor_user_role_changed = "vendor_user.role_changed"
    staff_account_provisioned = "staff_account.provisioned"
    staff_account_roles_changed = "staff_account.roles_changed"
    staff_account_activated = "staff_account.activated"
    staff_account_deactivated = "staff_account.deactivated"
    system_user_assignments_changed = "system_user.assignments_changed"
    subscriber_assignments_changed = "subscriber.assignments_changed"

    # Credential recovery lifecycle (2)
    password_recovery_requested = "password_recovery.requested"
    password_recovery_completed = "password_recovery.completed"

    # Referral-created customer credential enrollment lifecycle (2)
    customer_credential_enrollment_requested = (
        "customer_credential_enrollment.requested"
    )
    customer_credential_enrollment_completed = (
        "customer_credential_enrollment.completed"
    )

    # Referral program lifecycle (7) and account conversion lifecycle (1)
    referral_code_issued = "referral_code.issued"
    referral_captured = "referral.captured"
    referral_subscriber_attached = "referral.subscriber_attached"
    referral_qualified = "referral.qualified"
    referral_expired = "referral.expired"
    referral_rejected = "referral.rejected"
    referral_reward_issued = "referral.reward_issued"
    referral_reward_reconciled = "referral.reward_reconciled"
    referral_account_converted = "referral_account.converted"

    # Account-adjustment financial evidence lifecycle (2)
    account_adjustment_confirmed = "account_adjustment.confirmed"
    account_adjustment_reversed = "account_adjustment.reversed"

    # RBAC catalog events (2)
    rbac_role_catalog_changed = "rbac.role_catalog_changed"
    rbac_permission_catalog_changed = "rbac.permission_catalog_changed"

    # NAS events (7)
    nas_device_created = "nas_device.created"
    nas_device_updated = "nas_device.updated"
    nas_device_deleted = "nas_device.deleted"
    nas_backup_completed = "nas_backup.completed"
    nas_backup_failed = "nas_backup.failed"
    nas_provisioning_completed = "nas_provisioning.completed"
    nas_provisioning_failed = "nas_provisioning.failed"

    # TR-069 events (6)
    tr069_job_accepted = "tr069_job.accepted"
    tr069_job_completed = "tr069_job.completed"
    tr069_job_failed = "tr069_job.failed"
    tr069_job_unverified = "tr069_job.unverified"
    tr069_device_discovered = "tr069_device.discovered"
    tr069_device_stale = "tr069_device.stale"

    # Outage classifier customer notifications (design docs/designs/OUTAGE_CLASSIFIER.md
    # §P4). Emitted by the outage notifier so the notification system owns channel
    # selection + per-subscriber preferences; the notifier only supplies content.
    outage_area = "outage.area"
    outage_last_mile = "outage.last_mile"
    # Operator-confirmed cabinet (FDH) service notice dispatched
    # (network.cabinet_notice). Operational breadcrumb only: customer emails
    # are queued directly through communication intents, never via this event.
    cabinet_notice_dispatched = "cabinet_notice.dispatched"
    # Effective-dated SLA policy version recorded (customer.service_level,
    # OUTAGE_SLA_SPINE §4). Provenance breadcrumb: the authoritative record
    # is the immutable sla_policy_versions row itself.
    sla_policy_version_recorded = "sla_policy_version.recorded"
    # Customer outage communications pass completed
    # (network.outage_communications, OUTAGE_SLA_SPINE §3). Operational
    # breadcrumb only, same as the cabinet notice: the customer messages
    # themselves are communication intents, never this event.
    outage_customer_notice_dispatched = "outage_customer_notice.dispatched"

    # Outage incident lifecycle outputs
    # (docs/designs/NETWORK_OUTAGE_RESPONSE_LIFECYCLE.md). Staged atomically
    # with each incident transition; the registered outage lifecycle
    # projection handler applies cross-owner consequences. The legacy
    # ``network.alert`` fan-out keeps its payload for external webhooks.
    outage_created = "outage.created"
    outage_suspected = "outage.suspected"
    outage_confirmed = "outage.confirmed"
    outage_clearing = "outage.clearing"
    outage_reopened = "outage.reopened"
    outage_rerooted = "outage.rerooted"
    outage_discarded = "outage.discarded"
    outage_resolved = "outage.resolved"

    # Planned maintenance lifecycle outputs (docs/designs/OUTAGE_SLA_SPINE.md
    # §5). Staged atomically with each window transition by
    # network.maintenance_lifecycle; no projection handler consumes them yet.
    maintenance_announced = "maintenance.announced"
    maintenance_started = "maintenance.started"
    maintenance_completed = "maintenance.completed"
    maintenance_canceled = "maintenance.canceled"
    maintenance_overrun = "maintenance.overrun"

    # Custom event type for extensibility
    custom = "custom"


class AccountCreditFundingOrigin(str, enum.Enum):
    """Closed provenance vocabulary for account-credit funding events."""

    account_credit_deposit = "account_credit_deposit"
    verified_invoice_payment = "verified_invoice_payment"


class AccountCreditApplicationState(str, enum.Enum):
    """Closed application outcome carried by account-credit funding events."""

    allocated = "allocated"
    no_allocatable_balance = "no_allocatable_balance"
    retained_as_account_credit = "retained_as_account_credit"


# Mapping from EventType to LifecycleEventType for subscription events
SUBSCRIPTION_LIFECYCLE_MAP = {
    EventType.subscription_activated: "activate",
    EventType.subscription_suspended: "suspend",
    EventType.subscription_resumed: "resume",
    EventType.subscription_disabled: "other",
    EventType.subscription_canceled: "cancel",
    EventType.subscription_upgraded: "upgrade",
    EventType.subscription_downgraded: "downgrade",
    # Expiry is a distinct terminal transition. It is recorded as ``other``
    # (with reason="expired" in the payload) rather than a dedicated
    # ``expire`` LifecycleEventType: that enum is a native Postgres type and
    # adding a value needs an ALTER TYPE migration, deferred until the alembic
    # heads are merged. Mapping it here closes the audit hole where expiry
    # produced no lifecycle record at all.
    EventType.subscription_expired: "other",
}


@dataclass
class Event:
    """Represents an event that occurred in the system.

    This is the central data structure passed through the event system.
    It contains all information needed by handlers to process the event.
    """

    event_type: EventType
    payload: dict[str, Any]
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Context fields - optional, used for routing and filtering
    actor: str | None = None  # Who triggered the event (user ID, system, etc.)
    subscriber_id: UUID | None = None
    account_id: UUID | None = None
    subscription_id: UUID | None = None
    invoice_id: UUID | None = None
    service_order_id: UUID | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert event to dictionary for JSON serialization."""

        def _serialize(value: Any) -> Any:
            if isinstance(value, UUID):
                return str(value)
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, dict):
                return {key: _serialize(val) for key, val in value.items()}
            if isinstance(value, (list, tuple)):
                return [_serialize(item) for item in value]
            return value

        return {
            "event_id": str(self.event_id),
            "event_type": self.event_type.value,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": _serialize(self.payload),
            "context": {
                "actor": self.actor,
                "subscriber_id": str(self.subscriber_id)
                if self.subscriber_id
                else None,
                "account_id": str(self.account_id) if self.account_id else None,
                "subscription_id": str(self.subscription_id)
                if self.subscription_id
                else None,
                "invoice_id": str(self.invoice_id) if self.invoice_id else None,
                "service_order_id": str(self.service_order_id)
                if self.service_order_id
                else None,
            },
        }
