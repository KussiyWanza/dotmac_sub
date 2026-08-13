"""Canonical SOT declarations for the ui_list_projection domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    MigrationContract,
    OwnerRole,
    ProjectionContract,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
)
from app.services.sot_registry.model import DomainSOT

_CRM_REPORT_CONCERNS = (
    "network infrastructure report projection",
    "subscriber overview report projection",
    "churned subscriber report projection",
    "technician performance report projection",
    "online customer activity report projection",
    "subscriber billing-risk report projection",
    "subscriber revenue and pipeline report projection",
    "postpaid customer report projection",
    "CRM team performance report projection",
    "administrative agent performance report projection",
    "personal agent performance report projection",
    "operations SLA violation report projection",
    "inbox queue and issue-classification report projection",
    "subscriber lifecycle report projection",
    "subscriber service-quality report projection",
    "revenue and service downtime report projection",
    "project and task people-performance report projection",
)

DOMAIN = DomainSOT(
    domain="ui_list_projection",
    services=(
        SOTService(
            name="ui.list_contracts",
            module="app.services.list_query",
            owns=(
                "list query normalization",
                "page metadata derivation",
                "canonical list URL serialization",
                "list capability declarations",
            ),
        ),
        SOTService(
            name="ui.crm_operational_reports",
            module="app.services.crm_reporting",
            owns=_CRM_REPORT_CONCERNS,
            depends_on=(
                "auth.permission_gate",
                "communications.team_inbox_projection",
                "customer.accounts",
                "financial.invoices",
                "financial.payments",
                "network.customer_outage_accrual",
                "network.fiber_topology",
                "network.identity",
                "network.ip_pool_utilization",
                "network.radius_sessions",
                "network.ont_runtime_status",
                "operations.project_lifecycle",
                "operations.provisioning_workflow",
                "operations.work_orders",
                "service_intent.subscription_lifecycle",
                "support.ticket_lifecycle",
                "ui.list_contracts",
            ),
            notes=(
                "Read-only Self-Care report projections compose native owner facts. "
                "They never copy CRM retention notes, dispositions, follow-ups, "
                "campaign state, outreach history, or engagement records."
            ),
            contract=ServiceContract(
                concerns=tuple(
                    ConcernContract(
                        name=concern,
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "typed CRM report query",
                            "authorized report scope",
                            "native customer and subscription records",
                            "native billing records",
                            "native network inventory records",
                            "native ONT runtime observations",
                            "native IP pool utilization",
                            "native fiber plant records",
                            "native RADIUS records",
                            "native customer outage intervals",
                            "native inbox records",
                            "native support records",
                            "native work-order and project records",
                            "native provisioning records",
                        ),
                    )
                    for concern in _CRM_REPORT_CONCERNS
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed CRM report query",
                        owner="ui.list_contracts",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="inclusive dates, pagination, and personal-agent scope",
                    ),
                    AuthorityInput(
                        name="authorized report scope",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="exact customer, billing-report, or support-report permission",
                    ),
                    AuthorityInput(
                        name="native customer and subscription records",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="customer accounts and canonical subscription lifecycle",
                    ),
                    AuthorityInput(
                        name="native billing records",
                        owner="financial.invoices",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Invoices and settled Payments",
                    ),
                    AuthorityInput(
                        name="native RADIUS records",
                        owner="network.radius_sessions",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="fresh RADIUS accounting sessions",
                    ),
                    AuthorityInput(
                        name="native network inventory records",
                        owner="network.identity",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="OLT, ONT, IP-pool, VLAN, and PON inventory identity",
                    ),
                    AuthorityInput(
                        name="native ONT runtime observations",
                        owner="network.ont_runtime_status",
                        kind=AuthorityKind.OBSERVATION,
                        source="latest persisted OLT-observed ONT runtime status",
                    ),
                    AuthorityInput(
                        name="native IP pool utilization",
                        owner="network.ip_pool_utilization",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="live IP pool used and total counts",
                    ),
                    AuthorityInput(
                        name="native fiber plant records",
                        owner="network.fiber_topology",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="fibre strand, cabinet, and splitter inventory",
                    ),
                    AuthorityInput(
                        name="native customer outage intervals",
                        owner="network.customer_outage_accrual",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="customer outage impact intervals",
                    ),
                    AuthorityInput(
                        name="native inbox records",
                        owner="communications.team_inbox_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="inbox conversations, assignments, queues, messages, and recorded classifications",
                    ),
                    AuthorityInput(
                        name="native support records",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="support Tickets and SLA clocks",
                    ),
                    AuthorityInput(
                        name="native work-order and project records",
                        owner="operations.work_orders",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="work orders, projects, project tasks, and assignment facts",
                    ),
                    AuthorityInput(
                        name="native provisioning records",
                        owner="operations.provisioning_workflow",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="installation appointments, provisioning tasks, and service orders",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="The adapter supplies a read session; the projection never flushes or commits.",
                    locking="Committed operational facts require no mutation lock.",
                    idempotency="The same committed facts and typed query produce the same report rows.",
                    retries="Bounded report reads and CSV serialization are safe to retry.",
                ),
                errors=ErrorContract(
                    domain_codes=("ui.crm_operational_reports.invalid_query",),
                    mapping_owner="app.web.admin.reports operational report adapter",
                    fail_closed_on=(
                        "missing exact report permission",
                        "invalid report slug, date, or pagination input",
                        "missing signed-in identity for personal reporting",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.SHADOWING,
                    old_owner="dotmac_crm report projection routes and templates",
                    new_owner="ui.crm_operational_reports",
                    verification="typed owner, route, permission, render, empty-state, pagination, and export tests",
                    cutover_gate="report-by-report comparison against the retained CRM surface",
                    fallback_retirement="CRM routes retire only under the CRM web retirement gate",
                ),
                steward="Self-Care reporting",
                design_refs=(
                    "docs/designs/CRM_WEB_RETIREMENT.md",
                    "docs/designs/CRM_REPORT_CAPABILITY_MATRIX.md",
                    "docs/designs/CRM_REPORT_DATA_FLOW_GUIDE.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=("tests/test_crm_reporting.py",),
            ),
        ),
        SOTService(
            name="ui.document_discount_report",
            module="app.services.web_document_discount_report",
            owns=(
                "admin Invoice and Quote discount report projection",
                "Quote-inherited Invoice discount double-count disclosure",
            ),
            depends_on=(
                "auth.permission_gate",
                "financial.invoice_discounts",
                "sales.quote_discount_reporting",
                "ui.display_formatting",
                "ui.list_contracts",
                "ui.status_presentation",
            ),
            notes=(
                "The Reports UI composes the two canonical append-only history "
                "owners into separate typed tabs. It labels source-Quote provenance "
                "and never adds Quote and inherited Invoice amounts into one total."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="admin Invoice and Quote discount report projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "normalized document discount report query",
                            "canonical Invoice discount history",
                            "canonical Quote discount history projection",
                            "canonical financial display formatting",
                            "canonical document status presentation",
                            "authorized billing-report scope",
                        ),
                    ),
                    ConcernContract(
                        name="Quote-inherited Invoice discount double-count disclosure",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical Invoice discount history",
                            "canonical Quote discount history projection",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="normalized document discount report query",
                        owner="ui.list_contracts",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed tab, inclusive date range, customer, actor, "
                            "discount type, document status, source, and pagination"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical Invoice discount history",
                        owner="financial.invoice_discounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "append-only Invoice discount revisions including manual "
                            "or Quote source identity"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical Quote discount history projection",
                        owner="sales.quote_discount_reporting",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="typed filtered append-only Quote discount history",
                    ),
                    AuthorityInput(
                        name="canonical financial display formatting",
                        owner="ui.display_formatting",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "currency-explicit money and configured-timezone timestamp "
                            "formatting"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical document status presentation",
                        owner="ui.status_presentation",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Invoice and Quote status labels, tones, and icon keys",
                    ),
                    AuthorityInput(
                        name="authorized billing-report scope",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="reports:billing:read route authorization",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "The typed report reads one selected history owner on the "
                        "adapter session and never mutates, flushes, commits, or rolls back."
                    ),
                    locking="Append-only history reporting requires no mutation lock.",
                    idempotency=(
                        "The same committed histories and normalized query produce the "
                        "same deterministic rows, provenance labels, and pagination."
                    ),
                    retries="The bounded read-only report is safe to retry.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "financial.invoice_discounts.date_range_invalid",
                        "financial.invoice_discounts.page_invalid",
                        "financial.invoice_discounts.page_size_invalid",
                        "sales.quote_discount_reporting.date_range_invalid",
                        "sales.quote_discount_reporting.page_invalid",
                        "sales.quote_discount_reporting.page_size_invalid",
                    ),
                    mapping_owner="app.web.admin.reports discount adapter",
                    fail_closed_on=(
                        "missing reports:billing:read permission",
                        "invalid date range",
                        "invalid pagination",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "separate billing and sales operational history pages and "
                        "untyped web context builders"
                    ),
                    new_owner="ui.document_discount_report",
                    verification=(
                        "typed Invoice/Quote delegation, source provenance, route "
                        "redirect, template render, permission, and architecture tests"
                    ),
                    cutover_gate=(
                        "The Reports hub is the only rendered discount-history UI and "
                        "both legacy URLs redirect to its typed tabs."
                    ),
                    fallback_retirement=(
                        "Operational-page links, old templates, and untyped discount "
                        "context builders are removed."
                    ),
                ),
                steward="billing reports UI",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/designs/DOCUMENT_DISCOUNT_REPORT.md",
                ),
                test_refs=(
                    "tests/test_document_discount_report.py",
                    "tests/architecture/test_invoice_discount_ownership.py",
                ),
            ),
        ),
        SOTService(
            name="ui.form_contracts",
            module="app.services.form_contracts",
            owns=(
                "editor/form contract vocabulary",
                "rendered prerequisite and consequence disclosure shape",
            ),
            notes=(
                "Declarative contract for editor pages per the UI "
                "information/action standard: current vs proposed state, "
                "prerequisites near the control, impact preview, named "
                "consequences. The owning domain service evaluates "
                "prerequisites and computes impact; the command owner "
                "re-checks everything at execution — the rendered contract "
                "is disclosure, never enforcement. Pilot consumer: the "
                "customer plan-change editor (PLAN_CHANGE_FORM in "
                "customer_portal_flow_changes)."
            ),
        ),
        SOTService(
            name="ui.referral_list_projection",
            module="app.services.web_referrals",
            owns=(
                "admin referral filter and stable sort semantics",
                "admin referral row and page projection",
                "admin referral KPI values and exact cohort links",
                "admin referral list canonical URL",
            ),
            depends_on=(
                "ui.list_contracts",
                "ui.projection_contracts",
                "referrals.program",
            ),
            notes=(
                "The route redirects stale or clamped request state to the "
                "owner-provided canonical URL. Templates render ListQuery, "
                "PageMeta, and Kpi contracts without deriving totals, cohort "
                "links, sort rules, or pagination strings."
            ),
            contract=ServiceContract(
                concerns=tuple(
                    ConcernContract(
                        name=concern,
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical referral program state",
                            "normalized referral list query",
                            "UI projection vocabulary",
                        ),
                    )
                    for concern in (
                        "admin referral filter and stable sort semantics",
                        "admin referral row and page projection",
                        "admin referral KPI values and exact cohort links",
                        "admin referral list canonical URL",
                    )
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical referral program state",
                        owner="referrals.program",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Referral, ReferralCode, Party, Lead, Subscriber, "
                            "and resolved referral-program policy"
                        ),
                    ),
                    AuthorityInput(
                        name="normalized referral list query",
                        owner="ui.list_contracts",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="REFERRAL_LIST_DEFINITION and normalized ListQuery",
                    ),
                    AuthorityInput(
                        name="UI projection vocabulary",
                        owner="ui.projection_contracts",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="StateValue and Kpi contracts",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "The projection reads referral and program facts on the "
                        "adapter session and never mutates or completes a "
                        "transaction."
                    ),
                    locking="Stable read projection requires no mutation lock.",
                    idempotency=(
                        "The same canonical query and referral facts produce the "
                        "same rows, counts, cohort URLs, and canonical URL."
                    ),
                    retries="Read-only projection calls are safe to retry.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="app.web.admin.crm_referrals",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.web.admin.crm_referrals route-local filtering and "
                        "templates/admin/referrals list derivation"
                    ),
                    new_owner="ui.referral_list_projection",
                    verification=(
                        "List, stable-sort, exact-cohort KPI, canonicalization, "
                        "and template boundary tests."
                    ),
                    cutover_gate=(
                        "Admin referral routes and templates consume only the "
                        "owner-provided ListQuery, PageMeta, rows, and KPIs."
                    ),
                    fallback_retirement=(
                        "Route-local pagination and template-derived referral "
                        "totals, filters, and URLs are removed."
                    ),
                ),
                steward="subscriber growth",
                design_refs=(
                    "docs/designs/LIST_QUERY_MIGRATION.md",
                    "docs/designs/UI_PROJECTION_CONTRACTS.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_web_referrals_list.py",
                    "tests/architecture/test_template_projection_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="ui.customer_list_projection",
            module="app.services.web_customer_lists",
            owns=(
                "admin customer searchable fields",
                "admin customer filter semantics",
                "admin customer stable sort semantics",
                "admin customer row and page projection",
                "legacy customer offset API compatibility mapping",
            ),
            depends_on=(
                "ui.list_contracts",
                "customer.account_visibility",
            ),
        ),
        SOTService(
            name="ui.customer_timeline_projection",
            module="app.services.customer_timeline",
            owns=("admin customer timeline attribution and evidence projection",),
            depends_on=(
                "customer.accounts",
                "access.subscription_lifecycle",
                "financial.invoices",
                "financial.payments",
                "financial.dunning",
                "support.ticket_lifecycle",
                "operations.provisioning_workflow",
                "communications.notification_service",
                "observability.audit_log",
                "auth.staff_provisioning",
            ),
            notes=(
                "This read-only projection composes canonical customer-linked "
                "records and audit evidence for the admin customer detail page. "
                "Audit rows retain their recorded staff, customer, system, "
                "service, or API-key attribution. Record-only activity is marked "
                "Actor not recorded instead of inferring an actor from record "
                "ownership or timestamps."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=(
                            "admin customer timeline attribution and evidence "
                            "projection"
                        ),
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical customer account identity",
                            "canonical subscription lifecycle records",
                            "canonical invoice records",
                            "canonical payment records",
                            "canonical dunning records",
                            "canonical support-ticket records",
                            "canonical service-order records",
                            "canonical communication records",
                            "canonical audit evidence",
                            "canonical staff display identity",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical customer account identity",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="customer identifier and account relationships",
                    ),
                    AuthorityInput(
                        name="canonical subscription lifecycle records",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="customer-linked subscription rows",
                    ),
                    AuthorityInput(
                        name="canonical invoice records",
                        owner="financial.invoices",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="customer-linked invoice rows",
                    ),
                    AuthorityInput(
                        name="canonical payment records",
                        owner="financial.payments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="customer-linked payment rows",
                    ),
                    AuthorityInput(
                        name="canonical dunning records",
                        owner="financial.dunning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="customer-linked dunning-case rows",
                    ),
                    AuthorityInput(
                        name="canonical support-ticket records",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="customer-linked support-ticket rows",
                    ),
                    AuthorityInput(
                        name="canonical service-order records",
                        owner="operations.provisioning_workflow",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="customer-linked service-order rows",
                    ),
                    AuthorityInput(
                        name="canonical communication records",
                        owner="communications.notification_service",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="customer-linked communication-log rows",
                    ),
                    AuthorityInput(
                        name="canonical audit evidence",
                        owner="observability.audit_log",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "immutable actor, action, outcome, change, and request "
                            "evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical staff display identity",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "write-time audit actor-label snapshot, with current "
                            "SystemUser lookup only for legacy rows"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "The admin adapter creates and closes the session. The "
                        "projection reads canonical records and audit evidence and "
                        "never writes or completes a transaction."
                    ),
                    locking=(
                        "No mutation lock is required; each timeline reflects the "
                        "canonical snapshot visible to the caller session."
                    ),
                    idempotency=(
                        "The same visible records and audit evidence produce the "
                        "same stable timeline keys, attribution, ordering, and "
                        "details."
                    ),
                    retries="Read-only projection calls are safe to retry.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="admin customer web adapter",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.services.web_customer_details mixed record and audit "
                        "dictionary assembly plus template-local attribution"
                    ),
                    new_owner="ui.customer_timeline_projection",
                    verification=(
                        "Focused attribution, audit evidence, record fallback, "
                        "template, and architecture tests."
                    ),
                    cutover_gate=(
                        "The customer detail snapshot and template consume only the "
                        "typed timeline projection."
                    ),
                    fallback_retirement=(
                        "The untyped helper and template inference of actor display "
                        "are removed."
                    ),
                ),
                steward="customer operations UI",
                design_refs=(
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/FRONTEND_SPEC.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_customer_timeline_projection.py",
                    "tests/architecture/test_customer_timeline_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="ui.subscriber_list_projection",
            module="app.services.web_subscriber_lists",
            owns=(
                "subscriber table searchable fields",
                "subscriber table filter semantics",
                "subscriber table stable sort semantics",
                "subscriber table page projection",
                "legacy subscriber offset API compatibility mapping",
            ),
            depends_on=("ui.list_contracts",),
            notes=(
                "Subscriber scope and full-text search delegate to "
                "app.services.subscriber.Subscribers.query. List reads never "
                "generate or persist subscriber identifiers."
            ),
        ),
        SOTService(
            name="ui.invoice_list_projection",
            module="app.services.web_billing_overview",
            owns=(
                "admin invoice searchable fields",
                "admin invoice filter semantics",
                "admin invoice stable sort semantics",
                "admin invoice page and status-summary projection",
                "admin invoice export scope",
            ),
            depends_on=(
                "ui.list_contracts",
                "financial.invoices",
                "customer.accounts",
            ),
            notes=(
                "The full page and HTMX response share one list partial. "
                "Explicit start_date and end_date filters bound UTC created_at "
                "with an inclusive end date. Exports consume the same canonical "
                "scope without a page cap. The CSV customer_name column uses "
                "the customer account's human display identity and does not "
                "expose account UUIDs."
            ),
        ),
        SOTService(
            name="ui.payments_list_projection",
            module="app.services.web_billing_payments",
            owns=(
                "admin payments searchable fields",
                "admin payments filter semantics",
                "admin payments stable sort and default-order semantics",
                "admin payments list pagination normalization",
                "admin payments export scope",
            ),
            depends_on=(
                "ui.list_contracts",
                "financial.payments",
                "customer.accounts",
            ),
            notes=(
                "PAYMENTS_LIST_DEFINITION declares the list capabilities and "
                "build_payments_list_query normalizes/validates request state; "
                "build_payments_list_data remains the read owner that issues the "
                "SQL, status totals, and enrichment. Explicit start_date and "
                "end_date filters bound UTC created_at with an inclusive end "
                "date. The route validates through the contract and delegates. "
                "The streaming CSV export reuses the "
                "same filter and stable-sort owner without a page cap or full-body "
                "materialization. Its customer_name column uses the customer "
                "account's human display identity and does not expose account "
                "UUIDs. Gated by the existing granular "
                "billing:payment:read. Read-only: no admin bulk command declared, "
                "so no selection or bulk."
            ),
        ),
        SOTService(
            name="ui.support_ticket_list_projection",
            module="app.services.web_support_tickets",
            owns=(
                "admin support-ticket searchable fields",
                "admin support-ticket filter semantics",
                "admin support-ticket per-user applied-list restoration",
                "admin support-ticket stable sort semantics",
                "admin support-ticket page and status-summary projection",
                "admin support-ticket export scope",
            ),
            depends_on=(
                "ui.list_contracts",
                "support.ticket_lifecycle",
                "support.ticket_configuration",
            ),
            notes=(
                "app.services.support.Tickets owns the canonical filtered "
                "domain query. The web projection declares list capabilities, "
                "normalizes request state, and renders full-page and targeted "
                "HTMX result reads from the same table projection. Targeted "
                "refreshes update the status summary and export URL while "
                "leaving the filter and column controls mounted, expose loading "
                "state, and retain current results with retry feedback on failure. "
                "The browser stores the canonical typed ListQuery URL per signed-in "
                "user after successful full-page or HTMX reads; a bare list visit "
                "restores that URL, explicit query parameters take precedence, and "
                "the cache never becomes authoritative for Ticket facts. "
                "Exports consume the same complete scope without a silent row cap."
            ),
            contract=ServiceContract(
                concerns=tuple(
                    ConcernContract(
                        name=name,
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "typed support Ticket list query",
                            "canonical ticket lifecycle state",
                            "ticket configuration",
                        ),
                    )
                    for name in (
                        "admin support-ticket searchable fields",
                        "admin support-ticket filter semantics",
                        "admin support-ticket per-user applied-list restoration",
                        "admin support-ticket stable sort semantics",
                        "admin support-ticket page and status-summary projection",
                        "admin support-ticket export scope",
                    )
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed support Ticket list query",
                        owner="ui.support_ticket_list_projection",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "SUPPORT_TICKET_LIST_DEFINITION-normalized ListQuery with declared "
                            "search/filter/sort/pagination capabilities"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical ticket lifecycle state",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="canonical Tickets.query scope before sort and pagination",
                    ),
                    AuthorityInput(
                        name="ticket configuration",
                        owner="support.ticket_configuration",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="configured filter option vocabulary",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="list, summaries, and export are read-only projections",
                    locking="stable unique Ticket.id tie-breaker prevents page ambiguity",
                    idempotency="same ListQuery and database snapshot yield the same scope",
                    retries="repeat the read from the canonical query",
                ),
                errors=ErrorContract(
                    domain_codes=("support_ticket_list_invalid_query",),
                    mapping_owner="admin support list and export HTTP adapters",
                    fail_closed_on=("unsupported filter", "unsupported sort"),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="support list route, template, and export parameter logic",
                    new_owner="ui.support_ticket_list_projection",
                    verification="list projection, export, HTMX, browser, and architecture tests",
                    cutover_gate="full page, partial, summaries, and export share one ListQuery",
                    fallback_retirement="route/template pagination and silent export caps are absent",
                ),
                steward="support product UI",
                design_refs=(
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/designs/SUPPORT_UX_POLISH_AUDIT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_support_ticket_list_ui_contract.py",
                    "tests/playwright/e2e/test_support_tickets.py",
                ),
            ),
        ),
        SOTService(
            name="ui.reseller_list_projection",
            module="app.services.web_admin_resellers",
            owns=(
                "admin reseller list filter and stable sort semantics",
                "admin reseller list pagination normalization",
            ),
            depends_on=("ui.list_contracts",),
            notes=(
                "web_admin_resellers owns the reseller read; this projection "
                "declares the list capabilities (status filter, name sort, "
                "pagination) so the route derives no pagination or filter rules. "
                "The admin reseller surface is granularly gated by reseller:read "
                "(list) and reseller:write (create/edit), split off the shared "
                "customer:read/write."
            ),
        ),
        SOTService(
            name="ui.field_live_map_projection",
            module="app.services.field_maps",
            owns=(
                "admin field-map sharing-authorized technician position projection",
                "admin field-map searchable fields and focus coordinates",
                "admin field-map stale-position semantics",
            ),
            depends_on=(
                "customer.accounts",
                "operations.work_orders",
            ),
            notes=(
                "field_maps owns the typed admin live-map feed and search "
                "projection. Technician visibility fails closed when location "
                "sharing is disabled. Search resolves technician identity and "
                "native work-order/customer/service-address facts before "
                "returning only results with focusable coordinates. The admin "
                "web adapter enforces operations:dispatch:read and the sidebar "
                "uses the same permission for discoverability."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=(
                            "admin field-map sharing-authorized technician "
                            "position projection"
                        ),
                        role=OwnerRole.RESOLVER,
                        input_names=("native field-technician presence facts",),
                    ),
                    ConcernContract(
                        name="admin field-map searchable fields and focus coordinates",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "native field-technician presence facts",
                            "canonical work-order map facts",
                            "canonical subscriber service-address facts",
                            "admin field-map search input",
                        ),
                    ),
                    ConcernContract(
                        name="admin field-map stale-position semantics",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "native field-technician presence facts",
                            "admin field-map freshness input",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="native field-technician presence facts",
                        owner="ui.field_live_map_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "FieldTechPresence technician/person identity, sharing "
                            "preference, status, latest coordinates, accuracy, and "
                            "observation time"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical work-order map facts",
                        owner="operations.work_orders",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active native WorkOrder public identity, searchable "
                            "dispatch fields, subscriber binding, and mapped location"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical subscriber service-address facts",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "native Subscriber identity/contact fields and canonical "
                            "Address street, locality, and coordinates"
                        ),
                    ),
                    AuthorityInput(
                        name="admin field-map search input",
                        owner="ui.field_live_map_projection",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="typed normalized search text and bounded result limit",
                    ),
                    AuthorityInput(
                        name="admin field-map freshness input",
                        owner="ui.field_live_map_projection",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="typed bounded stale-after duration",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Feed and search queries read one adapter-owned session and "
                        "perform no ORM mutation or transaction completion."
                    ),
                    locking="No locks; the projection is observational and read-only.",
                    idempotency=(
                        "Equivalent search/freshness inputs return the same typed "
                        "projection for the same database snapshot."
                    ),
                    retries="Read availability failures may be retried safely.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "ui.field_live_map_projection.invalid_search",
                        "ui.field_live_map_projection.unauthorized",
                    ),
                    mapping_owner="admin field-map web adapter",
                    fail_closed_on=(
                        "missing operations:dispatch:read permission",
                        "disabled technician location sharing",
                        "missing focus coordinates",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "untyped field-map feed and template-local visibility/search "
                        "behavior"
                    ),
                    new_owner="ui.field_live_map_projection",
                    verification=(
                        "typed feed/search contracts, sharing/privacy tests, street "
                        "search tests, route permission tests, and UI focus tests"
                    ),
                    cutover_gate=(
                        "Routes return owner-provided typed outcomes and the template "
                        "only renders or focuses those outcomes."
                    ),
                    fallback_retirement=(
                        "The feed no longer exposes non-sharing technicians and no "
                        "template-only street filtering or ungated navigation remains."
                    ),
                ),
                steward="field operations UI",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_admin_maps_web.py",
                    "tests/architecture/test_field_live_map_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="ui.work_order_list_projection",
            module="app.services.web_dispatch_work_orders",
            owns=(
                "admin work-order searchable fields",
                "admin work-order status and native project-task filter semantics",
                "admin work-order stable sort semantics",
                "admin work-order list pagination normalization",
                "admin work-order global KPI and exact-cohort link projection",
                "admin task-originated work-order creation prefill",
                "admin work-order detail and linked-origin projection",
            ),
            depends_on=(
                "ui.list_contracts",
                "ui.projection_contracts",
                "customer.accounts",
                "operations.project_lifecycle",
                "operations.work_order_commands",
                "operations.work_orders",
            ),
            notes=(
                "work_order_views.query_work_orders owns the canonical filtered "
                "and sorted work-order query; this projection declares list "
                "capabilities, normalizes native project-task UUID scope, and "
                "delegates the read. Task filtering is independent of creation "
                "permission and composes with search, status, lifecycle, sort, "
                "and pagination. KPI cards remain documented global queue "
                "cohorts with exact global links. Task-originated prefill reloads "
                "the authoritative task, project, and subscriber and never trusts "
                "duplicated URL scope. Native form mutations delegate to "
                "operations.work_order_commands; creation and assignment remain "
                "separate decisions. Each dispatch route is granularly gated "
                "(operations:dispatch:read/write/assign)."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="admin work-order searchable fields",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical work-order list facts",
                            "shared list contract",
                        ),
                    ),
                    ConcernContract(
                        name=(
                            "admin work-order status and native project-task "
                            "filter semantics"
                        ),
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical work-order list facts",
                            "canonical project-task scope",
                            "shared list contract",
                        ),
                    ),
                    ConcernContract(
                        name="admin work-order stable sort semantics",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical work-order list facts",
                            "shared list contract",
                        ),
                    ),
                    ConcernContract(
                        name="admin work-order list pagination normalization",
                        role=OwnerRole.POLICY,
                        input_names=("shared list contract",),
                    ),
                    ConcernContract(
                        name=(
                            "admin work-order global KPI and exact-cohort link "
                            "projection"
                        ),
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical work-order list facts",
                            "UI projection vocabulary",
                        ),
                    ),
                    ConcernContract(
                        name="admin task-originated work-order creation prefill",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical project-task scope",
                            "canonical subscriber scope",
                            "work-order creation protocol",
                        ),
                    ),
                    ConcernContract(
                        name="admin work-order detail and linked-origin projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical work-order list facts",
                            "canonical project-task scope",
                            "canonical subscriber scope",
                            "work-order creation protocol",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical work-order list facts",
                        owner="operations.work_orders",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "native active WorkOrder identity, lifecycle, "
                            "subscriber, project, and project_task_id bindings"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical project-task scope",
                        owner="operations.project_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "native ProjectTask and Project UUID relationship, "
                            "active state, labels, description, priority, and "
                            "subscriber binding"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical subscriber scope",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="the exact native Subscriber row bound to the project",
                    ),
                    AuthorityInput(
                        name="shared list contract",
                        owner="ui.list_contracts",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed search, status, native task filter, stable "
                            "sort, pagination, and permission scope"
                        ),
                    ),
                    AuthorityInput(
                        name="UI projection vocabulary",
                        owner="ui.projection_contracts",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="typed StateValue, Kpi, and Action semantics",
                    ),
                    AuthorityInput(
                        name="work-order creation protocol",
                        owner="operations.work_order_commands",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "authoritative subscriber/project/task consistency, "
                            "initial status, work type, assignment separation, "
                            "and operations:dispatch:write requirements"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "List, detail, KPI, and creation-prefill projection execute "
                        "without ORM mutation or transaction completion."
                    ),
                    locking=(
                        "No locks; the canonical work-order query uses stable "
                        "native UUID tie-breakers."
                    ),
                    idempotency=(
                        "Equivalent filters and permission scope return the same "
                        "projection for the same authoritative snapshot."
                    ),
                    retries=(
                        "Read availability failures may be retried. Invalid UUID, "
                        "missing scope, and authorization failures are not retryable."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "ui.work_order_list_projection.invalid_filter",
                        "ui.work_order_list_projection.invalid_page",
                        "ui.work_order_list_projection.task_not_found",
                        "ui.work_order_list_projection.project_not_found",
                        "ui.work_order_list_projection.subscriber_not_found",
                        "ui.work_order_list_projection.incomplete_scope",
                        "ui.work_order_list_projection.unauthorized",
                    ),
                    mapping_owner="admin dispatch web adapter",
                    fail_closed_on=(
                        "invalid native project-task identifier",
                        "inactive or missing task or project creation scope",
                        "missing project subscriber",
                        "missing permission scope",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "dispatch template and route-local task prefill, filtering, "
                        "KPI, and pagination decisions"
                    ),
                    new_owner="ui.work_order_list_projection",
                    verification=(
                        "dispatch filtering, KPI cohort, prefill, permission, CSRF, "
                        "rendering, and architecture tests"
                    ),
                    cutover_gate=(
                        "The route delegates typed task scope and list inputs; "
                        "templates render owner-provided KPIs, Actions, and prefill."
                    ),
                    fallback_retirement=(
                        "CRM join keys, route-local query decisions, unvalidated "
                        "duplicate scope, and create-with-technician shortcuts are absent."
                    ),
                ),
                steward="field operations UI",
                design_refs=(
                    "docs/designs/PROJECTS_SOT_COMPLETION.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_web_dispatch_work_orders.py",
                    "tests/test_dispatch_work_orders_contracts.py",
                    "tests/test_dispatch_work_orders_csrf.py",
                    "tests/test_work_order_views.py",
                ),
            ),
        ),
        SOTService(
            name="ui.project_list_projection",
            module="app.services.web_projects",
            owns=(
                "admin project searchable fields",
                "admin project filter and stable sort semantics",
                "admin project list pagination normalization",
                "admin project-task list field-work action projection",
                "admin project and task detail field-work composition",
                "admin project-task work-order creation action projection",
            ),
            depends_on=(
                "ui.list_contracts",
                "operations.project_lifecycle",
                "operations.work_order_commands",
                "operations.work_orders",
            ),
            notes=(
                "projects_service.projects.list (operations.project_lifecycle) "
                "owns the canonical filtered/sorted project query; this "
                "projection declares the list capabilities and normalizes "
                "request state, then delegates the read. It issues no query of "
                "its own. The task-list projection bulk-loads one page of native "
                "linked-work summaries and returns typed zero/one/many Actions "
                "without lazy loading. Detail projections compose the native "
                "project/task scope with operations.work_orders and expose a "
                "secondary work-order creation action; "
                "operations.work_order_commands revalidates the exact "
                "subscriber/project/task scope. Gated by project:read or "
                "project:task:read; work-order details require "
                "operations:dispatch:read and creation separately requires "
                "operations:dispatch:write."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="admin project searchable fields",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical project list facts",
                            "shared list contract",
                        ),
                    ),
                    ConcernContract(
                        name="admin project filter and stable sort semantics",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical project list facts",
                            "shared list contract",
                        ),
                    ),
                    ConcernContract(
                        name="admin project list pagination normalization",
                        role=OwnerRole.POLICY,
                        input_names=("shared list contract",),
                    ),
                    ConcernContract(
                        name=("admin project-task list field-work action projection"),
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical project detail facts",
                            "native linked field-work facts",
                            "work-order creation protocol",
                        ),
                    ),
                    ConcernContract(
                        name="admin project and task detail field-work composition",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical project detail facts",
                            "native linked field-work facts",
                        ),
                    ),
                    ConcernContract(
                        name="admin project-task work-order creation action projection",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical project detail facts",
                            "work-order creation protocol",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical project list facts",
                        owner="operations.project_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="native active Project rows and owner-projected action eligibility",
                    ),
                    AuthorityInput(
                        name="shared list contract",
                        owner="ui.list_contracts",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="typed search, filters, stable sort, pagination, permission scope, and action eligibility request",
                    ),
                    AuthorityInput(
                        name="canonical project detail facts",
                        owner="operations.project_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "native active Project and ProjectTask identity, "
                            "relationship, lifecycle, and subscriber scope"
                        ),
                    ),
                    AuthorityInput(
                        name="native linked field-work facts",
                        owner="operations.work_orders",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "native WorkOrder rows selected by authoritative "
                            "project_id or project_task_id"
                        ),
                    ),
                    AuthorityInput(
                        name="work-order creation protocol",
                        owner="operations.work_order_commands",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "subscriber/project/task consistency requirements and "
                            "operations:dispatch:write permission"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Typed project lists, bulk task field-work composition, "
                        "and detail projections execute without committing or "
                        "mutating ORM state."
                    ),
                    locking="No locks; stable ordering includes native Project UUID as the final tie-breaker.",
                    idempotency="Equivalent query and visibility scope return the same page for the same authoritative snapshot.",
                    retries="Read availability errors may be retried; invalid filters and unauthorized scopes are not retryable.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "ui.project_list_projection.invalid_filter",
                        "ui.project_list_projection.invalid_sort",
                        "ui.project_list_projection.invalid_page",
                        "ui.project_list_projection.unauthorized",
                    ),
                    mapping_owner="project API and admin-web adapters",
                    fail_closed_on=(
                        "unknown filter field",
                        "unknown sort field",
                        "missing permission scope",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    new_owner="ui.project_list_projection",
                    old_owner="route/template-local project list decisions",
                    verification=(
                        "typed query contract, API/admin parity, bulk task "
                        "work-order projection, permission rendering, and template "
                        "projection architecture tests"
                    ),
                    cutover_gate="all list routes delegate typed filter, sort, pagination, status, permission, and eligibility inputs",
                    fallback_retirement="route and template list-policy inference removed",
                ),
                steward="service delivery UI",
                design_refs=(
                    "docs/designs/PROJECTS_SOT_COMPLETION.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_web_projects_service.py",
                    "tests/test_web_admin_projects_render.py",
                    "tests/test_web_dispatch_work_orders.py",
                    "tests/test_projects_api.py",
                    "tests/architecture/test_projects_sot_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="ui.vendor_supply_projection",
            module="app.services.vendor_supply_views",
            owns=(
                "vendor project supply workspace projection",
                "staff vendor supply review queues and impact previews",
                "latest active vendor supply record selection",
                "material provider issue observation presentation",
                "advance payables observation presentation",
            ),
            depends_on=(
                "auth.permission_gate",
                "operations.vendor_advances",
                "operations.vendor_material_release",
                "operations.vendor_project_lifecycle",
                "operations.vendor_project_records",
                "ui.status_presentation",
            ),
            notes=(
                "Read-only composition for vendor material and mobilisation-advance "
                "workflows. It renders owner-supplied eligibility, exact decision "
                "state, and provider observations without inferring stock issue, "
                "payment, settlement, or stale data as current."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="vendor project supply workspace projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical vendor project lifecycle facts",
                            "canonical vendor material release decisions",
                            "canonical vendor advance decisions",
                            "vendor supply request capabilities",
                            "canonical vendor supply status presentation",
                        ),
                    ),
                    ConcernContract(
                        name="staff vendor supply review queues and impact previews",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical vendor material release decisions",
                            "canonical vendor advance decisions",
                            "canonical vendor project records",
                            "staff vendor supply review capabilities",
                        ),
                    ),
                    ConcernContract(
                        name="latest active vendor supply record selection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical vendor material release decisions",
                            "canonical vendor advance decisions",
                        ),
                    ),
                    ConcernContract(
                        name="material provider issue observation presentation",
                        role=OwnerRole.RESOLVER,
                        input_names=("material provider issue observation",),
                    ),
                    ConcernContract(
                        name="advance payables observation presentation",
                        role=OwnerRole.RESOLVER,
                        input_names=("advance payables settlement observation",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical vendor project lifecycle facts",
                        owner="operations.vendor_project_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active InstallationProject assignment and lifecycle state"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical vendor project records",
                        owner="operations.vendor_project_records",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="approved quote identity, total, and currency",
                    ),
                    AuthorityInput(
                        name="canonical vendor material release decisions",
                        owner="operations.vendor_material_release",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="active release, line, review, and provider-correlation rows",
                    ),
                    AuthorityInput(
                        name="canonical vendor advance decisions",
                        owner="operations.vendor_advances",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active advance, quote allowance, review, and "
                            "payables-correlation rows"
                        ),
                    ),
                    AuthorityInput(
                        name="vendor supply request capabilities",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "vendor:material:request and vendor:advance:request "
                            "capabilities"
                        ),
                    ),
                    AuthorityInput(
                        name="staff vendor supply review capabilities",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="inventory and finance accounts-payable read/write results",
                    ),
                    AuthorityInput(
                        name="canonical vendor supply status presentation",
                        owner="ui.status_presentation",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="labels, semantic tones, and icon keys",
                    ),
                    AuthorityInput(
                        name="material provider issue observation",
                        owner="operations.vendor_material_release",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "support system, reference, status, and observed timestamp"
                        ),
                    ),
                    AuthorityInput(
                        name="advance payables settlement observation",
                        owner="operations.vendor_advances",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "payables system, reference, status, and observed timestamp"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Typed workspace, queue, detail, and preview queries never "
                        "commit, flush, or make a business decision."
                    ),
                    locking=(
                        "Ordinary projections do not lock; confirmation preview can "
                        "request a row lock for stale-safe revalidation."
                    ),
                    idempotency=(
                        "Equivalent scope and snapshot return equivalent typed results."
                    ),
                    retries="Read availability failures may be retried.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "ui.vendor_supply_projection.material_release_not_found",
                        "ui.vendor_supply_projection.advance_not_found",
                        "ui.vendor_supply_projection.unsupported_action",
                        "ui.vendor_supply_projection.reason_required",
                        "ui.vendor_supply_projection.reason_too_long",
                        "ui.vendor_supply_projection.material_not_reviewable",
                        "ui.vendor_supply_projection.advance_not_reviewable",
                    ),
                    mapping_owner=(
                        "app.web.vendor_portal and app.web.admin.vendor_operations"
                    ),
                    fail_closed_on=(
                        "vendor/project scope mismatch",
                        "missing review record",
                        "non-reviewable state",
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="material provider issue observation presentation",
                        input_names=("material provider issue observation",),
                        writer="ui.vendor_supply_projection",
                        freshness=(
                            "Each present observation carries its provider-observed "
                            "timestamp; absent and not-applicable states are explicit."
                        ),
                        stale_behavior=(
                            "Retain and label the last observation; never infer issue "
                            "or payment from a Dotmac approval."
                        ),
                        drift_signal=(
                            "An approved record without a later provider observation."
                        ),
                        rebuild_operation=(
                            "Re-run project_workspace or the relevant review/detail "
                            "query after the provider owner refreshes its observation."
                        ),
                        repair_owner=(
                            "integration.dotmac_erp_material_support_adapter"
                        ),
                    ),
                    ProjectionContract(
                        name="advance payables observation presentation",
                        input_names=("advance payables settlement observation",),
                        writer="ui.vendor_supply_projection",
                        freshness=(
                            "Each present observation carries its provider-observed "
                            "timestamp; absent and not-applicable states are explicit."
                        ),
                        stale_behavior=(
                            "Retain and label the last observation; never infer "
                            "payment from a Dotmac approval."
                        ),
                        drift_signal=(
                            "An approved advance without a later payables observation."
                        ),
                        rebuild_operation=(
                            "Re-run project_workspace or the relevant review/detail "
                            "query after the provider owner refreshes its observation."
                        ),
                        repair_owner="integration.dotmac_erp_payables_adapter",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "missing vendor UI and route/template-local staff supply forms"
                    ),
                    new_owner="ui.vendor_supply_projection",
                    verification=(
                        "Eligibility, status, observation, permission, queue, preview, "
                        "and template architecture tests."
                    ),
                    cutover_gate=(
                        "Vendor detail and staff queue consume only typed projection "
                        "objects and owner-supplied actions."
                    ),
                    fallback_retirement=(
                        "Templates do not infer supply transitions, payment, or stock "
                        "issue state."
                    ),
                ),
                steward="vendor operations UI",
                design_refs=(
                    "docs/designs/VENDOR_SUPPLY_UI.md",
                    "docs/designs/UI_PROJECTION_CONTRACTS.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_vendor_supply_ui.py",
                    "tests/architecture/test_vendor_supply_ui_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="ui.vendor_delivery_portfolio_projection",
            module="app.services.vendor_delivery_portfolio",
            owns=(
                "admin vendor operational portfolio composition",
                "admin vendor project portfolio filtering and pagination",
                "admin vendor portfolio KPI and cohort parity",
                "admin vendor portfolio field visibility",
            ),
            depends_on=(
                "auth.permission_gate",
                "operations.vendor_advances",
                "operations.vendor_material_release",
                "operations.vendor_project_lifecycle",
                "operations.vendor_project_records",
                "operations.vendor_purchase_invoices",
                "ui.project_vendor_delivery_projection",
                "ui.status_presentation",
                "ui.vendor_supply_projection",
            ),
            notes=(
                "Read-only, permission-scoped composition for the admin vendor "
                "detail page. It pages active installation projects assigned to "
                "one authorized vendor, reuses project-delivery current-record "
                "selection, bulk-loads the latest material and advance projections, "
                "and links every KPI to its exact lifecycle-status cohort."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="admin vendor operational portfolio composition",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "authorized vendor portfolio scope",
                            "canonical vendor project lifecycle facts",
                            "canonical project vendor-delivery composition",
                            "canonical latest vendor supply projection",
                            "canonical vendor status presentation",
                        ),
                    ),
                    ConcernContract(
                        name=(
                            "admin vendor project portfolio filtering and pagination"
                        ),
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "authorized vendor portfolio scope",
                            "canonical vendor project lifecycle facts",
                            "vendor portfolio query contract",
                        ),
                    ),
                    ConcernContract(
                        name="admin vendor portfolio KPI and cohort parity",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical vendor project lifecycle facts",
                            "canonical vendor status presentation",
                            "vendor portfolio query contract",
                        ),
                    ),
                    ConcernContract(
                        name="admin vendor portfolio field visibility",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "authorized vendor portfolio scope",
                            "canonical project vendor-delivery composition",
                            "canonical latest vendor supply projection",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authorized vendor portfolio scope",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "authenticated vendor UUID scope plus inventory, fiber, "
                            "and accounts-payable read results supplied by the adapter"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical vendor project lifecycle facts",
                        owner="operations.vendor_project_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active InstallationProject assignment, lifecycle state, "
                            "native project identity, and update timestamp"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical project vendor-delivery composition",
                        owner="ui.project_vendor_delivery_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "current quote, route revision, as-built, purchase invoice, "
                            "and permission-scoped payment observation"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical latest vendor supply projection",
                        owner="ui.vendor_supply_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "latest active material release and advance per installation "
                            "project with separate provider observations"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical vendor status presentation",
                        owner="ui.status_presentation",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="server-owned labels, semantic tones, and icon keys",
                    ),
                    AuthorityInput(
                        name="vendor portfolio query contract",
                        owner="ui.vendor_delivery_portfolio_projection",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed lifecycle-status filter, project search, stable "
                            "updated-time ordering, page size, and offset"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Loads and composes one authorized vendor portfolio without "
                        "committing, flushing, mutating ORM state, or invoking a command."
                    ),
                    locking=(
                        "No locks; the projection reads committed rows and applies "
                        "stable updated-at and UUID ordering."
                    ),
                    idempotency=(
                        "Equivalent vendor scope, capabilities, filters, pagination, "
                        "and committed snapshot return equivalent typed results."
                    ),
                    retries=(
                        "Read availability failures may be retried; invalid transport "
                        "filters are rejected by the adapter."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="app.web.admin.vendors",
                    fail_closed_on=(
                        "missing inventory read scope",
                        "missing fiber or accounts-payable capability for protected fields",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "admin vendor detail limited to profile and portal-login data"
                    ),
                    new_owner="ui.vendor_delivery_portfolio_projection",
                    verification=(
                        "vendor scoping, current-record selection, KPI cohort parity, "
                        "permission omission, stable pagination, provider freshness, "
                        "template rendering, and query-boundary tests"
                    ),
                    cutover_gate=(
                        "The admin vendor detail route delegates operational reads to "
                        "the typed portfolio and templates render only its fields."
                    ),
                    fallback_retirement=(
                        "No route or template performs project selection, lifecycle "
                        "grouping, financial visibility, or provider-state inference."
                    ),
                ),
                steward="vendor operations UI",
                design_refs=(
                    "docs/designs/VENDOR_DELIVERY_PORTFOLIO_UI.md",
                    "docs/designs/VENDOR_PROJECT_REVIEW_UI.md",
                    "docs/designs/VENDOR_SUPPLY_UI.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_vendor_delivery_portfolio.py",
                    "tests/architecture/test_vendor_delivery_portfolio_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="ui.project_vendor_delivery_projection",
            module="app.services.project_vendor_delivery",
            owns=(
                "admin project vendor-delivery composition",
                "admin project vendor-delivery current-record selection",
                "admin project vendor-delivery field visibility",
            ),
            depends_on=(
                "auth.permission_gate",
                "integration.dotmac_erp_payables_adapter",
                "operations.vendor_project_lifecycle",
                "operations.vendor_project_records",
                "operations.vendor_purchase_invoices",
                "ui.status_presentation",
            ),
            notes=(
                "Read-only, permission-scoped composition for the native project "
                "detail page. It selects the approved or current active quote, "
                "that quote's latest route revision, the latest as-built record, "
                "and the assigned vendor's active purchase invoice. It delegates "
                "ERP payment availability and freshness to the existing payment "
                "projection and never creates or updates installation scope."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="admin project vendor-delivery composition",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical installation-project lifecycle facts",
                            "canonical vendor project records",
                            "canonical vendor purchase-invoice projection",
                            "timestamped ERP accounts-payable observation",
                            "canonical vendor status presentation",
                            "project-detail read capabilities",
                        ),
                    ),
                    ConcernContract(
                        name=("admin project vendor-delivery current-record selection"),
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical vendor project records",
                            "canonical vendor purchase-invoice projection",
                        ),
                    ),
                    ConcernContract(
                        name="admin project vendor-delivery field visibility",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "project-detail read capabilities",
                            "canonical installation-project lifecycle facts",
                            "canonical vendor project records",
                            "canonical vendor purchase-invoice projection",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical installation-project lifecycle facts",
                        owner="operations.vendor_project_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active InstallationProject status, assignment, and "
                            "native Project UUID"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical vendor project records",
                        owner="operations.vendor_project_records",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active quote, proposed route revision, and as-built "
                            "records linked to the InstallationProject"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical vendor purchase-invoice projection",
                        owner="operations.vendor_purchase_invoices",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "active purchase invoice for the installation project "
                            "and currently assigned vendor"
                        ),
                    ),
                    AuthorityInput(
                        name="timestamped ERP accounts-payable observation",
                        owner="integration.dotmac_erp_payables_adapter",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "validated payment status, total, paid, balance, source "
                            "timestamp, observation timestamp, and refresh error"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical vendor status presentation",
                        owner="ui.status_presentation",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "server-owned labels, semantic tones, and icon keys for "
                            "vendor delivery lifecycle values"
                        ),
                    ),
                    AuthorityInput(
                        name="project-detail read capabilities",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "inventory:read, network:fiber:read, and "
                            "finance:ap:read results supplied by the admin adapter"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Loads and composes vendor-delivery records without "
                        "committing, flushing, mutating ORM state, or calling a "
                        "business command."
                    ),
                    locking=(
                        "No locks; the projection reads one committed snapshot and "
                        "uses stable native UUID tie-breakers."
                    ),
                    idempotency=(
                        "The same project facts, observation time, and capability "
                        "scope produce the same projection."
                    ),
                    retries=(
                        "Read availability failures may be retried; missing "
                        "installation scope is a successful empty result."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="admin project web adapter",
                    fail_closed_on=("missing project-detail read capability",),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="ui.project_vendor_delivery_projection",
                ),
                steward="service delivery UI",
                design_refs=(
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md",
                ),
                test_refs=(
                    "tests/test_project_vendor_delivery_projection.py",
                    "tests/test_web_admin_projects_render.py",
                    "tests/architecture/test_projects_sot_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="ui.audit_events_list_projection",
            module="app.services.web_system_audit",
            owns=(
                "admin audit-log filterable fields",
                "admin audit-log sort and default-order semantics",
                "admin audit-log list pagination normalization",
            ),
            depends_on=(
                "ui.list_contracts",
                "observability.audit_log",
            ),
            notes=(
                "audit_service.audit_events.list (observability.audit_log) owns "
                "the canonical filtered/sorted audit query; this projection "
                "declares the list capabilities (filter by actor/action/entity, "
                "sort on occurred_at) and normalizes request state, then "
                "delegates the read and count. It issues no query of its own. "
                "Read-only: audit events are immutable observations with no admin "
                "bulk command, so no selection or bulk is declared. Gated by the "
                "existing granular audit:read."
            ),
        ),
        SOTService(
            name="ui.quote_detail_projection",
            module="app.services.web_sales",
            owns=("admin Quote delivery eligibility and activity presentation",),
            depends_on=(
                "communications.notification_service",
                "observability.audit_log",
                "sales.quote_delivery",
                "sales.service",
            ),
            notes=(
                "The Quote detail builder presents delivery eligibility and the "
                "official Quote timeline from authoritative Quote, immutable audit, "
                "and durable notification records. It does not infer final mailbox "
                "receipt from SMTP transport acceptance."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=(
                            "admin Quote delivery eligibility and activity presentation"
                        ),
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical Quote detail state",
                            "canonical Quote audit evidence",
                            "canonical Quote delivery outcome",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical Quote detail state",
                        owner="sales.service",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active Quote, lines, status, expiry, Lead, Party recipient, "
                            "and related commercial records"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical Quote audit evidence",
                        owner="observability.audit_log",
                        kind=AuthorityKind.OBSERVATION,
                        source="immutable Quote-scoped action and actor evidence",
                    ),
                    AuthorityInput(
                        name="canonical Quote delivery outcome",
                        owner="communications.notification_service",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "durable notification queue state and mail-transport "
                            "acceptance or terminal failure evidence"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="The admin GET adapter owns one read-only session.",
                    locking="No row locks; this is a current-state presentation query.",
                    idempotency="Equivalent reads return the same projection for the same rows.",
                    retries="A failed read may be retried without side effects.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="admin Quote detail adapter",
                    fail_closed_on=("missing Quote detail read capability",),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="ui.quote_detail_projection",
                ),
                steward="sales operations UI",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_quote_documents_and_delivery.py",
                    "tests/architecture/test_quote_document_delivery_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="ui.nas_list_projection",
            module="app.services.nas.web_builders",
            owns=(
                "admin NAS dashboard searchable fields",
                "admin NAS dashboard filter semantics",
                "admin NAS dashboard sort and default-order semantics",
                "admin NAS dashboard list pagination normalization",
            ),
            depends_on=("ui.list_contracts", "network.nas_inventory"),
            notes=(
                "NAS_LIST_DEFINITION declares the list capabilities and "
                "build_nas_list_query normalizes/validates request state; "
                "build_nas_dashboard_data is the read owner. SQL-expressible "
                "filters (vendor/nas_type/status/pop_site/search) paginate and "
                "count in the database via NasDevices.list/count; partner_org_id "
                "(tag) and olt_status (ping cache) are post-query filters that "
                "page over a bounded in-memory scan (logged if the bound is hit) "
                "rather than the prior unconditional 1000-row load-then-slice. "
                "Gated by the router-level granular network:nas:read/write. "
                "Read-only list: no admin bulk command declared."
            ),
        ),
        SOTService(
            name="ui.notification_list_projection",
            module="app.services.web_notifications",
            owns=(
                "admin notification-template list searchable/filterable fields",
                "admin notification-queue list filterable fields",
                "admin notification-history list filterable fields",
                "admin notification list sort and default-order semantics",
                "admin notification list pagination normalization",
            ),
            depends_on=("ui.list_contracts", "communications.notification_service"),
            notes=(
                "One projection owner for the three admin notification lists "
                "(templates, queue, delivery history). "
                "NOTIFICATION_{TEMPLATES,QUEUE,HISTORY}_LIST_DEFINITION declare "
                "the per-list capabilities (search + filter channel/status, sort "
                "name; filter status/channel, sort created_at; filter status, "
                "sort occurred_at); templates_list_context / queue_context / "
                "history_context normalize request state and delegate the read + "
                "count to communications.notification_service. Gated by the "
                "granular notification:read/notification:write (split off the "
                "coarse system:read/write in migration 323). Read-only lists: "
                "mutations have their own routes; no bulk selection declared."
            ),
        ),
        SOTService(
            name="ui.ip_address_list_projection",
            module="app.services.web_network_ip",
            owns=(
                "admin IP-address list searchable/filterable fields",
                "admin IP-address list sort and default-order semantics",
                "admin IP-address list page-size normalization",
            ),
            depends_on=("ui.list_contracts",),
            notes=(
                "IP_ADDRESS_LIST_DEFINITION declares the addresses-tab list "
                "capabilities (search, filter by pool, sort by address) and "
                "build_ip_address_list_query normalizes/validates request state; "
                "build_ip_management_data remains the read owner. Gated by the "
                "existing granular network:ip:read. The addresses list pages "
                "across the concatenated IPv4-then-IPv6 ordering: the page window "
                "is applied to the merged sequence (per-family offset/take), so a "
                "page shows at most one page size and pages align across the two "
                "families. Read-only list: no bulk selection declared."
            ),
        ),
        SOTService(
            name="ui.network_device_list_projection",
            module="app.services.web_network_core_devices_inventory",
            owns=(
                "admin network-device list searchable/filterable fields",
                "admin network-device list sort and default-order semantics",
                "admin network-device list pagination normalization",
            ),
            depends_on=("ui.list_contracts", "network.device_projection"),
            notes=(
                "NETWORK_DEVICE_LIST_DEFINITION declares the list capabilities "
                "(search, filter type/status/vendor, sort name/last_seen) and "
                "build_network_device_list_query normalizes request state; the "
                "list reads the materialised device_projections table via "
                "device_projection_views (SQL search/filter/sort/paginate), the "
                "rebuildable read model owned by network.device_projection, "
                "instead of aggregating every device in memory. Projected "
                "operational_status is the binary network.device_state outcome; "
                "refreshed_at is internal repair evidence and never a client "
                "device state. Raw timestamped observations remain available at "
                "diagnostic depth. collect_devices is "
                "retired from the request path and remains the reconciler's "
                "derivation input. Gated by the existing granular "
                "network:device:read. Read-only list: no bulk command declared."
            ),
        ),
    ),
    entrypoints=(
        "app.api.tables",
        "app.services.subscriber",
        "app.services.table_config",
        "app.services.web_document_discount_report",
        "app.web.admin.customers",
        "app.web.admin.billing_invoices",
        "app.web.admin.reports",
        "app.web.admin.support_tickets",
        "templates.admin.billing.invoices",
        "templates.admin.customers",
        "templates.admin.reports.discounts",
        "templates.admin.support.tickets",
    ),
    rule="List routes normalize request parameters through one declared list "
    "contract. Owners filter before pagination and apply a stable unique "
    "tie-breaker. Compatibility APIs delegate row selection to a named "
    "resource owner and list reads do not mutate domain records. Templates "
    "consume ListQuery and PageMeta, preserve the canonical URL, and do not "
    "rebuild pagination or sort semantics.",
)
