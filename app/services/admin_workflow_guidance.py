"""One operator-facing explanation for each Admin workflow.

This projection deliberately explains the workflow without deciding whether an
action is allowed.  Routes and domain owners remain authoritative for that.
The route prefix is an auditable link between a page and its guide; changing an
Admin workflow requires updating this module in the same pull request.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AdminWorkflowGuidance:
    """Plain-language, read-only guidance for one staff workflow."""

    id: str
    category: str
    title: str
    audience: str
    purpose: str
    route_prefixes: tuple[str, ...]
    steps: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def matches_path(self, path: str) -> bool:
        return any(
            path == prefix or path.startswith(f"{prefix}/")
            for prefix in self.route_prefixes
        )


def _guide(
    id: str,
    category: str,
    title: str,
    audience: str,
    purpose: str,
    routes: tuple[str, ...],
    *steps: str,
    notes: tuple[str, ...] = (),
) -> AdminWorkflowGuidance:
    return AdminWorkflowGuidance(
        id, category, title, audience, purpose, routes, steps, notes
    )


# This is the canonical content inventory.  Keep steps short: the live form,
# preview, permitted actions, values, and outcomes remain the source of truth.
WORKFLOW_GUIDANCE: tuple[AdminWorkflowGuidance, ...] = (
    _guide(
        "admin-workspace",
        "Getting started",
        "Navigate the admin workspace",
        "All staff",
        "Find the right place to start customer work.",
        ("/admin/dashboard",),
        "Choose a work area from the sidebar.",
        "Start from Customers for a person or service, and Billing for an invoice, payment, proof, credit, or reconciliation item.",
        "Open the customer detail page for work affecting one customer.",
        notes=(
            "Use visible actions on the owning record; do not work from memory.",
            "If a session-expired page appears, use Refresh page so the same Admin page reloads with a fresh token.",
        ),
    ),
    _guide(
        "find-customer",
        "Customers",
        "Find a customer",
        "Support, billing, operations",
        "Locate and confirm a customer before acting.",
        ("/admin/customers",),
        "Search by name, phone, email, account number, business name, or known identifier.",
        "Use filters to narrow the list, then open the matching customer.",
        "Confirm contact details, billing account, service address, active service, and recent history.",
        notes=(
            "If records are similar, stop and compare the billing account and subscription before changing anything.",
        ),
    ),
    _guide(
        "create-customer",
        "Customers",
        "Create a customer",
        "Sales, onboarding, support",
        "Create an individual or business customer ready for service or billing.",
        ("/admin/customers/new",),
        "Choose Individual or Business.",
        "Enter verified identity, contact, address, and service-location information.",
        "Review the profile, then create the billing account or continue to subscription setup when needed.",
        notes=("Do not use placeholder identity data for a production customer.",),
    ),
    _guide(
        "customer-detail",
        "Customers",
        "Understand the customer detail page",
        "All staff",
        "Use one page to review customer, service, network, billing, ticket, and timeline context.",
        ("/admin/customers/",),
        "Use Account for profile and portal access, Service for subscriptions, Network for access, Billing for financial evidence, Tickets for support, and Timeline for history.",
        "In Billing, use Extensions to review pending, applied, canceled, and reversed service-extension requests; billing-date impact appears when an extension has been applied.",
        "Open the specific record before performing a state-changing action.",
        notes=(
            "Timeline and ledger entries are evidence; review them before deciding on a correction.",
        ),
    ),
    _guide(
        "customer-portal",
        "Customer portal",
        "Manage customer portal access",
        "Support, onboarding, account managers",
        "Review portal access or troubleshoot the customer experience safely.",
        ("/admin/customers/",),
        "Confirm the customer and the correct contact details.",
        "Use portal access settings to diagnose access; impersonate only when a valid support reason requires it.",
        "End an impersonation session when the support work is complete.",
        notes=(
            "Impersonation is privileged and audited. Never use it to bypass normal approval or billing controls.",
        ),
    ),
    _guide(
        "new-subscription",
        "Subscriptions",
        "Create a new subscription",
        "Sales, provisioning, operations",
        "Add a service to an existing customer.",
        ("/admin/catalog/subscriptions/new",),
        "Confirm the customer and choose the service offer or plan.",
        "Enter required billing, service-location, and provisioning details.",
        "Review before saving, then confirm subscription, billing, and access state.",
        notes=(
            "A subscription is the service record, not an invoice or an access move.",
        ),
    ),
    _guide(
        "change-plan",
        "Subscriptions",
        "Change a customer plan",
        "Billing, support, operations",
        "Move an existing subscription to another plan.",
        ("/admin/catalog/subscriptions",),
        "Open the affected subscription and choose Change Plan.",
        "Select the target plan, effective timing, and a clear operational reason.",
        "Read the preview and confirm only when billing and service-access effects match the approved request.",
        "Reopen the subscription and Billing tab to verify the result.",
        notes=("Use this for plan replacement, not for moving network access.",),
    ),
    _guide(
        "service-access",
        "Subscriptions",
        "Change customer service access",
        "Network operations, provisioning",
        "Move the network access assignment for a subscription.",
        ("/admin/catalog/subscriptions",),
        "Open the affected subscription and choose the service access move action.",
        "Review current access, router, NAS, IP, RADIUS, and session information.",
        "Select the target, provide the required reason, confirm, and verify Network afterwards.",
        notes=("This is not a plan change or billing correction.",),
    ),
    _guide(
        "subscription-lifecycle",
        "Subscriptions",
        "Manage subscription lifecycle",
        "Billing, support, operations",
        "Activate, restore, suspend, disable, cancel, or correct a subscription.",
        ("/admin/catalog/subscriptions",),
        "Open the subscription and choose the lifecycle action that matches the decision.",
        "Enter timing and a clear reason.",
        "Review billing and access consequences before confirming, then reopen the subscription to verify.",
        notes=(
            "Do not use a second subscription to hide an accidental activation; use the correction workflow.",
        ),
    ),
    _guide(
        "network-access",
        "Network and access",
        "Review customer network access",
        "Support, NOC, field operations",
        "Investigate service-access symptoms with customer context.",
        ("/admin/network",),
        "Review service lifecycle, active access, outage indicators, credentials, IP information, router or NAS, and service location.",
        "Open or update a ticket when the issue needs tracked follow-up or field work.",
        notes=(
            "Billing locks and lifecycle state can also affect access; check Service and Billing as well.",
        ),
    ),
    _guide(
        "project-authoring",
        "Projects",
        "Create and update projects",
        "Project managers, operations",
        "Create project work against the correct customer account or infrastructure.",
        ("/admin/projects",),
        "Open New Project or edit the project that owns the work.",
        "Search for an active customer by name, account ID, account number, subscriber number, or email, then choose the matching result.",
        "Clear the customer field when the project is intentionally not linked to a customer.",
        "For Cable Rerun infrastructure work, choose the infrastructure type, enter at least two characters, and select the matching result. Check its name and location or device details.",
        "Leave Infrastructure blank for customer-only work. A cable rerun can reference both a customer and infrastructure, or neither while planning.",
        "Select the appropriate template, save, and check the infrastructure on the project detail page. A template enabled for vendor work and either a customer or infrastructure prepares the vendor work record.",
        "Assign an active vendor through the vendor workflow when its work record is active and in draft.",
        "Review the project type, status, priority, team, schedule, and customer before saving.",
        notes=(
            "The visible customer label is search text; the selected customer account is the identity saved with the project.",
            "Typing an infrastructure name alone does not select it. Changing the search clears the previous selection; use Clear infrastructure to remove it.",
            "For an older unscoped project, review and save its infrastructure and vendor-enabled template. Do not guess the target from the project name.",
            "Assigned or published work cannot be retargeted through ordinary project edits. A draft vendor work record cannot lose its last customer, infrastructure, or buildout reference.",
            "Selecting infrastructure does not assign a vendor or approve a quote or payment.",
        ),
    ),
    _guide(
        "billing-overview",
        "Billing",
        "Understand customer billing",
        "Billing, support, finance",
        "Review the customer’s current financial position before acting.",
        ("/admin/billing",),
        "Open the customer Billing tab or the relevant billing list.",
        "Review invoices, payments, proofs, credits, extensions, balances, and ledger evidence.",
        "Open the specific record that explains the issue before taking action.",
        notes=(
            "Do not use one workflow to imitate another: payments, credits, voids, write-offs, and extensions have different meanings.",
        ),
    ),
    _guide(
        "invoice",
        "Billing",
        "Create, issue, and correct invoices",
        "Billing staff",
        "Manage a customer invoice through its proper lifecycle.",
        ("/admin/billing/invoices",),
        "Create or open the invoice and verify customer, account, lines, amounts, dates, tax, and memo.",
        "Save drafts first; issue and send only after review.",
        "Use Void only when an invoice should never have existed; use Write Off for valid debt that will not be collected.",
        notes=("Issued invoices use post-issue actions, not direct edits.",),
    ),
    _guide(
        "credit",
        "Billing",
        "Manage customer credit",
        "Billing staff, billing leads",
        "Issue, apply, or reverse approved credit safely.",
        ("/admin/billing/credits",),
        "Select the billing account and enter the approved amount and clear reason.",
        "Review the preview for invoice, ledger, balance, funding, and access effects.",
        "Confirm and verify the Billing tab afterwards.",
        notes=(
            "Do not record a fake payment to reduce an invoice; use approved credit.",
        ),
    ),
    _guide(
        "service-extension",
        "Billing",
        "Extend customer service",
        "Billing, support, operations",
        "Grant or reverse approved temporary service coverage.",
        ("/admin/billing/service-extensions",),
        "Set the reason, dates or days, and affected customer or subscription scope.",
        "Review billing-date and access effects before confirming.",
        "Use cancellation for pending extensions and reversal for applied extensions.",
        "Verify the request state and any recorded billing-date impact from the customer Billing tab under Extensions.",
        notes=("Do not manually edit billing dates to undo an extension.",),
    ),
    _guide(
        "payments",
        "Payments",
        "Record and allocate payments",
        "Billing staff",
        "Record confirmed money and apply it to the right invoices.",
        ("/admin/billing/payments",),
        "Confirm external payment evidence, then enter amount, currency, method, date, reference, and memo.",
        "Review the preview, duplicate-reference and duplicate-evidence warnings, allocation, and service effects before confirming.",
        "When verified prepaid credit covers the complete renewal charge, the system creates and pays one invoice for that service period, grants the matching coverage, and updates the next billing date together.",
        "If the complete prepaid charge is unavailable, no renewal invoice is created and the billing date is not moved.",
        "Acknowledge duplicate risk only when the reviewed bank evidence proves the payment is distinct.",
        "Use allocation for existing unallocated value; it does not create new money.",
        notes=(
            "Use Payment Proof review for customer-uploaded transfer receipts; never bypass a duplicate warning by changing the reference.",
            "Do not create a manual invoice or manually change the next billing date to imitate a prepaid renewal.",
        ),
    ),
    _guide(
        "payment-proofs",
        "Payments",
        "Review payment proofs",
        "Billing reviewers",
        "Verify or reject customer-uploaded transfer receipts.",
        ("/admin/billing/payment-proofs",),
        "Compare the receipt and claimed transfer with bank evidence and duplicate warnings.",
        "Verify and record the confirmed amount, or reject with a clear reason.",
        "Check the resulting proof, payment, invoice, and Billing tab.",
        notes=("Never verify a transfer from the image alone.",),
    ),
    _guide(
        "payment-reconciliation",
        "Payments",
        "Reconcile payments and bank statements",
        "Billing, finance",
        "Match internal payment records with external settlement evidence.",
        ("/admin/billing/reconciliation",),
        "Choose the relevant date range and review unmatched batches or duplicates.",
        "Open the related payment, proof, invoice, or account before correcting anything.",
        "Use the specific import, allocation, refund, reversal, or proof-correction workflow.",
        notes=("Do not change balances until bank-side facts are clear.",),
    ),
    _guide(
        "support-tickets",
        "Support",
        "Work customer support tickets",
        "Support, operations",
        "Track customer issues with the right account, service, network, and billing context.",
        ("/admin/support",),
        "Check existing tickets before creating a new one.",
        "Link the customer and relevant subscription, invoice, payment, proof, or network facts.",
        "Use ordinary ticket editing for status, priority, description, and assignment details.",
        "Assign or reassign an engineer, manager, service team, or additional assignee from the ticket edit workflow.",
        "Update the ticket after completing related admin work.",
        notes=(
            "Tickets track communication and follow-up; they do not own billing or service state changes.",
            "Ticket assignment uses the same ticket-update authority as the rest of the edit workflow.",
        ),
    ),
    _guide(
        "support-csat-report",
        "Support",
        "Review support CSAT",
        "Support leads, managers",
        "Review customer satisfaction evidence for resolved support interactions.",
        ("/admin/reports/support-csat",),
        "Filter by date range, rating, source, status, agent, or service team.",
        "Open the linked ticket or inbox conversation when the rating needs operational follow-up.",
        "Export CSV only for authorized support review or management reporting.",
        notes=(
            "CSAT rows are historical snapshots; do not reinterpret them from current assignment state.",
        ),
    ),
    _guide(
        "team-inbox",
        "Support",
        "Use the team inbox",
        "Support, operations",
        "Review and filter customer conversations without losing route, channel, or assignment context.",
        ("/admin/inbox",),
        "Use search, lifecycle, assignment, channel, team, and activity filters to narrow the queue.",
        "Use All only when historical conversations should be included; use Active or a specific status for operational work.",
        "Open the conversation or linked ticket before acting, then return to the same filtered queue context.",
        notes=(
            "Historical inbox views load bounded pages and may show that more results are available before an exact final total is known.",
            "In Manager AI, select a Conversation or use Period Review with a period and any channel or status filters, then submit your question with Ask AI.",
            "Read the response under Answer; emphasis and lists are formatted, while HTML-like text remains plain text. Verify AI advice against the source conversations before acting.",
        ),
    ),
    _guide(
        "customer-timeline",
        "Customer history",
        "Use the customer timeline",
        "All staff",
        "Understand what happened before changing customer state.",
        ("/admin/customers/",),
        "Review recent customer, service, billing, payment, proof, network, and communication events.",
        "Open linked records for detail, then record required follow-up in the right ticket or operational record.",
        notes=("Use timeline and ledger evidence together for unexpected changes.",),
    ),
    _guide(
        "staff-safety",
        "Getting started",
        "Staff safety rules for customer actions",
        "All staff",
        "Avoid accidental service, billing, and access changes.",
        ("/admin/dashboard",),
        "Confirm the customer and open the record that owns the action.",
        "Read previews and check billing, access, ledger, and notification effects.",
        "Enter clear reasons, verify the visible result, and record customer follow-up when needed.",
        notes=(
            "When unsure, stop and escalate with the record link and preview result.",
        ),
    ),
)


def guidance_for_path(path: str) -> AdminWorkflowGuidance | None:
    """Return the most-specific guide for an Admin page path."""
    matches = [guide for guide in WORKFLOW_GUIDANCE if guide.matches_path(path)]
    return max(
        matches, key=lambda guide: max(map(len, guide.route_prefixes)), default=None
    )


def search_guidance(
    *, query: str = "", category: str = ""
) -> tuple[AdminWorkflowGuidance, ...]:
    needle = query.strip().casefold()
    selected_category = category.strip().casefold()

    def matches(guide: AdminWorkflowGuidance) -> bool:
        text = " ".join(
            (guide.title, guide.purpose, *guide.steps, *guide.notes)
        ).casefold()
        return (
            not selected_category or guide.category.casefold() == selected_category
        ) and (not needle or needle in text)

    return tuple(guide for guide in WORKFLOW_GUIDANCE if matches(guide))


def guidance_categories() -> tuple[str, ...]:
    return tuple(sorted({guide.category for guide in WORKFLOW_GUIDANCE}))


def all_guidance() -> Iterable[AdminWorkflowGuidance]:
    return WORKFLOW_GUIDANCE
