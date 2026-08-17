"""Canonical semantic presentation of domain lifecycle statuses.

Lifecycle services own the status values and transitions. This projection owns
their human label, semantic tone, and icon key so web and mobile clients do not
create competing interpretations. Clients still own concrete colors, spacing,
and platform-native rendering for each semantic tone.
"""

from __future__ import annotations

from enum import Enum

from app.models.billing import CreditNoteStatus, InvoiceStatus, PaymentStatus
from app.models.catalog import OfferStatus, SubscriptionStatus
from app.models.fup_state import FupActionStatus
from app.models.network import Ipv6PrefixState
from app.models.payment_proof import WithholdingTaxStatus
from app.models.project import ProjectStatus, ProjectTaskStatus
from app.models.provisioning import (
    AppointmentStatus,
    ProvisioningRunStatus,
    ServiceOrderStatus,
    TaskStatus,
)
from app.models.sales import QuoteStatus, SalesOrderStatus
from app.models.subscriber import SubscriberStatus
from app.models.support import TicketStatus, canonical_ticket_status_value
from app.models.vendor_routes import (
    AsBuiltRouteStatus,
    InstallationProjectStatus,
    ProjectQuoteStatus,
    ProposedRouteRevisionStatus,
    VendorPurchaseInvoiceStatus,
)
from app.models.vendor_supply import (
    VendorAdvanceStatus,
    VendorMaterialReleaseStatus,
)
from app.schemas.status_presentation import (
    StatusIcon,
    StatusPresentation,
    StatusTone,
)
from app.services.field.work_order_status import WorkOrderStatus
from app.services.topology.connection_status import ConnectionHealthState
from app.services.topology.outage import OutageStatus

_ACCOUNT_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    SubscriberStatus.new.value: ("New", StatusTone.info, StatusIcon.clock),
    SubscriberStatus.active.value: ("Active", StatusTone.positive, StatusIcon.check),
    SubscriberStatus.delinquent.value: (
        "Delinquent",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    SubscriberStatus.suspended.value: (
        "Suspended",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    SubscriberStatus.blocked.value: ("Blocked", StatusTone.negative, StatusIcon.x),
    SubscriberStatus.disabled.value: ("Disabled", StatusTone.negative, StatusIcon.x),
    SubscriberStatus.canceled.value: ("Canceled", StatusTone.negative, StatusIcon.x),
    "inactive": ("Inactive", StatusTone.neutral, StatusIcon.minus),
}

_SUBSCRIPTION_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    SubscriptionStatus.pending.value: ("Pending", StatusTone.info, StatusIcon.clock),
    SubscriptionStatus.active.value: (
        "Active",
        StatusTone.positive,
        StatusIcon.check,
    ),
    SubscriptionStatus.blocked.value: (
        "Blocked",
        StatusTone.negative,
        StatusIcon.x,
    ),
    SubscriptionStatus.suspended.value: (
        "Suspended",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    SubscriptionStatus.stopped.value: (
        "Stopped",
        StatusTone.warning,
        StatusIcon.minus,
    ),
    SubscriptionStatus.disabled.value: (
        "Disabled",
        StatusTone.negative,
        StatusIcon.x,
    ),
    SubscriptionStatus.hidden.value: (
        "Hidden",
        StatusTone.neutral,
        StatusIcon.minus,
    ),
    SubscriptionStatus.archived.value: (
        "Archived",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    SubscriptionStatus.canceled.value: (
        "Canceled",
        StatusTone.negative,
        StatusIcon.x,
    ),
    SubscriptionStatus.expired.value: (
        "Expired",
        StatusTone.negative,
        StatusIcon.x,
    ),
}

_WORK_ORDER_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    WorkOrderStatus.draft.value: ("Draft", StatusTone.neutral, StatusIcon.archive),
    WorkOrderStatus.scheduled.value: (
        "Scheduled",
        StatusTone.info,
        StatusIcon.clock,
    ),
    WorkOrderStatus.dispatched.value: (
        "Dispatched",
        StatusTone.info,
        StatusIcon.info,
    ),
    WorkOrderStatus.in_progress.value: (
        "In progress",
        StatusTone.info,
        StatusIcon.clock,
    ),
    WorkOrderStatus.paused.value: (
        "Paused",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    WorkOrderStatus.completed.value: (
        "Completed",
        StatusTone.positive,
        StatusIcon.check,
    ),
    WorkOrderStatus.canceled.value: (
        "Canceled",
        StatusTone.negative,
        StatusIcon.x,
    ),
    "cancelled": ("Canceled", StatusTone.negative, StatusIcon.x),
}

_TICKET_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    TicketStatus.new.value: ("New", StatusTone.info, StatusIcon.clock),
    TicketStatus.open.value: ("Open", StatusTone.info, StatusIcon.info),
    TicketStatus.pending.value: ("Pending", StatusTone.warning, StatusIcon.clock),
    TicketStatus.waiting_on_customer.value: (
        "Waiting on customer",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    TicketStatus.lastmile_rerun.value: (
        "Last-mile rerun",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    TicketStatus.site_under_construction.value: (
        "Site under construction",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    TicketStatus.on_hold.value: (
        "On hold",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    TicketStatus.pending_confirmation.value: (
        "Pending confirmation",
        StatusTone.info,
        StatusIcon.clock,
    ),
    TicketStatus.closed.value: (
        "Closed",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    TicketStatus.canceled.value: (
        "Canceled",
        StatusTone.negative,
        StatusIcon.x,
    ),
    TicketStatus.merged.value: (
        "Merged",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
}

_INVOICE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    InvoiceStatus.draft.value: (
        "Draft",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    InvoiceStatus.issued.value: (
        "Issued",
        StatusTone.info,
        StatusIcon.info,
    ),
    InvoiceStatus.partially_paid.value: (
        "Partially paid",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    InvoiceStatus.paid.value: (
        "Paid",
        StatusTone.positive,
        StatusIcon.check,
    ),
    InvoiceStatus.void.value: (
        "Void",
        StatusTone.neutral,
        StatusIcon.x,
    ),
    InvoiceStatus.overdue.value: (
        "Overdue",
        StatusTone.negative,
        StatusIcon.alert,
    ),
    InvoiceStatus.written_off.value: (
        "Written off",
        StatusTone.negative,
        StatusIcon.archive,
    ),
}

_CREDIT_NOTE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    CreditNoteStatus.draft.value: (
        "Draft",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    CreditNoteStatus.issued.value: (
        "Issued",
        StatusTone.info,
        StatusIcon.info,
    ),
    CreditNoteStatus.partially_applied.value: (
        "Partially applied",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    CreditNoteStatus.applied.value: (
        "Applied",
        StatusTone.positive,
        StatusIcon.check,
    ),
    CreditNoteStatus.void.value: (
        "Void",
        StatusTone.neutral,
        StatusIcon.x,
    ),
}

_PAYMENT_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    PaymentStatus.pending.value: (
        "Pending",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    PaymentStatus.succeeded.value: (
        "Succeeded",
        StatusTone.positive,
        StatusIcon.check,
    ),
    PaymentStatus.failed.value: (
        "Failed",
        StatusTone.negative,
        StatusIcon.x,
    ),
    PaymentStatus.refunded.value: (
        "Refunded",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    PaymentStatus.partially_refunded.value: (
        "Partially refunded",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    PaymentStatus.reversed.value: (
        "Reversed",
        StatusTone.negative,
        StatusIcon.alert,
    ),
    PaymentStatus.canceled.value: (
        "Canceled",
        StatusTone.neutral,
        StatusIcon.x,
    ),
}

_OUTAGE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    OutageStatus.open.value: ("Open", StatusTone.negative, StatusIcon.alert),
    OutageStatus.suspected.value: (
        "Suspected",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    OutageStatus.confirmed.value: (
        "Confirmed",
        StatusTone.negative,
        StatusIcon.alert,
    ),
    OutageStatus.clearing.value: (
        "Clearing",
        StatusTone.info,
        StatusIcon.clock,
    ),
    OutageStatus.resolved.value: (
        "Resolved",
        StatusTone.positive,
        StatusIcon.check,
    ),
    OutageStatus.discarded.value: (
        "Discarded",
        StatusTone.neutral,
        StatusIcon.x,
    ),
}

_DEVICE_OPERATIONAL_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "working": ("Working", StatusTone.positive, StatusIcon.check),
    "not_working": ("Not working", StatusTone.negative, StatusIcon.x),
}

_CONNECTION_HEALTH_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    ConnectionHealthState.connected.value: (
        "Connected",
        StatusTone.positive,
        StatusIcon.check,
    ),
    ConnectionHealthState.trouble.value: (
        "Connection issue",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    ConnectionHealthState.outage.value: (
        "Area outage",
        StatusTone.negative,
        StatusIcon.alert,
    ),
}

_SERVICE_IMPACT_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    # Exposure is never downtime: potentially_affected stays a warning word.
    "potentially_affected": (
        "Potentially affected",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    "confirmed_unavailable": (
        "Confirmed unavailable",
        StatusTone.negative,
        StatusIcon.x,
    ),
    "degraded": ("Degraded", StatusTone.warning, StatusIcon.alert),
    "restored": ("Restored", StatusTone.positive, StatusIcon.check),
    "unknown": ("Unknown", StatusTone.neutral, StatusIcon.minus),
    "excluded": ("Excluded", StatusTone.neutral, StatusIcon.minus),
}

_SLA_VERDICT_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "passing": ("SLA passing", StatusTone.positive, StatusIcon.check),
    "at_risk": ("SLA at risk", StatusTone.warning, StatusIcon.alert),
    "breach": ("SLA breach", StatusTone.negative, StatusIcon.alert),
    "unavailable": ("SLA unavailable", StatusTone.neutral, StatusIcon.minus),
    # Never an invented target: measured availability renders with this word.
    "no_contractual_sla": ("No contractual SLA", StatusTone.neutral, StatusIcon.info),
}

_TOPOLOGY_HOP_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "up": ("Up", StatusTone.positive, StatusIcon.check),
    "down": ("Down", StatusTone.negative, StatusIcon.x),
    "degraded": ("Degraded", StatusTone.warning, StatusIcon.alert),
    # An unenriched hop is honestly unknown — never dressed up as up or down.
    "unknown": ("Unknown", StatusTone.neutral, StatusIcon.minus),
    # Passive plant has identity and continuity, not an up/down state. Kept
    # distinct from unknown: nothing is missing, the question does not apply.
    "not_applicable": ("Passive", StatusTone.neutral, StatusIcon.minus),
}

_ACCESS_ENDPOINT_SOURCE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "live_session": ("Live session", StatusTone.positive, StatusIcon.check),
    "provisioning": ("Provisioned NAS", StatusTone.neutral, StatusIcon.info),
    "ont_assignment": ("ONT assignment", StatusTone.neutral, StatusIcon.info),
    "uisp_observation": ("UISP observation", StatusTone.neutral, StatusIcon.info),
    "unresolved": ("Unresolved", StatusTone.warning, StatusIcon.alert),
}

_RADIO_SIGNAL_FRESHNESS_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "fresh": ("Observed", StatusTone.neutral, StatusIcon.check),
    "stale": ("Stale", StatusTone.warning, StatusIcon.clock),
    "unavailable": ("Signal unavailable", StatusTone.neutral, StatusIcon.minus),
}

_ACCESS_SESSION_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "connected": ("Connected", StatusTone.positive, StatusIcon.check),
    "stale": ("Last seen", StatusTone.warning, StatusIcon.clock),
    "offline": ("Not connected", StatusTone.neutral, StatusIcon.x),
    "inactive": ("Not connected", StatusTone.neutral, StatusIcon.minus),
}

_SERVICE_ACCESS_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "available": ("Available", StatusTone.positive, StatusIcon.check),
    "restricted": ("Restricted", StatusTone.warning, StatusIcon.alert),
    "unavailable": ("Unavailable", StatusTone.neutral, StatusIcon.minus),
}

_WITHHOLDING_TAX_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    WithholdingTaxStatus.pending.value: (
        "Pending certificate",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    WithholdingTaxStatus.certified.value: (
        "Certified",
        StatusTone.info,
        StatusIcon.info,
    ),
    WithholdingTaxStatus.reclaimed.value: (
        "Reclaimed",
        StatusTone.positive,
        StatusIcon.check,
    ),
    WithholdingTaxStatus.written_off.value: (
        "Written off",
        StatusTone.negative,
        StatusIcon.archive,
    ),
}


_INFRASTRUCTURE_SERVICE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "up": ("Up", StatusTone.positive, StatusIcon.check),
    "healthy": ("Healthy", StatusTone.positive, StatusIcon.check),
    "ok": ("OK", StatusTone.positive, StatusIcon.check),
    "streaming": ("Streaming", StatusTone.positive, StatusIcon.check),
    "degraded": ("Degraded", StatusTone.warning, StatusIcon.alert),
    "partial": ("Partial", StatusTone.warning, StatusIcon.alert),
    "warning": ("Warning", StatusTone.warning, StatusIcon.alert),
    "down": ("Down", StatusTone.negative, StatusIcon.x),
    "critical": ("Critical", StatusTone.negative, StatusIcon.x),
    "failed": ("Failed", StatusTone.negative, StatusIcon.x),
}


_SERVICE_ORDER_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    ServiceOrderStatus.draft.value: ("Draft", StatusTone.neutral, StatusIcon.archive),
    ServiceOrderStatus.submitted.value: (
        "Submitted",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    ServiceOrderStatus.scheduled.value: (
        "Scheduled",
        StatusTone.info,
        StatusIcon.clock,
    ),
    ServiceOrderStatus.provisioning.value: (
        "Provisioning",
        StatusTone.info,
        StatusIcon.info,
    ),
    ServiceOrderStatus.active.value: ("Active", StatusTone.positive, StatusIcon.check),
    ServiceOrderStatus.canceled.value: ("Canceled", StatusTone.neutral, StatusIcon.x),
    ServiceOrderStatus.failed.value: ("Failed", StatusTone.negative, StatusIcon.x),
}

_TASK_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    TaskStatus.pending.value: ("Pending", StatusTone.neutral, StatusIcon.clock),
    TaskStatus.in_progress.value: (
        "In progress",
        StatusTone.info,
        StatusIcon.clock,
    ),
    TaskStatus.blocked.value: (
        "Blocked",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    TaskStatus.completed.value: (
        "Completed",
        StatusTone.positive,
        StatusIcon.check,
    ),
    TaskStatus.failed.value: ("Failed", StatusTone.negative, StatusIcon.x),
}

_APPOINTMENT_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    AppointmentStatus.proposed.value: (
        "Proposed",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    AppointmentStatus.confirmed.value: (
        "Confirmed",
        StatusTone.info,
        StatusIcon.check,
    ),
    AppointmentStatus.completed.value: (
        "Completed",
        StatusTone.positive,
        StatusIcon.check,
    ),
    AppointmentStatus.no_show.value: ("No show", StatusTone.negative, StatusIcon.x),
    AppointmentStatus.canceled.value: (
        "Canceled",
        StatusTone.neutral,
        StatusIcon.x,
    ),
}


_OFFER_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    OfferStatus.active.value: ("Active", StatusTone.positive, StatusIcon.check),
    OfferStatus.inactive.value: ("Inactive", StatusTone.neutral, StatusIcon.minus),
    OfferStatus.archived.value: ("Archived", StatusTone.neutral, StatusIcon.archive),
}

_QUOTE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    QuoteStatus.draft.value: ("Draft", StatusTone.neutral, StatusIcon.archive),
    QuoteStatus.sent.value: ("Sent", StatusTone.info, StatusIcon.clock),
    QuoteStatus.accepted.value: ("Accepted", StatusTone.positive, StatusIcon.check),
    QuoteStatus.rejected.value: ("Rejected", StatusTone.negative, StatusIcon.x),
    QuoteStatus.expired.value: ("Expired", StatusTone.warning, StatusIcon.clock),
}

_SALES_ORDER_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    SalesOrderStatus.draft.value: ("Draft", StatusTone.neutral, StatusIcon.archive),
    SalesOrderStatus.confirmed.value: ("Confirmed", StatusTone.info, StatusIcon.check),
    SalesOrderStatus.paid.value: ("Paid", StatusTone.positive, StatusIcon.check),
    SalesOrderStatus.fulfilled.value: (
        "Fulfilled",
        StatusTone.positive,
        StatusIcon.check,
    ),
    SalesOrderStatus.cancelled.value: ("Cancelled", StatusTone.neutral, StatusIcon.x),
}

_PROJECT_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    ProjectStatus.open.value: ("Open", StatusTone.info, StatusIcon.info),
    ProjectStatus.planned.value: ("Planned", StatusTone.info, StatusIcon.clock),
    ProjectStatus.active.value: ("Active", StatusTone.positive, StatusIcon.check),
    ProjectStatus.on_hold.value: ("On hold", StatusTone.warning, StatusIcon.alert),
    ProjectStatus.completed.value: (
        "Completed",
        StatusTone.positive,
        StatusIcon.check,
    ),
    ProjectStatus.canceled.value: ("Canceled", StatusTone.neutral, StatusIcon.x),
}

_PROJECT_TASK_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    ProjectTaskStatus.backlog.value: (
        "Backlog",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    ProjectTaskStatus.todo.value: ("To do", StatusTone.info, StatusIcon.clock),
    ProjectTaskStatus.in_progress.value: (
        "In progress",
        StatusTone.info,
        StatusIcon.clock,
    ),
    ProjectTaskStatus.blocked.value: ("Blocked", StatusTone.warning, StatusIcon.alert),
    ProjectTaskStatus.done.value: ("Done", StatusTone.positive, StatusIcon.check),
}


def _status_value(status: object | None) -> str:
    if isinstance(status, Enum):
        return str(status.value).strip().lower()
    return str(status or "").strip().lower()


def _fallback(value: str) -> StatusPresentation:
    normalized = value or "unknown"
    return StatusPresentation(
        value=normalized,
        label=normalized.replace("_", " ").title(),
        tone=StatusTone.neutral,
        icon=StatusIcon.info,
    )


def _presentation(
    value: str,
    presentations: dict[str, tuple[str, StatusTone, StatusIcon]],
) -> StatusPresentation:
    definition = presentations.get(value)
    if definition is None:
        return _fallback(value)
    label, tone, icon = definition
    return StatusPresentation(value=value, label=label, tone=tone, icon=icon)


def infrastructure_service_status_presentation(
    status: str | None,
) -> StatusPresentation:
    """Project an infrastructure service/worker health status.

    Canonical vocabulary for the up/degraded/down/unknown status strings emitted
    by infrastructure_health and worker health, so the dashboard service and its
    templates stop each carrying their own status→colour mapping. Unknown values
    fall back to a neutral tone.
    """
    return _presentation(_status_value(status), _INFRASTRUCTURE_SERVICE_PRESENTATIONS)


def service_order_status_presentation(
    status: ServiceOrderStatus | str | None,
) -> StatusPresentation:
    """Project a provisioning service-order status without changing workflow."""
    return _presentation(_status_value(status), _SERVICE_ORDER_PRESENTATIONS)


def provisioning_task_status_presentation(
    status: TaskStatus | str | None,
) -> StatusPresentation:
    """Project a provisioning-task status without changing workflow state."""
    return _presentation(_status_value(status), _TASK_PRESENTATIONS)


def appointment_status_presentation(
    status: AppointmentStatus | str | None,
) -> StatusPresentation:
    """Project an install-appointment status without changing workflow."""
    return _presentation(_status_value(status), _APPOINTMENT_PRESENTATIONS)


def offer_status_presentation(status: OfferStatus | str | None) -> StatusPresentation:
    """Project a catalog offer status without changing catalog state."""
    return _presentation(_status_value(status), _OFFER_PRESENTATIONS)


def quote_status_presentation(status: QuoteStatus | str | None) -> StatusPresentation:
    """Project a sales quote status without changing sales workflow."""
    return _presentation(_status_value(status), _QUOTE_PRESENTATIONS)


def sales_order_status_presentation(
    status: SalesOrderStatus | str | None,
) -> StatusPresentation:
    """Project a sales order status without changing sales workflow."""
    return _presentation(_status_value(status), _SALES_ORDER_PRESENTATIONS)


def project_status_presentation(
    status: ProjectStatus | str | None,
) -> StatusPresentation:
    """Project a project status without changing project workflow."""
    return _presentation(_status_value(status), _PROJECT_PRESENTATIONS)


def project_task_status_presentation(
    status: ProjectTaskStatus | str | None,
) -> StatusPresentation:
    """Project a CRM project-task status without changing task workflow."""
    return _presentation(_status_value(status), _PROJECT_TASK_PRESENTATIONS)


def account_status_presentation(
    status: SubscriberStatus | str | None,
    *,
    is_active: bool | None = None,
) -> StatusPresentation:
    """Project an account status without deriving or changing lifecycle state."""
    value = _status_value(status)
    if not value and is_active is not None:
        value = "active" if is_active else "inactive"
    return _presentation(value, _ACCOUNT_PRESENTATIONS)


def subscription_status_presentation(
    status: SubscriptionStatus | str | None,
) -> StatusPresentation:
    """Project a subscription status without deriving service availability."""
    return _presentation(_status_value(status), _SUBSCRIPTION_PRESENTATIONS)


def work_order_status_presentation(
    status: WorkOrderStatus | str | None,
) -> StatusPresentation:
    """Project field work-order status without changing workflow state."""
    return _presentation(_status_value(status), _WORK_ORDER_PRESENTATIONS)


def ticket_status_presentation(
    status: TicketStatus | str | None,
) -> StatusPresentation:
    """Project support-ticket status without changing lifecycle state."""
    value = _status_value(status)
    if value:
        value = canonical_ticket_status_value(value)
    return _presentation(value, _TICKET_PRESENTATIONS)


def invoice_status_presentation(
    status: InvoiceStatus | str | None,
) -> StatusPresentation:
    """Project invoice lifecycle status without deriving collectibility."""
    return _presentation(_status_value(status), _INVOICE_PRESENTATIONS)


def credit_note_status_presentation(
    status: CreditNoteStatus | str | None,
) -> StatusPresentation:
    """Project credit-note lifecycle status without deriving tax treatment."""
    return _presentation(_status_value(status), _CREDIT_NOTE_PRESENTATIONS)


def payment_status_presentation(
    status: PaymentStatus | str | None,
) -> StatusPresentation:
    """Project payment lifecycle status without deriving settlement state."""
    return _presentation(_status_value(status), _PAYMENT_PRESENTATIONS)


def outage_status_presentation(
    status: OutageStatus | str | None,
) -> StatusPresentation:
    """Project persisted outage lifecycle status without changing visibility."""
    return _presentation(_status_value(status), _OUTAGE_PRESENTATIONS)


def device_operational_status_presentation(
    status: object | str | None,
) -> StatusPresentation:
    """Project the owner-resolved binary device-operation outcome."""

    value = _status_value(getattr(status, "status", status))
    return _presentation(value, _DEVICE_OPERATIONAL_PRESENTATIONS)


def network_device_list_status_presentation(
    status: object | str | None,
) -> StatusPresentation:
    """Present binary device operation in familiar connectivity language."""

    presentation = device_operational_status_presentation(status)
    labels = {"working": "Online", "not_working": "Offline"}
    return presentation.model_copy(
        update={"label": labels.get(presentation.value, presentation.label)}
    )


def topology_hop_status_presentation(state: str | None) -> StatusPresentation:
    """Project an access-path hop's observed state without re-deriving it.

    network.access_path owns hop identity and ordering; observation owners
    supply the up/down/degraded/unknown word. This mapping owns only its
    label, tone, and icon so path chips stop carrying template colour maps.
    """
    return _presentation(_status_value(state), _TOPOLOGY_HOP_PRESENTATIONS)


def path_gap_presentation(code: str | None) -> StatusPresentation:
    """Project an access-path break as a warning without softening the code.

    Every provable gap renders in the warning tone; the owner's break code is
    the label so support quotes the exact machine word back to engineering.
    """
    value = str(code or "unknown").strip()
    return StatusPresentation(
        value=value,
        label=value,
        tone=StatusTone.warning,
        icon=StatusIcon.alert,
    )


def coverage_metric_presentation(open_count: int) -> StatusPresentation:
    """Project a coverage/drift worklist size without re-deriving the facts.

    Zero open items is the healthy word; anything else needs review. The
    metric owner supplies the count; this mapping owns only its meaning.
    """
    if open_count <= 0:
        return StatusPresentation(
            value="clear",
            label="Clear",
            tone=StatusTone.positive,
            icon=StatusIcon.check,
        )
    return StatusPresentation(
        value="needs_review",
        label="Needs review",
        tone=StatusTone.warning,
        icon=StatusIcon.alert,
    )


def access_endpoint_source_presentation(source: str | None) -> StatusPresentation:
    """Project which record proved the serving endpoint, named not implied."""
    return _presentation(_status_value(source), _ACCESS_ENDPOINT_SOURCE_PRESENTATIONS)


def radio_signal_freshness_presentation(freshness: str | None) -> StatusPresentation:
    """Project owner-resolved RF observation freshness without re-aging it."""
    return _presentation(
        _status_value(freshness), _RADIO_SIGNAL_FRESHNESS_PRESENTATIONS
    )


def connection_health_status_presentation(
    status: ConnectionHealthState | str | None,
) -> StatusPresentation:
    """Project customer-safe connection health without re-diagnosing it."""
    return _presentation(_status_value(status), _CONNECTION_HEALTH_PRESENTATIONS)


def service_impact_state_presentation(state: object | None) -> StatusPresentation:
    """Project the six-state service-impact word without re-deciding it."""
    value = _status_value(getattr(state, "value", state))
    return _presentation(value, _SERVICE_IMPACT_PRESENTATIONS)


def sla_verdict_presentation(verdict: object | None) -> StatusPresentation:
    """Project the per-period SLA verdict without recomputing the score."""
    value = _status_value(getattr(verdict, "value", verdict))
    return _presentation(value, _SLA_VERDICT_PRESENTATIONS)


def access_session_status_presentation(status: str | None) -> StatusPresentation:
    """Project the admin RADIUS-session observation without deriving health."""
    return _presentation(_status_value(status), _ACCESS_SESSION_PRESENTATIONS)


def service_access_status_presentation(status: str | None) -> StatusPresentation:
    """Project an already-resolved service-access availability state."""
    return _presentation(_status_value(status), _SERVICE_ACCESS_PRESENTATIONS)


def withholding_tax_status_presentation(
    status: WithholdingTaxStatus | str | None,
) -> StatusPresentation:
    """Project the official WHT receivable lifecycle without re-deriving it."""
    return _presentation(_status_value(status), _WITHHOLDING_TAX_PRESENTATIONS)


_VENDOR_PURCHASE_INVOICE_PRESENTATIONS: dict[
    str, tuple[str, StatusTone, StatusIcon]
] = {
    VendorPurchaseInvoiceStatus.draft.value: (
        "Draft",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    VendorPurchaseInvoiceStatus.submitted.value: (
        "Submitted",
        StatusTone.info,
        StatusIcon.info,
    ),
    VendorPurchaseInvoiceStatus.under_review.value: (
        "Under review",
        StatusTone.info,
        StatusIcon.clock,
    ),
    VendorPurchaseInvoiceStatus.approved.value: (
        "Approved",
        StatusTone.positive,
        StatusIcon.check,
    ),
    VendorPurchaseInvoiceStatus.rejected.value: (
        "Rejected",
        StatusTone.negative,
        StatusIcon.x,
    ),
    VendorPurchaseInvoiceStatus.revision_requested.value: (
        "Revision requested",
        StatusTone.warning,
        StatusIcon.alert,
    ),
}


_INSTALLATION_PROJECT_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    InstallationProjectStatus.draft.value: (
        "Draft",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    InstallationProjectStatus.open_for_bidding.value: (
        "Open for bidding",
        StatusTone.info,
        StatusIcon.clock,
    ),
    InstallationProjectStatus.quoted.value: (
        "Quoted",
        StatusTone.info,
        StatusIcon.info,
    ),
    InstallationProjectStatus.approved.value: (
        "Approved",
        StatusTone.positive,
        StatusIcon.check,
    ),
    InstallationProjectStatus.assigned.value: (
        "Assigned",
        StatusTone.info,
        StatusIcon.check,
    ),
    InstallationProjectStatus.in_progress.value: (
        "In progress",
        StatusTone.info,
        StatusIcon.clock,
    ),
    InstallationProjectStatus.completed.value: (
        "Completed",
        StatusTone.positive,
        StatusIcon.check,
    ),
    InstallationProjectStatus.verified.value: (
        "Verified",
        StatusTone.positive,
        StatusIcon.check,
    ),
}

_VENDOR_QUOTE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    ProjectQuoteStatus.draft.value: (
        "Draft",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    ProjectQuoteStatus.submitted.value: (
        "Submitted",
        StatusTone.info,
        StatusIcon.info,
    ),
    ProjectQuoteStatus.under_review.value: (
        "Under review",
        StatusTone.info,
        StatusIcon.clock,
    ),
    ProjectQuoteStatus.approved.value: (
        "Approved",
        StatusTone.positive,
        StatusIcon.check,
    ),
    ProjectQuoteStatus.rejected.value: (
        "Rejected",
        StatusTone.negative,
        StatusIcon.x,
    ),
    ProjectQuoteStatus.revision_requested.value: (
        "Revision requested",
        StatusTone.warning,
        StatusIcon.alert,
    ),
}

_PROPOSED_ROUTE_REVISION_PRESENTATIONS: dict[
    str, tuple[str, StatusTone, StatusIcon]
] = {
    ProposedRouteRevisionStatus.draft.value: (
        "Draft",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    ProposedRouteRevisionStatus.submitted.value: (
        "Submitted",
        StatusTone.info,
        StatusIcon.info,
    ),
    ProposedRouteRevisionStatus.accepted.value: (
        "Accepted",
        StatusTone.positive,
        StatusIcon.check,
    ),
    ProposedRouteRevisionStatus.rejected.value: (
        "Rejected",
        StatusTone.negative,
        StatusIcon.x,
    ),
}

_AS_BUILT_ROUTE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    AsBuiltRouteStatus.submitted.value: (
        "Submitted",
        StatusTone.info,
        StatusIcon.info,
    ),
    AsBuiltRouteStatus.under_review.value: (
        "Under review",
        StatusTone.info,
        StatusIcon.clock,
    ),
    AsBuiltRouteStatus.accepted.value: (
        "Accepted",
        StatusTone.positive,
        StatusIcon.check,
    ),
    AsBuiltRouteStatus.rejected.value: (
        "Rejected",
        StatusTone.negative,
        StatusIcon.x,
    ),
}


def installation_project_status_presentation(
    status: InstallationProjectStatus | str | None,
) -> StatusPresentation:
    """Project vendor-installation lifecycle status without changing it."""
    return _presentation(_status_value(status), _INSTALLATION_PROJECT_PRESENTATIONS)


def vendor_quote_status_presentation(
    status: ProjectQuoteStatus | str | None,
) -> StatusPresentation:
    """Project vendor-quote approval status without changing it."""
    return _presentation(_status_value(status), _VENDOR_QUOTE_PRESENTATIONS)


def proposed_route_revision_status_presentation(
    status: ProposedRouteRevisionStatus | str | None,
) -> StatusPresentation:
    """Project a proposed route revision status without changing it."""
    return _presentation(_status_value(status), _PROPOSED_ROUTE_REVISION_PRESENTATIONS)


def as_built_route_status_presentation(
    status: AsBuiltRouteStatus | str | None,
) -> StatusPresentation:
    """Project an as-built review status without changing it."""
    return _presentation(_status_value(status), _AS_BUILT_ROUTE_PRESENTATIONS)


def vendor_purchase_invoice_status_presentation(
    status: VendorPurchaseInvoiceStatus | str | None,
) -> StatusPresentation:
    """Project vendor purchase-invoice approval status without re-deriving it."""
    return _presentation(_status_value(status), _VENDOR_PURCHASE_INVOICE_PRESENTATIONS)


_VENDOR_MATERIAL_RELEASE_PRESENTATIONS: dict[
    str, tuple[str, StatusTone, StatusIcon]
] = {
    VendorMaterialReleaseStatus.draft.value: (
        "Draft",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    VendorMaterialReleaseStatus.requested.value: (
        "Requested",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    VendorMaterialReleaseStatus.approved.value: (
        "Approved; issue pending",
        StatusTone.info,
        StatusIcon.clock,
    ),
    VendorMaterialReleaseStatus.rejected.value: (
        "Rejected",
        StatusTone.negative,
        StatusIcon.x,
    ),
    VendorMaterialReleaseStatus.issued.value: (
        "Issued",
        StatusTone.positive,
        StatusIcon.check,
    ),
    VendorMaterialReleaseStatus.canceled.value: (
        "Canceled",
        StatusTone.neutral,
        StatusIcon.x,
    ),
}

_VENDOR_ADVANCE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    VendorAdvanceStatus.requested.value: (
        "Requested",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    VendorAdvanceStatus.approved.value: (
        "Approved; payment pending",
        StatusTone.info,
        StatusIcon.clock,
    ),
    VendorAdvanceStatus.rejected.value: (
        "Rejected",
        StatusTone.negative,
        StatusIcon.x,
    ),
    VendorAdvanceStatus.settled.value: (
        "Settled",
        StatusTone.positive,
        StatusIcon.check,
    ),
    VendorAdvanceStatus.canceled.value: (
        "Canceled",
        StatusTone.neutral,
        StatusIcon.x,
    ),
}


def vendor_material_release_status_presentation(
    status: VendorMaterialReleaseStatus | str | None,
) -> StatusPresentation:
    return _presentation(_status_value(status), _VENDOR_MATERIAL_RELEASE_PRESENTATIONS)


def vendor_advance_status_presentation(
    status: VendorAdvanceStatus | str | None,
) -> StatusPresentation:
    return _presentation(_status_value(status), _VENDOR_ADVANCE_PRESENTATIONS)


_SUPPLIER_INVOICE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "draft": ("Finance draft", StatusTone.neutral, StatusIcon.archive),
    "submitted": ("Submitted to finance", StatusTone.info, StatusIcon.info),
    "pending_approval": (
        "Payment approval pending",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    "approved": ("Approved for payment", StatusTone.info, StatusIcon.check),
    "posted": ("Awaiting payment", StatusTone.warning, StatusIcon.clock),
    "partially_paid": ("Partly paid", StatusTone.info, StatusIcon.info),
    "paid": ("Paid", StatusTone.positive, StatusIcon.check),
    "on_hold": ("Payment on hold", StatusTone.warning, StatusIcon.alert),
    "rejected": ("Rejected by finance", StatusTone.negative, StatusIcon.x),
    "void": ("Voided", StatusTone.neutral, StatusIcon.archive),
    "disputed": ("Disputed", StatusTone.warning, StatusIcon.alert),
}


def supplier_invoice_status_presentation(
    status: str | None,
) -> StatusPresentation:
    """Project an observed AP state without inferring settlement."""
    return _presentation(_status_value(status), _SUPPLIER_INVOICE_PRESENTATIONS)


def erp_supplier_invoice_status_presentation(
    status: str | None,
) -> StatusPresentation:
    """Compatibility alias; use ``supplier_invoice_status_presentation``."""
    return supplier_invoice_status_presentation(status)


_FIELD_EXPENSE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "draft": ("Draft", StatusTone.neutral, StatusIcon.archive),
    "submitted": ("Submitted", StatusTone.info, StatusIcon.clock),
    "approved": ("Approved", StatusTone.positive, StatusIcon.check),
    "rejected": ("Rejected", StatusTone.negative, StatusIcon.x),
    "paid": ("Paid", StatusTone.positive, StatusIcon.check),
}


def field_expense_status_presentation(status: str | None) -> StatusPresentation:
    """Project field-expense claim status without re-deriving the workflow."""
    return _presentation(_status_value(status), _FIELD_EXPENSE_PRESENTATIONS)


_FIELD_MATERIAL_REQUEST_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "draft": ("Draft", StatusTone.neutral, StatusIcon.archive),
    "submitted": ("Submitted", StatusTone.info, StatusIcon.clock),
    "approved": ("Approved", StatusTone.positive, StatusIcon.check),
    "rejected": ("Rejected", StatusTone.negative, StatusIcon.x),
    "issued": ("Issued", StatusTone.info, StatusIcon.info),
    "fulfilled": ("Fulfilled", StatusTone.positive, StatusIcon.check),
    "canceled": ("Canceled", StatusTone.negative, StatusIcon.x),
}


def field_material_request_status_presentation(
    status: str | None,
) -> StatusPresentation:
    """Project field material-request status without re-deriving the workflow."""
    return _presentation(_status_value(status), _FIELD_MATERIAL_REQUEST_PRESENTATIONS)


_SYSTEM_JOB_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "queued": ("Queued", StatusTone.info, StatusIcon.clock),
    "running": ("Running", StatusTone.info, StatusIcon.clock),
    "completed": ("Completed", StatusTone.positive, StatusIcon.check),
    "failed": ("Failed", StatusTone.negative, StatusIcon.x),
    "canceled": ("Canceled", StatusTone.neutral, StatusIcon.minus),
    "cancelled": ("Canceled", StatusTone.neutral, StatusIcon.minus),
}


def system_job_status_presentation(status: str | None) -> StatusPresentation:
    """Project a system/background job run status without re-deriving it."""
    return _presentation(_status_value(status), _SYSTEM_JOB_PRESENTATIONS)


# --- Fiber plant status presentations (inventory owners return raw strings) ---
_FIBER_STRAND_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "available": ("Available", StatusTone.positive, StatusIcon.check),
    "in_use": ("In use", StatusTone.info, StatusIcon.check),
    "reserved": ("Reserved", StatusTone.warning, StatusIcon.clock),
    "faulted": ("Faulted", StatusTone.negative, StatusIcon.x),
    "retired": ("Retired", StatusTone.neutral, StatusIcon.archive),
}

_FIBER_CHANGE_REQUEST_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "pending": ("Pending", StatusTone.warning, StatusIcon.clock),
    "applied": ("Applied", StatusTone.positive, StatusIcon.check),
    "rejected": ("Rejected", StatusTone.negative, StatusIcon.x),
}

_FIBER_SUPPORT_LIFECYCLE_PRESENTATIONS: dict[
    str, tuple[str, StatusTone, StatusIcon]
] = {
    "planned": ("Planned", StatusTone.info, StatusIcon.clock),
    "active": ("Active", StatusTone.positive, StatusIcon.check),
    "suspended": ("Suspended", StatusTone.warning, StatusIcon.alert),
    "retired": ("Retired", StatusTone.neutral, StatusIcon.archive),
}

_FIBER_SUPPORT_INSPECTION_PRESENTATIONS: dict[
    str, tuple[str, StatusTone, StatusIcon]
] = {
    "passed": ("Passed", StatusTone.positive, StatusIcon.check),
    "due": ("Due", StatusTone.warning, StatusIcon.clock),
    "conditional": ("Conditional", StatusTone.warning, StatusIcon.alert),
    "failed": ("Failed", StatusTone.negative, StatusIcon.x),
    "uninspected": ("Uninspected", StatusTone.neutral, StatusIcon.minus),
}


def fiber_strand_status_presentation(status: object | None) -> StatusPresentation:
    """Server-owned presentation for a FiberStrand.status (SoT tone contract)."""
    return _presentation(_status_value(status), _FIBER_STRAND_PRESENTATIONS)


def fiber_change_request_status_presentation(
    status: object | None,
) -> StatusPresentation:
    """Server-owned presentation for a FiberChangeRequest.status."""
    return _presentation(_status_value(status), _FIBER_CHANGE_REQUEST_PRESENTATIONS)


def fiber_support_lifecycle_presentation(status: object | None) -> StatusPresentation:
    """Server-owned presentation for a FiberSupportStructure.lifecycle_status."""
    return _presentation(_status_value(status), _FIBER_SUPPORT_LIFECYCLE_PRESENTATIONS)


def fiber_support_inspection_presentation(status: object | None) -> StatusPresentation:
    """Server-owned presentation for a FiberSupportStructure.inspection_status."""
    return _presentation(_status_value(status), _FIBER_SUPPORT_INSPECTION_PRESENTATIONS)


# --- Monitoring alarm presentations (Alert enums have no projector) ---
_ALARM_SEVERITY_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "info": ("Info", StatusTone.info, StatusIcon.info),
    "warning": ("Warning", StatusTone.warning, StatusIcon.alert),
    "critical": ("Critical", StatusTone.negative, StatusIcon.alert),
}

_ALARM_STATUS_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "open": ("Open", StatusTone.warning, StatusIcon.alert),
    "acknowledged": ("Acknowledged", StatusTone.info, StatusIcon.clock),
    "resolved": ("Resolved", StatusTone.positive, StatusIcon.check),
}


def alarm_severity_presentation(severity: object | None) -> StatusPresentation:
    """Server-owned presentation for a monitoring Alert.severity."""
    return _presentation(_status_value(severity), _ALARM_SEVERITY_PRESENTATIONS)


def alarm_status_presentation(status: object | None) -> StatusPresentation:
    """Server-owned presentation for a monitoring Alert.status."""
    return _presentation(_status_value(status), _ALARM_STATUS_PRESENTATIONS)


_FUP_ACTION_STATUS_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    FupActionStatus.none.value: ("Normal", StatusTone.positive, StatusIcon.check),
    FupActionStatus.notified.value: ("Notified", StatusTone.info, StatusIcon.info),
    FupActionStatus.throttled.value: (
        "Throttled",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    FupActionStatus.blocked.value: ("Blocked", StatusTone.negative, StatusIcon.x),
}


def fup_action_status_presentation(
    status: FupActionStatus | str | None,
) -> StatusPresentation:
    """Project the FUP enforcement action state (server-owned tone)."""
    return _presentation(_status_value(status), _FUP_ACTION_STATUS_PRESENTATIONS)


_IPV6_PREFIX_STATE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    Ipv6PrefixState.available.value: ("Available", StatusTone.info, StatusIcon.info),
    Ipv6PrefixState.reserved.value: ("Reserved", StatusTone.warning, StatusIcon.clock),
    Ipv6PrefixState.assigned.value: ("Assigned", StatusTone.positive, StatusIcon.check),
}


def ipv6_prefix_state_presentation(
    status: Ipv6PrefixState | str | None,
) -> StatusPresentation:
    """Project the IPv6 delegated-prefix lifecycle state (server-owned tone)."""
    return _presentation(_status_value(status), _IPV6_PREFIX_STATE_PRESENTATIONS)


_PROVISIONING_RUN_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    ProvisioningRunStatus.pending.value: ("Pending", StatusTone.info, StatusIcon.info),
    ProvisioningRunStatus.running.value: ("Running", StatusTone.info, StatusIcon.clock),
    ProvisioningRunStatus.success.value: (
        "Success",
        StatusTone.positive,
        StatusIcon.check,
    ),
    ProvisioningRunStatus.failed.value: ("Failed", StatusTone.negative, StatusIcon.x),
}


def provisioning_run_status_presentation(
    status: ProvisioningRunStatus | str | None,
) -> StatusPresentation:
    """Project the provisioning-run lifecycle state (server-owned tone)."""
    return _presentation(_status_value(status), _PROVISIONING_RUN_PRESENTATIONS)


# ControlPlanePhase (owner: control_plane_intent) values, keyed as strings to
# avoid importing a service-layer enum into the presentation owner.
_CONTROL_PLANE_PHASE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "desired": ("Desired", StatusTone.neutral, StatusIcon.archive),
    "planned": ("Planned", StatusTone.info, StatusIcon.info),
    "queued": ("Queued", StatusTone.info, StatusIcon.clock),
    "applying": ("Applying", StatusTone.info, StatusIcon.clock),
    "readback_pending": ("Readback pending", StatusTone.warning, StatusIcon.clock),
    "verified": ("Verified", StatusTone.positive, StatusIcon.check),
    "drifted": ("Drifted", StatusTone.warning, StatusIcon.alert),
    "failed": ("Failed", StatusTone.negative, StatusIcon.x),
}


def control_plane_phase_presentation(status: object) -> StatusPresentation:
    """Project the control-plane convergence phase (server-owned tone)."""
    return _presentation(_status_value(status), _CONTROL_PLANE_PHASE_PRESENTATIONS)
