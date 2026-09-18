"""One operator-facing explanation for each Admin workflow.

This projection deliberately explains the workflow without deciding whether an
action is allowed.  Routes and domain owners remain authoritative for that.
The route selector is an auditable link between a page and its guide; changing
an Admin workflow requires updating this module in the same pull request.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.services.integrations.registry import (
    META_CONNECTION_ADMIN_PATH,
    META_CONNECTION_READ_PERMISSION,
)
from app.services.sales.service import LEAD_READ_PERMISSION, LEAD_WRITE_PERMISSION


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
    route_templates: tuple[str, ...] = ()
    excluded_route_prefixes: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def match_specificity(self, path: str) -> int | None:
        """Return selector specificity, preferring exact route templates."""
        if any(
            path == excluded or path.startswith(f"{excluded}/")
            for prefix in self.excluded_route_prefixes
            if (excluded := prefix.rstrip("/"))
        ):
            return None
        scores = [
            len(normalized_prefix)
            for prefix in self.route_prefixes
            if (normalized_prefix := prefix.rstrip("/"))
            and (path == normalized_prefix or path.startswith(f"{normalized_prefix}/"))
        ]
        scores.extend(
            10_000 + _template_specificity(template)
            for template in self.route_templates
            if _matches_route_template(path, template)
        )
        return max(scores, default=None)

    def matches_path(self, path: str) -> bool:
        return self.match_specificity(path) is not None


@dataclass(frozen=True, slots=True)
class AdminHelpAction:
    """One documented page action and its permission-aware instructions."""

    id: str
    title: str
    steps: tuple[str, ...]
    permission: str = ""


@dataclass(frozen=True, slots=True)
class AdminHelpNavigationSection:
    """One Admin-sidebar destination and its ordered help pages."""

    id: str
    label: str
    guide_ids: tuple[str, ...]
    permission: str = ""
    any_permissions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ActionSpec:
    id: str
    title: str
    step_indexes: tuple[int, ...]
    permission: str = ""


def _action(
    id: str,
    title: str,
    *step_indexes: int,
    permission: str = "",
) -> _ActionSpec:
    return _ActionSpec(
        id=id,
        title=title,
        step_indexes=tuple(step_indexes),
        permission=permission,
    )


def _matches_route_template(path: str, template: str) -> bool:
    """Match exact path segments, ``{name}`` placeholders, and a final ``**``."""
    path_parts = tuple(part for part in path.strip("/").split("/") if part)
    template_parts = tuple(part for part in template.strip("/").split("/") if part)
    for index, expected in enumerate(template_parts):
        if expected == "**":
            return index == len(template_parts) - 1
        if index >= len(path_parts):
            return False
        if expected.startswith("{") and expected.endswith("}"):
            continue
        if path_parts[index] != expected:
            return False
    return len(path_parts) == len(template_parts)


def _template_specificity(template: str) -> int:
    parts = tuple(part for part in template.strip("/").split("/") if part)
    literal_parts = sum(
        part != "**" and not (part.startswith("{") and part.endswith("}"))
        for part in parts
    )
    return literal_parts * 100 + len(parts)


def _guide(
    id: str,
    category: str,
    title: str,
    audience: str,
    purpose: str,
    routes: tuple[str, ...],
    *steps: str,
    route_templates: tuple[str, ...] = (),
    excluded_route_prefixes: tuple[str, ...] = (),
    notes: tuple[str, ...] = (),
) -> AdminWorkflowGuidance:
    return AdminWorkflowGuidance(
        id=id,
        category=category,
        title=title,
        audience=audience,
        purpose=purpose,
        route_prefixes=routes,
        steps=steps,
        route_templates=route_templates,
        excluded_route_prefixes=excluded_route_prefixes,
        notes=notes,
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
        (),
        "Choose a work area from the sidebar.",
        "Start from Customers for a person or service, and Billing for an invoice, payment, proof, credit, or reconciliation item.",
        "Open the customer detail page for work affecting one customer.",
        "Confirm the customer and owning record before using an action.",
        "Read previews, check billing and service effects, enter a clear reason, and verify the visible result.",
        route_templates=("/admin/dashboard",),
        notes=(
            "Use visible actions on the owning record; do not work from memory.",
            "If a session-expired page appears, use Refresh page so the same Admin page reloads with a fresh token.",
            "When unsure, stop and escalate with the record link and preview result.",
        ),
    ),
    _guide(
        "automation-center",
        "Administration",
        "Review the Automation Center",
        "Administrators and automation operators",
        "Review the governed module, rule, and execution surfaces for central automation.",
        ("/admin/automation",),
        "Confirm that your role has Automation Center access before opening the hub.",
        "Review the module registry to see which modules and events are eligible for central automation.",
        "Review central rules and recent execution evidence only when your role grants those additional permissions.",
        "Use the existing automation ownership section to identify workflows that remain managed outside the hub.",
        notes=(
            "The initial hub is read-only and creates no rules or business side effects.",
            "Custom fields and migration of existing rules are outside this delivery sequence.",
        ),
    ),
    _guide(
        "find-customer",
        "Customers",
        "Find a customer",
        "Support, billing, operations",
        "Locate and confirm a customer before acting.",
        (),
        "Search by name, phone, email, account number, business name, or known identifier.",
        "Use filters to narrow the list, then open the matching customer.",
        "Confirm contact details, billing account, service address, active service, and recent history.",
        notes=(
            "If records are similar, stop and compare the billing account and subscription before changing anything.",
        ),
        route_templates=("/admin/customers",),
    ),
    _guide(
        "create-customer",
        "Customers",
        "Create a customer",
        "Sales, onboarding, support",
        "Create an individual or business customer ready for service or billing.",
        (),
        "Choose Individual or Business.",
        "Enter verified identity, contact, address, and service-location information.",
        "Review the profile, then create the billing account or continue to subscription setup when needed.",
        route_templates=("/admin/customers/new", "/admin/customers/wizard"),
        notes=("Do not use placeholder identity data for a production customer.",),
    ),
    _guide(
        "customer-detail",
        "Customers",
        "Understand the customer detail page",
        "All staff",
        "Use one page to review customer, service, network, billing, ticket, and timeline context.",
        (),
        "Use Account for profile and portal access, Service for subscriptions, Network for access, Billing for financial evidence, Tickets for support, and Timeline for history.",
        "In Billing, use Extensions to review pending, applied, canceled, and reversed service-extension requests; billing-date impact appears when an extension has been applied.",
        "In Payment intents, Cancel stale intent appears only for an expired bank-transfer intent whose exact submitted proof is still unreviewed and has not created a payment.",
        "Open the linked proof, confirm from bank evidence that no payment was received, enter a clear reason, and confirm the cancellation.",
        "For portal access, confirm the contact details and impersonate only when a valid support reason requires it.",
        "Use Timeline to review recent events, then open linked records for the detail behind a change.",
        "Open the specific record before performing a state-changing action.",
        notes=(
            "Timeline and ledger entries are evidence; review them before deciding on a correction.",
            "Canceling a stale intent rejects its linked proof and cancels the intent together, allowing the customer to start a new payment. Verified or paid evidence cannot be canceled here.",
            "The action requires permission to cancel payment intents and review payment proofs.",
            "Customer pages use a short-lived notification-choice snapshot; use the bulk notification setup workflow when provider templates need to be refreshed.",
            "Impersonation is privileged and audited. Never use it to bypass normal approval or billing controls.",
        ),
        route_templates=(
            "/admin/customers/{customer_type}/{customer_id}/**",
            "/admin/customers/{subscriber_id}/availability",
            "/admin/customers/{subscriber_id}/subscriptions/{subscription_id}/sla-review",
        ),
    ),
    _guide(
        "new-subscription",
        "Subscriptions",
        "Create a new subscription",
        "Sales, provisioning, operations",
        "Add a service to an existing customer.",
        (),
        "Confirm the customer and choose the service offer or plan.",
        "Enter required billing, service-location, and provisioning details.",
        "Review before saving, then confirm subscription, billing, and access state.",
        notes=(
            "A subscription is the service record, not an invoice or an access move.",
        ),
        route_templates=("/admin/catalog/subscriptions/new",),
    ),
    _guide(
        "service-access",
        "Subscriptions",
        "Change customer service access",
        "Network operations, provisioning",
        "Move the network access assignment for a subscription.",
        (),
        "Open the affected subscription and choose the service access move action.",
        "Review current access, router, NAS, IP, RADIUS, and session information.",
        "Select the target, provide the required reason, confirm, and verify Network afterwards.",
        route_templates=("/admin/catalog/subscriptions/{subscription_id}/access/move",),
        notes=("This is not a plan change or billing correction.",),
    ),
    _guide(
        "subscription-lifecycle",
        "Subscriptions",
        "Manage subscription lifecycle",
        "Billing, support, operations",
        "Review or change a subscription without confusing plan, lifecycle, and network-access actions.",
        (),
        "Open the subscription and choose the lifecycle action that matches the decision.",
        "Enter timing and a clear reason.",
        "Review billing and access consequences before confirming, then reopen the subscription to verify.",
        "For a plan change, select the target plan and effective timing, then confirm only when the preview matches the approved request.",
        route_templates=(
            "/admin/catalog/subscriptions",
            "/admin/catalog/subscriptions/{subscription_id}/**",
        ),
        notes=(
            "Do not use a second subscription to hide an accidental activation; use the correction workflow.",
            "A plan change replaces the plan; it does not move network access.",
        ),
    ),
    _guide(
        "network-access",
        "Network and access",
        "Review customer network access",
        "Support, NOC, field operations",
        "Investigate service-access symptoms with customer context.",
        (),
        "Review service lifecycle, active access, outage indicators, credentials, IP information, router or NAS, and service location.",
        "Open or update a ticket when the issue needs tracked follow-up or field work.",
        "For a shared outage, open Network → Outages and select Create infrastructure ticket on the open outage row before issuing field work.",
        "The infrastructure ticket has no subscriber and is linked to the outage; use the linked ticket and the outage row Resolve action for canonical follow-up.",
        notes=(
            "Billing locks and lifecycle state can also affect access; check Service and Billing as well.",
        ),
        route_templates=("/admin/network",),
    ),
    _guide(
        "olt-operational-health",
        "Network and access",
        "Interpret OLT operational health",
        "NOC, network operations",
        "Compare OLT status in the inventory and investigate the same evidence on an OLT detail page.",
        ("/admin/network/olts",),
        "Start with the Working or Not working badge; administrative Active or Inactive is a separate inventory lifecycle.",
        "On the detail page, compare the native OLT poll, Ping, and SNMP evidence and open each timestamp tooltip when recency matters.",
        "Treat Expired or Not current evidence as historical, not as proof that the OLT is currently reachable.",
        "Use Refresh Telemetry or the explicit test actions when current verification is required, then reload the inventory comparison.",
        notes=(
            "A fresh successful native OLT poll can confirm operation even when an older ping or linked monitoring record is stale.",
            "A linked monitoring record contributes fallback evidence only while that record is active and its polling observation is current.",
        ),
    ),
    _guide(
        "ont-wifi-pppoe-actions",
        "Network and access",
        "Push WiFi, PPPoE, and resync changes to an ONT",
        "NOC, field operations",
        "Update a customer's WiFi or PPPoE settings on their ONT, or recover a device stuck out of sync after a failed push.",
        ("/admin/network/onts",),
        "Confirm the exact ONT by serial number, account, and OLT/port before changing anything.",
        "Use Set WiFi Password or Set WiFi SSID for a routine change; the value is saved immediately but only pushed to the device at its next check-in unless you force it.",
        "Use Force-Push WiFi Password when the customer reports the change did not take effect (for example, after a factory reset wiped the device's saved settings) and it needs to apply right away.",
        "Use Force Resync only after a previous attempt failed and you have checked it is safe to retry — this re-attempts the whole reconcile against the device, not just the one field you changed.",
        "If the device is on a known-slow OLT shelf, ask for the longer wait-time option so a slow OLT does not cut the change off partway through.",
        notes=(
            "Force Resync is refused on purpose when the last attempt left the ONT out of sync — that is a deliberate checkpoint asking you to confirm it is safe before retrying, not a bug.",
            "A failed push does not necessarily mean nothing happened on the device; check the ONT's actual status before assuming it is still on the old settings.",
        ),
    ),
    _guide(
        "cpe-detail-wifi-actions",
        "Network and access",
        "Change WiFi SSID or password from a CPE's detail page",
        "NOC, field operations",
        "Update the WiFi SSID or password for the ONT behind a specific CPE inventory record, from that CPE's own detail page.",
        ("/admin/network/cpes",),
        "This action resolves the exact, currently active TR-069 identity for this one CPE record, then routes the change through the same owner as the ONT Configure tab — it never writes to the device directly.",
        "It will refuse, with a specific reason, if the CPE has no linked ONT, the ONT has no active service assignment, or more than one TR-069 identity or assignment is active for it at once — do not treat that as a bug; it means the identity is ambiguous and needs review before a WiFi change can be trusted.",
        "You need both network:cpe:write and network:ont:write to use this action — it exists because the change is actually applied by the ONT owner, not the CPE record itself.",
        "Unchanged WiFi fields (channel, security mode, on/off) are carried forward from the ONT's current settings automatically; you do not need to re-enter them.",
        notes=(
            "A refusal naming a missing or ambiguous identity is not something to retry blindly — check Network Explorer's assignment-drift review queue and the CPE's TR-069 link before trying again.",
            "This is the same durable, tracked delivery path as the ONT Configure tab's WiFi actions: the change is saved and applied at the device's next check-in through the normal reconcile lifecycle, not written to the device from this page directly.",
        ),
    ),
    _guide(
        "work-order-expenses",
        "Operations",
        "Record a work-order expense",
        "Field operations staff and expense managers",
        "Create, review, and track an expense claim against the exact work order.",
        (),
        "Open the exact work order and review its customer and operational context.",
        (
            "Choose New Expense Claim from any work order you can open; the form "
            "becomes available after a technician is assigned and ERP categories "
            "are available."
        ),
        "Choose the intended approver from ERP's current eligible list.",
        "Use the masked ERP payment profile or choose different details for this expense, then verify the beneficiary, bank, and account before submitting.",
        "Enter the purpose, date, currency, and item details; attach required receipt evidence before submitting. The work-order route supplies the identity and cannot be edited in the form.",
        "Submission stages the claim for ERP draft creation; a manager then approves or rejects it after checking the work order, category, receipt, approver, and payment-destination evidence.",
        "Return to the work order to track draft submission, manager decision delivery, required receipt delivery, ERP acceptance, and payment separately.",
        notes=(
            "Only your claims appear in this card. A submitted claim may already have an ERP draft, but it is not approved or released for payment until the manager decision is delivered.",
            "Different payment details apply only to this expense and do not change the technician's ERP profile.",
            "The selected approver alone can approve or reject the submitted expense; ERP remains authoritative for eligibility, account verification, reimbursement, and payment.",
            "Only masked payment details are displayed. If ERP verification is unavailable or expires, verify again before submitting.",
            "Required uploaded receipts remain private and must reach the ERP claim before delivery is accepted.",
            "A submitted claim is not approved, a sent delivery is not ERP acceptance, and reimbursement and payment remain in ERP.",
        ),
        route_templates=("/admin/dispatch/work-orders/{work_order_id}",),
    ),
    _guide(
        "material-requests",
        "Operations",
        "Review and cancel material requests",
        "Field operations and material-request reviewers",
        "Track a field material request and request cancellation without overriding ERP stock authority.",
        ("/admin/operations/material-requests",),
        "Open the request and confirm its work context, requested items, priority, source warehouse, and current status.",
        "Compare the local request state with ERP delivery, reference, and observed status before taking action.",
        "When cancellation is available, enter a clear reason and submit it once.",
        "For a request already accepted by ERP, treat Cancellation pending as an acknowledgement wait; refresh later for ERP's final outcome.",
        notes=(
            "ERP owns stock availability, serial allocation, issuance, and whether an accepted request can still be canceled.",
            "Do not retry a pending cancellation or treat it as canceled until ERP confirms that no stock was issued.",
        ),
    ),
    _guide(
        "project-template-plans",
        "Projects",
        "Configure project template tasks and subtasks",
        "Project managers, operations administrators",
        "Define the reusable task plan that future projects receive.",
        ("/admin/projects/templates",),
        "Open the intended project template and choose Edit Tasks.",
        "Add top-level tasks, then use Add subtask for completion checks that belong under a task.",
        "Keep each subtask after its parent and select dependencies only from earlier work in the plan.",
        "Review work-order automation and evidence requirements for every task that can create field work.",
        "Save the plan as a new revision, then confirm the revision and hierarchy on the template detail page.",
        notes=(
            "Saving a template revision does not alter projects that already use the template.",
            "A parent task cannot be completed until all of its active subtasks are done.",
            "A project receives the new revision when it is created with the template or when an operator explicitly changes that project's template.",
        ),
    ),
    _guide(
        "project-task-subtasks",
        "Projects",
        "Manage project tasks and project-specific subtasks",
        "Project managers, operations",
        "Track project work and add completion checks that apply only to one project.",
        ("/admin/projects/tasks",),
        "Open the project task that owns the work and review its status, dependencies, field work, and existing subtasks.",
        "Choose Add Subtask, confirm the project and parent task, then describe the project-specific completion check.",
        "Complete every active subtask before completing its parent task.",
        "When changing a project's template, review the selected revision and task counts before saving.",
        "After a template change, use the current task plan for new work and open Previous template plan only when historical evidence is needed.",
        notes=(
            "An ad-hoc subtask belongs only to this project and does not change the reusable template.",
            "Changing or clearing a project's template preserves ad-hoc work and retains the former generated plan as read-only history.",
            "Previous-plan tasks cannot be edited or used to create new field work, but their existing linked work remains available.",
        ),
    ),
    _guide(
        "project-authoring",
        "Projects",
        "Create and update projects",
        "Project managers, operations",
        "Create project work against the correct customer account or infrastructure.",
        (),
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
        route_templates=(
            "/admin/projects",
            "/admin/projects/new",
            "/admin/projects/{project_ref}",
            "/admin/projects/{project_ref}/edit",
        ),
        excluded_route_prefixes=(
            "/admin/projects/customers",
            "/admin/projects/infrastructure-options",
            "/admin/projects/tasks",
            "/admin/projects/templates",
            "/admin/projects/export.csv",
        ),
    ),
    _guide(
        "sales-quotes",
        "Sales",
        "Create and manage sales Quotes",
        "Sales and account managers",
        "Prepare a pricing proposal for a genuine Lead or an existing Customer.",
        ("/admin/sales/quotes",),
        "Open New Quote and search at least two characters for exactly one Lead or Customer.",
        "Choose Lead only when the proposal belongs to an open sales opportunity; choose Customer when the account already exists.",
        "Confirm the selected result, Project Type, line items, discount, tax, expiry, and optional install location before creating the Quote.",
        "For an installation estimate awaiting staff review, confirm the customer, pinned installation address, feasibility result, price, deposit, and expiry.",
        "Choose Approve for payment when the estimate is correct, or reject it with a clear reason. Confirm the page shows Approved and the customer notification has been queued.",
        "Send the Quote for review, then use acceptance only after the customer agrees to the commercial terms.",
        notes=(
            "A Customer-backed Quote uses the selected Subscriber account directly and does not create a Lead or require a Party binding.",
            "Accepting a Lead-backed Quote converts its reviewed identity and marks that Lead Won; accepting a Customer-backed Quote reuses the existing active Subscriber. Both continue through the same sales-order and implementation workflow.",
            "Typing text alone does not select a recipient. Choose an exact typeahead result; changing the text clears the previous selection.",
            "Customers can see an installation estimate while it is under review, but payment remains unavailable.",
            "Approval records the reviewer, time, revision, and exact Quote snapshot. Material Quote changes require a new review.",
        ),
    ),
    _guide(
        "sales-orders",
        "Sales",
        "Fund and prepare a sales order for service",
        "Sales, finance, provisioning, operations",
        "Record customer funding without starting service or allocating network resources too early.",
        ("/admin/sales/sales-order",),
        "Confirm the customer, commercial lines, installation amount, tax, total, payment state, and outstanding balance.",
        "Use Record Payment to open Finance's customer-account payment workflow with the outstanding balance suggested.",
        "Review and confirm the receipt in Finance; do not set Paid from the sales-order editor.",
        "Confirm the installation invoice and allocation. Any remaining amount stays as customer account credit.",
        "Treat Paid as funding evidence only: it does not create a subscription, recurring invoice, credential, service order, add-on, or IP assignment.",
        "When installation and network details are ready, create the subscription from the customer workflow and explicitly select the offer, service address, access details, and IP requirements.",
        notes=(
            "Pending subscription creation may keep service start and next-billing dates empty; generate the initial invoice only when billing should begin.",
            "After a receipt or waiver exists, do not edit or delete the commercial document. Use the appropriate Finance refund, credit-note, or adjustment workflow.",
            "Payment state, subscription lifecycle, and network provisioning are separate decisions owned by their respective workflows.",
        ),
    ),
    _guide(
        "billing-overview",
        "Billing",
        "Understand customer billing",
        "Billing, support, finance",
        "Review the customer’s current financial position before acting.",
        (),
        "Open the customer Billing tab or the relevant billing list.",
        "Review invoices, payments, proofs, credits, extensions, balances, and ledger evidence.",
        "Open the specific record that explains the issue before taking action.",
        notes=(
            "Do not use one workflow to imitate another: payments, credits, voids, write-offs, and extensions have different meanings.",
        ),
        route_templates=("/admin/billing",),
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
        "When arriving from a sales order, verify the customer and suggested outstanding balance before previewing the account-level receipt.",
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
        ("/admin/billing/payments/reconciliation",),
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
        ("/admin/support/tickets", "/admin/tickets"),
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
        "ticket-sla-report",
        "Support",
        "Review current ticket SLA workload",
        "Support leads, managers",
        "Identify currently open tickets that are already breaching their SLA.",
        ("/admin/reports/ticket-sla",),
        "Read each total and breakdown as currently breaching divided by currently open tickets.",
        "Select a service team or region to open the matching not-closed ticket queue; any selected report dates remain created-date filters on that queue.",
        "Use Historical SLA Starts and the breach queue only as historical evidence, not as the current open workload.",
        notes=(
            "Closed, canceled, and merged tickets do not contribute to the current-open breakdowns.",
            "Unassigned Region means the open ticket has no region value; open the drilldown to review and repair its ticket data.",
        ),
    ),
    _guide(
        "ncc-complaints-report",
        "Reports",
        "Export NCC complaints",
        "Customer experience managers, compliance staff",
        "Prepare the weekly NCC complaints CSV for Box submission.",
        ("/admin/reports/ncc-complaints", "/admin/reports/ncc-weekly-runs"),
        "Use the default completed Monday-Sunday reporting week unless NCC has explicitly named another date range.",
        "Review Not yet filable before exporting; every row must be filing-ready before submission.",
        "Download the CSV and confirm the filename follows the required week format, such as 36_2026_COMPLAINTS_DOTMAC.csv.",
        "Upload exactly one CSV file to the provider folder in Box.",
        notes=(
            "Do not submit the validation workbook or any file with multiple sheets.",
            "Scheduled NCC delivery preserves the same single CSV artifact for the completed reporting week.",
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
        "smtp-senders",
        "System",
        "Configure SMTP senders",
        "System administrators, support leads, NOC leads, sales leads",
        "Create verified outbound identities for service teams and mailbox routes.",
        ("/admin/system/email",),
        "Confirm the settings-encryption keyring is provisioned in OpenBao before entering a sender password.",
        "Create one sender key for each required identity, such as support, NOC, or sales, and enter its verified From address and SMTP credentials.",
        "Test the sender, make it active, and set a default only when it should be the application-wide fallback.",
        "Open Team Inbox settings and assign the intended Reply sender to each mailbox route.",
        "Send a controlled reply from each route and confirm the delivered From address matches that team.",
        notes=(
            "The OpenBao field is secret/settings/crypto#settings_encryption_keyring; bootstrap it before saving secret-valued settings, then recreate API and Celery processes.",
            "A sender profile does not select itself for a team. The mailbox route's Reply sender owns that mapping.",
            "Do not use support as the default to conceal a missing NOC or sales sender mapping.",
        ),
    ),
    _guide(
        "team-inbox",
        "Support",
        "Use the team inbox",
        "Support, operations",
        "Review and filter customer conversations without losing route, channel, or assignment context.",
        ("/admin/inbox",),
        "Use All for active human-actionable work, AI Intake for conversations still owned by AI, Queue for durable handoffs waiting for capacity, and History for resolved or older conversations.",
        "Use search, lifecycle, assignment, channel, team, and activity filters to narrow the selected ownership view.",
        "While AI is handling a conversation or waiting for the customer, review it read-only; normal reply, note, assignment, status, ticket, macro, and bulk actions remain unavailable.",
        "When authorized human intervention is intentional, choose Take Over Conversation and confirm it before replying; an ordinary reply never takes ownership away from AI.",
        "After AI hands the conversation to the human queue, the first eligible reply claims it for that agent. If another agent already owns it, the reply is not sent and the Inbox names the current owner.",
        "Before resolving, identify whether the sender is the Customer, a Lead, or a representative. When the sender represents someone else, select the exact participant and existing Customer or active Lead, then record the reason; this links only the current conversation and does not make the sender that person or create a global contact route.",
        "To link the sender to an existing Customer, open Contact details and click Existing Customer to load likely matches. If none is right, type at least two characters to search all active Customers by name, email, phone, company, account number, subscriber number, or Customer ID, then choose the exact result before selecting Link Customer.",
        "Keep the Inbox visible while available; its authenticated heartbeat refreshes your routing presence but never overrides Away, On break, or Offline.",
        "Open the conversation or linked ticket before acting, then return to the same filtered queue context.",
        "On Channel routing, save and validate an AI intake draft before activation; review the exact channel scope, allowed tools, playbook, tone, follow-up limits, and customer-wait handoff interval.",
        "In Queue messaging, keep heartbeats off unless reassurance is explicitly required; when enabled, use different non-position wording and a longer interval than position checks.",
        notes=(
            "Historical inbox views load bounded pages and may show that more results are available before an exact final total is known.",
            "A displayed queue position is the customer's current rank in that team, not the durable admission sequence; position messages are sent only when that rank moves forward.",
            "Normal self-assignment and manager assignment cannot skip an older queued conversation or exceed the selected agent's active-conversation capacity.",
            "Reply auto-claim uses the same active team membership, availability, capacity, and queue-order checks as assignment.",
            "AI-owned conversations have a separate AI Intake count and do not contribute to normal human workload counts until handoff, queueing, or assignment.",
            "Take Over stops the active AI session and acquires the conversation through the existing Team Inbox assignment rules; if it fails, refresh and leave AI ownership unchanged.",
            "Awaiting-customer AI sessions remain resumable for ten minutes. After that, normal assignment selects an available agent or admits the conversation to the team's FIFO queue.",
            "SLA policies define response and resolution targets, working hours, and warning time. Use a unique policy name and check the saved values before relying on them.",
            "Turning an SLA policy off prevents it being selected for new conversations; existing conversation deadlines continue. Scheduled checks record each warning once, before the response deadline.",
            "In Manager AI, select a Conversation or use Period Review with a period and any channel or status filters, then submit your question with Ask AI.",
            "Read the response under Answer; emphasis and lists are formatted, while HTML-like text remains plain text. Verify AI advice against the source conversations before acting.",
        ),
    ),
)


# These guides complete the Help Center's Admin-sidebar inventory. They are not
# matched by ``guidance_for_path``, so adding Help content never adds a new
# contextual question-mark control to a page.
HELP_ONLY_GUIDANCE: tuple[AdminWorkflowGuidance, ...] = (
    _guide(
        "workqueue",
        "Workqueue",
        "Work assigned through the workqueue",
        "Support and operations staff",
        "Find, prioritize, and complete work assigned to you or your team.",
        (),
        "Open Workqueue and use the ownership, status, priority, and age filters to narrow the list.",
        "Open an item to confirm its customer, assignment, due state, and linked operational record.",
        "Complete the work in the owning ticket, conversation, or work-order page, then return to refresh the queue.",
        route_templates=("/admin/workqueue",),
    ),
    _guide(
        "surveys",
        "Surveys",
        "Create and manage surveys",
        "Customer experience staff",
        "Create customer surveys, publish them, and review responses.",
        ("/admin/surveys",),
        "Search or filter the survey list, then open a survey to review its questions and response status.",
        "Choose New Survey, enter its name and purpose, add the required questions, and save it.",
        "Open the saved survey to copy its public response link, activate or close collection, and review responses.",
        "Export responses only when the intended audience and date range are correct.",
    ),
    _guide(
        "sales-overview",
        "Sales",
        "Use the sales workspace",
        "Sales and account staff",
        "Move leads, quotes, and funded orders through the correct sales workflow.",
        (),
        "Open Leads to find, create, qualify, assign, or update a prospect. Use Created date to select All time, Last 7 days, Last 30 days, or Custom range, then click Filter. Custom ranges require both dates and include both endpoints; all dates use UTC. Reset clears the filters.",
        "Open Quotes to prepare and send reviewed commercial terms for a Lead or Customer.",
        "Open Sales Orders to review funding and hand approved work to the service workflow.",
        "Verify the customer or lead identity before changing stage, pricing, or ownership.",
        route_templates=("/admin/sales",),
    ),
    _guide(
        "service-requests",
        "Service Requests",
        "Review service requests",
        "Provisioning and operations staff",
        "Review customer service requests and move eligible work into provisioning.",
        ("/admin/service-requests",),
        "Filter requests by state and open the exact request to review customer, service, location, and timing.",
        "Confirm required customer and network information before accepting or rejecting the request.",
        "Use the available transition, record a clear reason, and verify the resulting provisioning state.",
    ),
    _guide(
        "referrals",
        "Referrals",
        "Manage customer referrals",
        "Sales and customer-experience staff",
        "Review referral evidence and convert eligible referrals without losing their origin.",
        ("/admin/referrals",),
        "Search or filter referrals and open the matching referral record.",
        "Confirm the referrer, referred contact, campaign, and current reward or conversion state.",
        "Update the referral or convert it only after the customer identity and eligibility are confirmed.",
        "Review the resulting Lead or Customer link and reward state.",
    ),
    _guide(
        "catalog-overview",
        "Catalog",
        "Manage the service catalog",
        "Product, billing, and provisioning staff",
        "Manage offers, plans, add-ons, and subscription-facing catalog rules.",
        ("/admin/catalog",),
        "Choose Offers, Plans, Add-ons, or Subscriptions for the item you need to review.",
        "Search the list and open the exact catalog record to confirm pricing, billing, availability, and lifecycle state.",
        "Create or edit an item, review its customer and billing effect, then save and verify the result.",
        "Deactivate instead of deleting an item that is already referenced by customer service history.",
    ),
    _guide(
        "vpn",
        "VPN",
        "Manage VPN services",
        "Network operations staff",
        "Review VPN customers, sessions, gateways, and configuration safely.",
        ("/admin/network/vpn",),
        "Search for the customer, service, gateway, or session involved in the request.",
        "Open the exact VPN record and review assignment, connection state, address, and recent evidence.",
        "Use the permitted configuration or lifecycle action, confirm the target and reason, then verify the result.",
    ),
    _guide(
        "work-orders",
        "Work Orders",
        "Manage work orders",
        "Dispatch and field-operations staff",
        "Create, assign, schedule, and track field work.",
        (),
        "Search or filter the work-order list by status, priority, technician, team, or date.",
        "Open a work order to confirm its customer, linked ticket or project, scope, location, and current assignment.",
        "Create or update planning details before assigning a technician and schedule.",
        "Track notes, materials, expenses, evidence, and completion from the work-order detail page.",
        "For shared outages, open the outage console, confirm the incident, scope revision, infrastructure ticket, and team assignment, then issue infrastructure field work with a clear reason.",
        route_templates=("/admin/dispatch/work-orders",),
        notes=(
            "Shared outage work has no individual customer target; verify the outage scope and infrastructure location before issuing it.",
        ),
    ),
    _guide(
        "field-live-map",
        "Field Live Map",
        "Use the field live map",
        "Dispatch and field-operations staff",
        "Review current technician locations and movement evidence for dispatch decisions.",
        ("/admin/dispatch/live-map",),
        "Use the map filters to select the team, technician, status, or time period you need.",
        "Select a map marker to confirm the technician, observation time, and linked work context.",
        "Open movement playback when historical travel evidence is required.",
        "Treat missing or stale location evidence as unavailable rather than assuming a current position.",
    ),
    _guide(
        "vendor-records",
        "Vendors",
        "Manage vendor records",
        "Inventory and network operations staff",
        "Maintain vendor identity, contacts, capabilities, and operational context.",
        ("/admin/vendors",),
        "Search the vendor list and open the exact supplier or contractor record.",
        "Review contacts, capabilities, projects, routes, quotes, invoices, and current status.",
        "Create or edit a vendor using verified business and contact information.",
        "Deactivate a vendor only after checking active work and financial dependencies.",
        excluded_route_prefixes=("/admin/vendors/operations", "/admin/vendors/routes"),
    ),
    _guide(
        "vendor-reviews",
        "Vendors",
        "Review vendor quotes and delivery",
        "Inventory, project, and finance reviewers",
        "Review vendor submissions against their project, field, and financial evidence.",
        ("/admin/vendors/operations",),
        "Choose the relevant review queue and open the exact vendor submission.",
        "Compare scope, quantities, route or field evidence, quote, invoice, and approval history.",
        "Approve or reject only the review assigned to your role and record a clear reason.",
        "Verify the resulting vendor and project status after the decision.",
    ),
    _guide(
        "vendor-routes",
        "Vendors",
        "Review vendor routes",
        "Network and fiber reviewers",
        "Review proposed vendor routes before accepting them into network work.",
        ("/admin/vendors/routes",),
        "Filter the route queue and open the exact proposal.",
        "Review geometry, endpoints, project scope, evidence, and existing network conflicts.",
        "Approve or reject the route with a clear reason, then verify the resulting project state.",
    ),
    _guide(
        "reports-overview",
        "Reports",
        "Use operational reports",
        "Authorized reporting and operational staff",
        "Choose, filter, review, and export an authoritative operational report.",
        (),
        "Choose the report that matches the business question you need to answer.",
        "Set the date range, team, status, or other scope before reviewing totals and rows.",
        "Open linked records when a total or exception needs investigation.",
        "Export only the filtered scope you are authorized to use.",
        route_templates=("/admin/reports",),
    ),
    _guide(
        "gis",
        "GIS / Map",
        "Manage service locations and map layers",
        "GIS and network staff",
        "Review and maintain authoritative service-location and map information.",
        ("/admin/gis",),
        "Search the map or use its filters to locate the customer, area, layer, or pending request.",
        "Open a feature to review its coordinates, ownership, source, and current status.",
        "Review pending customer pin corrections before accepting or rejecting them.",
        "Create or edit locations, areas, and layers only from verified geographic evidence.",
    ),
    _guide(
        "integrations",
        "Integrations",
        "Manage integrations",
        "System administrators",
        "Configure and monitor external connectors without treating them as local authority.",
        ("/admin/integrations",),
        "Choose the connector or provider and review its current configuration, health, and last activity.",
        "Configure the approved endpoint and credentials using the designated secret storage.",
        "Test or validate the connection before enabling it for normal delivery.",
        "Use sync and failure evidence to investigate problems without repeatedly sending the same operation.",
    ),
    _guide(
        "notifications",
        "Notifications",
        "Manage notifications",
        "Communications and system administrators",
        "Manage templates, policies, delivery queues, and notification evidence.",
        ("/admin/notifications",),
        "Choose Templates, Policies, Queue, or Delivery Logs for the task you need to perform.",
        "Search and open the exact notification record before editing or retrying anything.",
        "Create or edit templates and policies, preview the resulting message, then save and verify them.",
        "Review delivery failure evidence before retrying or canceling a queued notification.",
    ),
    _guide(
        "provisioning",
        "Provisioning",
        "Manage provisioning work",
        "Provisioning and network operations staff",
        "Review service provisioning state, appointments, workflows, and reconciliation work.",
        ("/admin/provisioning",),
        "Filter provisioning records by state, service, customer, workflow, or age.",
        "Open the exact record and review prerequisites, operations, failures, and next action.",
        "Use the permitted activate, migrate, reconcile, schedule, or retry action only after reviewing its preview.",
        "Follow the operation until verified completion; queued or delivered work is not final success.",
    ),
    _guide(
        "system-overview",
        "System Overview",
        "Administer the system",
        "System and security administrators",
        "Review system health and administer modules, users, roles, jobs, audit, and maintenance tools.",
        ("/admin/system",),
        "Choose the system area that owns the configuration or evidence you need.",
        "Review current health, effective settings, permissions, audit evidence, or job state before changing anything.",
        "Use the specific user, role, module, scheduler, import, export, restore, or maintenance workflow.",
        "Verify the result and audit record; use destructive tools only with an approved operational plan.",
        excluded_route_prefixes=("/admin/system/email",),
    ),
    _guide(
        "settings",
        "Settings",
        "Manage application settings",
        "System administrators",
        "Review and change effective application configuration.",
        ("/admin/settings", "/admin/system/config"),
        "Choose the settings area and review the effective value, source, and affected scope.",
        "Change only the intended setting and read its validation or operational warning.",
        "Save the change, then reopen the page to confirm the effective value.",
        "Use audit and health evidence when a saved setting does not produce the expected behavior.",
    ),
    _guide(
        "meta-connection",
        "Meta connection",
        "Manage the Meta connection",
        "Communications and system administrators",
        "Connect and monitor approved Facebook and Instagram messaging accounts.",
        (META_CONNECTION_ADMIN_PATH,),
        "Review the current Meta application, Page, Instagram account, token health, and last activity.",
        "Start or renew the connection only with the approved business account and required permissions.",
        "Confirm the selected assets before completing the connection.",
        "Use health and delivery evidence to investigate expired access or missing messages.",
    ),
)


_ACTION_SPECS: dict[str, tuple[_ActionSpec, ...]] = {
    "automation-center": (
        _action("review-automation-access", "Review Automation Center access", 0),
        _action("review-automation-modules", "Review eligible modules and events", 1),
        _action(
            "review-automation-rules", "Review central rules and execution evidence", 2
        ),
        _action(
            "confirm-automation-boundary",
            "Confirm the remaining module ownership boundary",
            3,
        ),
    ),
    "admin-workspace": (
        _action("choose-work-area", "Choose the right work area", 0, 1),
        _action("start-customer-work", "Start customer work", 2, 3),
        _action("verify-result", "Verify the result", 4),
    ),
    "find-customer": (
        _action("search-customers", "Search and filter customers", 0, 1),
        _action("open-customer", "Open and confirm a customer", 2),
    ),
    "create-customer": (
        _action("choose-customer-type", "Choose the customer type", 0),
        _action(
            "enter-customer-details",
            "Enter customer details",
            1,
            permission="customer:write",
        ),
        _action(
            "create-customer", "Create the customer", 2, permission="customer:write"
        ),
    ),
    "customer-detail": (
        _action("review-customer", "Review customer information", 0, 1),
        _action(
            "cancel-stale-intent",
            "Cancel an eligible stale payment intent",
            2,
            3,
            permission="billing:payment_intent:cancel",
        ),
        _action(
            "support-portal-access",
            "Support the customer through portal access",
            4,
            permission="subscriber:impersonate",
        ),
        _action("review-history", "Review customer history", 5, 6),
    ),
    "new-subscription": (
        _action("select-service", "Select the customer service", 0),
        _action(
            "enter-subscription-details",
            "Enter subscription details",
            1,
            permission="catalog:write",
        ),
        _action(
            "create-subscription",
            "Create and verify the subscription",
            2,
            permission="catalog:write",
        ),
    ),
    "service-access": (
        _action(
            "open-access-move",
            "Open the service-access move",
            0,
            permission="catalog:write",
        ),
        _action("review-access", "Review current network access", 1),
        _action(
            "move-access",
            "Move and verify service access",
            2,
            permission="catalog:write",
        ),
    ),
    "subscription-lifecycle": (
        _action("review-subscription", "Review subscription lifecycle", 0),
        _action(
            "change-lifecycle",
            "Change subscription lifecycle",
            1,
            2,
            permission="catalog:write",
        ),
        _action(
            "change-plan", "Change the subscription plan", 3, permission="catalog:write"
        ),
    ),
    "network-access": (
        _action("investigate-access", "Investigate customer network access", 0),
        _action(
            "track-follow-up",
            "Create or update tracked follow-up",
            1,
            permission="support:ticket:update",
        ),
        _action("review-shared-outage", "Review a shared outage", 2),
        _action(
            "confirm-infrastructure-ticket",
            "Confirm infrastructure ticket follow-up",
            3,
            permission="support:ticket:create",
        ),
    ),
    "olt-operational-health": (
        _action("compare-olts", "Compare OLT health", 0),
        _action("inspect-olt-evidence", "Inspect OLT evidence", 1, 2),
        _action(
            "refresh-olt-telemetry",
            "Refresh OLT telemetry",
            3,
            permission="network:olt:write",
        ),
    ),
    "ont-wifi-pppoe-actions": (
        _action("confirm-ont", "Confirm the correct ONT", 0),
        _action(
            "change-wifi", "Change WiFi settings", 1, 2, permission="network:ont:write"
        ),
        _action(
            "resync-ont", "Force an ONT resync", 3, 4, permission="network:ont:write"
        ),
    ),
    "cpe-detail-wifi-actions": (
        _action(
            "change-cpe-wifi",
            "Change WiFi from the CPE page",
            0,
            3,
            permission="network:ont:write",
        ),
        _action(
            "resolve-cpe-identity",
            "Resolve missing or ambiguous CPE identity",
            1,
            permission="network:cpe:write",
        ),
        _action("confirm-permissions", "Confirm the required access", 2),
    ),
    "work-order-expenses": (
        _action("review-work-order", "Review the work order", 0),
        _action("start-expense", "Start an expense claim", 1, 2),
        _action("complete-expense", "Complete and submit the expense", 3, 4),
        _action("track-expense", "Track approval and ERP delivery", 5, 6),
    ),
    "material-requests": (
        _action("review-material-request", "Review a material request", 0, 1),
        _action(
            "cancel-material-request",
            "Request cancellation",
            2,
            permission="operations:material_request:write",
        ),
        _action("track-cancellation", "Track cancellation", 3),
    ),
    "project-template-plans": (
        _action("open-template-plan", "Open a project template plan", 0),
        _action(
            "build-template-plan",
            "Build tasks and subtasks",
            1,
            2,
            permission="project:write",
        ),
        _action(
            "configure-automation",
            "Configure work-order automation",
            3,
            permission="project:write",
        ),
        _action(
            "save-template-plan",
            "Save the template plan",
            4,
            permission="project:write",
        ),
    ),
    "project-task-subtasks": (
        _action("review-project-tasks", "Review project tasks", 0),
        _action(
            "manage-project-subtasks",
            "Manage project subtasks",
            1,
            2,
            permission="project:write",
        ),
        _action(
            "manage-task-dependencies",
            "Manage task dependencies",
            3,
            permission="project:write",
        ),
        _action("verify-task-plan", "Verify the task plan", 4),
    ),
    "project-authoring": (
        _action("find-project", "Find and open a project", 0, 1),
        _action(
            "create-project", "Create a project", 2, 3, 4, permission="project:write"
        ),
        _action(
            "edit-project", "Edit an eligible project", 5, 6, permission="project:write"
        ),
        _action("verify-project", "Verify the project", 7),
    ),
    "sales-quotes": (
        _action(
            "create-quote", "Create a quote", 0, 1, 2, permission=LEAD_WRITE_PERMISSION
        ),
        _action(
            "review-estimate",
            "Review an installation estimate",
            3,
            4,
            permission=LEAD_WRITE_PERMISSION,
        ),
        _action(
            "send-accept-quote",
            "Send and accept a quote",
            5,
            permission=LEAD_WRITE_PERMISSION,
        ),
    ),
    "sales-orders": (
        _action("review-sales-order", "Review a sales order", 0),
        _action(
            "record-funding",
            "Record customer funding",
            1,
            2,
            permission="billing:payment:create",
        ),
        _action("confirm-allocation", "Confirm invoice allocation", 3),
        _action(
            "prepare-service",
            "Prepare the funded order for service",
            4,
            5,
            permission="catalog:write",
        ),
    ),
    "billing-overview": (
        _action("open-billing", "Open customer billing", 0),
        _action("review-billing", "Review financial records", 1),
        _action("investigate-billing-record", "Investigate a billing record", 2),
    ),
    "invoice": (
        _action(
            "create-review-invoice",
            "Create and review an invoice",
            0,
            permission="billing:invoice:create",
        ),
        _action(
            "issue-invoice",
            "Issue and send an invoice",
            1,
            permission="billing:invoice:update",
        ),
        _action(
            "correct-invoice",
            "Void or write off an invoice",
            2,
            permission="billing:invoice:update",
        ),
    ),
    "credit": (
        _action(
            "prepare-credit",
            "Prepare customer credit",
            0,
            permission="billing:credit_note:create",
        ),
        _action("review-credit", "Review the credit preview", 1),
        _action(
            "apply-credit",
            "Apply and verify credit",
            2,
            permission="billing:credit_note:create",
        ),
    ),
    "service-extension": (
        _action(
            "prepare-extension",
            "Prepare a service extension",
            0,
            permission="billing:extension:create",
        ),
        _action("review-extension", "Review extension effects", 1),
        _action(
            "cancel-reverse-extension",
            "Cancel or reverse an extension",
            2,
            permission="billing:extension:reverse",
        ),
        _action("verify-extension", "Verify extension history", 3),
    ),
    "payments": (
        _action(
            "record-payment",
            "Record a payment",
            0,
            1,
            permission="billing:payment:create",
        ),
        _action("review-payment", "Review duplicate and allocation evidence", 2, 5),
        _action(
            "apply-prepaid-renewal",
            "Apply an eligible prepaid renewal",
            3,
            4,
            permission="billing:payment:create",
        ),
        _action(
            "allocate-payment",
            "Allocate existing payment value",
            6,
            permission="billing:payment:update",
        ),
    ),
    "payment-proofs": (
        _action("review-payment-proof", "Review payment proof", 0),
        _action(
            "decide-payment-proof",
            "Verify or reject payment proof",
            1,
            permission="billing:proof:verify",
        ),
        _action("verify-proof-result", "Verify the resulting records", 2),
    ),
    "payment-reconciliation": (
        _action("filter-reconciliation", "Filter reconciliation evidence", 0),
        _action("investigate-reconciliation", "Investigate an unmatched item", 1),
        _action(
            "resolve-reconciliation",
            "Use the correct resolution workflow",
            2,
            permission="billing:ledger:write",
        ),
    ),
    "support-tickets": (
        _action("find-ticket", "Find or create the correct ticket", 0, 1),
        _action(
            "update-ticket",
            "Update ticket details",
            2,
            permission="support:ticket:update",
        ),
        _action(
            "assign-ticket",
            "Assign or reassign the ticket",
            3,
            permission="support:ticket:update",
        ),
        _action(
            "complete-follow-up",
            "Record completed follow-up",
            4,
            permission="support:ticket:update",
        ),
    ),
    "ticket-sla-report": (
        _action("read-current-sla", "Read the current SLA workload", 0),
        _action("open-sla-drilldown", "Open a matching ticket queue", 1),
        _action("review-sla-history", "Review historical SLA evidence", 2),
    ),
    "ncc-complaints-report": (
        _action("select-report-week", "Select the reporting week", 0),
        _action("validate-complaints", "Validate complaint rows", 1),
        _action(
            "export-ncc-csv", "Export the NCC CSV", 2, permission="reports:ncc:export"
        ),
        _action(
            "submit-ncc-csv",
            "Submit the CSV to Box",
            3,
            permission="reports:ncc:export",
        ),
    ),
    "support-csat-report": (
        _action("filter-csat", "Filter CSAT evidence", 0),
        _action("investigate-csat", "Investigate a CSAT response", 1),
        _action(
            "export-csat", "Export CSAT results", 2, permission="reports:support:read"
        ),
    ),
    "smtp-senders": (
        _action(
            "prepare-sender-security",
            "Prepare sender security",
            0,
            permission="system:settings:write",
        ),
        _action(
            "create-smtp-sender",
            "Create an SMTP sender",
            1,
            2,
            permission="system:settings:write",
        ),
        _action(
            "assign-reply-sender",
            "Assign a reply sender",
            3,
            permission="system:settings:write",
        ),
        _action(
            "test-reply-sender",
            "Test the sender mapping",
            4,
            permission="system:settings:write",
        ),
    ),
    "team-inbox": (
        _action("filter-inbox", "Filter the Inbox", 0, 1),
        _action("review-ai-conversation", "Review an AI-owned conversation", 2),
        _action(
            "take-over-conversation",
            "Take over a conversation",
            3,
            permission="support:ticket:update",
        ),
        _action(
            "claim-reply",
            "Claim and reply to a conversation",
            4,
            permission="support:ticket:update",
        ),
        _action("identify-participant", "Identify the customer or participant", 5, 6),
        _action("manage-presence", "Maintain routing presence", 7),
        _action("open-related-work", "Open related work", 8),
        _action(
            "configure-channel-routing",
            "Configure channel routing",
            9,
            permission="system:settings:write",
        ),
        _action(
            "configure-queue-messaging",
            "Configure queue messaging",
            10,
            permission="system:settings:write",
        ),
    ),
    "workqueue": (
        _action("filter-workqueue", "Filter the workqueue", 0),
        _action("review-work-item", "Review a work item", 1),
        _action(
            "complete-work-item",
            "Complete the owning work",
            2,
            permission="support:ticket:update",
        ),
    ),
    "surveys": (
        _action("review-surveys", "Find and review surveys", 0),
        _action("create-survey", "Create a survey", 1, permission="customer:write"),
        _action(
            "publish-survey",
            "Publish or close a survey",
            2,
            permission="customer:write",
        ),
        _action(
            "export-survey", "Export survey responses", 3, permission="customer:write"
        ),
    ),
    "sales-overview": (
        _action("manage-leads", "Manage leads", 0, permission=LEAD_WRITE_PERMISSION),
        _action("manage-quotes", "Manage quotes", 1, permission=LEAD_WRITE_PERMISSION),
        _action("review-sales-orders", "Review sales orders", 2),
        _action("verify-sales-identity", "Verify sales identity", 3),
    ),
    "service-requests": (
        _action("find-service-request", "Find and review a service request", 0),
        _action("validate-service-request", "Validate request information", 1),
        _action(
            "decide-service-request",
            "Accept or reject a service request",
            2,
            permission="provisioning:write",
        ),
    ),
    "referrals": (
        _action("find-referral", "Find a referral", 0),
        _action("review-referral", "Review referral evidence", 1),
        _action(
            "convert-referral",
            "Update or convert a referral",
            2,
            permission=LEAD_WRITE_PERMISSION,
        ),
        _action("verify-referral", "Verify conversion and reward state", 3),
    ),
    "catalog-overview": (
        _action("choose-catalog-area", "Choose a catalog area", 0),
        _action("review-catalog-item", "Review a catalog item", 1),
        _action(
            "manage-catalog-item",
            "Create or edit a catalog item",
            2,
            permission="catalog:write",
        ),
        _action(
            "retire-catalog-item",
            "Deactivate a catalog item",
            3,
            permission="catalog:write",
        ),
    ),
    "vpn": (
        _action("find-vpn-service", "Find a VPN service", 0),
        _action("review-vpn-service", "Review VPN evidence", 1),
        _action(
            "manage-vpn-service",
            "Configure or change VPN service",
            2,
            permission="network:vpn:write",
        ),
    ),
    "work-orders": (
        _action("find-work-order", "Find a work order", 0),
        _action("review-work-order", "Review a work order", 1),
        _action(
            "plan-work-order",
            "Create or plan a work order",
            2,
            permission="operations:dispatch:write",
        ),
        _action("track-work-order", "Track work-order delivery", 3),
        _action(
            "issue-outage-work-order",
            "Issue shared-outage field work",
            4,
            permission="operations:dispatch:write",
        ),
    ),
    "field-live-map": (
        _action("filter-field-map", "Filter the field map", 0),
        _action("inspect-technician-location", "Inspect technician location", 1),
        _action("playback-movement", "Review movement playback", 2),
        _action("check-location-freshness", "Check location freshness", 3),
    ),
    "vendor-records": (
        _action("find-vendor", "Find a vendor", 0),
        _action("review-vendor", "Review vendor information", 1),
        _action(
            "manage-vendor", "Create or edit a vendor", 2, permission="inventory:write"
        ),
        _action(
            "deactivate-vendor", "Deactivate a vendor", 3, permission="inventory:write"
        ),
    ),
    "vendor-reviews": (
        _action("open-vendor-review", "Open a vendor review", 0),
        _action("compare-vendor-evidence", "Compare vendor evidence", 1),
        _action(
            "decide-vendor-review",
            "Approve or reject a submission",
            2,
            permission="inventory:write",
        ),
        _action("verify-vendor-review", "Verify the review result", 3),
    ),
    "vendor-routes": (
        _action("find-vendor-route", "Find a vendor route", 0),
        _action("review-vendor-route", "Review route evidence", 1),
        _action(
            "decide-vendor-route",
            "Approve or reject a route",
            2,
            permission="network:fiber:write",
        ),
    ),
    "reports-overview": (
        _action("choose-report", "Choose a report", 0),
        _action("filter-report", "Filter report results", 1),
        _action("investigate-report", "Investigate a report item", 2),
        _action(
            "export-report", "Export a report", 3, permission="reports:billing:export"
        ),
    ),
    "gis": (
        _action("find-map-feature", "Find a map feature", 0),
        _action("review-map-feature", "Review map evidence", 1),
        _action(
            "review-pin-correction",
            "Review a pin correction",
            2,
            permission="gis:location_request:review",
        ),
        _action(
            "manage-map-data",
            "Create or edit map data",
            3,
            permission="gis:location:write",
        ),
    ),
    "integrations": (
        _action("review-integration", "Review an integration", 0),
        _action(
            "configure-integration",
            "Configure an integration",
            1,
            permission="system:settings:write",
        ),
        _action(
            "test-integration",
            "Test and enable an integration",
            2,
            permission="system:settings:write",
        ),
        _action("investigate-integration", "Investigate integration failures", 3),
    ),
    "notifications": (
        _action("choose-notification-area", "Choose a notification area", 0),
        _action("find-notification", "Find a notification record", 1),
        _action(
            "manage-notification",
            "Create or edit notification content",
            2,
            permission="notification:write",
        ),
        _action(
            "resolve-notification",
            "Review or retry delivery",
            3,
            permission="notification:write",
        ),
    ),
    "provisioning": (
        _action("find-provisioning-work", "Find provisioning work", 0),
        _action("review-provisioning-work", "Review provisioning evidence", 1),
        _action(
            "run-provisioning-action",
            "Run a provisioning action",
            2,
            permission="provisioning:write",
        ),
        _action("track-provisioning", "Track provisioning to completion", 3),
    ),
    "system-overview": (
        _action("choose-system-area", "Choose a system area", 0),
        _action("review-system-state", "Review system state", 1),
        _action(
            "administer-system",
            "Administer system configuration",
            2,
            permission="system:settings:write",
        ),
        _action("verify-system-change", "Verify and audit a system change", 3),
    ),
    "settings": (
        _action("choose-settings-area", "Choose a settings area", 0),
        _action("review-effective-setting", "Review an effective setting", 1),
        _action(
            "change-setting", "Change a setting", 2, permission="system:settings:write"
        ),
        _action("investigate-setting", "Investigate setting behavior", 3),
    ),
    "meta-connection": (
        _action("review-meta-connection", "Review the Meta connection", 0),
        _action(
            "connect-meta",
            "Connect or renew Meta",
            1,
            2,
            permission="system:settings:write",
        ),
        _action("investigate-meta", "Investigate Meta connection problems", 3),
    ),
}


HELP_NAVIGATION: tuple[AdminHelpNavigationSection, ...] = (
    AdminHelpNavigationSection(
        "dashboard",
        "Dashboard",
        ("admin-workspace",),
        any_permissions=(
            "billing:invoice:read",
            "monitoring:read",
            "customer:read",
        ),
    ),
    AdminHelpNavigationSection(
        "automation",
        "Automation Center",
        ("automation-center",),
        "automation:hub:read",
    ),
    AdminHelpNavigationSection(
        "customers",
        "Customers",
        ("find-customer", "create-customer", "customer-detail"),
        "customer:read",
    ),
    AdminHelpNavigationSection(
        "support", "Support", ("support-tickets",), "support:ticket:read"
    ),
    AdminHelpNavigationSection(
        "workqueue", "Workqueue", ("workqueue",), "support:ticket:read"
    ),
    AdminHelpNavigationSection(
        "inbox", "Inbox", ("team-inbox",), "support:ticket:read"
    ),
    AdminHelpNavigationSection("surveys", "Surveys", ("surveys",), "customer:read"),
    AdminHelpNavigationSection(
        "sales",
        "Sales",
        ("sales-overview", "sales-quotes", "sales-orders"),
        LEAD_READ_PERMISSION,
    ),
    AdminHelpNavigationSection(
        "service-requests",
        "Service Requests",
        ("service-requests",),
        "provisioning:read",
    ),
    AdminHelpNavigationSection(
        "referrals", "Referrals", ("referrals",), LEAD_READ_PERMISSION
    ),
    AdminHelpNavigationSection(
        "projects",
        "Projects",
        ("project-authoring", "project-template-plans", "project-task-subtasks"),
        "project:read",
    ),
    AdminHelpNavigationSection(
        "billing",
        "Billing",
        (
            "billing-overview",
            "invoice",
            "credit",
            "service-extension",
            "payments",
            "payment-proofs",
            "payment-reconciliation",
        ),
        "billing_account:read",
    ),
    AdminHelpNavigationSection(
        "catalog",
        "Catalog",
        (
            "catalog-overview",
            "new-subscription",
            "subscription-lifecycle",
            "service-access",
        ),
        "catalog:read",
    ),
    AdminHelpNavigationSection(
        "network",
        "Network",
        (
            "network-access",
            "olt-operational-health",
            "ont-wifi-pppoe-actions",
            "cpe-detail-wifi-actions",
        ),
        "network:hub:read",
    ),
    AdminHelpNavigationSection("vpn", "VPN", ("vpn",), "network:vpn:read"),
    AdminHelpNavigationSection(
        "work-orders",
        "Work Orders",
        ("work-orders", "work-order-expenses"),
        "operations:dispatch:read",
    ),
    AdminHelpNavigationSection(
        "material-requests",
        "Material Requests",
        ("material-requests",),
        "operations:material_request:read",
    ),
    AdminHelpNavigationSection(
        "field-live-map",
        "Field Live Map",
        ("field-live-map",),
        "operations:dispatch:read",
    ),
    AdminHelpNavigationSection(
        "vendors",
        "Vendors",
        ("vendor-records", "vendor-reviews", "vendor-routes"),
        any_permissions=("inventory:read", "network:fiber:read"),
    ),
    AdminHelpNavigationSection(
        "reports",
        "Reports",
        (
            "reports-overview",
            "ticket-sla-report",
            "ncc-complaints-report",
            "support-csat-report",
        ),
        any_permissions=(
            "reports:billing:read",
            "reports:network:read",
            "reports:support:read",
            "customer:read",
            "reports:ncc:read",
        ),
    ),
    AdminHelpNavigationSection("gis", "GIS / Map", ("gis",), "gis:map:view"),
    AdminHelpNavigationSection(
        "integrations", "Integrations", ("integrations",), "system:settings:read"
    ),
    AdminHelpNavigationSection(
        "notifications", "Notifications", ("notifications",), "notification:read"
    ),
    AdminHelpNavigationSection(
        "provisioning", "Provisioning", ("provisioning",), "provisioning:read"
    ),
    AdminHelpNavigationSection(
        "system", "System Overview", ("system-overview",), "system:settings:read"
    ),
    AdminHelpNavigationSection(
        "settings", "Settings", ("settings", "smtp-senders"), "system:settings:read"
    ),
    AdminHelpNavigationSection(
        "meta",
        "Meta connection",
        ("meta-connection",),
        META_CONNECTION_READ_PERMISSION,
    ),
)


# Only sections containing pages with different read scopes need an override.
# Other pages inherit their Admin-sidebar section's visibility.
HELP_GUIDE_VIEW_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "invoice": ("billing:invoice:read",),
    "credit": ("billing:credit_note:read",),
    "service-extension": ("billing:extension:read",),
    "payments": ("billing:payment:read",),
    "payment-proofs": ("billing:proof:read",),
    "payment-reconciliation": ("billing:ledger:read",),
    "vendor-records": ("inventory:read",),
    "vendor-reviews": ("inventory:read", "finance:ap:read"),
    "vendor-routes": ("network:fiber:read",),
    "ticket-sla-report": ("reports:support:read",),
    "ncc-complaints-report": ("reports:ncc:read",),
    "support-csat-report": ("reports:support:read",),
}


def guidance_for_path(path: str) -> AdminWorkflowGuidance | None:
    """Return the most-specific guide for an Admin page path."""
    matches = (
        (specificity, guide)
        for guide in WORKFLOW_GUIDANCE
        if (specificity := guide.match_specificity(path)) is not None
    )
    return max(matches, key=lambda match: match[0], default=(0, None))[1]


def help_actions_for(guide: AdminWorkflowGuidance) -> tuple[AdminHelpAction, ...]:
    """Resolve one guide's action sections from its canonical ordered steps."""
    specs = _ACTION_SPECS.get(guide.id, ())
    return tuple(
        AdminHelpAction(
            id=spec.id,
            title=spec.title,
            steps=tuple(guide.steps[index] for index in spec.step_indexes),
            permission=spec.permission,
        )
        for spec in specs
    )


def guidance_by_id(guide_id: str) -> AdminWorkflowGuidance | None:
    needle = guide_id.strip()
    return next((guide for guide in all_guidance() if guide.id == needle), None)


def help_navigation() -> tuple[AdminHelpNavigationSection, ...]:
    return HELP_NAVIGATION


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

    return tuple(guide for guide in all_guidance() if matches(guide))


def guidance_categories() -> tuple[str, ...]:
    categories = {guide.category for guide in all_guidance()}
    return tuple(
        sorted(
            categories,
            key=lambda category: (category != "Getting started", category.casefold()),
        )
    )


def all_guidance() -> Iterable[AdminWorkflowGuidance]:
    return (*WORKFLOW_GUIDANCE, *HELP_ONLY_GUIDANCE)
