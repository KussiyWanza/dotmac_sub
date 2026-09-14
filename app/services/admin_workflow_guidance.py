"""One operator-facing explanation for each Admin workflow.

This projection deliberately explains the workflow without deciding whether an
action is allowed.  Routes and domain owners remain authoritative for that.
The route selector is an auditable link between a page and its guide; changing
an Admin workflow requires updating this module in the same pull request.
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


def guidance_for_path(path: str) -> AdminWorkflowGuidance | None:
    """Return the most-specific guide for an Admin page path."""
    matches = (
        (specificity, guide)
        for guide in WORKFLOW_GUIDANCE
        if (specificity := guide.match_specificity(path)) is not None
    )
    return max(matches, key=lambda match: match[0], default=(0, None))[1]


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
    categories = {guide.category for guide in WORKFLOW_GUIDANCE}
    return tuple(
        sorted(
            categories,
            key=lambda category: (category != "Getting started", category.casefold()),
        )
    )


def all_guidance() -> Iterable[AdminWorkflowGuidance]:
    return WORKFLOW_GUIDANCE
